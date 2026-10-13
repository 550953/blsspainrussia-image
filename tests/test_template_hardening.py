import asyncio
import hashlib
import sys
import time
import types
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template.autofix import apply
from ocr_service.email_template.contrast import ratio, suggest_fix
from ocr_service.email_template.validator import RULES, validate

FIXTURE = Path(__file__).parent / "fixtures" / "good.html"


def stub_optional_ocr_dependencies(monkeypatch):
    cv2 = types.ModuleType("cv2")
    numpy = types.ModuleType("numpy")
    numpy.uint8 = object
    numpy.ndarray = object
    ddddocr = types.ModuleType("ddddocr")
    ddddocr.DdddOcr = lambda *args, **kwargs: object()
    for name, module in (("cv2", cv2), ("numpy", numpy), ("ddddocr", ddddocr)):
        monkeypatch.setitem(sys.modules, name, module)


def test_golden_email_is_valid_and_rule_registry_is_complete():
    report = validate(FIXTURE.read_text(encoding="utf-8"), 1)
    assert report.ok, [(item.rule, item.message) for item in report.errors]
    assert not report.warnings
    expected = {
        *(f"V{i:02d}" for i in range(1, 12)),
        *(f"T{i:02d}" for i in range(1, 26)),
        *(f"S{i:02d}" for i in range(1, 13)),
        "L01", "L02", "A01", "A02", "A03", "A04", "P01", "P02",
        *(f"M{i:02d}" for i in range(1, 21)),
    }
    assert expected <= set(RULES)


def test_parser_handles_500_nested_elements_and_large_text():
    source = FIXTURE.read_text(encoding="utf-8")
    nested = "<div>" * 500 + "x" + "</div>" * 500
    source = source.replace("<!-- Здесь вставляется тело письма -->", nested + "<!-- Здесь вставляется тело письма -->")
    report = validate(source)
    assert isinstance(report.ok, bool)


def test_prompt_conflicts_are_warnings_outside_message_and_method_8_15_currency_order_is_allowed():
    source = FIXTURE.read_text(encoding="utf-8")
    source = source.replace("{{brandName}}</p>", "{{brandName}} — в течение дня мы подготовили данные</p>")
    report = validate(source)
    assert "S07" in {item.rule for item in report.warnings}
    assert "S07" not in {item.rule for item in report.errors}
    body = source.replace(
        '<span style="color:#4f6953;font-size:18px;vertical-align:super">$</span>'
        '<span style="color:#24352d;font-size:26px;font-weight:700">{{price}}</span>',
        '<span style="color:#24352d;font-size:26px;font-weight:700">{{price}}</span>'
        '<span style="color:#4f6953;font-size:18px">$</span>',
    )
    for method in (8, 15):
        report = validate(body, method)
        assert "V08" not in {item.rule for item in report.errors}
        assert "L01" not in {item.rule for item in report.errors}


def test_visible_double_slash_is_not_external_resource():
    source = FIXTURE.read_text(encoding="utf-8").replace(
        "Мы подготовили свежие детали", "Мы подготовили свежие детали: путь // хранится локально;"
    )
    report = validate(source)
    assert "T05" not in {item.rule for item in report.errors}


def test_contrast_gradient_uses_worst_stop_and_suggestion_reaches_target():
    assert ratio("#767676", "#fff") > 4.5
    assert ratio("#000000", "linear-gradient(#fff, #111)") < 1.2
    fix = suggest_fix("#8a8a8a", "#ffffff")
    assert fix and ratio(fix, "#ffffff") >= 4.5
    assert suggest_fix("#000000", "#ffffff") is None


def test_contrast_warning_includes_a_specific_repair_color(monkeypatch):
    from ocr_service.email_template import validator

    message = validator._contrast_message("Контраст текста", "#8a8a8a", "#ffffff", ratio("#8a8a8a", "#ffffff"))
    match = __import__("re").search(r"на (#[0-9a-f]{6})", message)
    assert match and ratio(match.group(1), "#ffffff") >= 4.5
    monkeypatch.setattr(validator, "TEMPLATE_CONTRAST_SUGGEST", False)
    assert "на #" not in validator._contrast_message("Контраст текста", "#8a8a8a", "#fff", 3.5)


def test_font_fallback_autofixes_are_scoped_idempotent_and_reported():
    source = '<p style="font-family:&quot;Trebuchet MS&quot;,Arial;font-size:16px">font-family:Georgia</p>'
    fixed, changes = apply(source)
    assert "AF11" in changes and "AF12" in changes
    assert "'Trebuchet MS',Arial, sans-serif" in fixed
    assert "font-family:Georgia</p>" in fixed
    assert apply(fixed)[1] == []


