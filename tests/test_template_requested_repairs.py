import asyncio
import re
from pathlib import Path

from ocr_service.email_template.autofix import apply
from ocr_service.email_template.prompt import build_repair_message
from ocr_service.email_template.validator import MARKER, validate


GOOD = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")


def test_t18_diagnostics_include_the_relevant_html_fragment():
    missing = validate(GOOD.replace(MARKER, "", 1))
    issue = next(item for item in missing.errors if item.rule == "T18")
    assert "<h2" in issue.where
    assert len(issue.where) <= 200

    duplicate = validate(GOOD.replace(MARKER, MARKER + MARKER, 1))
    issue = next(item for item in duplicate.errors if item.rule == "T18")
    assert MARKER in issue.where

    misplaced = validate(GOOD.replace(
        MARKER + "<h2", MARKER + "<hr style=\"margin:8px 0\"><h2", 1,
    ))
    issue = next(item for item in misplaced.errors if item.rule == "T18")
    assert MARKER in issue.where and "<hr" in issue.where


def test_v07_diagnostics_point_to_duplicate_anchor_and_email_inside_anchor():
    support = re.search(r"<a\b[^>]*>[\s\S]*?</a\s*>", GOOD, re.I).group(0)
    duplicate = validate(GOOD.replace(support, support + support, 1))
    issue = next(item for item in duplicate.errors if item.rule == "V07")
    assert "<a" in issue.where
    assert len(issue.where) <= 200

    email_link = GOOD.replace(
        "Письмо отправлено на {{email}}",
        'Письмо отправлено на <a href="mailto:recipient@example.com">{{email}}</a>',
    )
    report = validate(email_link)
    issue = next(item for item in report.errors if item.rule == "V07" and "{{email}}" in item.message)
    assert "{{email}}" in issue.where and "<a" in issue.where

    address_label = validate(GOOD.replace(">Напишите нам</a>", ">support@example.test</a>", 1))
    issue = next(item for item in address_label.errors if item.rule == "V07")
    assert "support@example.test" in issue.where


def test_t18_autofix_inserts_exactly_one_marker_before_message_heading_and_is_idempotent():
    broken = GOOD.replace(MARKER, "", 1)
    fixed, fixes = apply(broken)
    assert "AF13" in fixes
    assert fixed.count(MARKER) == 1
    assert re.search(re.escape(MARKER) + r"<h2\b", fixed, re.I)
    assert apply(fixed)[1] == []

    duplicated, fixes = apply(GOOD.replace(MARKER, MARKER + MARKER, 1))
    assert "AF13" in fixes
    assert duplicated.count(MARKER) == 1
    assert apply(duplicated)[1] == []


def test_v07_autofix_keeps_first_support_link_flattens_others_and_moves_email_out():
    broken = GOOD.replace(
        ">Напишите нам</a>",
        '>Напишите нам {{email}}</a> и <a href="https://example.com">страница {{email}}</a>',
        1,
    )
    fixed, fixes = apply(broken)
    assert "AF14" in fixes
    assert len(re.findall(r"<a\b", fixed, re.I)) == 1
    support = re.search(r"<a\b[^>]*>[\s\S]*?</a\s*>", fixed, re.I).group(0)
    assert 'href="mailto:{{supportEmail}}"' in support
    assert "{{email}}" not in support
    assert len(re.findall(r"\{\{email\}\}", fixed)) == 1
    support_end = fixed.index("</a>", fixed.index('href="mailto:{{supportEmail}}"')) + len("</a>")
    assert fixed.index("{{email}}") > support_end
    assert re.search(r"<p\b[^>]*>[^<]*\{\{email\}\}[^<]*</p>", fixed, re.I)
    report = validate(fixed)
    assert not any(item.rule == "V07" for item in report.errors)
    assert apply(fixed)[1] == []

    duplicate_placeholder = GOOD.replace(
        ">Напишите нам</a>",
        '>Напишите нам</a> <a href="mailto:{{supportEmail}}">{{supportEmail}}</a>',
        1,
    )
    fixed, fixes = apply(duplicate_placeholder)
    assert "AF14" in fixes
    assert fixed.count("{{supportEmail}}") == 1
    assert not any(item.rule == "V07" for item in validate(fixed).errors)

    link_token = GOOD.replace(">Напишите нам</a>", ">{{supportEmail}}</a>", 1)
    fixed, fixes = apply(link_token)
    assert "AF14" in fixes
    assert ">Напишите нам</a>" in fixed
    assert not any(item.rule == "V07" for item in validate(fixed).errors)


def test_v07_wraps_email_in_separate_span_when_it_shares_the_link_paragraph():
    source = re.sub(r"<p\b[^>]*>Письмо отправлено на \{\{email\}\}</p>", "", GOOD, count=1, flags=re.I)
    source = source.replace(">Напишите нам</a>", ">Напишите нам</a> {{email}}", 1)
    assert any(item.rule == "V07" for item in validate(source).errors)
    fixed, fixes = apply(source)
    assert "AF14" in fixes
    assert re.search(r"</a\s*><span>\{\{email\}\}</span>", fixed, re.I)
    assert not any(item.rule == "V07" for item in validate(fixed).errors)


