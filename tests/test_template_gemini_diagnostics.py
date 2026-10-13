import asyncio
import json

import pytest

from ocr_service.email_template import runner
from ocr_service.gemini_errors import GeminiAPIError, _classify_gemini_http_error
from ocr_service.key_pool import GeminiKeyPool


class DirectProxyPool:
    def next(self, excluded=None):
        return None

    def mark_ok(self, _proxy):
        pass

    def mark_failed(self, *_args):
        pass


def make_pool():
    pool = GeminiKeyPool()
    pool.keys = ["test-secret-key-not-for-results"]
    pool.key_names = ["GEMINI_API_KEY_TEST"]
    pool.blocked = [0.0]
    pool.reserved = [False]
    pool.dead = [False]
    pool.last_used = [0.0]
    pool.reserved_at = [0.0]
    pool.last_error = [None]
    pool.lease_ttl = [60.0]
    pool.blackout_until = 0.0
    pool.rr_index = 0
    return pool


@pytest.mark.parametrize(
    ("status", "provider_status", "message", "public_code", "pair_blocked"),
    [
        (400, "INVALID_ARGUMENT", "Bad model request", "invalid_request", True),
        (400, "API_KEY_INVALID", "API key not valid", "key_rejected", False),
        (403, "PERMISSION_DENIED", "Model access is not enabled", "model_rejected", True),
        (404, "NOT_FOUND", "Model was not found", "model_rejected", True),
        (429, "RESOURCE_EXHAUSTED", "Rate limit exceeded", "quota", True),
        (503, "UNAVAILABLE", "Upstream unavailable", "upstream_unavailable", False),
        (401, "API_KEY_INVALID", "API key not valid", "key_rejected", False),
    ],
)
def test_template_http_errors_keep_expected_global_key_state(
    monkeypatch, capsys, status, provider_status, message, public_code, pair_blocked,
):
    pool = make_pool()
    proxy_pool = DirectProxyPool()
    monkeypatch.setattr(runner, "gemini_pool", pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", proxy_pool)
    monkeypatch.setattr(runner, "TEMPLATE_RESERVE_KEYS_FOR_OCR", 0)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", 1)
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 0.01)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0.0)
    policy = _classify_gemini_http_error(status, provider_status, message)
    provider_error = GeminiAPIError(
        status, provider_status, message + " echoed SUPPRESS_THIS_PROMPT_TEXT", policy,
    )

    async def reject(**_kwargs):
        raise provider_error

    monkeypatch.setattr(runner, "call_gemini_generate", reject)
    before = (pool.blocked[:], pool.dead[:], pool.last_error[:], pool.blackout_until)
    try:
        asyncio.run(runner.generate(
            [{"role": "user", "parts": [{"text": "SUPPRESS_THIS_PROMPT_TEXT"}]}],
            "", ["unit-model"], "diag-test", {},
        ))
    except runner.TemplateGenerationError as exc:
        assert exc.code == public_code
        detail = exc.error_detail
    else:
        raise AssertionError("Gemini rejection should fail the template generation")

    assert detail["http_status"] == status
    assert detail["provider_status"] == provider_status
    assert detail["key_name"] == "GEMINI_API_KEY_TEST"
    assert detail["api_attempts"] == 1
    assert len(detail["attempt_times_ms"]) == 1
    assert len(detail["safe_message"]) <= 200
    assert "SUPPRESS_THIS_PROMPT_TEXT" not in json.dumps(detail)
    assert "test-secret-key-not-for-results" not in json.dumps(detail)

    if public_code == "key_rejected":
        assert pool.dead == [True]
        assert pool.last_error[0]["category"] == "INVALID_API_KEY"
    else:
        assert (pool.blocked, pool.dead, pool.last_error, pool.blackout_until) == before
        assert pool.reserved == [False]
        assert bool(pool.key_model_blocked.get((0, "unit-model"), 0)) is pair_blocked

    logged = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert logged["event"] == "tpl_gemini_error"
    for field in ("http_status", "provider_status", "key_name", "safe_message"):
        assert field in logged
    assert "SUPPRESS_THIS_PROMPT_TEXT" not in json.dumps(logged)
    assert "test-secret-key-not-for-results" not in json.dumps(logged)


@pytest.mark.parametrize(
    ("status", "provider_status", "message", "expected_category", "expected_code"),
    [
        (402, "PAYMENT_REQUIRED", "Billing payment required", "BILLING_PAYMENT_REQUIRED", "quota"),
        (400, "FAILED_PRECONDITION", "Prepayment credits are depleted", "BILLING_CREDITS_DEPLETED", "quota"),
        (403, "PERMISSION_DENIED", "Spend cap breached", "BILLING_SPEND_CAP_BREACHED", "quota"),
        (403, "PERMISSION_DENIED", "This project has been denied access", "PROJECT_ACCESS_DENIED", "key_rejected"),
    ],
)
def test_explicit_billing_and_project_errors_still_hold_the_key(
    monkeypatch, status, provider_status, message, expected_category, expected_code,
):
    pool = make_pool()
    monkeypatch.setattr(runner, "gemini_pool", pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", DirectProxyPool())
    monkeypatch.setattr(runner, "TEMPLATE_RESERVE_KEYS_FOR_OCR", 0)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", 1)
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 0.01)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0.0)
    error = GeminiAPIError(
        status, provider_status, message,
        _classify_gemini_http_error(status, provider_status, message),
    )

    async def reject(**_kwargs):
        raise error

    monkeypatch.setattr(runner, "call_gemini_generate", reject)
    with pytest.raises(runner.TemplateGenerationError) as raised:
        asyncio.run(runner.generate([], "", ["unit-model"], "project-denied", {}))

    assert raised.value.code == expected_code
    assert raised.value.error_detail["category"] == expected_category
    assert pool.blocked[0] == float("inf")
    assert pool.key_model_blocked == {}


def test_selfcheck_does_not_penalize_key_on_model_error(monkeypatch):
    pool = make_pool()
    monkeypatch.setattr(runner, "gemini_pool", pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", DirectProxyPool())

    async def reject(**_kwargs):
        raise GeminiAPIError(
            404, "NOT_FOUND", "Model was not found",
            _classify_gemini_http_error(404, "NOT_FOUND", "Model was not found"),
        )

    monkeypatch.setattr(runner, "call_gemini_generate", reject)
    result = asyncio.run(runner.run_template_selfcheck("unit-model"))

    assert result["ok"] is False
    assert result["step"] == "model"
    assert result["http_status"] == 404
    assert result["provider_status"] == "NOT_FOUND"
    assert result["time"] >= 0
    assert result["model"] == "unit-model"
    assert pool.blocked == [0.0]
    assert pool.dead == [False]
    assert pool.last_error == [None]
    assert pool.reserved == [False]
    assert pool.key_model_blocked == {}