def test_best_attempt_survives_infrastructure_failure_and_repair_stops_without_progress(monkeypatch):
    from ocr_service.email_template import generate
    from ocr_service.email_template.runner import TemplateGenerationError

    monkeypatch.setattr(generate, "TEMPLATE_MAX_ATTEMPTS", 4)
    job = {
        "job_id": "best-on-error", "max_attempts": 4, "models": ["unit-model"], "deadline_s": 420,
        "brief": {"currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
                  "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 1},
    }
    calls = 0

    async def fails_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise TemplateGenerationError("no_proxy")
        return "No document", {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, "unit-model"

    monkeypatch.setattr(generate, "call_model", fails_after_first)
    asyncio.run(generate.run_template_job("best-on-error", job))
    assert job["status"] == "done"
    assert job["template"]["generation_error"] == "no_proxy"
    assert job["template"]["attempts"][-1]["finish_reason"] == "NO_PROXY"

    calls = 0

    async def never_improves(*args, **kwargs):
        nonlocal calls
        calls += 1
        return "No document", {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, "unit-model"

    job["job_id"] = "no-progress"
    monkeypatch.setattr(generate, "call_model", never_improves)
    asyncio.run(generate.run_template_job("no-progress", job))
    assert job["status"] == "done"
    assert calls == 3
    assert job["template"]["generation_error"] == "repair_no_progress"


def test_generation_repairs_bad_output_then_accepts_good_result(monkeypatch):
    from ocr_service.email_template import generate

    good_html = FIXTURE.read_text(encoding="utf-8")
    responses = iter(["No document", good_html])
    calls = []

    async def fake_model(contents, system, models, job_id, generation_config, deadline_ts=None):
        calls.append(contents)
        return next(responses), {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    job = {
        "job_id": "bad-then-good", "max_attempts": 2, "models": ["unit-model"], "deadline_s": 420,
        "brief": {"currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
                  "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 1},
    }
    asyncio.run(generate.run_template_job("bad-then-good", job))
    assert job["status"] == "done"
    assert job["template"]["valid"] is True
    assert [item["errors"] for item in job["template"]["attempts"]][0] == ["X01"]
    assert len(calls) == 2


def test_autofix_can_rescue_output_without_second_model_call(monkeypatch):
    from ocr_service.email_template import generate

    source = FIXTURE.read_text(encoding="utf-8").replace('role="presentation"', "", 1)
    calls = 0

    async def fake_model(contents, system, models, job_id, generation_config, deadline_ts=None):
        nonlocal calls
        calls += 1
        return source, {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    job = {
        "job_id": "autofix-rescue", "max_attempts": 3, "models": ["unit-model"], "deadline_s": 420,
        "brief": {"currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
                  "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 1},
    }
    asyncio.run(generate.run_template_job("autofix-rescue", job))
    attempt = job["template"]["attempts"][0]
    assert job["template"]["valid"] is True
    assert "T08" in attempt["raw_errors"]
    assert "AF04" in attempt["autofixes"]
    assert attempt["errors"] == []
    assert calls == 1


def test_max_tokens_x02_is_included_in_repair_request(monkeypatch):
    from ocr_service.email_template import generate

    good_html = FIXTURE.read_text(encoding="utf-8")
    calls = []

    async def fake_model(contents, system, models, job_id, generation_config, deadline_ts=None):
        calls.append(contents)
        return good_html, {
            "finish_reason": "MAX_TOKENS" if len(calls) == 1 else "STOP",
            "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    job = {
        "job_id": "max-tokens-repair", "max_attempts": 2, "models": ["unit-model"], "deadline_s": 420,
        "brief": {"currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
                  "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 1},
    }
    asyncio.run(generate.run_template_job("max-tokens-repair", job))
    assert "X02" in job["template"]["attempts"][0]["errors"]
    assert job["template"]["valid"] is True
    assert len(calls) == 2
    assert "X02" in calls[1][-1]["parts"][0]["text"]


def test_runner_reports_ocr_priority_timeout_before_acquiring_a_key(monkeypatch):
    from ocr_service.email_template import runner

    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", True)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0.0)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 0.05)
    monkeypatch.setattr(runner, "_ocr_busy", lambda: True)
    deadline = time.monotonic() + 0.5

    async def run():
        try:
            await runner.generate([], "", ["unit-model"], "priority-test", {}, deadline_ts=deadline)
        except runner.TemplateGenerationError as exc:
            assert exc.code == "ocr_priority_timeout"
        else:
            raise AssertionError("OCR priority should stop a template call near its deadline")

    asyncio.run(run())


def test_deadline_during_repair_keeps_the_first_invalid_result(monkeypatch):
    from pathlib import Path
    from ocr_service.email_template import generate
    from ocr_service.email_template.runner import GenerationDeadline

    html = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")
    html = html.replace(
        '<h2 style="margin:20px 0 12px;color:#24352d;font-size:20px;line-height:1.5">Покупка уже в работе</h2>',
        "",
    )
    monkeypatch.setattr(generate, "TEMPLATE_MAX_ATTEMPTS", 2)
    calls = 0

    async def deadline_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GenerationDeadline()
        return "```html\n" + html + "\n```", {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, "unit-model"

    monkeypatch.setattr(generate, "call_model", deadline_after_first)
    job = {
        "job_id": "deadline-after-best", "max_attempts": 2, "models": ["unit-model"], "deadline_s": 420,
        "brief": {"currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
                  "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 2},
    }
    asyncio.run(generate.run_template_job("deadline-after-best", job))
    assert job["status"] == "done"
    assert job["template"]["valid"] is False
    assert job["template"]["generation_error"] == "deadline"
    assert [item["finish_reason"] for item in job["template"]["attempts"]] == ["STOP", "DEADLINE"]


def test_cancel_after_first_attempt_keeps_best_and_returns_key(monkeypatch):
    from pathlib import Path
    from ocr_service.email_template import generate, runner

    html = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")
    html = html.replace(
        '<h2 style="margin:20px 0 12px;color:#24352d;font-size:20px;line-height:1.5">Покупка уже в работе</h2>',
        "",
    )
    entered = asyncio.Event()

    class FakePool:
        keys = ["secret-key"]
        dead = [False]
        reserved = [False]

        def _is_operator_hold(self, _index):
            return False

        def acquire(self, **kwargs):
            self.reserved[0] = True
            return 0, "key_1"

        def pick_model(self, *_args, **_kwargs):
            return "unit-model"

        def release(self, _index, **_kwargs):
            self.reserved[0] = False

        def ensure_released(self, _index):
            self.reserved[0] = False

        def model_allowed(self, *_args, **_kwargs):
            return True

    class FakeProxyPool:
        def next(self):
            return "http://proxy.test"

        def mark_ok(self, _proxy):
            pass

        def mark_failed(self, *_args):
            pass

    pool = FakePool()
    monkeypatch.setattr(runner, "gemini_pool", pool)
    monkeypatch.setattr(runner, "smart_proxy_pool", FakeProxyPool())
    monkeypatch.setattr(runner, "TEMPLATE_YIELD_TO_OCR", False)
    monkeypatch.setattr(runner, "TEMPLATE_INFRA_RETRIES", 1)
    monkeypatch.setattr(runner, "TEMPLATE_MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(runner, "TEMPLATE_DEADLINE_MARGIN", 0)

    async def hanging_gemini(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "call_gemini_generate", hanging_gemini)
    monkeypatch.setattr(generate, "TEMPLATE_MAX_ATTEMPTS", 2)
    calls = 0

    async def first_then_runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "```html\n" + html + "\n```", {
                "finish_reason": "STOP", "prompt_block_reason": "",
                "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
            }, "unit-model"
        return await runner.generate(*args, **kwargs)

    monkeypatch.setattr(generate, "call_model", first_then_runner)
    job = {
        "job_id": "cancel-after-best", "max_attempts": 2, "models": ["unit-model"], "deadline_s": 420,
        "brief": {"currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
                  "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 3},
    }

    async def run_and_cancel():
        task = asyncio.create_task(generate.run_template_job("cancel-after-best", job))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_and_cancel())
    assert job["status"] == "done"
    assert job["template"]["valid"] is False
    assert job["template"]["generation_error"] == "cancelled"
    assert pool.reserved == [False]


def test_import_surface_and_lightweight_template_import(monkeypatch):
    stub_optional_ocr_dependencies(monkeypatch)
    from ocr_service import jobs, key_pool, telemetry

    for name in (
        "JOBS", "JOB_TASKS", "JOB_MAX_SECONDS", "JOB_RESULT_TTL_SECONDS", "spawn_job",
        "_job_queue_metrics", "_job_public_payload", "_cleanup_expired_jobs", "_watchdog_tick",
        "start_cleanup_loop", "stop_cleanup_loop", "start_watchdog", "stop_watchdog",
        "gemini_pool_lease_ttl",
    ):
        assert hasattr(jobs, name)
    for name in ("gemini_pool", "GEMINI_KEYS", "gemini_model_name", "KEY_LEASE_TTL"):
        assert hasattr(key_pool, name)
    for name in ("ATTEMPTS", "JOB_STATS", "betterstack_queue_size", "_job_log"):
        assert hasattr(telemetry, name)
    code = (
        "import sys; import ocr_service.email_template.validator, "
        "ocr_service.email_template.prompt, ocr_service.email_template.director; "
        "assert 'cv2' not in sys.modules and 'ddddocr' not in sys.modules"
    )
    import subprocess
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr


def test_rate_limit_quota_is_model_scoped_and_daily_quota_does_not_block_ocr(monkeypatch):
    from ocr_service.gemini_errors import GeminiAPIError, _classify_gemini_http_error
    from ocr_service.key_pool import GeminiKeyPool

    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    pool = GeminiKeyPool()
    pool.keys, pool.key_names = ["secret"], ["GEMINI_API_KEY_1"]
    pool.blocked, pool.reserved, pool.dead = [0.0], [True], [False]
    pool.last_error, pool.lease_ttl = [None], [60.0]
    short = GeminiAPIError(429, "RESOURCE_EXHAUSTED", 'retryDelay: "30s"',
                           _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", 'retryDelay: "30s"'))
    pool.release(0, error=short, model="template-model")
    assert pool.blocked[0] == 0
    assert pool.key_model_blocked[(0, "template-model")] - now >= 60
    assert pool.pick_model(0, chain=["template-model"]) == "template-model"

    pool.reserved[0] = True
    daily_message = "Quota exceeded per day; retryDelay: 3600s"
    daily = GeminiAPIError(429, "RESOURCE_EXHAUSTED", daily_message,
                           _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", daily_message))
    result = pool.release(0, error=daily, model="daily-model")
    assert result["daily_quota"]
    assert pool.key_model_daily_blocked[(0, "daily-model")] - now == 900
    assert pool.blocked[0] == 0
    assert pool.model_allowed(0, "daily-model", respect_key_cooldown=False)


def test_template_logs_redact_content_and_do_not_mutate_ocr_counters(monkeypatch, capsys):
    from ocr_service import telemetry

    before = {
        "attempts": list(telemetry.ATTEMPTS),
        "quality": list(telemetry.QUALITY),
        "key_last": dict(telemetry.KEY_LAST),
        "last_event": dict(telemetry.LAST_EVENT),
        "accounts": dict(telemetry.ACCOUNTS),
        "job_stats": dict(telemetry.JOB_STATS),
    }
    telemetry._job_log(
        "tpl_test", "safe-id",
        where="SENSITIVE_WHERE", html="SENSITIVE_HTML", prompt="SENSITIVE_PROMPT",
        api_key="SENSITIVE_KEY", nested={"response": "SENSITIVE_RESPONSE", "safe": "kept"},
    )
    output = capsys.readouterr().out
    for secret in ("SENSITIVE_WHERE", "SENSITIVE_HTML", "SENSITIVE_PROMPT", "SENSITIVE_KEY", "SENSITIVE_RESPONSE"):
        assert secret not in output
    logged = __import__("json").loads(output)
    assert logged["nested"] == {"safe": "kept"}
    assert before == {
        "attempts": list(telemetry.ATTEMPTS),
        "quality": list(telemetry.QUALITY),
        "key_last": dict(telemetry.KEY_LAST),
        "last_event": dict(telemetry.LAST_EVENT),
        "accounts": dict(telemetry.ACCOUNTS),
        "job_stats": dict(telemetry.JOB_STATS),
    }


def test_submit_order_reused_request_and_api_key_namespaces(monkeypatch):
    stub_optional_ocr_dependencies(monkeypatch)
    from ocr_service.routers import template
    from ocr_service.email_template import director
    from ocr_service.telemetry import TEMPLATE_STATS

    class DormantTask:
        def __init__(self):
            self._done = False
            self._callbacks = []

        def done(self):
            return self._done

        def add_done_callback(self, callback):
            self._callbacks.append(callback)

        def cancel(self):
            self._done = True
            for callback in self._callbacks:
                callback(self)

    class FakePool:
        keys = ["key-1", "key-2"]
        dead = [False, False]

        @staticmethod
        def _is_operator_hold(_index):
            return False

    monkeypatch.setattr(template, "TEMPLATE_ENABLED", True)
    monkeypatch.setattr(template, "TEMPLATE_API_KEYS", ["client-a", "client-b"])
    monkeypatch.setattr(template, "TEMPLATE_MODELS", ["unit-model"])
    monkeypatch.setattr(template, "gemini_pool", FakePool())
    monkeypatch.setattr(template.asyncio, "create_task", lambda coroutine: (coroutine.close(), DormantTask())[1])
    template.TEMPLATE_JOBS.clear()
    template._IDEMPOTENCY.clear()
    template._RATE.clear()
    director.reset_history()
    old_daily_jobs = TEMPLATE_STATS.get("daily_jobs", 0)
    TEMPLATE_STATS["daily_jobs"] = 0

    def request(key, payload):
        raw = __import__("json").dumps(payload).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        return Request({
            "type": "http", "method": "POST", "path": "/template/submit",
            "headers": [(b"x-api-key", key.encode()), (b"content-length", str(len(raw)).encode())],
            "query_string": b"", "server": ("test", 80), "client": ("test", 1), "scheme": "http",
        }, receive)

    base = {"account_id": "same-store", "client_request_id": "same-request", "seed": 55}

    async def run():
        rejected = False
        try:
            await template.submit(request("client-a", {**base, "max_attempts": 6}))
        except HTTPException as exc:
            assert exc.status_code == 400
            rejected = True
        assert rejected and len(director.HISTORY) == 0 and TEMPLATE_STATS["daily_jobs"] == 0
        first = await template.submit(request("client-a", base))
        reused = await template.submit(request("client-a", base))
        second_namespace = await template.submit(request("client-b", base))
        assert first["reused"] is False
        assert reused["reused"] is True and reused["job_id"] == first["job_id"]
        assert second_namespace["reused"] is False and second_namespace["job_id"] != first["job_id"]
        assert TEMPLATE_STATS["daily_jobs"] == 2
        assert len(director.HISTORY) == 2
        template.TEMPLATE_JOBS[second_namespace["job_id"]]["status"] = "done"
        monkeypatch.setattr(template, "TEMPLATE_DAILY_CAP", 2)
        try:
            await template.submit(request("client-a", {
                "account_id": "same-store", "client_request_id": "would-be-rejected", "seed": 56,
            }))
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("daily cap must reject before brief/history accounting")
        assert TEMPLATE_STATS["daily_jobs"] == 2 and len(director.HISTORY) == 2
        foreign = request("client-b", base)
        try:
            await template.result(first["job_id"], foreign, 0)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("cross-namespace result access must be hidden")

    try:
        asyncio.run(run())
    finally:
        for task in list(template.TEMPLATE_TASKS):
            task.cancel()
        template.TEMPLATE_TASKS.clear()
        template._TEMPLATE_TASK_BY_ID.clear()
        template.TEMPLATE_JOBS.clear()
        template._IDEMPOTENCY.clear()
        template._RATE.clear()
        director.reset_history()
        TEMPLATE_STATS["daily_jobs"] = old_daily_jobs


def test_cancel_checks_namespace_and_stops_a_pending_task(monkeypatch):
    stub_optional_ocr_dependencies(monkeypatch)
    from ocr_service.routers import template

    monkeypatch.setattr(template, "TEMPLATE_API_KEYS", ["client-key"])
    job_id = "cancel-test"
    namespace = hashlib.sha1(b"client-key").hexdigest()[:8]
    job = {"job_id": job_id, "namespace": namespace, "status": "pending"}
    template.TEMPLATE_JOBS[job_id] = job

    async def run():
        request = Request({
            "type": "http", "method": "POST", "path": "/template/cancel/" + job_id,
            "headers": [(b"x-api-key", b"client-key")], "query_string": b"",
            "server": ("test", 80), "client": ("test", 1), "scheme": "http",
        })
        task = asyncio.create_task(asyncio.Event().wait())
        template._TEMPLATE_TASK_BY_ID[job_id] = task
        response = await template.cancel(job_id, request)
        assert response["error"] == "cancelled"
        try:
            await task
        except asyncio.CancelledError:
            pass
        foreign = Request({
            "type": "http", "method": "POST", "path": "/template/cancel/" + job_id,
            "headers": [(b"x-api-key", b"other-key")], "query_string": b"",
            "server": ("test", 80), "client": ("test", 1), "scheme": "http",
        })
        try:
            await template.cancel(job_id, foreign)
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("foreign key unexpectedly accessed another namespace")

    try:
        asyncio.run(run())
    finally:
        template.TEMPLATE_JOBS.pop(job_id, None)
        template._TEMPLATE_TASK_BY_ID.pop(job_id, None)
