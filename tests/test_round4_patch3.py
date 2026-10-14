import re

from ocr_service.email_template import generate, validator


def test_pinned_model_keeps_chain_as_fallback(monkeypatch):
    monkeypatch.setattr(generate, "TEMPLATE_PINNED_FALLBACK", True)
    job = {"model": "m2", "models": ["m1", "m2", "m3"]}
    assert generate._request_chain(job) == ["m2", "m1", "m3"]


def test_pinned_model_strict_when_fallback_off(monkeypatch):
    monkeypatch.setattr(generate, "TEMPLATE_PINNED_FALLBACK", False)
    assert generate._request_chain({"model": "m2", "models": ["m1", "m2"]}) == ["m2"]


def test_no_pinned_model_uses_whole_chain():
    assert generate._request_chain({"model": None, "models": ["m1", "m2"]}) == ["m1", "m2"]


def test_pinned_model_outside_list_is_still_first(monkeypatch):
    monkeypatch.setattr(generate, "TEMPLATE_PINNED_FALLBACK", True)
    assert generate._request_chain({"model": "x", "models": ["m1"]}) == ["x", "m1"]


def test_s08_catches_under_control_phrase():
    assert re.search(validator._CONTENT_PATTERNS["S08"], "мы держим всё под контролем")
    assert not re.search(validator._CONTENT_PATTERNS["S08"], "складывается в единое целое")


def test_s14_does_not_fire_on_sklad_inside_words():
    pattern = validator._INVENTED_PATTERN
    assert not re.search(pattern, "заказ складывается, носки раскладываются по парам")
    assert re.search(pattern, "передали на склад")