def test_brand_header_warns_for_duplicate_occurrences_and_fixed_badge():
    duplicate = GOOD.replace("{{brandName}}</p>", "{{brandName}} {{brandName}}</p>", 1)
    issue = next(item for item in validate(duplicate).warnings if item.rule == "V12")
    assert issue.severity == "W" and "{{brandName}}" in issue.where

    fixed_badge = GOOD.replace(
        '<p style="margin:0 0 20px;font-size:18px;line-height:1.5;color:#24352d">{{brandName}}</p>',
        '<p style="margin:0 0 20px;font-size:18px;line-height:1.5;color:#24352d"><span style="width:40px;height:40px;border-radius:50%;line-height:18px">{{brandName}}</span></p>',
        1,
    )
    issue = next(item for item in validate(fixed_badge).warnings if item.rule == "V12")
    assert "{{brandName}}" in issue.where


def test_timing_promises_are_warnings_with_the_matched_phrase():
    for phrase in ("не более пары дней", "в течение трёх дней", "за 4 часа", "через 2 дня"):
        source = GOOD.replace("Когда этап изменится,", phrase + ", когда этап изменится,", 1)
        report = validate(source)
        issue = next(item for item in report.warnings if item.rule == "S07")
        assert phrase.replace("ё", "е") in issue.where
        assert not any(item.rule == "S07" for item in report.errors)


def test_repair_message_contains_concrete_fix_fragment_and_requires_full_html():
    message = build_repair_message([
        {"rule": "T18", "message": "Маркер не найден", "where": "<h2>Сейчас</h2>"},
        {"rule": "V07", "message": "Неправильная ссылка", "where": "<a>{{email}}</a>"},
    ])
    assert "ровно один <!-- Здесь вставляется тело письма -->" in message
    assert "href=\"mailto:{{supportEmail}}\"" in message
    assert "<a>{{email}}</a>" in message
    assert "весь HTML целиком" in message and "<!DOCTYPE html>" in message

    compact = build_repair_message([
        {"rule": "T18", "message": "Маркер не найден", "where": "<h2>Сейчас</h2>"},
    ], compact=True)
    assert "Перезапусти исправление" in compact
    assert "<!DOCTYPE html>" in compact


def _job():
    return {
        "job_id": "persistent-rules", "max_attempts": 3,
        "models": ["unit-model-a", "unit-model-b"], "deadline_s": 420,
        "brief": {
            "currency_method": 1, "palette_family": "green", "fonts": "Georgia + Verdana",
            "header": "header", "indicator": "indicator", "avoid_palettes": [], "seed": 1,
        },
    }


def test_repeated_rules_trigger_compact_restart_on_final_attempt(monkeypatch):
    from ocr_service.email_template import generate

    monkeypatch.setattr(generate, "TEMPLATE_REPAIR_MODEL", "")
    calls, events = [], []

    async def fake_model(contents, _system, models, _job_id, _config, deadline_ts=None):
        calls.append((contents, list(models)))
        return "No document", {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    monkeypatch.setattr(generate, "_job_log", lambda event, _job_id, **fields: events.append((event, fields)))
    job = _job()
    asyncio.run(generate.run_template_job(job["job_id"], job))

    assert len(calls) == 3
    assert calls[-1][1] == ["unit-model-a", "unit-model-b"]
    assert "Перезапусти исправление" in calls[-1][0][0]["parts"][0]["text"]
    strategies = [fields for event, fields in events if event == "tpl_repair_strategy"]
    assert strategies[-1]["strategy"] == "compact_restart"
    assert strategies[-1]["persistent_rule_ids"] == ["X01"]


def test_configured_repair_model_is_used_for_persistent_rules(monkeypatch):
    from ocr_service.email_template import generate

    monkeypatch.setattr(generate, "TEMPLATE_REPAIR_MODEL", "next")
    calls, events = [], []

    async def fake_model(contents, _system, models, _job_id, _config, deadline_ts=None):
        calls.append(list(models))
        return "No document", {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    monkeypatch.setattr(generate, "_job_log", lambda event, _job_id, **fields: events.append((event, fields)))
    job = _job()
    asyncio.run(generate.run_template_job(job["job_id"], job))

    assert calls[-1] == ["unit-model-b"]
    strategies = [fields for event, fields in events if event == "tpl_repair_strategy"]
    assert strategies[-1]["strategy"] == "alternate_model"
    assert strategies[-1]["model"] == "unit-model-b"


def test_no_progress_skips_to_the_configured_last_attempt_for_final_strategy(monkeypatch):
    from ocr_service.email_template import generate

    monkeypatch.setattr(generate, "TEMPLATE_REPAIR_MODEL", "")
    calls = []

    async def fake_model(contents, _system, models, _job_id, _config, deadline_ts=None):
        calls.append(list(models))
        return "No document", {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    job = _job()
    job["max_attempts"] = 5
    asyncio.run(generate.run_template_job(job["job_id"], job))

    assert len(calls) == 4
    assert [item["n"] for item in job["template"]["attempts"]] == [1, 2, 3, 5]
