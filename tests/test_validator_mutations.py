from pathlib import Path
import time

import pytest

from ocr_service.email_template.validator import BANNED, RULES, validate


GOOD = (Path(__file__).parent / "fixtures" / "good.html").read_text(encoding="utf-8")


def _swap_first_tokens(source, first, second):
    sentinel = "__RULE_MUTATION_SENTINEL__"
    return source.replace(first, sentinel, 1).replace(second, first, 1).replace(sentinel, second, 1)


ADDITIONAL_MUTATIONS = {
    "V05": lambda s: _swap_first_tokens(s, "{{brandName}}", "{{name}}"),
    "V06": lambda s: s.replace(
        "Добрый день, {{name}}!", "Добрый день!", 1
    ).replace(
        "Номер: {{transactionalId}}", "Номер: {{transactionalId}}, {{name}}", 1
    ),
    "V07": lambda s: s.replace("{{email}}", "recipient@example.com", 1),
    "V12": lambda s: s.replace(
        '<p style="margin:0 0 20px;font-size:18px;line-height:1.5;color:#24352d">{{brandName}}</p>',
        '<p style="margin:0 0 20px;font-size:18px;line-height:1.5;color:#24352d"><span style="display:inline-block;width:36px;height:36px;border-radius:50%;line-height:24px">{{brandName}}</span></p>',
        1,
    ),
    "T01": lambda s: s.replace("</head>", "<style>p{color:red}</style></head>", 1),
    "T02": lambda s: s.replace("<body ", '<body class="mail" ', 1),
    "T03": lambda s: s.replace("</body>", "<script>alert(1)</script></body>", 1),
    "T05": lambda s: s.replace("</body>", '<img src="https://example.test/pixel.png"></body>', 1),
    "T06": lambda s: s.replace("<!DOCTYPE html>", "", 1),
    "T07": lambda s: s.replace('width="560"', 'width="600"', 1),
    "T10": lambda s: s.replace("mso-hide:all", "mso-hide:none", 1),
    "T13": lambda s: s.replace(
        'font-size:16px;line-height:1.5;color:#24352d">Мы подготовили',
        'font-size:13px;line-height:1.5;color:#24352d">Мы подготовили', 1,
    ),
    "T14": lambda s: s.replace(
        'font-size:16px;line-height:1.5;color:#24352d">Мы подготовили',
        'font-size:16px;line-height:1.1;color:#24352d">Мы подготовили', 1,
    ),
    "T15": lambda s: s.replace("font-family:Arial,sans-serif", "font-family:Comic Sans MS", 1),
    "T18": lambda s: s.replace("<!-- Здесь вставляется тело письма -->", "", 1),
    "T19": lambda s: s.replace("</head>", "<!-- лишний комментарий --></head>", 1),
    "T21": lambda s: s.replace("</li>", "", 1),
    "T22": lambda s: s.replace("text-decoration:underline", "text-decoration:none", 1),
    "T23": lambda s: s + (" " * 98001),
    "T24": lambda s: s.replace("</body>", "<ol><li>Этап</li></ol></body>", 1),
    "T25": lambda s: s.replace("</body>", '<p style="font-size:16">Текст</p></body>', 1),
    "S01": lambda s: s.replace("Мы подготовили свежие детали", "Мы подготовили urgent детали", 1),
    "S02": lambda s: s.replace("Мы подготовили свежие детали", "Мы подготовили свежие детали!!", 1),
    "S13": lambda s: s.replace(
        'font-size:24px;line-height:1.5;color:#24352d">Добрый день',
        'font-size:24px;line-height:1.5;text-transform:uppercase;color:#24352d">Добрый день',
        1,
    ),
    "S03": lambda s: s.replace("Мы подготовили свежие детали", "Мы подготовили свежие детали ⚡", 1),
    "S05": lambda s: s.replace("</body>", '<div role="button">Открыть</div></body>', 1),
    "S06": lambda s: s.replace("Мы подготовили свежие детали", "Спасибо за покупку", 1),
    "S09": lambda s: __import__("re").sub(r"<h1\b[^>]*>[\s\S]*?</h1>", "", s, count=1, flags=__import__("re").I),
    "S10": lambda s: s.replace("Мы подготовили свежие детали", "Мы подготовили для тебя", 1),
    "L01": lambda s: _swap_first_tokens(s, "{{brandName}}", "{{name}}"),
    "L02": lambda s: s[:s.find("{{productName}}")] + s[s.find("<!-- Здесь вставляется тело письма -->"):],
    "A01": lambda s: s.replace(
        'color:#24352d">Мы подготовили', 'color:#ffffff">Мы подготовили', 1,
    ),
    "A04": lambda s: s.replace(
        "background-color:#f4f5f0", "background-color:transparent"
    ).replace("background-color:#ffffff", "background-color:transparent")
     .replace('bgcolor="#f4f5f0"', 'bgcolor="transparent"')
     .replace('bgcolor="#ffffff"', 'bgcolor="transparent"'),
    "P02": lambda s: __import__("re").sub(r"#[0-9a-fA-F]{3,8}", "#24352d", s),
}


