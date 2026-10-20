"""Параллельный ddddocr: индекс давления, гистерезис, слияние результатов, переключение по дедлайну."""
import asyncio
import sys
import time
import types

import pytest

if "httpx" not in sys.modules:
    try:
        import httpx  # noqa: F401
    except ImportError:
        stub = types.ModuleType("httpx")
        stub.AsyncClient = object
        stub.Timeout = lambda *a, **k: None
        stub.RequestError = Exception
        stub.ReadTimeout = Exception
        sys.modules["httpx"] = stub

from ocr_service import pipeline, pressure, telemetry  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    telemetry.ATTEMPTS.clear()
    telemetry.QUALITY.clear()
    pressure._INFLIGHT.clear()
    pressure._cache.update(at=0.0, value=None)
    pressure._hedge_on = False
    monkeypatch.setattr(pressure, "HEDGE_ENABLED", True)
    yield


def _attempts(ok, bad):
    now = time.time()
    for i in range(ok):
        telemetry.ATTEMPTS.append((now - i * 0.1, "k", "DIRECT", "SUCCESS", 100, 54, 0, "m"))
    for i in range(bad):
        telemetry.ATTEMPTS.append((now - i * 0.1, "k", "DIRECT", "RATE_LIMIT_OR_QUOTA", 100, 54, 0, "m"))


def test_index_calm_vs_distress():
    _attempts(ok=18, bad=2)
    assert pressure.compute()["index"] < 15
    telemetry.ATTEMPTS.clear()
    _attempts(ok=33, bad=67)
    pressure._INFLIGHT.update({1: time.monotonic() - 20, 2: time.monotonic() - 24, 3: time.monotonic() - 30})
    assert pressure.compute()["index"] >= 50


def test_small_sample_is_ignored():
    _attempts(ok=0, bad=3)
    assert pressure.compute()["fail_rate"] == 0.0


def test_hysteresis(monkeypatch):
    seq = iter([55, 40, 25, 40])
    monkeypatch.setattr(pressure, "compute", lambda: {"index": next(seq), "fail_rate": 0, "samples": 0,
                                                     "median_age_s": 0, "inflight": 0})
    states = []
    for _ in range(4):
        pressure._cache.update(at=0.0, value=None)
        states.append(pressure.snapshot()["hedge_on"])
    assert states == [True, True, False, False]


def _gemini(results, delay=0.0):
    async def fake(chunk):
        await asyncio.sleep(delay)
        return [dict(r) for r in results]
    return fake


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_gemini_wins_and_dddd_fills_zero(monkeypatch):
    monkeypatch.setattr(pressure, "should_hedge", lambda elapsed: True)
    monkeypatch.setattr(pipeline, "recognize_one_sync_part", lambda img: {b"a": "111", b"b": "222"}[img])
    g = [{"text": "999", "source": "gemini_sheet"}, {"text": "0", "source": "gemini_sheet"}]
    monkeypatch.setattr(pipeline, "recognize_gemini_sheet_async", _gemini(g, delay=0.6))
    res, tried = _run(pipeline._recognize_chunk([b"a", b"b"]))
    assert tried is True
    assert res[0]["text"] == "999"                      # Gemini приоритетнее ddddocr
    assert res[1]["text"] == "222" and "ddddocr_hedge" in res[1]["source"]


def test_no_hedge_when_calm(monkeypatch):
    monkeypatch.setattr(pressure, "should_hedge", lambda elapsed: False)
    g = [{"text": "123", "source": "gemini_sheet"}]
    monkeypatch.setattr(pipeline, "recognize_gemini_sheet_async", _gemini(g))
    res, tried = _run(pipeline._recognize_chunk([b"a"]))
    assert tried is False and res[0]["text"] == "123"


def test_cutover_when_gemini_hangs(monkeypatch):
    monkeypatch.setattr(pressure, "should_hedge", lambda elapsed: True)
    monkeypatch.setattr(pressure, "HEDGE_DEADLINE_S", 5.0)
    monkeypatch.setattr(pressure, "HEDGE_GRACE_S", 0.3)
    monkeypatch.setattr(pipeline, "recognize_one_sync_part", lambda img: "777")
    monkeypatch.setattr(pipeline, "recognize_gemini_sheet_async", _gemini([{"text": "1", "source": "x"}], delay=30))
    t0 = time.monotonic()
    res, tried = _run(pipeline._recognize_chunk([b"a", b"b"]))
    assert time.monotonic() - t0 < 4
    assert tried and [r["text"] for r in res] == ["777", "777"]
    assert res[0]["source"] == "ddddocr_hedge_cutover"
