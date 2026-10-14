"""Регрессии по реальному неудачному прогону gemini-3.8-flash (job 2bf5decd…, valid=False).

Фикстура real_flash38_run2.html — HTML после автофикса из того прогона. В ней были:
T09 (у основной таблицы нет bgcolor), V08 (ложное: знак «$» в соседней вложенной таблице),
L01 (каскад от отсутствующего <ul>), T17 (список из «•» в таблице), A01/A02 (контраст).
"""
import asyncio
import time
from pathlib import Path

from ocr_service.email_template import autofix
from ocr_service.email_template.prompt import build_repair_message
from ocr_service.email_template.validator import validate

FIXTURE = Path(__file__).parent / "fixtures" / "real_flash38_run2.html"
METHOD = 19


def _rules(report):
    return {item.rule for item in report.errors}


def test_autofix_adds_bgcolor_to_main_table_and_is_idempotent():
    source = FIXTURE.read_text(encoding="utf-8")
    assert "T09" in _rules(validate(source, METHOD))
    fixed, fix_ids = autofix.apply(source)
    assert "AF09" in fix_ids
    assert "T09" not in _rules(validate(fixed, METHOD))
    assert autofix.apply(fixed) == (fixed, [])


def test_currency_badge_in_neighbouring_nested_table_is_not_v08():
    report = validate(FIXTURE.read_text(encoding="utf-8"), METHOD)
    assert "V08" not in _rules(report)


def test_v08_still_fires_when_currency_sign_is_missing():
    source = FIXTURE.read_text(encoding="utf-8").replace(">$</td>", "></td>")
    assert "V08" in _rules(validate(source, METHOD))


def test_l01_is_not_a_cascade_of_missing_list_but_t17_is_reported():
    rules = _rules(validate(FIXTURE.read_text(encoding="utf-8"), METHOD))
    assert "T17" in rules
    assert "L01" not in rules


def test_l01_still_fires_when_blocks_are_really_out_of_order():
    source = FIXTURE.read_text(encoding="utf-8")
    swapped = source.replace("{{name}}", "@@", 1).replace("{{brandName}}", "{{name}}", 1).replace("@@", "{{brandName}}", 1)
    assert "L01" in _rules(validate(swapped, METHOD))


def test_contrast_hint_for_white_text_on_badge_changes_background_not_text():
    report = validate(FIXTURE.read_text(encoding="utf-8"), METHOD)
    badge = [item.message for item in report.errors if item.rule == "A02"]
    assert badge and "Замените цвет фона" in badge[0]
    assert "#0a0a0a" not in badge[0]


def test_repair_message_explains_ul_requirement():
    report = validate(FIXTURE.read_text(encoding="utf-8"), METHOD)
    issues = [{"rule": i.rule, "message": i.message, "where": i.where} for i in report.errors]
    assert "<ul>" in build_repair_message(issues) and "<li>" in build_repair_message(issues)


# ---- runner ----------------------------------------------------------------------------------

def _fakes(runner, key_count, routes):
    class KeyPool:
        keys = ["secret-%d" % i for i in range(key_count)]
        key_names = ["GEMINI_API_KEY_%d" % i for i in range(key_count)]

        def __init__(self):
            self.dead = [False] * key_count
            self.reserved = [False] * key_count

        def _is_operator_hold(self, _index):
            return False

        def acquire(self, exclude_indices=None, **_kwargs):
            excluded = set(exclude_indices or ())
            index = next(
                (i for i in range(key_count) if i not in excluded and not self.reserved[i] and not self.dead[i]),
                None,
            )
            if index is None:
                return None, None
            self.reserved[index] = True
            return index, self.key_names[index]

        def pick_model(self, *_args, **_kwargs):
            return "unit-model"

        def release(self, index, error=None, **_kwargs):
            self.reserved[index] = False
            if error is not None:
                self.dead[index] = True   # ключ на холде, как при PROJECT_ACCESS_DENIED

        def block_model_for_key(self, *_args, **_kwargs):
            pass

        def mark_model_overloaded(self, *_args, **_kwargs):
            pass

        def ensure_released(self, index):
            self.reserved[index] = False

    class ProxyPool:
        def __init__(self):
            self.routes = list(routes)
            self.position = 0

        def next(self, excluded=None):
            excluded = set(excluded or ())
            for offset in range(len(self.routes)):
                index = (self.position + offset) % len(self.routes)
                if self.routes[index] not in excluded:
                    self.position = (index + 1) % len(self.routes)
                    return self.routes[index]
            raise LookupError("no routes")

        def mark_ok(self, _route):
            pass

        def mark_failed(self, *_args):
            pass

    return KeyPool(), ProxyPool()


