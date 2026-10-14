"""Раунд 4: пулы текстов 30+, история при малом пуле, запреты и S14/S08/S01, автофикс T27, группировка предупреждений."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_service.email_template import autofix, director, prompt
from ocr_service.email_template.validator import validate

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = (FIXTURES / "good.html").read_text(encoding="utf-8")
REAL = sorted(FIXTURES.glob("real_batch_lite_*.html"))


def _rules(report):
    return {issue.rule for issue in report.errors + report.warnings}


def _with_text(text):
    return GOOD.replace("Мы сверяем состав", text, 1)


# ---------------------------------------------------------------- пулы и история

def test_pools_have_30_plus_unique_clean_variants():
    for pool, token in ((director.GREETINGS, "{{name}}"), (director.SIGNOFFS, "{{brandName}}"), (director.HEADINGS, None)):
        assert len(pool) >= 30 and len(set(pool)) == len(pool)
        for text in pool:
            low = text.lower()
            assert not any(stem in low for stem in prompt.FORBIDDEN_STEMS), text
            assert "!" not in text and "ваш заказ" not in low and "статус заказа" not in low, text
            assert not re.search(r"(?<!\w)(?:ты|тебе|тебя|твой)(?!\w)", low), text
            assert token is None or text.count(token) == 1, text
            leftovers = re.sub(r"\{\{(?:name|brandName)\}\}", "", text)
            assert "{" not in leftovers and "}" not in leftovers, text
    assert len(director.VOICES) >= 8


def test_pool_texts_raise_no_banned_or_stock_rules_in_validator():
    for greeting in director.GREETINGS:
        report = validate(GOOD.replace("Добрый день, {{name}}!", greeting, 1), 1)
        assert not ({"S01", "S06", "S08", "S10", "S14"} & _rules(report)), greeting
    for heading in director.HEADINGS:
        report = validate(GOOD.replace("Покупка уже в работе", heading, 1), 1)
        assert not ({"S01", "S06", "S08", "S10", "S14"} & _rules(report)), heading
    for signoff in director.SIGNOFFS:
        report = validate(GOOD.replace("Спасибо, что выбрали нас.", signoff, 1), 1)
        assert not ({"S01", "S06", "S08", "S10", "S14"} & _rules(report)), signoff


def test_30_briefs_in_a_row_give_at_least_24_distinct_texts():
    director.reset_history()
    briefs = [director.pick_brief(seed=1000 + number) for number in range(30)]
    director.reset_history()
    for field in ("greeting", "heading", "signoff"):
        assert len({getattr(brief, field) for brief in briefs}) >= 24, field


def test_pick_with_pool_smaller_than_history_takes_least_recent_and_does_not_fail():
    import random
    rng = random.Random(1)
    pool = ["a", "b", "c"]
    assert director._pick(rng, pool, ["a", "b", "c", "a", "b"], 12) == "c"
    assert director._pick(rng, pool, [], 12) in pool
    assert director._pick(rng, ["only"], ["only"] * 12, 12) == "only"
    seen = []
    for _ in range(9):
        seen.append(director._pick(rng, pool, seen, 12))
    assert all(len(set(seen[i:i + 3])) == 3 for i in range(0, 7))


# ---------------------------------------------------------------- промпт 1.6

def test_prompt_version_and_supplement_cover_new_bans_steps_and_jokes():
    assert prompt.PROMPT_VERSION == "1.6"
    brief = director.pick_brief(seed=5, currency_method=1, record=False).public()
    message = prompt.build_user_message(brief)
    for phrase in ("персональный менеджер", "под контролем специалистов", "бережно", "максимум внимания", "по накладной", "службе доставки", "отдел …"):
        assert phrase in message, phrase
    assert "привязанные к этапам статуса" in message
    assert "чай" in message and "быстрее" in message


def test_currency_contract_depends_on_method():
    plate = prompt.build_user_message(director.pick_brief(seed=5, currency_method=4, record=False).public())
    plain = prompt.build_user_message(director.pick_brief(seed=5, currency_method=1, record=False).public())
    # Контракт плашки отменён (живой прогон: M04 в 10 из 10, T26 в 3 из 10): для всех способов одна и та же строка про <span>.
    assert "соседних ячейках" not in plate and "соседних ячейках" not in plain
    assert "обычными <span>" in plate and "обычными <span>" in plain


# ---------------------------------------------------------------- S01, S14, S08

def test_s01_faster_is_only_a_warning_but_other_stop_words_stay_errors():
    soft = validate(_with_text("Пока чайник закипает быстрее, чем кот прыгает, мы сверяем состав"), 1)
    assert "S01" in {i.rule for i in soft.warnings} and "S01" not in {i.rule for i in soft.errors}
    assert any("быстрее" in i.where for i in soft.warnings if i.rule == "S01")
    hard = validate(_with_text("Срочно сверяем состав"), 1)
    assert "S01" in {i.rule for i in hard.errors}


def test_s14_catches_invented_details_in_message_block():
    for phrase in (
        "Сверяем позиции по накладной", "Передаём службе доставки", "Под присмотром специалистов",
        "Ваш персональный менеджер на связи", "Отдел логистики всё проверит", "Бережно упакуем", "Передаём на склад",
    ):
        report = validate(_with_text(phrase), 1)
        found = [i for i in report.warnings if i.rule == "S14"]
        assert found and all(i.where for i in found), phrase
        assert "S14" not in {i.rule for i in report.errors}, phrase
    assert "S14" not in _rules(validate(GOOD, 1))


def test_s08_flags_stock_phrases_but_not_how_it_progresses_inside_a_sentence():
    assert "S08" in _rules(validate(_with_text("Вот что происходит с заказом"), 1))
    assert "S08" in _rules(validate(_with_text("С уважением к вам"), 1))
    heading = validate(GOOD.replace("Покупка уже в работе", "Как продвигается покупка", 1), 1)
    assert "S08" in _rules(heading)
    assert "S08" not in _rules(validate(GOOD, 1))  # «как продвигается покупка» в обычном предложении не заголовок


# ---------------------------------------------------------------- AF32 (T27)

SPAN_FIXTURE = GOOD.replace(
    "Мы сверяем состав",
    '<span style="margin-left:6px;color:#24352d;">один</span> '
    '<span style="padding:0 8px;color:#24352d;">два</span> '
    '<span style="margin-right:12px;color:#24352d;">три</span> '
    '<span style="padding-left:1em;color:#24352d;">четыре</span> Мы сверяем состав',
    1,
)


def test_t27_autofix_replaces_small_spacing_with_nbsp_and_keeps_large_and_non_px():
    assert "T27" in _rules(validate(SPAN_FIXTURE, 1))
    fixed, ids = autofix.apply(SPAN_FIXTURE)
    assert "AF32" in ids
    assert '<span style="color:#24352d;">&nbsp;&nbsp;один</span>' in fixed  # 6px -> два &nbsp;
    assert '<span style="color:#24352d;">&nbsp;&nbsp;два&nbsp;&nbsp;</span>' in fixed  # 8px с обеих сторон
    assert 'margin-right:12px' in fixed and 'padding-left:1em' in fixed  # больше 8px и не px не трогаем
    again, ids_again = autofix.apply(fixed)
    assert again == fixed and "AF32" not in ids_again
    assert len(validate(fixed, 1).errors) <= len(validate(SPAN_FIXTURE, 1).errors)


def test_t27_autofix_handles_nested_spans():
    nested = GOOD.replace(
        "Мы сверяем состав",
        '<span style="color:#24352d;"><span style="margin-left:4px;color:#24352d;">вложенный</span> снаружи</span> Мы сверяем состав',
        1,
    )
    fixed, ids = autofix.apply(nested)
    assert "AF32" in ids
    assert '<span style="color:#24352d;"><span style="color:#24352d;">&nbsp;вложенный</span> снаружи</span>' in fixed
    assert validate(fixed, 1).ok == validate(nested, 1).ok


def test_t27_is_gone_after_autofix_on_real_fixtures():
    assert len(REAL) == 10
    for path in REAL:
        fixed, _ids = autofix.apply(path.read_text(encoding="utf-8"))
        report = validate(fixed, 4)
        assert "T27" not in {i.rule for i in report.warnings}, path.name
        assert report.ok, path.name


# ---------------------------------------------------------------- D.2 группировка

def test_repeated_line_height_warnings_are_grouped_with_count():
    cells = "".join(
        '<tr><td style="font-family:Arial,sans-serif;font-size:15px;line-height:1.1;color:#24352d;">Строка %d</td></tr>' % n
        for n in range(8)
    )
    html = GOOD.replace("</table>", "<tr><td><table>" + cells + "</table></td></tr></table>", 1)
    report = validate(html, 1)
    t14 = [i for i in report.warnings if i.rule == "T14"]
    assert len(t14) == 1 and t14[0].count >= 8 and t14[0].where.startswith("td")


def test_warning_lines_per_rule_are_limited_and_rest_goes_to_suppressed():
    from ocr_service.email_template import validator
    report = validator.Report(ok=True)
    for number in range(9):
        report.warnings.append(validator.Issue("S99", "W", "тест", "фрагмент %d" % number))
    validator._group_warnings(report)
    assert len(report.warnings) == 5 and report.stats["suppressed_warnings"] == {"S99": 4}


def test_real_fixtures_after_autofix_have_few_warning_lines():
    total = 0
    for path in REAL:
        fixed, _ids = autofix.apply(path.read_text(encoding="utf-8"))
        report = validate(fixed, 4)
        assert all(i.where for i in report.errors + report.warnings)
        # S08/S14 в этих старых письмах (до пулов) законно срабатывают на выдуманные подробности и шаблонные обороты.
        total += len([i for i in report.warnings if i.rule not in {"S08", "S14"}])
    assert total <= 30, total
