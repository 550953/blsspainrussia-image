"""Раунд 3, пакет D (часть): where у каждого замечания, error_details без пробелов, сохранение HTML попыток."""
import glob
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template import validator

FIXTURES = sorted(glob.glob(str(Path(__file__).parent / "fixtures" / "real_batch_lite_*.html")))


def test_every_issue_on_fixtures_has_where_within_limit():
    total = 0
    for path in FIXTURES:
        report = validator.validate(Path(path).read_text(encoding="utf-8"))
        for issue in report.errors + report.warnings:
            total += 1
            assert issue.where and len(issue.where) <= 120, (path, issue.rule)
    assert total > 0


def test_a06_has_where_fragment():
    report = validator.validate(Path(FIXTURES[1]).read_text(encoding="utf-8"))
    a06 = [i for i in report.errors if i.rule == "A06"]
    assert a06 and "<td" in a06[0].where


def test_v02_where_points_at_stray_placeholder():
    html = Path(FIXTURES[0]).read_text(encoding="utf-8").replace("{{name}}", "{{ name }}", 1)
    report = validator.validate(html)
    v02 = [i for i in report.errors if i.rule == "V02"]
    assert v02 and "{{ name }}" in v02[0].where


def test_s01_where_shows_banned_phrase():
    html = Path(FIXTURES[0]).read_text(encoding="utf-8").replace("</p>", " Срочно проверьте, пока чай остывает.</p>", 1)
    report = validator.validate(html)
    s01 = [i for i in report.errors if i.rule == "S01"]
    assert s01 and "срочно" in s01[0].where


def test_document_level_rule_still_gets_nonempty_where():
    report = validator.validate("<html><body><p>x</p></body></html>")
    assert report.errors and all(i.where for i in report.errors + report.warnings)


def test_attempt_html_flag_is_off_by_default():
    from ocr_service import config
    assert config.TEMPLATE_KEEP_ATTEMPT_HTML is False


def test_autofix_normalizes_placeholder_case_so_v02_is_not_raised():
    from ocr_service.email_template import autofix
    base = Path(FIXTURES[0]).read_text(encoding="utf-8")
    broken = base.replace("{{transactionalId}}", "{{transactionid}}", 1).replace("{{name}}", "{{Name }}", 1)
    assert any(i.rule == "V02" for i in validator.validate(broken).errors)
    fixed, ids = autofix.apply(broken)
    assert "AF02" in ids
    assert not any(i.rule == "V02" for i in validator.validate(fixed).errors)
    again, ids2 = autofix.apply(fixed)
    assert again == fixed and "AF02" not in ids2


def test_product_name_in_preheader_does_not_break_block_order():
    html = Path(FIXTURES[0]).read_text(encoding="utf-8")
    html = re.sub(r'(mso-hide:all;[^>]*>)', r'\1Заказ {{productName}} принят. ', html, count=1)
    assert "{{productName}} принят" in html
    assert not any(i.rule == "L01" for i in validator.validate(html).errors)


def test_l01_still_fires_when_details_lose_product_and_has_where():
    html = Path(FIXTURES[0]).read_text(encoding="utf-8")
    head, sep, body = html.partition("<body")
    bad = head + sep + body.replace("{{productName}}", "товар")
    l01 = [i for i in validator.validate(bad).errors if i.rule == "L01"]
    assert l01 and l01[0].where


def test_placeholder_near_misses_are_canonicalised():
    from ocr_service.email_template import autofix
    html = "<p>{{ transaction_id }} {{TransactionID}} {{product_name}} {{unknownVar}}</p>"
    fixed, ids = autofix.apply(html)
    assert "{{transactionalId}} {{transactionalId}} {{productName}} {{unknownVar}}" in fixed