def _patch_runner(monkeypatch, runner, key_pool, proxy_pool, retries):
    async def healthy_preflight(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "gemini_pool", key_pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", proxy_pool)
    monkeypatch.setattr(runner, "SLOW_ROUTE_PENALTIES", {})
    monkeypatch.setattr(runner, "TEMPLATE_SLOW_ROUTE_COOLDOWN", 10)
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_RESERVE_KEYS_FOR_OCR", 0)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", retries)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0)
    monkeypatch.setattr(runner, "preflight_template_proxy", healthy_preflight)


def test_dead_project_keys_do_not_spend_the_infra_retry_budget(monkeypatch):
    from ocr_service.email_template import runner
    from ocr_service.gemini_errors import GeminiAPIError, _classify_gemini_http_error

    key_pool, proxy_pool = _fakes(runner, 3, [None])
    _patch_runner(monkeypatch, runner, key_pool, proxy_pool, retries=1)
    message = "Your project has been denied access. Please contact support."
    denied = GeminiAPIError(403, "PERMISSION_DENIED", message, _classify_gemini_http_error(403, "PERMISSION_DENIED", message))
    assert denied.category == "PROJECT_ACCESS_DENIED"
    seen = []

    async def call(**kwargs):
        seen.append(kwargs["key"])
        if kwargs["key"] in ("secret-0", "secret-1"):
            raise denied
        return "ok", {"usage": {}}

    monkeypatch.setattr(runner, "call_gemini_generate", call)
    text, metadata, _model = asyncio.run(runner.generate(
        [], "", ["unit-model"], "dead-keys", {}, deadline_ts=time.monotonic() + 60,
    ))
    assert text == "ok"
    assert seen == ["secret-0", "secret-1", "secret-2"]
    assert [item["outcome"] for item in metadata["api_trace"]] == ["api_error", "api_error", "success"]


def test_only_first_timeout_in_a_call_penalizes_the_route(monkeypatch):
    from ocr_service.email_template import runner
    from ocr_service.gemini_client import GenerationTimeout

    key_pool, proxy_pool = _fakes(runner, 3, [None, "http://p1.example:1", "http://p2.example:2"])
    _patch_runner(monkeypatch, runner, key_pool, proxy_pool, retries=3)
    calls = []

    async def call(**kwargs):
        calls.append(kwargs["key"])
        if len(calls) <= 2:
            raise GenerationTimeout("first byte timed out")
        return "ok", {"usage": {}}

    monkeypatch.setattr(runner, "call_gemini_generate", call)
    text, metadata, _model = asyncio.run(runner.generate(
        [], "", ["unit-model"], "timeouts", {}, deadline_ts=time.monotonic() + 60,
    ))
    assert text == "ok"
    assert runner.slow_route_penalty_count() == 1
    assert [item["outcome"] for item in metadata["api_trace"]] == ["timeout", "timeout", "success"]


# ---- first-byte timeout для «больших» (ремонтных) запросов -------------------------------------

def test_large_repair_request_gets_longer_first_byte_timeout(monkeypatch):
    from ocr_service import config, gemini_client

    captured = []

    async def fake_stream(client, payload, model, key, timeout, connect, first_byte, stall):
        captured.append(first_byte)
        return "ok", {"finish_reason": "STOP", "usage": {}}

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda *_a, **_k: object())
    monkeypatch.setattr(gemini_client, "_call_gemini_stream", fake_stream)
    monkeypatch.setattr(config, "TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT", 25.0)
    monkeypatch.setattr(config, "TEMPLATE_REPAIR_FIRST_BYTE_TIMEOUT", 75.0)
    monkeypatch.setattr(config, "TEMPLATE_LARGE_REQUEST_CHARS", 22000)

    small = [{"role": "user", "parts": [{"text": "x" * 1000}]}]
    large = [
        {"role": "user", "parts": [{"text": "x" * 1000}]},
        {"role": "model", "parts": [{"text": "y" * 20000}]},
        {"role": "user", "parts": [{"text": "z" * 1000}]},
    ]
    for contents in (small, large):
        asyncio.run(gemini_client.call_gemini_generate(
            contents=contents, system_instruction="s" * 14000, model="unit-model", key="k",
            proxy_url=None, profile="long", hard_timeout=100,
        ))
    assert captured == [25.0, 75.0]
