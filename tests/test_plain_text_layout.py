"""Пакет A (раунд 3): text/plain без одиночных символов, «Сумма: $ {{price}}» одной строкой, индикатор одной строкой."""
import glob
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template import autofix, postprocess

FIXTURES = sorted(glob.glob(str(Path(__file__).parent / "fixtures" / "real_batch_lite_*.html")))


def _text(path):
    html, _ = autofix.apply(Path(path).read_text(encoding="utf-8"))
    return postprocess.plain_text(html)


def test_fixtures_present():
    assert len(FIXTURES) == 10


def test_no_single_character_lines_and_no_triple_blank_lines():
    for path in FIXTURES:
        text = _text(path)
        assert text, path
        assert not [line for line in text.split("\n") if len(line.strip()) == 1], path
        assert "\n\n\n" not in text, path


def test_sum_label_and_value_share_one_line():
    for path in FIXTURES:
        lines = _text(path).split("\n")
        sums = [line for line in lines if line.startswith("Сумма:")]
        assert len(sums) == 1 and "{{price}}" in sums[0], path


def test_status_indicator_is_one_line_with_current_stage_word():
    for path in FIXTURES:
        status = [line for line in _text(path).split("\n") if line.startswith("Статус:")]
        assert len(status) == 1, path
        assert re.fullmatch(r"Статус: ✓ [^→]+ → [^→]+ \(сейчас\) → [^→]+", status[0]), status[0]


def test_variables_and_mailto_survive():
    for path in FIXTURES:
        text = _text(path)
        for token in ("{{brandName}}", "{{name}}", "{{supportEmail}}", "{{email}}", "{{transactionalId}}"):
            assert token in text, (path, token)


def test_lone_symbol_lines_are_dropped_and_check_mark_moves_to_label():
    html = "<body><p>Сумма:</p><p>$</p><p>{{price}}</p><p>✓</p><p>Принят</p><p>•</p><p>В сборке</p><p>Дальше</p></body>"
    text = postprocess.plain_text(html)
    assert "Сумма: $ {{price}}" in text
    assert "Статус: ✓ Принят → В сборке (сейчас) → Дальше" in text
    assert "•" not in text


def test_broken_head_does_not_hide_the_body():
    html = "<html><head><title>T</title></><body><p>Видно {{name}}</p></body></html>"
    assert "Видно {{name}}" in postprocess.plain_text(html)


def test_plain_text_is_idempotent_on_fixtures():
    for path in FIXTURES:
        html, _ = autofix.apply(Path(path).read_text(encoding="utf-8"))
        assert postprocess.plain_text(html) == postprocess.plain_text(html)
