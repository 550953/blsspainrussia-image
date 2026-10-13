import asyncio
import json
from pathlib import Path
import time

import pytest

from ocr_service import gemini_client, telemetry
from ocr_service.email_template import autofix
from ocr_service.email_template.extract import parse
from ocr_service.email_template.validator import validate
from ocr_service.gemini_client import GenerationTimeout


FIXTURE = Path(__file__).parent / "fixtures" / "real_flash38_run1.html"


def test_real_flash38_fixture_autofixes_all_errors_and_is_idempotent(monkeypatch):
    source = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(autofix, "TEMPLATE_AUTOFIX_CONTRAST", True)

    fixed, fix_ids = autofix.apply(source)
    report = validate(fixed, 18)

    assert not report.errors
    assert {"AF15", "AF16", "AF17", "AF18"}.issubset(fix_ids)
    assert "AF13" in fix_ids
    assert autofix.apply(fixed)[1] == []
    assert any(item.rule == "V07" and item.severity == "W" for item in report.warnings)


def test_contrast_autofix_is_opt_in(monkeypatch):
    source = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(autofix, "TEMPLATE_AUTOFIX_CONTRAST", False)
    _, fix_ids = autofix.apply(source)
    assert "AF18" not in fix_ids


def test_validator_and_extractor_accept_utf8_bom():
    plain = FIXTURE.read_text(encoding="utf-8")
    source = "\ufeff \r\n" + plain
    parsed = parse(source)
    assert parsed.html[:15].lower() == "<!doctype html>"
    assert [item.rule for item in validate(source, 18).errors] == [
        item.rule for item in validate(plain, 18).errors
    ]


def test_template_api_telemetry_is_secret_free_and_p50_uses_successes(monkeypatch):
    monkeypatch.setattr(telemetry, "TEMPLATE_API_CALLS", telemetry.deque(maxlen=8))
    telemetry.record_template_api_call({
        "n": 1, "wait_ocr_ms": 5, "wait_key_ms": 3, "preflight_ms": 7,
        "call_ms": 20, "key_name": "GEMINI_API_KEY_1", "proxy": "proxy-1",
        "model": "unit-model", "outcome": "success", "http_status": 200,
    })
    telemetry.record_template_api_call({
        "n": 2, "call_ms": 100, "key_name": "GEMINI_API_KEY_1",
        "proxy": "proxy-1", "model": "unit-model", "outcome": "timeout",
    })
    assert telemetry.template_success_p50_ms("unit-model") == 20
    assert all("secret" not in str(row).lower() for row in telemetry.TEMPLATE_API_CALLS)


def test_status_separates_template_api_latency_by_key_and_proxy(monkeypatch):
    from ocr_service import config, status_data
    from ocr_service.routers import template as template_router

    now = time.time()
    monkeypatch.setattr(config, "TEMPLATE_ENABLED", True)
    monkeypatch.setattr(status_data, "TEMPLATE_API_CALLS", [
        {"ts": now, "n": 1, "call_ms": 100, "key_name": "GEMINI_API_KEY_1",
         "proxy": "http://proxy.example:8080", "model": "unit-model",
         "outcome": "success", "http_status": 200},
        {"ts": now, "n": 2, "call_ms": 300, "key_name": "GEMINI_API_KEY_1",
         "proxy": "http://proxy.example:8080", "model": "unit-model",
         "outcome": "timeout", "http_status": None},
    ])
    monkeypatch.setattr(status_data, "TEMPLATE_RUNS", [])
    monkeypatch.setattr(status_data, "TEMPLATE_STATS", {})
    monkeypatch.setattr(template_router, "TEMPLATE_JOBS", {})
    monkeypatch.setattr(template_router, "effective_concurrency", lambda: 1)

    api = status_data.build_status()["templates"]["api_calls"]
    assert api["calls"] == 2
    assert api["by_key"]["GEMINI_API_KEY_1"]["p50_ms"] == 100
    assert api["by_proxy"]["http://proxy.example:8080"]["p95_ms"] == 300
    assert api["by_model"]["unit-model"]["timeouts"] == 1


