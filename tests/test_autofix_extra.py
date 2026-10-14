"""Механические исправления AF19–AF29: баланс тегов, T16/T17, T09, T10, круги, отметка этапа, цвета."""
import re
from pathlib import Path

import pytest

from ocr_service.email_template import autofix, director, postprocess
from ocr_service.email_template.validator import validate

GOOD = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")


def rules(source, method=1):
    fixed, _ids = autofix.apply(source)
    fixed, _pp = postprocess.apply(fixed)
    report = validate(fixed, method)
    return {issue.rule for issue in report.errors}, fixed


def drop(source, pattern, nth=0):
    found = list(re.finditer(pattern, source, re.I))
    match = found[nth % len(found)]
    return source[:match.start()] + source[match.end():]


def test_good_fixture_is_untouched_by_balancer():
    fixed, ids = autofix.apply(GOOD)
    assert "AF19" not in ids
    assert validate(fixed, 1).ok


@pytest.mark.parametrize("tag", ["</td>", "</tr>", "</table>", "</p>", "</li>"])
@pytest.mark.parametrize("nth", [0, 1, 2])
def test_missing_closing_tags_are_restored(tag, nth):
    broken = drop(GOOD, re.escape(tag), nth)
    assert "T21" in {i.rule for i in validate(broken, 1).errors}
    errors, _fixed = rules(broken)
    assert "T21" not in errors


def test_stray_closing_tag_is_removed():
    broken = GOOD.replace("</tr>", "</td></tr>", 1)
    errors, _fixed = rules(broken)
    assert "T21" not in errors


def test_message_tags_without_inline_style_get_styles():
    start = GOOD.index("Здесь вставляется")
    end = GOOD.rindex("mailto")
    stripped = GOOD[:start] + re.sub(r'(<(?:p|li|ul|h2|hr)\b[^>]*?)\sstyle="[^"]*"', r"\1", GOOD[start:end]) + GOOD[end:]
    assert "T16" in {i.rule for i in validate(stripped, 1).errors}
    errors, _fixed = rules(stripped)
    assert "T16" not in errors and "T17" not in errors


def test_missing_hr_strong_italic_and_extra_list_item_are_repaired():
    broken = re.sub(r"<hr[^>]*>", "", GOOD, count=1)
    broken = re.sub(r"</?strong[^>]*>", "", broken)
    broken = broken.replace("font-style:italic", "").replace("<em", "<span").replace("</em>", "</span>")
    item = re.search(r"<li\b[^>]*>[\s\S]*?</li>", broken).group(0)
    broken = broken.replace(item, item + item, 1)
    errors, _fixed = rules(broken)
    assert not errors


def test_h3_message_heading_is_promoted():
    start = GOOD.index("Здесь вставляется")
    broken = GOOD[:start] + GOOD[start:].replace("<h2", "<h3", 1).replace("</h2>", "</h3>", 1)
    errors, _fixed = rules(broken)
    assert not errors


def test_ordered_list_becomes_unordered():
    broken = GOOD.replace("<ul", "<ol").replace("</ul>", "</ol>")
    errors, _fixed = rules(broken)
    assert "T24" not in errors


def test_missing_backgrounds_use_neighbour_or_white():
    broken = re.sub(r'\sbgcolor="[^"]*"', "", re.sub(r"background-color:[^;\"]*;?", "", GOOD, count=2), count=2)
    errors, fixed = rules(broken)
    assert "T09" not in errors


def test_preheader_missing_styles_and_too_long_text_are_fixed():
    broken = re.sub(r"(<(?:div|span)[^>]*display:none)[^\"]*\"", r'\1"', GOOD, count=1)
    errors, _fixed = rules(broken)
    assert "T10" not in errors
    long_text = "Подробный и очень длинный текст прехедера, который не помещается в сто десять символов и поэтому должен быть аккуратно сокращён"
    pre = re.search(r"(<body[^>]*>\s*<(?:div|span)[^>]*>)([\s\S]*?)(</(?:div|span)>)", GOOD)
    broken = GOOD[:pre.start(2)] + long_text + "&nbsp;&zwnj;" * 6 + GOOD[pre.end(2):]
    errors, _fixed = rules(broken)
    assert "T10" not in errors


def test_round_shapes_are_made_square_but_bars_are_not_touched():
    oval = ('<td width="28" style="width:28px;height:20px;line-height:20px;border-radius:14px;'
            'background-color:#2e7d4f;color:#ffffff;text-align:center">1</td>')
    bar = '<td style="background-color:#f2b705;width:32px;height:6px;border-radius:3px;font-size:1px;line-height:1px">&nbsp;</td>'
    probe = GOOD.replace(
        "<!-- Здесь вставляется тело письма -->",
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>' + oval + bar + "</tr></table>"
        "<!-- Здесь вставляется тело письма -->", 1,
    )
    errors, fixed = rules(probe)
    assert "A05" not in errors
    assert "width:32px;height:6px" in fixed


def test_done_mark_is_added_before_first_stage_label():
    stage = re.search(r">\s*(✓\s*Принят)\s*<", GOOD)
    broken = GOOD.replace(stage.group(1), "Заказ подтверждён", 1) if stage else GOOD
    errors, fixed = rules(broken)
    assert "A03" not in errors


def test_red_orange_is_shifted_out_of_palette():
    broken = GOOD.replace("#f4f5f0", "#ff4b20", 1)
    errors, fixed = rules(broken)
    assert "P01" not in errors


def test_stop_phrase_is_replaced_in_text_only():
    broken = GOOD.replace("Мы сверяем состав", "Мы прямо сейчас сверяем состав", 1)
    errors, _fixed = rules(broken)
    assert "S01" not in errors


def test_palettes_are_concrete_and_not_blue_white_or_red():
    from ocr_service.email_template.contrast import is_red_orange

    for key, text in director.PALETTES.items():
        colors = re.findall(r"#[0-9a-f]{6}", text)
        assert len(colors) >= 5, key
        assert not any(is_red_orange(c) for c in colors), key
    assert sum("Georgia" in item for item in director.FONTS) <= 2