@pytest.mark.parametrize("rule", sorted(ADDITIONAL_MUTATIONS))
def test_additional_validator_rule_mutations(rule):
    source = ADDITIONAL_MUTATIONS[rule](GOOD)
    report = validate(source, 1)
    assert rule in {issue.rule for issue in report.errors + report.warnings}


def test_every_registered_validator_rule_has_a_negative_mutation():
    already_mutated = {
        "V01", "V02", "V03", "V04", "V08", "V09", "V11",
        "T04", "T08", "T09", "T11", "T12", "T16", "T17", "T20",
        "S04", "S07", "S08", "S11", "S12",
        "A02", "A03", "P01", "V10",
        *(f"M{number:02d}" for number in range(1, 21)),
    }
    covered = already_mutated | set(ADDITIONAL_MUTATIONS)
    assert set(RULES) <= covered


@pytest.mark.parametrize(
    ("before", "after", "rule", "severity"),
    [
        ('{{name}}', 'Покупатель', "V01", "E"),
        ("Позиция: {{productName}}", "Позиция: {{unknown}}", "V02", "E"),
        ("<title>Детали {{productName}}</title>", "<title>Детали товара</title>", "V03", "E"),
        ("<p style=\"margin:0 0 8px;color:#24352d;font-size:16px;line-height:1.5\">Позиция: {{productName}}</p>",
         "<p style=\"margin:0 0 8px;color:#24352d;font-size:16px;line-height:1.5\">Позиция: покупка</p>", "V04", "E"),
        ('<span style="color:#4f6953;font-size:18px;vertical-align:super">$</span>', "", "V08", "E"),
        ('<span style="color:#4f6953;font-size:18px;vertical-align:super">$</span>',
         '<span style="color:#dddddd;font-size:18px;vertical-align:super">$</span>', "A02", "E"),
        ("{{supportEmail}}", "{{supportEmail}}?subject=Hello", "V09", "W"),
        ("{{price}}</span>", "{{price}}</span>", "V11", "W"),  # price font mutation is added below
        ("<body bgcolor=\"#f4f5f0\"", "<body", "T09", "E"),
        ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560"',
         '<table cellpadding="0" cellspacing="0" border="0" width="560"', "T08", "E"),
        ('border-collapse:separate', 'border-collapse:collapse', "T11", "E"),
        ("font-size:16px;line-height:1.5;color:#24352d\">Мы подготовили свежие детали",
         "font-size:11px;line-height:1.5;color:#24352d\">Мы подготовили свежие детали", "T12", "E"),
        ("<title>Детали {{productName}}</title>", "<title></title>", "T20", "E"),
        ("<p style=\"margin:0 0 12px;color:#24352d;font-size:16px;line-height:1.5\">Когда этап изменится",
         "<p style=\"margin:0 0 12px;color:#24352d;font-size:16px;line-height:1.5\">Завтра, когда этап изменится", "S07", "W"),
        ("Позиция: {{productName}}", "Ваш заказ: {{productName}}", "S08", "W"),
        ("Мы сверяем состав", "Нажмите, чтобы продолжить. Мы сверяем состав", "S11", "W"),
        ("Команда {{brandName}}", "Команда скидка {{brandName}}", "S12", "W"),
        ("✓ Принят", "Принят", "A03", "W"),
        ("#f4f5f0", "#ff4b20", "P01", "W"),
        ("<strong style=\"color:#24352d;font-weight:700\">", "<strong>", "T16", "W"),
        ("</p>\n<ul style=", "</p>\n<p style=\"margin:0;color:#24352d;font-size:16px;line-height:1.5\">Ещё одно сообщение</p>\n<ul style=", "T17", "W"),
        ("font-size:24px;line-height:1.5;color:#24352d\">Добрый день",
          "font-size:24px;line-height:1.5;text-transform:uppercase;color:#24352d\">Добрый день", "S13", "E"),
        ('style="margin:0;background-color:#f4f5f0;font-family:Arial,sans-serif"',
         'style="margin:0;float:left;background-color:#f4f5f0;font-family:Arial,sans-serif"', "T04", "W"),
    ],
)
def test_validator_rule_mutations(before, after, rule, severity):
    if rule == "V11":
        source = GOOD.replace(
            'font-size:26px;font-weight:700">{{price}}',
            'font-size:22px;font-weight:700">{{price}}',
        )
    elif rule == "T11":
        source = GOOD.replace("border-collapse:separate", "border-collapse:collapse;border-radius:8px", 1)
    else:
        assert before in GOOD, (rule, before)
        source = GOOD.replace(before, after, 1)
    report = validate(source, 1)
    collection = report.errors if severity == "E" else report.warnings
    assert rule in {issue.rule for issue in collection}


