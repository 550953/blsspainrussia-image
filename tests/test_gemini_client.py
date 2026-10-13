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

        def next(self, excluded=None):
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
    async def healthy_preflight(*args, **kwargs):
        return None
    monkeypatch.setattr(runner, "preflight_template_proxy", healthy_preflight)

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


def test_preflight_failure_is_before_key_acquisition(monkeypatch):
    class FakePool:
        keys = ["dummy"]
        dead = [False]
        reserved = [False]
        acquire_calls = 0

        def _is_operator_hold(self, _index):
            return False

        def acquire(self, **kwargs):
            self.acquire_calls += 1
            self.reserved[0] = True
            return 0, "GEMINI_API_KEY_1"

    class FakeProxyPool:
        def __init__(self):
            self.routes = ["http://proxy.test"]
            self.failed = []

        def next(self, excluded=None):
            excluded = set(excluded or ())
            route = next((route for route in self.routes if route not in excluded), None)
            if route is None:
                raise LookupError("no_proxy_routes")
            return route

        def mark_failed(self, route, error):
            self.failed.append((route, error))

    pool, proxies = FakePool(), FakeProxyPool()
    monkeypatch.setattr(runner, "gemini_pool", pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", proxies)
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", 1)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0)

    async def failed_preflight(*args, **kwargs):
        raise ProxyUnavailable("proxy preflight failed (ConnectError)")

    monkeypatch.setattr(runner, "preflight_template_proxy", failed_preflight)
    with pytest.raises(runner.TemplateGenerationError) as exc:
        asyncio.run(runner.generate(
            [], "", ["unit-model"], "preflight-test", {}, deadline_ts=time.monotonic() + 60,
        ))
    assert exc.value.code == "no_proxy"
    assert pool.acquire_calls == 0
    assert pool.reserved == [False]
    assert len(proxies.failed) == 1


def test_direct_preflight_skips_http(monkeypatch):
    class ShouldNotBeConstructed:
        def __init__(self, **kwargs):
            raise AssertionError("DIRECT must not make a preflight request")

    monkeypatch.setattr(httpx, "AsyncClient", ShouldNotBeConstructed)
    asyncio.run(gemini_client.preflight_template_proxy(None))


def test_generation_timeout_rechecks_and_blames_only_dead_proxy(monkeypatch):
    class FakePool:
        keys = ["dummy"]
        dead = [False]
        reserved = [False]

        def _is_operator_hold(self, _index):
            return False

        def acquire(self, **kwargs):
            self.reserved[0] = True
            return 0, "GEMINI_API_KEY_1"

        def pick_model(self, *_args, **_kwargs):
            return "unit-model"

        def release(self, index, **_kwargs):
            self.reserved[index] = False

        def ensure_released(self, index):
            self.reserved[index] = False

    class FakeProxyPool:
        def __init__(self):
            self.failed = []
            self.routes = ["http://proxy.test"]

        def next(self, excluded=None):
            route = next((r for r in self.routes if r not in set(excluded or ())), None)
            if route is None:
                raise LookupError("no_proxy_routes")
            return route

        def mark_failed(self, route, error):
            self.failed.append((route, error))

        def mark_ok(self, _route):
            pass

    pool, proxies = FakePool(), FakeProxyPool()
    monkeypatch.setattr(runner, "gemini_pool", pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", proxies)
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", 1)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0)
    preflights = 0

    async def preflight(*_args, **_kwargs):
        nonlocal preflights
        preflights += 1
        if preflights == 2:
            raise ProxyUnavailable("proxy preflight failed (ConnectError)")

    async def timed_out(**_kwargs):
        raise GenerationTimeout("read timed out")

    monkeypatch.setattr(runner, "preflight_template_proxy", preflight)
    monkeypatch.setattr(runner, "call_gemini_generate", timed_out)
    with pytest.raises(runner.TemplateGenerationError) as exc:
        asyncio.run(runner.generate(
            [], "", ["unit-model"], "post-timeout-check", {}, deadline_ts=time.monotonic() + 60,
        ))
    assert exc.value.code == "no_proxy"
    assert preflights == 2
    assert len(proxies.failed) == 1
    assert pool.reserved == [False]


def test_proxy_preflight_accepts_any_http_status(monkeypatch):
    observed = {}

    class Response:
        status_code = 503

    class Client:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            observed["url"] = url
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    asyncio.run(gemini_client.preflight_template_proxy("http://proxy.test", timeout=2))
    assert observed["url"] == gemini_client.TEMPLATE_PROXY_PREFLIGHT_URL
    assert observed["client"]["proxy"] == "http://proxy.test"
    assert observed["client"]["trust_env"] is False


@pytest.mark.parametrize(
    ("model", "budget", "level", "expected", "label"),
    [
        ("gemini-2.5-flash", 0, "low", {"thinkingBudget": 0}, "budget:0"),
        ("gemini-2.5-flash", -1, "low", {"thinkingBudget": -1}, "budget:-1"),
        ("gemini-2.5-pro", 0, "low", None, "model-default (2.5 Pro cannot disable thinking)"),
        ("gemini-2.5-pro", 128, "low", {"thinkingBudget": 128}, "budget:128"),
        ("gemini-3.8-flash", 0, "low", {"thinkingLevel": "low"}, "level:low"),
        ("gemini-3.8-flash", 0, "minimal", None, "model-default (unsupported level:minimal)"),
        ("gemini-3.6-flash", 0, "minimal", {"thinkingLevel": "minimal"}, "level:minimal"),
        ("gemini-4-argon", 0, "high", {"thinkingLevel": "high"}, "level:high"),
        ("legacy-model", 0, "low", None, "model-default"),
    ],
)
def test_thinking_config_is_model_specific(monkeypatch, model, budget, level, expected, label):
    monkeypatch.setattr(runner, "TEMPLATE_THINKING_BUDGET", budget)
    monkeypatch.setattr(runner, "TEMPLATE_THINKING_LEVEL", level)
    assert runner.thinking_config_for_model(model) == (expected, label)
