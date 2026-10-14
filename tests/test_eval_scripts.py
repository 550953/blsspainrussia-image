from scripts import eval_templates
from scripts.eval_templates import jaccard, percentile, was_autofix_rescued
from scripts.isolation_check import percentile as isolation_percentile


def test_eval_detects_raw_failure_rescued_by_autofix():
    assert was_autofix_rescued([
        {"raw_errors": ["T16"], "errors": [], "autofixes": ["AF01"]},
    ])
    assert not was_autofix_rescued([
        {"raw_errors": ["T16"], "errors": ["T16"], "autofixes": []},
    ])
    assert not was_autofix_rescued([
        {"raw_errors": [], "errors": [], "autofixes": ["AF01"]},
    ])


def test_eval_similarity_and_percentile_helpers():
    assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert percentile([], 0.5) is None
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert isolation_percentile([1, 2, 3, 4, 5]) == 5


def test_eval_text_metrics_and_stems_stay_in_sync_with_prompt():
    from ocr_service.email_template import prompt
    assert tuple(eval_templates.FORBIDDEN_STEMS) == tuple(prompt.FORBIDDEN_STEMS)
    document = (
        '<h1 style="font-size:24px">Привет, {{name}}</h1><h2>Что дальше</h2><p>Бережно упакуем по накладной</p>'
        '<p style="font-style:italic">До связи, {{brandName}}</p>'
    )
    assert eval_templates.extract_texts(document) == {"greeting": "Привет, {{name}}", "h2": "Что дальше", "signoff": "До связи, {{brandName}}"}
    found = eval_templates.count_forbidden(document)
    assert found["бережн"] == 1 and found["накладн"] == 1 and "склад" not in found