def _currency_html(method, sign_style, price_style, after_sign="", before_sign=""):
    base = GOOD
    original = (
        '<span style="color:#4f6953;font-size:18px;vertical-align:super">$</span>'
        '<span style="color:#24352d;font-size:26px;font-weight:700">{{price}}</span>'
    )
    return base.replace(
        original,
        before_sign + f'<span style="{sign_style}">$</span>'
        + f'<span style="{price_style}">{{{{price}}}}</span>' + after_sign,
    )


@pytest.mark.parametrize(
    ("method", "sign_style", "price_style", "after_sign"),
    [
        (1, "font-size:18px;vertical-align:super;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (2, "font-size:18px;vertical-align:super;color:#24352d", "font-size:26px;font-weight:700;color:#24352d", ""),
        (3, "display:inline-block;width:24px;height:24px;border-radius:50%;line-height:24px;text-align:center;background-color:#4f6953;color:#fff", "font-size:26px;font-weight:700;color:#24352d", ""),
        (4, "display:inline-block;width:24px;height:24px;border-radius:7px;line-height:24px;text-align:center;background-color:#4f6953;color:#fff", "font-size:26px;font-weight:700;color:#24352d", ""),
        (5, "border:2px solid #4f6953;color:#4f6953;font-size:18px", "font-size:26px;font-weight:700;color:#24352d", ""),
        (6, "font-size:26px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (7, "font-size:18px;vertical-align:middle;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (8, "font-size:14px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", "$"),
        (9, "font-size:18px;border-bottom:3px solid #4f6953;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (10, "font-size:18px;font-style:italic;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (11, "font-size:18px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d;background-color:#f4f5f0", ""),
        (12, "font-size:18px;background-color:#4f6953;color:#fff", "font-size:26px;font-weight:700;color:#24352d", ""),
        (13, "font-size:26px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (14, "font-size:18px;border-left:2px solid #4f6953;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (15, "font-size:16px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", " USD"),
        (16, "display:inline-block;width:24px;height:24px;border-radius:8px;line-height:24px;text-align:center;background-color:#4f6953;color:#fff", "font-size:26px;font-weight:700;color:#24352d", ""),
        (17, "display:inline-block;width:24px;height:24px;border-radius:50%;line-height:24px;text-align:center;background-color:#24352d;color:#fff", "font-size:26px;font-weight:700;color:#24352d", ""),
        (18, "display:inline-block;width:24px;height:24px;border-radius:8px;line-height:24px;text-align:center;background-color:#4f6953;color:#fff", "font-size:26px;font-weight:700;color:#24352d", ""),
        (19, "display:inline-block;width:24px;height:24px;border-radius:50%;line-height:24px;text-align:center;background-color:#4f6953;color:#fff", "font-size:26px;font-weight:700;color:#24352d", ""),
        (20, "font-size:18px;border-right:3px solid #4f6953;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
    ],
)
def test_currency_method_specific_warning_is_absent_for_matching_markup(method, sign_style, price_style, after_sign):
    source = _currency_html(method, sign_style, price_style, after_sign)
    report = validate(source, method)
    assert f"M{method:02d}" not in {issue.rule for issue in report.warnings}


@pytest.mark.parametrize(
    ("method", "sign_style", "price_style", "after_sign"),
    [
        (1, "font-size:18px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (2, "font-size:18px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (3, "display:inline-block;width:24px;height:24px;border-radius:2px;line-height:24px;text-align:center;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (4, "display:inline-block;width:24px;height:24px;border-radius:9px;line-height:24px;text-align:center;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (5, "border:1px solid #4f6953;background-color:#fff;color:#4f6953;font-size:18px", "font-size:26px;font-weight:700;color:#24352d", ""),
        (6, "font-size:18px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (7, "font-size:28px;vertical-align:middle;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (8, "font-size:14px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (9, "font-size:18px;border-bottom:2px solid #4f6953;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (10, "font-size:18px;font-style:normal;color:#4f6953", "font-size:26px;font-weight:400;color:#24352d", ""),
        (11, "font-size:18px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (12, "font-size:18px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (13, "font-size:26px;color:#24352d", "font-size:26px;font-weight:700;color:#24352d", ""),
        (14, "font-size:18px;border-left:1px solid #4f6953;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (15, "font-size:16px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (16, "display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (17, "display:inline-block;width:24px;height:26px;border-radius:50%;line-height:26px;text-align:center;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (18, "display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (19, "display:inline-block;width:24px;height:24px;border-radius:2px;line-height:24px;text-align:center;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
        (20, "font-size:24px;color:#4f6953", "font-size:26px;font-weight:700;color:#24352d", ""),
    ],
)
def test_currency_method_mutation_emits_its_warning(method, sign_style, price_style, after_sign):
    source = _currency_html(method, sign_style, price_style, after_sign)
    report = validate(source, method)
    assert f"M{method:02d}" in {issue.rule for issue in report.warnings}


def test_validator_bounds_large_malformed_input_and_is_fast_on_golden():
    start = time.perf_counter()
    report = validate(GOOD)
    elapsed = time.perf_counter() - start
    assert report.ok and elapsed < 0.2
    base_size = len(GOOD.encode("utf-8"))
    at_warning_threshold = validate(GOOD + (" " * (98_000 - base_size)))
    assert not any(issue.rule == "T23" and issue.severity == "E"
                   for issue in at_warning_threshold.errors)
    over_error_threshold = validate(GOOD + (" " * (98_001 - base_size)))
    assert any(issue.rule == "T23" and issue.severity == "E"
               for issue in over_error_threshold.errors)
    malformed = validate("<" * 300_001)
    assert [issue.rule for issue in malformed.errors] == ["T23"]
    assert validate("").ok is False


@pytest.mark.parametrize("term", BANNED)
def test_each_banned_term_triggers_the_content_rule(term):
    visible_term = term.removesuffix("*")
    source = GOOD.replace("Мы подготовили свежие детали", visible_term, 1)
    report = validate(source)
    assert "S01" in {issue.rule for issue in report.errors}, term


def test_spam_terms_use_word_boundaries_but_still_catch_exact_promises():
    safe = validate(GOOD.replace("свежие детали", "insurgent hotline"))
    assert "S01" not in {issue.rule for issue in safe.errors}
    unsafe = validate(GOOD.replace("свежие детали", "urgent"))
    assert "S01" in {issue.rule for issue in unsafe.errors}
