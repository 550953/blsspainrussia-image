import asyncio
import time

import httpx
import pytest

from ocr_service import gemini_client
from ocr_service.email_template import runner
from ocr_service.gemini_client import GenerationTimeout
from ocr_service.proxies import ProxyUnavailable


def test_long_generation_payload_omits_image_data_and_filters_thought_parts(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "modelVersion": "unit-model-v2",
                "candidates": [{
                    "content": {"parts": [{"text": "HTML answer"}, {"text": "private reasoning", "thought": True}]},
                    "finishReason": "STOP",
                }],
                "usageMetadata": {
                    "promptTokenCount": 11, "candidatesTokenCount": 22,
                    "thoughtsTokenCount": 33, "cachedContentTokenCount": 4,
                },
            }

    class Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return Response()

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda proxy, profile: Client())
    text, metadata = asyncio.run(gemini_client.call_gemini_generate(
        contents=[{"parts": [{"text": "closed whitelist brief"}]}],
        system_instruction="generate HTML only",
        model="unit-model",
        key="dummy-test-key",
        proxy_url="http://proxy.test",
        generation_config={"temperature": 1.0, "maxOutputTokens": 100},
        profile="long",
        hard_timeout=150,
    ))
    assert text == "HTML answer"
    assert "private reasoning" not in text
    assert metadata["finish_reason"] == "STOP"
    assert metadata["usage"] == {"in": 11, "out": 22, "thoughts": 33, "cached": 4}
    assert captured["json"]["contents"] == [{"parts": [{"text": "closed whitelist brief"}]}]
    assert "inline_data" not in str(captured["json"])
    assert captured["headers"] == {"x-goog-api-key": "dummy-test-key"}
    assert captured["url"].endswith("/unit-model:generateContent")


@pytest.mark.parametrize(
    ("profile", "exception_type"),
    [("long", GenerationTimeout), ("short", ProxyUnavailable)],
)
def test_read_timeout_classification_depends_on_profile(monkeypatch, profile, exception_type):
    class Client:
        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda *args, **kwargs: Client())
    with pytest.raises(exception_type):
        asyncio.run(gemini_client.call_gemini_generate(
            contents=[], system_instruction="", model="unit-model", key="dummy",
            proxy_url=None, profile=profile, hard_timeout=10,
        ))


def test_connect_error_remains_proxy_failure(monkeypatch):
    class Client:
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("connect failed", request=httpx.Request("POST", "https://example.test"))

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda *args, **kwargs: Client())
    with pytest.raises(ProxyUnavailable):
        asyncio.run(gemini_client.call_gemini_generate(
            contents=[], system_instruction="", model="unit-model", key="dummy",
            proxy_url=None, profile="long", hard_timeout=10,
        ))


def test_http_clients_are_separate_for_long_and_short_profiles(monkeypatch):
    constructed = []

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            constructed.append(self)

    monkeypatch.setattr(gemini_client, "_HTTPX_CLIENTS", {})
    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTPClient)
    long = gemini_client._get_http_client("http://proxy.test", profile="long")
    short = gemini_client._get_http_client("http://proxy.test", profile="short")
    assert long is not short
    assert long.kwargs["timeout"].read >= 100
    assert len(constructed) == 2


def test_long_timeout_does_not_quarantine_proxy_or_key(monkeypatch):
    class FakePool:
        keys = ["dummy"]
        dead = [False]
        blocked = [0.0]
        reserved = [False]
        reserved_at = [0.0]
        last_error = [None]
        lease_ttl = [200.0]
        key_names = ["GEMINI_API_KEY_1"]

        def _is_operator_hold(self, _index):
            return False

        def acquire(self, **kwargs):
            self.reserved[0] = True
            return 0, "GEMINI_API_KEY_1"

        def pick_model(self, *_args, **_kwargs):
            return "unit-model"

        def release(self, index, error=None, model=None):
            self.reserved[index] = False

        def ensure_released(self, index):
            self.reserved[index] = False

    class FakeProxyPool:
        failed = 0

        def next(self):
            return "http://proxy.test"

        def mark_failed(self, *_args):
            self.failed += 1

        def mark_ok(self, *_args):
            pass

    pool = FakePool()
    proxies = FakeProxyPool()
    monkeypatch.setattr(runner, "gemini_pool", pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", proxies)
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", 1)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0)

    async def timed_out(**kwargs):
        raise GenerationTimeout("read timed out")

    monkeypatch.setattr(runner, "call_gemini_generate", timed_out)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.generate(
            [], "", ["unit-model"], "timeout-test", {}, deadline_ts=time.monotonic() + 60,
        ))
    assert proxies.failed == 0
    assert pool.reserved == [False]
    assert pool.blocked == [0.0]
