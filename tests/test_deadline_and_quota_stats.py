"""Дедлайн задания (частичный результат вместо TimeoutError) и учёт 429 «дневная квота» на панели."""
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
from ocr_service.gemini_errors import GeminiAPIError, _classify_gemini_http_error  # noqa: E402
from ocr_service.key_pool import GeminiKeyPool  # noqa: E402

DAILY_MSG = ("Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
             "limit: 20, model: gemini-3.6-flash Please retry in 11h3m0s.")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    telemetry.ATTEMPTS.clear()
    telemetry.QUALITY.clear()
    pressure._INFLIGHT.clear()
    pressure._cache.update(at=0.0, value=None)
    pressure._hedge_on = False
    monkeypatch.setattr(pressure, "HEDGE_ENABLED", True)
    yield


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _quality(kind):
    return sum(n for _ts, k, n in telemetry.QUALITY if k == kind)


# --- ddddocr: готовые ячейки переживают отмену чанка -------------------------------------

def test_cutover_keeps_cells_dddd_finished_before_timeout(monkeypatch):
    """Раньше gather выбрасывал весь чанк, если не досчиталась одна ячейка."""
    monkeypatch.setattr(pressure, "should_hedge", lambda elapsed: True)
    monkeypatch.setattr(pressure, "HEDGE_DEADLINE_S", 0.5)
    monkeypatch.setattr(pressure, "HEDGE_FINISH_S", 0.8)

    def dddd(img):
        if img == b"slow":
            time.sleep(3)
        return "555"

    async def gemini_hangs(chunk):
        await asyncio.sleep(30)

    monkeypatch.setattr(pipeline, "recognize_dddd_light", dddd)
    monkeypatch.setattr(pipeline, "recognize_gemini_sheet_async", gemini_hangs)
    res, tried = _run(pipeline._recognize_chunk([b"a", b"slow"]))
    assert tried
    assert res[0]["text"] == "555" and res[0]["source"] == "ddddocr_hedge_cutover"
    assert res[1]["text"] == "0"


def test_chunk_cutover_moves_earlier_when_job_deadline_is_close(monkeypatch):
    monkeypatch.setattr(pressure, "should_hedge", lambda elapsed: True)
    monkeypatch.setattr(pressure, "HEDGE_DEADLINE_S", 40.0)
    monkeypatch.setattr(pressure, "HEDGE_FINISH_S", 0.5)
    monkeypatch.setattr(pipeline, "recognize_dddd_light", lambda img: "321")

    async def gemini_hangs(chunk):
        await asyncio.sleep(30)

    monkeypatch.setattr(pipeline, "recognize_gemini_sheet_async", gemini_hangs)
    t0 = time.monotonic()
    res, _ = _run(pipeline._recognize_chunk([b"a"], deadline=time.monotonic() + 2.0))
    assert time.monotonic() - t0 < 3.0
    assert res[0]["text"] == "321"


# --- добивание нулей укладывается в дедлайн -------------------------------------------------

def test_variant_retry_skipped_when_no_time_left(monkeypatch):
    calls = []

    async def retry(img):
        calls.append(img)
        return "999", 3

    monkeypatch.setattr(pipeline, "recognize_gemini_variant_retry", retry)
    results = [{"text": "0", "source": "gemini_sheet"}, {"text": "123", "source": "gemini_sheet"}]
    out = _run(pipeline._fill_unrecognized([b"a", b"b"], results, dddd_tried=True,
                                           deadline=time.monotonic() + 1.0))
    assert calls == []                       # повтор Gemini даже не начат
    assert out[0]["text"] == "0" and out[1]["text"] == "123"
    assert _quality("variant_skipped_deadline") == 1


def test_variant_retry_is_cut_by_deadline_and_dddd_still_runs(monkeypatch):
    monkeypatch.setattr(pipeline, "VARIANT_RETRY_MIN_LEFT_S", 0.2)
    monkeypatch.setattr(pipeline, "DDDD_MIN_LEFT_S", 0.3)
    monkeypatch.setattr(pipeline, "DDDD_RESERVE_S", 0.8)

    async def slow_retry(img):
        await asyncio.sleep(30)

    monkeypatch.setattr(pipeline, "recognize_gemini_variant_retry", slow_retry)
    monkeypatch.setattr(pipeline, "recognize_one_sync_part", lambda img: "777")
    results = [{"text": "0", "source": "gemini_sheet"}]
    t0 = time.monotonic()
    out = _run(pipeline._fill_unrecognized([b"a"], results, deadline=time.monotonic() + 1.5))
    assert time.monotonic() - t0 < 2.0
    assert out[0]["text"] == "777" and "ddddocr_fallback" in out[0]["source"]