def test_timed_out_key_proxy_pair_gets_penalty_and_retry_uses_alternates(monkeypatch):
    from ocr_service.email_template import runner
    from ocr_service.gemini_client import GenerationTimeout

    class KeyPool:
        keys = ["secret-1", "secret-2"]
        dead = [False, False]
        reserved = [False, False]
        key_names = ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2"]

        def _is_operator_hold(self, _index):
            return False

        def acquire(self, exclude_indices=None, **_kwargs):
            excluded = set(exclude_indices or ())
            index = next((i for i in range(2) if i not in excluded and not self.reserved[i]), None)
            if index is None:
                return None, None
            self.reserved[index] = True
            return index, self.key_names[index]

        def pick_model(self, *_args, **_kwargs):
            return "unit-model"

        def release(self, index, **_kwargs):
            self.reserved[index] = False

        def ensure_released(self, index):
            self.reserved[index] = False

    class ProxyPool:
        routes = [None, "http://user:password@proxy.example:8080"]

        def __init__(self):
            self.position = 0

        def next(self, excluded=None):
            excluded = set(excluded or ())
            for offset in range(len(self.routes)):
                index = (self.position + offset) % len(self.routes)
                route = self.routes[index]
                if route not in excluded:
                    self.position = (index + 1) % len(self.routes)
                    return route
            raise LookupError("no routes")

        def mark_ok(self, _route):
            pass

        def mark_failed(self, *_args):
            pass

    key_pool, proxy_pool = KeyPool(), ProxyPool()
    monkeypatch.setattr(runner, "gemini_pool", key_pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", proxy_pool)
    monkeypatch.setattr(runner, "SLOW_ROUTE_PENALTIES", {})
    monkeypatch.setattr(runner, "TEMPLATE_SLOW_ROUTE_COOLDOWN", 10)
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", 2)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0)

    async def healthy_preflight(*_args, **_kwargs):
        return None

    seen = []

    async def call(**kwargs):
        seen.append((kwargs["key"], kwargs["proxy_url"]))
        if len(seen) == 1:
            raise GenerationTimeout("simulated slow model")
        return "ok", {"usage": {}}

    monkeypatch.setattr(runner, "preflight_template_proxy", healthy_preflight)
    monkeypatch.setattr(runner, "call_gemini_generate", call)
    text, metadata, _model = asyncio.run(runner.generate(
        [], "", ["unit-model"], "route-test", {}, deadline_ts=time.monotonic() + 60,
    ))

    assert text == "ok"
    assert seen == [("secret-1", None), ("secret-2", "http://user:password@proxy.example:8080")]
    assert runner.SLOW_ROUTE_PENALTIES[(0, None)]["hits"] == 1
    assert runner.slow_route_penalty_count() == 1
    trace = metadata["api_trace"]
    assert [item["outcome"] for item in trace] == ["timeout", "success"]
    assert trace[0]["proxy"] == "DIRECT"
    assert trace[1]["proxy"] == "http://proxy.example:8080"
    assert "password" not in str(trace)


class _StreamResponse:
    status_code = 200

    def __init__(self, chunks):
        self.chunks = iter(chunks)

    async def aiter_bytes(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            if isinstance(chunk, tuple):
                await asyncio.sleep(chunk[0])
                yield chunk[1]
            else:
                yield chunk


class _StreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


def _event(text, finish_reason=None):
    value = {
        "candidates": [{
            "content": {"parts": [{"text": text}]},
            **({"finishReason": finish_reason} if finish_reason else {}),
        }],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
    }
    return ("data: " + json.dumps(value) + "\n\n").encode()


def test_long_generation_uses_sse_and_accumulates_text(monkeypatch):
    observed = {}

    class Client:
        def stream(self, method, url, **kwargs):
            observed.update(method=method, url=url, kwargs=kwargs)
            return _StreamContext(_StreamResponse([
                _event("first "), _event("answer", "STOP"), b"data: [DONE]\n\n",
            ]))

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda *_args, **_kwargs: Client())
    text, metadata = asyncio.run(gemini_client.call_gemini_generate(
        contents=[], system_instruction="", model="unit-model", key="test-key",
        proxy_url=None, profile="long", hard_timeout=3,
    ))
    assert observed["url"].endswith("/unit-model:streamGenerateContent")
    assert observed["kwargs"]["params"] == {"alt": "sse"}
    assert text == "first answer"
    assert metadata["finish_reason"] == "STOP"
    assert metadata["usage"]["in"] == 4


def test_sse_first_byte_timeout_is_classified_for_route_penalty(monkeypatch):
    class Client:
        def stream(self, *_args, **_kwargs):
            return _StreamContext(_StreamResponse([(0.05, _event("late"))]))

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(gemini_client, "TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(gemini_client, "TEMPLATE_STREAM_STALL_TIMEOUT", 0.1, raising=False)
    # Settings are imported from config within call_gemini_generate.
    from ocr_service import config
    monkeypatch.setattr(config, "TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT", 0.01)
    with pytest.raises(GenerationTimeout, match="first byte"):
        asyncio.run(gemini_client.call_gemini_generate(
            contents=[], system_instruction="", model="unit-model", key="test-key",
            proxy_url=None, profile="long", hard_timeout=2,
        ))


def test_sse_stall_timeout_is_classified_for_route_penalty(monkeypatch):
    class Client:
        def stream(self, *_args, **_kwargs):
            return _StreamContext(_StreamResponse([
                _event("first"),
                (0.05, _event("late")),
            ]))

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda *_args, **_kwargs: Client())
    from ocr_service import config
    monkeypatch.setattr(config, "TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT", 0.1)
    monkeypatch.setattr(config, "TEMPLATE_STREAM_STALL_TIMEOUT", 0.01)
    with pytest.raises(GenerationTimeout, match="stalled"):
        asyncio.run(gemini_client.call_gemini_generate(
            contents=[], system_instruction="", model="unit-model", key="test-key",
            proxy_url=None, profile="long", hard_timeout=2,
        ))
