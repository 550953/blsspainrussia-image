"""Раунд 5: V08 при знаке-сущности (&#36;), автозамена выдуманных оборотов (AF34, AF35)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template import autofix, prompt
from ocr_service.email_template.validator import validate

GOOD = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")
METHOD = 4


def _rules(report):
    return {i.rule for i in report.errors + report.warnings}


def _swap(text):
    return GOOD.replace("Мы сверяем состав", text, 1)


def _text(html):
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_dollar_entity_does_not_trigger_v08():
    assert "$" in GOOD
    for entity in ("&#36;", "&#x24;", "&dollar;"):
        html = GOOD.replace("$", entity, 1)
        assert "V08" not in {i.rule for i in validate(html, METHOD).errors}, entity


def test_af34_normalizes_dollar_entity_and_is_idempotent():
    html = GOOD.replace("$", "&#36;&nbsp;", 1)
    fixed, ids = autofix.apply(html)
    assert "AF34" in ids and "&#36;" not in fixed
    again, ids2 = autofix.apply(fixed)
    assert again == fixed and "AF34" not in ids2


def test_af34_leaves_attributes_alone():
    html = GOOD.replace("<body", '<body data-x="&#36;"', 1)
    fixed, _ = autofix.apply(html)
    assert 'data-x="&#36;"' in fixed


def test_af35_drops_besherno_and_capitalizes():
    html = _swap("Бережно проверяем данные. Мы бережно собираем всё воедино.")
    fixed, ids = autofix.apply(html)
    assert "AF35" in ids
    t = _text(fixed)
    assert "ережно" not in t and "Проверяем данные." in t and "Мы собираем всё воедино." in t


def test_af35_besherno_otnosimsya():
    fixed, _ = autofix.apply(_swap("Мы бережно относимся к каждой детали."))
    assert "внимательно относимся к каждой детали" in _text(fixed)


def test_af35_sklad_service_workshop_control():
    html = _swap("Система передала данные на склад. Заказ передан службе доставки. "
                 "Он в работе мастерской. Всё под контролем специалистов.")
    fixed, ids = autofix.apply(html)
    t = _text(fixed)
    assert "AF35" in ids
    for stem in ("склад", "служб", "мастерск", "специалист", "под контролем"):
        assert stem not in t.lower(), stem
    assert "в работу" in t and "доставке" in t and "в работе у нашей команды" in t


def test_af35_ignores_tags_and_is_idempotent():
    html = _swap("Мы бережно проверяем данные.").replace("<body", '<body title="бережно"', 1)
    fixed, _ = autofix.apply(html)
    assert 'title="бережно"' in fixed
    again, ids = autofix.apply(fixed)
    assert again == fixed and "AF35" not in ids


def test_af35_result_has_no_s14_and_no_new_errors():
    html = _swap("Мы бережно проверяем данные на складе, передан службе доставки.")
    before = {i.rule for i in validate(html, METHOD).errors}
    fixed, _ = autofix.apply(html)
    report = validate(fixed, METHOD)
    assert "S14" not in _rules(report)
    assert {i.rule for i in report.errors} <= before


def test_v08_repair_instruction_mentions_plain_dollar():
    assert "&#36;" in prompt._REPAIR_INSTRUCTIONS["V08"]
