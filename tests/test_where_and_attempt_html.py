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
    html = Path(FIXTURES[0]).read_text(encoding="utf-8").replace("</p>", " Чай остывает быстрее, чем вещи.</p>", 1)
    report = validator.validate(html)
    s01 = [i for i in report.errors if i.rule == "S01"]
    assert s01 and "быстрее" in s01[0].where


def test_document_level_rule_still_gets_nonempty_where():
    report = validator.validate("<html><body><p>x</p></body></html>")
    assert report.errors and all(i.where for i in report.errors + report.warnings)


def test_attempt_html_flag_is_off_by_default():
    from ocr_service import config
    assert config.TEMPLATE_KEEP_ATTEMPT_HTML is False
