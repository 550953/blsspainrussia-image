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
