"""429: разбор «retry in 17h», карантин пары ключ+модель, закрытый клиент, не-JSON ответ."""
import asyncio

import pytest

from ocr_service import gemini_client, gemini_sheet, key_pool as key_pool_mod
from ocr_service.config import (
    GEMINI_429_QUARANTINE_AFTER, GEMINI_429_QUARANTINE_SECONDS, GEMINI_QUOTA_RESET_MARGIN,
)
from ocr_service.gemini_errors import GeminiAPIError, _classify_gemini_http_error, _parse_retry_delay
from ocr_service.key_pool import GeminiKeyPool

REAL_DAILY = (
    "You exceeded your current quota, please check your plan and billing details. "
    "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: 500, model: gemini-3.5-flash-lite Please retry in 17h24m17.501760214s."
)


def make_pool(n=1):
    pool = GeminiKeyPool()
    pool.keys = [f"secret-{i}" for i in range(n)]
    pool.key_names = [f"GEMINI_API_KEY_{i}" for i in range(n)]
    pool.blocked = [0.0] * n
    pool.reserved = [False] * n
    pool.dead = [False] * n
    pool.last_used = [0.0] * n
    pool.reserved_at = [0.0] * n
    pool.last_error = [None] * n
    pool.lease_ttl = [60.0] * n
    return pool


def err429(message):
    return GeminiAPIError(429, "RESOURCE_EXHAUSTED", message,
                          _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", message))


def test_parse_retry_delay_understands_google_duration_formats():
    assert _parse_retry_delay("Please retry in 17h24m17.501760214s.") == pytest.approx(17 * 3600 + 24 * 60 + 17.5017, abs=0.01)
    assert _parse_retry_delay("Please retry in 17h56.126564719s.") == pytest.approx(17 * 3600 + 56.1265, abs=0.01)
    assert _parse_retry_delay("Please retry in 62.4s") == pytest.approx(62.4)
    assert _parse_retry_delay('"retryDelay": "30s"') == 30.0
    assert _parse_retry_delay("no hint here") == 0.0


def test_real_daily_quota_message_is_classified_with_exact_reset():
    policy = _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", REAL_DAILY)
    assert policy["daily_quota"] is True
    assert policy["cooldown_seconds"] > 17 * 3600


def test_daily_quota_goes_to_quarantine_on_first_429_until_reset():
    pool = make_pool()
    idx, _ = pool.acquire(model_chain=["m1"], respect_key_cooldown=True)
    outcome = pool.release(idx, error=err429(REAL_DAILY), model="m1")
    assert outcome["state"] == "QUARANTINE" and outcome["quarantine_reason"] == "daily_quota"
    assert outcome["cooldown_seconds"] == pytest.approx(17 * 3600 + 24 * 60 + 17.5 + GEMINI_QUOTA_RESET_MARGIN, abs=1)
    assert pool.acquire(model_chain=["m1"], respect_key_cooldown=True) == (None, None)
    info = pool.quarantine_info(0)
    assert info and info[0]["model"] == "m1" and info[0]["reason"] == "daily_quota"


def test_quarantine_is_per_model_and_key_state_reports_it(monkeypatch):
    monkeypatch.setattr(key_pool_mod, "GEMINI_MODELS", ["m1", "m2"])
    pool = make_pool()
    pool.release(0, error=err429(REAL_DAILY), model="m1")
    import time
    assert pool.key_state(0, time.monotonic())[0] == "ready"      # m2 ещё работает
    assert pool.pick_model(0, chain=["m1", "m2"], respect_key_cooldown=True) == "m2"
    pool.release(0, error=err429(REAL_DAILY), model="m2")
    state, category, left = pool.key_state(0, time.monotonic())
    assert state == "quarantine" and category == "RATE_LIMIT_OR_QUOTA" and left > 17 * 3600
    assert pool.grouped_keys()["quarantine"]["RATE_LIMIT_OR_QUOTA"] == ["GEMINI_API_KEY_0"]


def test_repeated_429_without_known_reset_escalates_and_success_clears(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(key_pool_mod.time, "monotonic", lambda: now[0])
    pool = make_pool()
    plain = "Too many requests"
    outcomes = []
    for _ in range(GEMINI_429_QUARANTINE_AFTER + 1):
        outcomes.append(pool.release(0, error=err429(plain), model="m1"))
        now[0] += 5000          # любые паузы закончились, ключ снова пробуют
    assert outcomes[0]["state"] == "MODEL_COOLDOWN"
    assert outcomes[GEMINI_429_QUARANTINE_AFTER - 1]["state"] == "QUARANTINE"
    assert outcomes[GEMINI_429_QUARANTINE_AFTER - 1]["cooldown_seconds"] == GEMINI_429_QUARANTINE_SECONDS
    assert outcomes[GEMINI_429_QUARANTINE_AFTER]["cooldown_seconds"] == 2 * GEMINI_429_QUARANTINE_SECONDS
    pool.release(0, None, model="m1")   # успешный запрос
    assert (0, "m1") not in pool.key_model_strikes and (0, "m1") not in pool.quarantine
    assert pool.release(0, error=err429(plain), model="m1")["state"] == "MODEL_COOLDOWN"


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


async def test_closed_client_is_replaced_and_request_retried(monkeypatch):
    calls = []

    class Closed:
        async def post(self, *a, **k):
            calls.append("closed")
            raise RuntimeError("Cannot send a request, as the client has been closed.")

    class Fresh:
        async def post(self, *a, **k):
            calls.append("fresh")
            return _FakeResponse()

    clients = [Closed(), Fresh()]
    monkeypatch.setattr(gemini_client, "_get_http_client", lambda proxy, profile="short": clients.pop(0))
    text, _meta = await gemini_client._call_gemini_rest(b"png", "p", "m", "key", None)
    assert text == "ok" and calls == ["closed", "fresh"]


async def test_non_json_answer_does_not_penalise_key(monkeypatch):
    pool = make_pool(2)   # одна и та же пара ключ+канал второй раз не берётся, нужен второй ключ
    monkeypatch.setattr(gemini_sheet, "gemini_pool", pool)
    monkeypatch.setattr(gemini_sheet, "GEMINI_KEYS", pool.keys)
    monkeypatch.setattr(gemini_sheet, "gemini_model_name", "m1")
    monkeypatch.setattr(gemini_sheet, "GEMINI_MODELS", ["m1"])
    monkeypatch.setattr(gemini_sheet, "make_gemini_contact_sheet", lambda images, cols=None: b"png")
    answers = ["not json at all", '{"results": ["742"]}']

    async def fake_call(*args, **kwargs):
        return answers.pop(0), {}

    monkeypatch.setattr(gemini_sheet, "_call_gemini_rest", fake_call)
    result = await gemini_sheet.recognize_gemini_sheet_async([b"img"])
    assert result[0]["text"] == "742"
    assert pool.blocked == [0.0, 0.0]
    assert pool.last_error == [None, None]
