import re

from ocr_service.email_template import autofix, validator
from ocr_service.email_template.prompt import build_user_message


def _fixes(html):
    return autofix.apply(html)


def test_af04_fixes_wrong_table_attr_values():
    html = '<table role="none" cellpadding="8" cellspacing="0" border="1" width="100%"><tr><td>x</td></tr></table>'
    out, fixes = _fixes(html)
    tag = re.search(r"<table[^>]*>", out).group(0)
    assert 'role="presentation"' in tag and 'cellpadding="0"' in tag and 'border="0"' in tag
    assert "AF04" in fixes
    assert "border=\"1\"" not in tag


def test_af04_idempotent():
    html = '<table role="none" border="1"><tr><td>x</td></tr></table>'
    once, _ = _fixes(html)
    twice, _ = _fixes(once)
    assert once == twice


def test_af02_fixes_typo_variable_and_keeps_unknown():
    out, fixes = _fixes("<p>{{transactionlId}} {{totallyUnknownVar}}</p>")
    assert "{{transactionalId}}" in out
    assert "{{totallyUnknownVar}}" in out
    assert "AF02" in fixes


def test_af27_replaces_chto_proishodit():
    out, _ = _fixes("<p>Вот что происходит сейчас:</p><p>Ниже описано то, что происходит сейчас:</p>")
    assert "происходит" not in out
    assert "Вот как идёт работа:" in out
    assert "описаны наши шаги" in out


def test_af33_wraps_greeting_cell_in_h1():
    html = (
        '<html><body><table><tr><td style="font-family:Verdana,sans-serif;font-size:24px;font-weight:bold;color:#1f4d3a;padding-bottom:12px;">\n'
        'Всё готово, {{name}}\n</td></tr></table><!-- Здесь вставляется тело письма --></body></html>'
    )
    out, did = autofix._fix_missing_h1(html)
    assert did and "<h1" in out and "padding-bottom" not in re.search(r"<h1[^>]*>", out).group(0)
    assert autofix._fix_missing_h1(out)[1] is False


def test_plate_contract_is_gone():
    for method in (3, 4, 12, 16, 17, 18, 19, 1):
        brief = dict(currency_method=method, palette_family="x", fonts="x", header="x", indicator="x", seed=1)
        assert "соседних ячейках" not in build_user_message(brief)
