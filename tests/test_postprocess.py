"""Постпроцессор, text/plain и правила Outlook (T26, T27, P03, S14)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template import postprocess, validator
from ocr_service.email_template.validator import validate

GOOD = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")


def _rules(report):
    return {issue.rule for issue in report.errors + report.warnings}


# --------------------------------------------------------------- постпроцессор

def test_postprocess_adds_all_four_fixes():
    out, ids = postprocess.apply(GOOD)
    assert ids == ["PP01", "PP02", "PP03", "PP04"]
    assert '<meta name="x-apple-disable-message-reformatting">' in out
    assert '<meta name="color-scheme" content="light">' in out
    assert '<meta name="supported-color-schemes" content="light">' in out
    assert "-webkit-text-size-adjust:100%" in out
    assert out.count("mso-table-lspace:0pt") == GOOD.lower().count("<table")


def test_postprocess_is_idempotent():
    once, _ = postprocess.apply(GOOD)
    twice, ids = postprocess.apply(once)
    assert ids == [] and twice == once


def test_postprocess_keeps_document_valid_and_texts_intact():
    out, _ = postprocess.apply(GOOD)
    before, after = validate(GOOD, 1), validate(out, 1)
    assert after.ok and len(after.errors) <= len(before.errors)
    assert [i.rule for i in after.warnings] == [i.rule for i in before.warnings]
    assert postprocess.plain_text(out) == postprocess.plain_text(GOOD)


def test_postprocess_adds_style_where_missing_and_keeps_existing_props():
    src = '<html><head></head><body><table width="560"><tr><td>x</td></tr></table></body></html>'
    out, ids = postprocess.apply(src)
    assert 'style="-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%"' in out
    assert '<table width="560" style="mso-table-lspace:0pt;mso-table-rspace:0pt">' in out
    src2 = '<body style="margin:0;"><table style="width:100%;">'
    out2, _ = postprocess.apply(src2)
    assert 'style="margin:0;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%"' in out2
    assert 'style="width:100%;mso-table-lspace:0pt;mso-table-rspace:0pt"' in out2


def test_postprocess_without_head_skips_meta_and_never_crashes_on_garbage():
    out, ids = postprocess.apply("<body><table></table></body>")
    assert "PP01" not in ids and "PP02" not in ids
    for junk in ("", "<<<<", "<table", "<body " + "a" * 5000):
        postprocess.apply(junk)


# ------------------------------------------------------------------ text/plain

def test_plain_text_hides_preheader_and_keeps_placeholders_and_support_address():
    src = GOOD.replace(
        "<body", '<body', 1
    )
    text = postprocess.plain_text(src)
    assert "{{name}}" in text and "{{price}}" in text and "{{email}}" in text
    assert "{{supportEmail}}" in text
    assert "<" not in text and "&nbsp;" not in text and "\u200c" not in text


def test_plain_text_list_and_structure():
    text = postprocess.plain_text(GOOD)
    assert "- Мы сверяем состав\n- Проверяем упаковку\n- Готовим передачу" in text
    assert "--------" in text
    assert "\n\n\n" not in text and text.endswith("\n")


def test_plain_text_skips_display_none_block():
    html = '<body><div style="display:none;max-height:0">СКРЫТО</div><p>Видно</p></body>'
    text = postprocess.plain_text(html)
    assert "СКРЫТО" not in text and "Видно" in text


def test_plain_text_mailto_not_duplicated_when_address_is_link_text():
    html = '<body><p><a href="mailto:{{supportEmail}}">{{supportEmail}}</a></p></body>'
    assert postprocess.plain_text(html).count("{{supportEmail}}") == 1


# ---------------------------------------------------------------- новые правила

def test_good_fixture_has_no_new_warnings():
    report = validate(GOOD, 1)
    assert not ({"T26", "T27", "P03", "S14"} & _rules(report))


def test_t26_inline_table_and_dollar_in_nested_table():
    nested = GOOD.replace(
        '<span style="color:#4f6953;font-size:18px;vertical-align:super">$</span>',
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td style="color:#4f6953;font-size:18px">$</td></tr></table>',
        1,
    )
    assert "T26" in _rules(validate(nested, 1))
    inline = GOOD.replace('style="background-color:#f4f5f0;border-collapse:separate"',
                          'style="display:inline-table;background-color:#f4f5f0;border-collapse:separate"', 1)
    assert "T26" in _rules(validate(inline, 1))


def test_t26_not_triggered_by_table_without_dollar():
    other = GOOD.replace("Покупка уже в работе", "Покупка уже в работе", 1)
    assert "T26" not in _rules(validate(other, 1))


def test_t26_and_p03_are_errors_only_in_strict_mode(monkeypatch):
    nested = GOOD.replace(
        '<span style="color:#4f6953;font-size:18px;vertical-align:super">$</span>',
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td style="color:#4f6953;font-size:18px">$</td></tr></table>',
        1,
    )
    warm = GOOD.replace(
        '<td style="padding:6px 10px;color:#24352d;font-size:14px;line-height:1.5">В сборке</td>',
        '<td bgcolor="#c1694f" style="padding:6px 10px;background-color:#c1694f;color:#24352d;font-size:14px;line-height:1.5">В сборке</td>',
        1,
    )
    soft = validate(nested, 1), validate(warm, 1)
    assert "T26" in {i.rule for i in soft[0].warnings} and "P03" in {i.rule for i in soft[1].warnings}
    monkeypatch.setattr(validator, "TEMPLATE_STRICT_OUTLOOK", True)
    hard = validate(nested, 1), validate(warm, 1)
    assert "T26" in {i.rule for i in hard[0].errors} and not hard[0].ok
    assert "P03" in {i.rule for i in hard[1].errors}


def test_p03_ignores_cool_fills_and_fills_outside_indicator():
    cool = GOOD.replace('bgcolor="#f4f5f0"', 'bgcolor="#d9efe6"', 1)
    assert "P03" not in _rules(validate(cool, 1))
    warm_header = GOOD.replace('<body bgcolor="#f4f5f0"', '<body bgcolor="#e07a5f"', 1)
    assert "P03" not in _rules(validate(warm_header, 1))


def test_t27_only_padding_and_margin_on_span():
    padded = GOOD.replace('<span style="color:#24352d;font-size:26px;font-weight:700">',
                          '<span style="margin-left:4px;color:#24352d;font-size:26px;font-weight:700">', 1)
    assert "T27" in {i.rule for i in validate(padded, 1).warnings}
    sized = GOOD.replace('<span style="color:#24352d;font-size:26px;font-weight:700">',
                         '<span style="display:inline-block;width:20px;color:#24352d;font-size:26px;font-weight:700">', 1)
    assert "T27" not in _rules(validate(sized, 1))


def test_s14_flags_invented_facts_but_not_look_alike_words():
    for phrase in ("Наши мастера", "со склада", "курьер уже в пути", "ваш трек-номер"):
        assert "S14" in _rules(validate(GOOD.replace("Мы подготовили свежие детали", phrase, 1), 1)), phrase
    for phrase in ("Всё складывается отлично", "Мастерство важно"):
        text = GOOD.replace("Мы подготовили свежие детали", phrase, 1)
        found = "S14" in _rules(validate(text, 1))
        assert found == phrase.startswith("Мастерство"), phrase  # «мастер*» ловит только корень, «складывается» нет


def test_s14_ignores_message_block_after_marker():
    text = GOOD.replace("Мы сверяем состав", "Передаём на склад", 1)
    assert "S14" not in _rules(validate(text, 1))


# ------------------------------------------------------ интеграция с generate

def _run_generation(monkeypatch, config_value):
    from ocr_service.email_template import generate

    async def fake_model(contents, system, models, job_id, generation_config, deadline_ts=None):
        return "Концепция\n```html\n" + GOOD + "\n```\nСпособ оформления знака: №1", {
            "finish_reason": "STOP", "prompt_block_reason": "",
            "usage": {"in": 1, "out": 1, "thoughts": 0, "cached": 0},
        }, models[0]

    monkeypatch.setattr(generate, "call_model", fake_model)
    monkeypatch.setattr(generate, "TEMPLATE_POSTPROCESS", config_value)
    job = {
        "job_id": "pp", "max_attempts": 1, "models": ["m"], "deadline_s": 420,
        "brief": {"currency_method": 1, "palette_family": "x", "fonts": "Georgia + Verdana",
                  "header": "h", "indicator": "i", "avoid_palettes": [], "seed": 1},
    }
    asyncio.run(generate.run_template_job("pp", job))
    return job


def test_generation_applies_postprocess_and_returns_text(monkeypatch):
    job = _run_generation(monkeypatch, True)
    tpl = job["template"]
    assert job["status"] == "done" and tpl["valid"] is True
    assert tpl["postprocess"] == ["PP01", "PP02", "PP03", "PP04"]
    assert "x-apple-disable-message-reformatting" in tpl["html"]
    assert "{{name}}" in tpl["text"] and "<" not in tpl["text"]
    assert tpl["sha1"] and tpl["bytes"] == len(tpl["html"].encode("utf-8"))


def test_generation_postprocess_can_be_disabled(monkeypatch):
    tpl = _run_generation(monkeypatch, False)["template"]
    assert tpl["postprocess"] == [] and "x-apple-disable-message-reformatting" not in tpl["html"]
    assert tpl["text"]