def test_without_deadline_behaviour_is_unchanged(monkeypatch):
    async def retry(img):
        return "888", 2

    monkeypatch.setattr(pipeline, "recognize_gemini_variant_retry", retry)
    results = [{"text": "0", "source": "gemini_sheet"}]
    out = _run(pipeline._fill_unrecognized([b"a"], results))
    assert out[0]["text"] == "888"


# --- задание целиком: частичный ответ вместо TimeoutError -----------------------------------

def _patch_two_chunks(monkeypatch, second_hangs=True, first_text="111"):
    monkeypatch.setattr(pipeline, "GEMINI_SHEET_CHUNK_SIZE", 1)
    monkeypatch.setattr(pipeline, "clean_base64", lambda b64: b64.encode())

    async def chunk(images, deadline=None):
        if images == [b"B"] and second_hangs:
            await asyncio.sleep(30)
        return [{"text": first_text, "source": "gemini_sheet"}], False

    monkeypatch.setattr(pipeline, "_recognize_chunk", chunk)


def test_job_returns_partial_result_at_deadline(monkeypatch):
    _patch_two_chunks(monkeypatch)
    t0 = time.monotonic()
    out = _run(pipeline.process_images_gemini_sheet(["A", "B"], deadline=time.monotonic() + 0.5))
    assert time.monotonic() - t0 < 1.5
    assert out[0]["text"] == "111"
    assert out[1] == {"text": "0", "source": "deadline_no_result"}
    assert _quality("deadline_partial") == 1


def test_job_with_nothing_recognized_by_deadline_still_errors(monkeypatch):
    _patch_two_chunks(monkeypatch, first_text="0")
    with pytest.raises(asyncio.TimeoutError):
        _run(pipeline.process_images_gemini_sheet(["A", "B"], deadline=time.monotonic() + 0.3))


def test_job_without_deadline_waits_for_all_chunks(monkeypatch):
    _patch_two_chunks(monkeypatch, second_hangs=False)
    out = _run(pipeline.process_images_gemini_sheet(["A", "B"]))
    assert [r["text"] for r in out] == ["111", "111"]


# --- 429 «дневная квота» не портит процент успеха и индекс давления ------------------------

def _log_attempt(category, daily=False):
    telemetry._record_attempt({"key_profile": "k", "proxy": "DIRECT", "category": category,
                               "latency_ms": 10, "model": "m", "daily_quota": daily})


def test_daily_quota_429_has_own_category_and_is_ignored_by_pressure():
    for _ in range(10):
        _log_attempt("SUCCESS")
    for _ in range(40):
        _log_attempt("RATE_LIMIT_OR_QUOTA", daily=True)
    assert {row[3] for row in telemetry.ATTEMPTS} == {"SUCCESS", telemetry.DAILY_QUOTA}
    assert pressure.compute()["fail_rate"] == 0.0
    telemetry.ATTEMPTS.clear()
    for _ in range(10):
        _log_attempt("SUCCESS")
    for _ in range(10):
        _log_attempt("RATE_LIMIT_OR_QUOTA", daily=False)   # обычный 429 по-прежнему считается
    assert pressure.compute()["fail_rate"] == pytest.approx(0.5)


def test_daily_quota_flag_comes_from_classifier():
    exc = GeminiAPIError(429, "RESOURCE_EXHAUSTED", DAILY_MSG,
                         _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", DAILY_MSG))
    assert exc.category == "RATE_LIMIT_OR_QUOTA" and exc.daily_quota is True


def test_model_hold_summary_reports_model_closed_on_all_keys():
    pool = GeminiKeyPool()
    n = 3
    pool.keys = [f"s{i}" for i in range(n)]
    pool.key_names = [f"K{i}" for i in range(n)]
    pool.blocked, pool.reserved, pool.dead = [0.0] * n, [False] * n, [False] * n
    pool.last_used, pool.reserved_at, pool.last_error, pool.lease_ttl = [0.0] * n, [0.0] * n, [None] * n, [60.0] * n
    pool.dead[2] = True                      # мёртвый ключ не считается
    exc = GeminiAPIError(429, "RESOURCE_EXHAUSTED", DAILY_MSG,
                         _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", DAILY_MSG))
    pool.release(0, error=exc, model="m1")
    assert pool.model_hold_summary("m1")[:2] == (2, 1)
    pool.release(1, error=exc, model="m1")
    allowed, held, soonest = pool.model_hold_summary("m1")
    assert (allowed, held) == (2, 2) and soonest > 10 * 3600
    assert pool.model_hold_summary("m2")[:2] == (2, 0)
