"""Регрессия: письмо целиком внутри одной ячейки основной таблицы.

Раньше самым внешним tr подвала считалась строка-обёртка, начинающаяся ДО маркера,
область сообщения получалась пустой и корректное письмо получало семь ошибок T17.
"""
from pathlib import Path

import pytest

from ocr_service.email_template.validator import validate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name", ["nested_single_cell.html", "nested_single_cell_2.html"])
def test_single_wrapper_cell_layout_has_no_false_t17(name):
    source = (FIXTURES / name).read_text(encoding="utf-8")
    report = validate(source, 4)
    assert not [item for item in report.errors if item.rule == "T17"]


def test_message_area_still_ends_at_footer_row():
    """Без h2 в блоке сообщения T17 по-прежнему срабатывает (правка не ослабила правило)."""
    source = (FIXTURES / "nested_single_cell.html").read_text(encoding="utf-8")
    broken = source.replace("<h2", "<div").replace("</h2>", "</div>")
    report = validate(broken, 4)
    assert any(item.rule == "T17" and "h2" in item.message for item in report.errors)
