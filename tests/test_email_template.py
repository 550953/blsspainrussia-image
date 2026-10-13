import sys
import asyncio
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template.contrast import ratio
from ocr_service.email_template.extract import parse
from ocr_service.email_template.autofix import apply
from ocr_service.email_template.validator import validate
from ocr_service.key_pool import GeminiKeyPool


def test_wcag_contrast_gold_values():
    assert ratio("#000000", "#ffffff") == 21
    assert ratio("#767676", "#ffffff") > 4.5
    assert ratio("#777777", "#ffffff") < 4.5


def test_extract_html_document_and_detect_truncation():
    result = parse("Design notes\n```html\n<!DOCTYPE html><html><body>ok</body></html>\n```")
    assert result.html.startswith("<!DOCTYPE html>")
    assert result.concept == "Design notes"
    assert parse("<!DOCTYPE html><html>").errors[0]["rule"] == "X02"


def test_validator_rejects_missing_required_content():
    report = validate("<!DOCTYPE html><html lang='ru'><head></head><body></body></html>")
    rules = {item.rule for item in report.errors}
    assert not report.ok
    assert "V01" in rules
    assert "T18" in rules


def test_email_template_import_does_not_load_heavy_ocr_dependencies():
    code = (
        "import sys; import ocr_service.email_template.validator; "
        "assert 'cv2' not in sys.modules and 'ddddocr' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr


def test_autofix_is_narrow_and_idempotent():
    html = (
        '<!DOCTYPE html><html><head></head><body bgcolor="#ffffff">'
        '<div class="preheader" style="display:none;font-size:1px;line-height:1px;'
        'max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all">'
        'Информация по заказу</div><table style="border-radius:8px"></table></body></html>'
    )
    fixed, changes = apply(html)
    assert {"AF04", "AF05", "AF06", "AF07", "AF08", "AF09"}.issubset(changes)
    assert 'lang="ru"' in fixed
    assert "border-collapse:separate" in fixed
    assert apply(fixed)[1] == []


def test_each_autofix_rule_has_an_isolated_mutation():
    good = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")
    mutations = {
        "AF01": good.replace("<!DOCTYPE html>", "", 1),
        "AF02": good.replace("{{brandName}}", "{{ brandName }}", 1),
        "AF03": good.replace(
            "<!-- Здесь вставляется тело письма -->",
            "<!-- Здесь  вставляется тело письма -->", 1,
        ),
        "AF04": good.replace('role="presentation"', "", 1),
        "AF05": good.replace("&nbsp;&zwnj;" * 3, "", 1),
        "AF06": good.replace("<p style=", '<p class="legacy" style=', 1),
        "AF07": good.replace("border-collapse:separate", "border-radius:8px", 1),
        "AF08": good.replace('<html lang="ru">', '<html lang="en">', 1),
        "AF09": good.replace('bgcolor="#f4f5f0"', "", 1),
        "AF10": good.replace("text-decoration:underline", "color:#24352d", 1),
        "AF11": good.replace("font-family:Arial,sans-serif", "font-family:Arial", 1),
        "AF12": good.replace(
            "font-family:Arial,sans-serif", "font-family:&quot;Arial&quot;,sans-serif", 1,
        ),
    }
    for rule, source in mutations.items():
        fixed, changes = apply(source)
        assert rule in changes, rule
        assert apply(fixed)[1] == [], rule


def test_template_key_cooldown_does_not_change_ocr_model_pick():
    pool = GeminiKeyPool()
    pool.keys = ["secret"]
    pool.key_names = ["GEMINI_API_KEY_1"]
    pool.key_model_blocked[(0, "template-model-a")] = 10**20
    chain = ["template-model-a", "template-model-b"]
    assert pool.pick_model(0, chain=chain) == "template-model-a"
    assert pool.pick_model(0, chain=chain, respect_key_cooldown=True) == "template-model-b"


def test_rules_endpoint_defines_currency_checks_as_warnings():
    from ocr_service.email_template.validator import rules
    rule_map = {item["id"]: item for item in rules()}
    assert rule_map["M03"]["severity"] == "W"
    assert rule_map["T10"]["severity"] == "E"
    assert rule_map["S07"]["severity"] == "W"
    assert rule_map["S13"]["severity"] == "E"
    assert rule_map["V12"]["severity"] == "W"


def test_task_kind_registry_rejects_unknown_kind():
    from ocr_service.task_kinds import get
    try:
        get("not-registered")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown task kind must not silently fall back")


def test_registered_task_kinds_keep_ocr_and_template_stores_separate(monkeypatch):
    from ocr_service import jobs
    from ocr_service.routers import template
    from ocr_service.task_kinds import get

    ocr_kind = get("ocr_sheet")
    template_kind = get("email_template")
    assert ocr_kind.deadline_from == "created"
    assert template_kind.deadline_from == "started"
    assert jobs.JOBS is not template.TEMPLATE_JOBS
    assert jobs._JOB_SEMAPHORE is not template._SEMAPHORE
    monkeypatch.setattr(template, "_SEMAPHORE", None)
    assert template.effective_concurrency() >= 1


def test_batch_extract_contract_covers_codes_warnings_and_truncation():
    from ocr_service.email_template.extract import parse
    doc = '<!DOCTYPE html><html lang="ru"><body>x</body></html>'
    fenced = parse("Концепция\n```html\n" + doc + "\n```\nХвост №14", "STOP")
    assert fenced.ok and fenced.html.startswith("<!DOCTYPE")
    assert "Концепция" in fenced.concept and "№14" in fenced.tail
    two_docs = parse(doc + "\n" + doc, "STOP")
    assert two_docs.ok and "X04" in two_docs.codes
    assert two_docs.html.count("<!DOCTYPE") == 1
    assert "X02" in parse(doc, "MAX_TOKENS").codes
    assert parse("", "SAFETY").codes == ["X03"]


def test_batch_director_api_is_deterministic_and_keeps_recent_history():
    from ocr_service.email_template import director
    director.reset_history()
    director.set_history_size(12)
    first = director.pick_brief(seed=42, record=False)
    assert director.pick_brief(seed=42, record=False) == first
    overridden = director.pick_brief(seed=1, currency_method=7, overrides={"palette": "P3"}, record=False)
    assert overridden.palette_family == director.PALETTES["P3"]
    assert overridden.currency_method == 7
    seen = []
    director.reset_history()
    for seed in range(12):
        brief = director.pick_brief(seed=seed)
        assert brief.palette_family not in [row[0] for row in seen[-5:]]
        assert brief.currency_method not in [row[1] for row in seen[-8:]]
        seen.append((brief.palette_family, brief.currency_method))
    director.reset_history()


def test_batch_prompt_and_preview_helpers_accept_brief_and_safe_values():
    from ocr_service.email_template import director, preview, prompt
    brief = director.pick_brief(seed=9, currency_method=14, record=False)
    assert "№14" in prompt.first_user_message(brief)
    contents = prompt.build_contents(brief, "previous", [("T01", "inline CSS", "fragment")])
    assert [item["role"] for item in contents] == ["user", "model", "user"]
    assert "T01" in contents[-1]["parts"][0]["text"]
    rendered = preview.render("<p>{{name}} {{price}} {{unknown}}</p>", {"name": "<script>"})
    assert "&lt;script&gt;" in rendered and "49.90" in rendered
    assert "{{unknown}}" in rendered


def test_generation_keeps_best_invalid_attempt_without_exposing_model_exception(monkeypatch):
    from ocr_service.email_template import generate

    calls = []

    async def fake_model(contents, system, models, job_id, generation_config, deadline_ts=None):
        calls.append(contents)
        return "No document", {
            "finish_reason": "STOP",
            "prompt_block_reason": "",
            "usage": {"in": 11, "out": 7, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    job = {
        "job_id": "unit-test", "max_attempts": 2, "models": ["test-model"],
        "deadline_s": 420, "brief": {
            "currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
            "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 42,
        },
    }
    asyncio.run(generate.run_template_job("unit-test", job))
    assert job["status"] == "done"
    assert job["template"]["valid"] is False
    assert job["template"]["attempts"][-1]["errors"] == ["X01"]
    assert len(calls) == 2
    assert calls[1][0]["parts"][0]["text"] == calls[0][0]["parts"][0]["text"]
    assert calls[1][-1]["role"] == "user"


def test_generation_warns_when_model_adds_text_after_html(monkeypatch):
    from pathlib import Path
    from ocr_service.email_template import generate

    good_html = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")

    async def fake_model(contents, system, models, job_id, generation_config, deadline_ts=None):
        return good_html + "\nДополнительный текст после документа", {
            "finish_reason": "STOP",
            "prompt_block_reason": "",
            "usage": {"in": 11, "out": 7, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    job = {
        "job_id": "tail-warning", "max_attempts": 1, "models": ["test-model"],
        "deadline_s": 420, "brief": {
            "currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
            "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 42,
        },
    }
    asyncio.run(generate.run_template_job("tail-warning", job))
    warnings = job["template"]["attempts"][0]["warnings"]
    assert "V10" in warnings


def test_gemini_attempt_log_has_thought_token_count_only(monkeypatch):
    from ocr_service.email_template import generate

    events = []

    async def fake_model(contents, system, models, job_id, generation_config, deadline_ts=None):
        return "No document", {
            "finish_reason": "STOP",
            "prompt_block_reason": "",
            "usage": {"in": 11, "out": 7, "thoughts": 23, "cached": 0},
            "thinking_setting": "level:low",
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    monkeypatch.setattr(generate, "_job_log", lambda event, _job_id, **fields: events.append((event, fields)))
    job = {
        "job_id": "thought-log", "max_attempts": 1, "models": ["gemini-3.8-flash"],
        "deadline_s": 420, "brief": {
            "currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
            "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 42,
        },
    }
    asyncio.run(generate.run_template_job("thought-log", job))
    _, fields = next(item for item in events if item[0] == "tpl_gemini_attempt")
    assert fields["thoughts"] == 23
    assert fields["thinking_setting"] == "level:low"
    assert "private reasoning" not in str(fields)


def test_template_routes_are_not_registered_when_flag_is_off(monkeypatch):
    import importlib
    import sys
    from ocr_service import config

    sys.modules.pop("main", None)
    monkeypatch.setattr(config, "TEMPLATE_ENABLED", False)
    app_module = importlib.import_module("main")
    paths = {getattr(route, "path", "") for route in app_module.app.routes}
    assert not any(path.startswith("/template/") for path in paths)
