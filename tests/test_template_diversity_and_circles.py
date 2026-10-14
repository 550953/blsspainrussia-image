"""Пулы текстов director, серверные тексты в запросе, автофикс кругов (AF30), прехедер и шум T13/T14/T17."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template import autofix, director, prompt
from ocr_service.email_template.validator import validate

GOOD = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")
CIRCLE = (
    '<td width="28" height="28" style="width:28px;height:28px;line-height:28px;border-radius:14px;'
    'background-color:#d9efe6;text-align:center;color:#24352d;font-size:14px">1</td>'
)
LABEL_ROW = '<tr><td style="padding:6px 10px;color:#24352d;font-size:14px;line-height:1.5">✓ Принят</td>'
STRETCHED = GOOD.replace(LABEL_ROW, "<tr>" + CIRCLE + "</tr>" + LABEL_ROW, 1)


def _rules(report):
    return {issue.rule for issue in report.errors + report.warnings}


def test_text_pools_are_clean_and_keep_placeholders():
    banned = "|".join(re.escape(item.lower()) for item in prompt.OVERUSED_PHRASES)
    for pool, token in ((director.GREETINGS, "{{name}}"), (director.SIGNOFFS, "{{brandName}}"), (director.HEADINGS, None)):
        assert len(pool) >= 8 and len(set(pool)) == len(pool)
        for text in pool:
            assert not re.search(banned, text.lower()), text
            assert "!" not in text and "ваш заказ" not in text.lower() and "статус заказа" not in text.lower(), text
            assert token is None or token in text, text


def test_pool_texts_pass_validator_rules_when_placed_in_good_fixture():
    for greeting in director.GREETINGS:
        html = GOOD.replace("Добрый день, {{name}}!", greeting, 1)
        assert not ({"S01", "S06", "S08", "S10", "V01", "V06"} & {i.rule for i in validate(html, 1).errors + validate(html, 1).warnings}), greeting
    for heading in director.HEADINGS:
        html = GOOD.replace("Покупка уже в работе", heading, 1)
        assert not ({"S01", "S06", "S08", "S10"} & _rules(validate(html, 1))), heading


def test_brief_carries_texts_and_history_avoids_recent_repeats():
    director.reset_history()
    director.set_history_size(12)
    first = director.pick_brief(seed=11, currency_method=4, record=False)
    assert director.pick_brief(seed=11, currency_method=4, record=False) == first
    assert first.greeting in director.GREETINGS and first.signoff in director.SIGNOFFS and first.voice in director.VOICES
    seen = [director.pick_brief(seed=seed).heading for seed in range(5)]
    assert len(set(seen)) == 5
    director.reset_history()


def test_user_message_carries_server_texts_verbatim_and_legacy_brief_still_works():
    brief = director.pick_brief(seed=5, currency_method=4, record=False).public()
    message = prompt.build_user_message(brief)
    assert "ровно этот текст: " + brief["greeting"] in message and "{{name}}" in message or "{{brandName}}" in message
    assert "{name}" not in message.replace("{{name}}", "")
    for phrase in prompt.OVERUSED_PHRASES:
        assert phrase in message
    legacy = {key: brief[key] for key in ("currency_method", "palette_family", "fonts", "header", "indicator", "seed")}
    assert "ТЕКСТЫ ЭТОГО ПИСЬМА" not in prompt.build_user_message(legacy)
    assert "{{productName}}" in prompt.build_user_message(legacy)


def test_a06_detects_and_autofix_wraps_circle_in_fixed_width_table():
    assert "A06" in _rules(validate(STRETCHED, 1))
    fixed, ids = autofix.apply(STRETCHED)
    assert "AF30" in ids
    assert "A06" not in _rules(validate(fixed, 1)) and validate(fixed, 1).ok
    wrapped = re.search(r'<table[^>]*width="28"[^>]*><tr><td width="28" height="28"', fixed)
    assert wrapped
    again, ids_again = autofix.apply(fixed)
    assert again == fixed and "AF30" not in ids_again


def test_circle_next_to_its_label_is_not_flagged():
    side_by_side = GOOD.replace(
        LABEL_ROW, "<tr>" + CIRCLE + '<td style="padding:6px 10px;color:#24352d;font-size:14px;line-height:1.5">✓ Принят</td>', 1,
    )
    assert "A06" not in _rules(validate(side_by_side, 1))


def test_short_preheader_is_extended_without_new_facts():
    short = GOOD.replace("Небольшое обновление о вашем заказе номер {{transactionalId}} — детали уже готовы к просмотру", "Заказ {{transactionalId}} принят")
    assert "T10" in {i.rule for i in validate(short, 1).errors}
    fixed, ids = autofix.apply(short)
    assert "AF24" in ids and "T10" not in _rules(validate(fixed, 1))
    again, _ = autofix.apply(fixed)
    assert again == fixed


def test_t10_messages_name_the_failed_subcheck():
    no_style = GOOD.replace("mso-hide:all", "", 1)
    assert any("mso-hide:all" in i.message for i in validate(no_style, 1).errors if i.rule == "T10")
    few_pairs = GOOD.replace("&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>", "&nbsp;&zwnj;</div>", 1)
    assert any("сейчас 1" in i.message for i in validate(few_pairs, 1).errors if i.rule == "T10")


def test_t13_t14_ignore_empty_spacers_and_round_cells_and_t17_ignores_italic_signature():
    spacer = GOOD.replace("</table>\n<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" style=\"margin:20px 0",
                          '</table><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td height="8" style="font-size:8px;line-height:8px">&nbsp;</td></tr></table>'
                          '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0', 1)
    assert not ({"T13", "T14"} & {i.rule for i in validate(spacer, 1).warnings})
    signature = GOOD.replace('<em style="color:#24352d;font-style:italic">Команда {{brandName}}</em>', "Команда {{brandName}}", 1).replace(
        '<p style="margin:0 0 20px;color:#24352d;font-size:16px;line-height:1.5">Команда', '<p style="margin:0 0 20px;color:#24352d;font-size:16px;line-height:1.5;font-style:italic">Команда', 1)
    assert "T17" not in {i.rule for i in validate(signature, 1).warnings}


def test_t26_not_triggered_by_circle_wrapper_table_inside_td():
    fixed, _ = autofix.apply(STRETCHED)
    assert "T26" not in _rules(validate(fixed, 1))


def test_text_metrics_autofix_clamps_line_height_and_small_message_text_only():
    skewed = GOOD.replace(
        '<p style="margin:0 0 12px;color:#24352d;font-size:16px;line-height:1.5">Когда этап изменится',
        '<p style="margin:0 0 12px;color:#24352d;font-size:13px;line-height:1.1">Когда этап изменится', 1,
    )
    before = _rules(validate(skewed, 1))
    assert {"T13", "T14"} <= before
    fixed, ids = autofix.apply(skewed)
    assert "AF31" in ids and not ({"T13", "T14"} & _rules(validate(fixed, 1)))
    # подвал (13px) и круги не меняются
    assert "font-size:13px;line-height:1.5\">Письмо отправлено" in fixed
    again, _ = autofix.apply(fixed)
    assert again == fixed
