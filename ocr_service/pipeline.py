"""Полный цикл распознавания пачки: чанки -> Gemini -> повтор -> ddddocr."""
import asyncio
import gc
import os
import threading
import time
from typing import List, Optional

from .config import GEMINI_SHEET_CHUNK_SIZE
from .contact_sheet import _chunk
from .dddd_ocr import EXECUTOR, HEDGE_EXECUTOR, recognize_dddd_light, recognize_one_sync_part
from .gemini_sheet import recognize_gemini_sheet_async, recognize_gemini_variant_retry
from .key_pool import GEMINI_KEYS
from . import pressure
from .telemetry import _quality_note, _job_log
from .utils import clean_base64

# Бюджет времени задания. Повтор Gemini по ячейке запускаем, только если до дедлайна задания
# остаётся не меньше OCR_VARIANT_RETRY_MIN_LEFT_S; запасной ddddocr - если остаётся OCR_DDDD_MIN_LEFT_S.
VARIANT_RETRY_MIN_LEFT_S = float(os.getenv("OCR_VARIANT_RETRY_MIN_LEFT_S", "8"))
DDDD_MIN_LEFT_S = float(os.getenv("OCR_DDDD_MIN_LEFT_S", "1.5"))
# Сколько оставить запасному ddddocr, когда обрезаем повтор Gemini по дедлайну (с запасом над порогом).
DDDD_RESERVE_S = float(os.getenv("OCR_DDDD_RESERVE_S", "2.5"))


def _left(deadline: Optional[float]) -> float:
    return float("inf") if deadline is None else deadline - time.monotonic()


# Зависит от числа ключей, поэтому живёт здесь, а не в config.py.
GEMINI_SHEET_MAX_CONCURRENT_CHUNKS = int(
    os.getenv("GEMINI_SHEET_MAX_CONCURRENT_CHUNKS", str(max(len(GEMINI_KEYS), 1)))
)


async def _fill_unrecognized(images: List[bytes], results: List[dict], dddd_tried: bool = False,
                             deadline: Optional[float] = None) -> List[dict]:
    """Для каждой нераспознанной ячейки: 1) точечный Gemini-повтор, 2) ddddocr на оригинале.
    Меняет results на месте (частичный результат виден снаружи даже при отмене) и укладывается
    в deadline (time.monotonic()): лучше вернуть ноль по одной ячейке, чем потерять всё задание."""
    missing_idx = [i for i, r in enumerate(results) if r["text"] == "0"]
    if not missing_idx:
        return results
    _quality_note("missing", len(missing_idx))

    # Последовательно, а не gather: не толкаться за один пул ключей ради нескольких картинок.
    for n_done, i in enumerate(missing_idx):
        left = _left(deadline)
        if left < VARIANT_RETRY_MIN_LEFT_S:
            _quality_note("variant_skipped_deadline", len(missing_idx) - n_done)
            break
        try:
            retry = await asyncio.wait_for(recognize_gemini_variant_retry(images[i]),
                                           timeout=max(0.1, left - max(DDDD_RESERVE_S, DDDD_MIN_LEFT_S + 0.5)))
        except asyncio.TimeoutError:
            _quality_note("variant_skipped_deadline", len(missing_idx) - n_done)
            break
        if retry is not None:
            text, votes = retry
            results[i] = {
                "text": text,
                "source": f"{results[i]['source']}+gemini_variant_retry(votes={votes})",
            }
            _quality_note("variant_saved")

    still_missing = [i for i in missing_idx if results[i]["text"] == "0"]
    if not still_missing or dddd_tried:   # ddddocr уже пробовал эти ячейки параллельно с Gemini
        return results
    left = _left(deadline)
    if left < DDDD_MIN_LEFT_S:
        return results
    _quality_note("dddd_used", len(still_missing))

    loop = asyncio.get_running_loop()

    async def _one(i: int) -> None:
        value = await loop.run_in_executor(EXECUTOR, recognize_one_sync_part, images[i])
        if value and value != "0":
            results[i] = {"text": value, "source": f"{results[i]['source']}+ddddocr_fallback"}
            _quality_note("dddd_saved")

    tasks = [asyncio.ensure_future(_one(i)) for i in still_missing]
    timeout = None if deadline is None else max(0.1, left - 0.5)
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:                      # недосчитанные ячейки остаются нулями, готовые уже записаны
        t.cancel()
    return results


_HEDGE_SEM = asyncio.Semaphore(pressure.HEDGE_MAX_CHUNKS)


def _dddd_cell(image: bytes, stop: threading.Event) -> Optional[str]:
    """Одна ячейка в потоке; если чанк уже закрыт (Gemini успел), ячейку даже не начинаем."""
    if stop.is_set():
        return None
    return recognize_dddd_light(image)


async def _dddd_chunk(images: List[bytes], stop: threading.Event,
                      out: Optional[List[Optional[str]]] = None) -> List[Optional[str]]:
    """ddddocr по всему чанку. Число одновременных чанков ограничено (CPU общий с остальными).
    Каждая готовая ячейка сразу пишется в out: если чанк отменят по дедлайну, уже посчитанное
    не пропадёт (раньше gather выбрасывал весь чанк, даже когда не хватало одной ячейки)."""
    if out is None:
        out = [None] * len(images)
    async with _HEDGE_SEM:
        loop = asyncio.get_running_loop()

        async def _one(i: int, img: bytes) -> None:
            out[i] = await loop.run_in_executor(HEDGE_EXECUTOR, _dddd_cell, img, stop)

        await asyncio.gather(*[_one(i, img) for i, img in enumerate(images)])
    return out


async def _cancel(task: Optional[asyncio.Task]) -> None:
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def _dddd_result(task: Optional[asyncio.Task], n: int, out: Optional[List[Optional[str]]] = None) -> List[Optional[str]]:
    """Всё, что ddddocr успел посчитать (в том числе если задачу отменили на середине)."""
    if out is not None:
        return list(out)
    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return [None] * n
    return task.result()


async def _recognize_chunk(chunk: List[bytes], deadline: Optional[float] = None) -> tuple:
    """Gemini + (при давлении) ddddocr параллельно. Возвращает (results, dddd_tried).
    Gemini всегда в приоритете: ddddocr закрывает только ячейки, которые Gemini не отдал.
    deadline (time.monotonic()) - дедлайн задания: переключение на ddddocr сдвигается раньше,
    чтобы ddddocr успел досчитать до него, если чанк долго ждал своей очереди."""
    if not pressure.HEDGE_ENABLED:
        return await recognize_gemini_sheet_async(chunk), False

    n = len(chunk)
    started = time.monotonic()
    cut_at = pressure.HEDGE_DEADLINE_S
    if deadline is not None:
        cut_at = max(1.0, min(cut_at, deadline - started - pressure.HEDGE_FINISH_S))
    stop = threading.Event()
    d_out: List[Optional[str]] = [None] * n
    d_task: Optional[asyncio.Task] = None
    d_done_at: Optional[float] = None
    with pressure.track_chunk():
        g_task = asyncio.create_task(recognize_gemini_sheet_async(chunk))
        try:
            cutover = False
            while not g_task.done():
                elapsed = time.monotonic() - started
                if d_task is None and pressure.should_hedge(elapsed):
                    d_task = asyncio.create_task(_dddd_chunk(chunk, stop, d_out))
                    snap = pressure.snapshot()
                    _quality_note("hedge_start")
                    _job_log("hedge_start", pressure_index=snap["index"], fail_rate=snap["fail_rate"],
                             chunk_wait_s=round(elapsed, 1), images=n)
                if d_task is not None and d_task.done() and d_done_at is None:
                    d_done_at = time.monotonic()
                grace_over = d_done_at is not None and time.monotonic() - d_done_at >= pressure.HEDGE_GRACE_S
                if elapsed >= cut_at or grace_over:
                    cutover = True
                    break
                t_wait = time.monotonic()
                await asyncio.wait({g_task}, timeout=0.5)
                if not g_task.done():
                    pressure.note_lag(time.monotonic() - t_wait - 0.5)
                if d_task is not None and pressure.overloaded():
                    stop.set()   # сервис задыхается: невзятые ячейки не начинаем, живём на Gemini

            if cutover:
                # Gemini не уложился: отменяем (ключ вернётся через finally), добираем ddddocr.
                await _cancel(g_task)
                if d_task is None:
                    d_task = asyncio.create_task(_dddd_chunk(chunk, stop, d_out))
                try:
                    await asyncio.wait_for(asyncio.shield(d_task), timeout=pressure.HEDGE_FINISH_S)
                except (asyncio.TimeoutError, Exception):
                    pass
                stop.set()
                await _cancel(d_task)
                vals = _dddd_result(d_task, n, d_out)
                _quality_note("hedge_cut")
                results = [{"text": v if v and v != "0" else "0", "source": "ddddocr_hedge_cutover"} for v in vals]
                _quality_note("hedge_used", sum(1 for r in results if r["text"] != "0"))
                return results, True

            results = g_task.result()
            if d_task is None:
                return results, False

            has_zero = any(r["text"] == "0" for r in results)
            if has_zero and not d_task.done():
                left = max(0.0, cut_at - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(asyncio.shield(d_task), timeout=min(left, pressure.HEDGE_FINISH_S))
                except (asyncio.TimeoutError, Exception):
                    pass
            stop.set()
            await _cancel(d_task)
            vals = _dddd_result(d_task, n, d_out)
            for i, r in enumerate(results):
                v = vals[i]
                if r["text"] != "0":
                    if v and v != "0":   # живая оценка точности ddddocr на ячейках, где есть ответ Gemini
                        _quality_note("hedge_cmp")
                        if v == r["text"]:
                            _quality_note("hedge_agree")
                elif v and v != "0":
                    results[i] = {"text": v, "source": f"{r['source']}+ddddocr_hedge"}
                    _quality_note("hedge_used")
            return results, True
        finally:
            stop.set()
            if not g_task.done():
                g_task.cancel()
            if d_task is not None and not d_task.done():
                d_task.cancel()


async def process_images_gemini_sheet(images_b64: List[str], deadline: Optional[float] = None) -> List[dict]:
    """Бьёт вход на чанки, гонит их параллельно (семафор по числу ключей) и добивает
    нераспознанные ячейки (Gemini-повтор -> ddddocr).

    deadline (time.monotonic()) - к этому моменту отдаём то, что есть: недосчитанные чанки
    отменяются, их ячейки уже распознанные сохраняются, остальные - "0". Если к дедлайну
    не распознано вообще ничего, бросаем TimeoutError (клиент перешлёт задание, как раньше)."""
    images = [clean_base64(b64) for b64 in images_b64]
    chunks = _chunk(images, GEMINI_SHEET_CHUNK_SIZE)
    semaphore = asyncio.Semaphore(GEMINI_SHEET_MAX_CONCURRENT_CHUNKS)
    slots: List[Optional[List[dict]]] = [None] * len(chunks)

    async def _run_chunk(k: int, chunk: List[bytes]) -> None:
        async with semaphore:
            chunk_results, dddd_tried = await _recognize_chunk(chunk, deadline=deadline)
            slots[k] = chunk_results          # _fill_unrecognized дописывает в этот же список
            await _fill_unrecognized(chunk, chunk_results, dddd_tried=dddd_tried, deadline=deadline)

    tasks = [asyncio.ensure_future(_run_chunk(k, c)) for k, c in enumerate(chunks)]
    timeout = None if deadline is None else max(0.1, _left(deadline))
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    if pending:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    for t in done:
        if t.exception() is not None:     # ошибки кода не прячем: как раньше, задание падает
            raise t.exception()

    flat: List[dict] = []
    for k, chunk in enumerate(chunks):
        flat.extend(slots[k] or [{"text": "0", "source": "deadline_no_result"} for _ in chunk])
    if pending:
        recognized = sum(1 for r in flat if r["text"] != "0")
        _quality_note("deadline_partial")
        _job_log("deadline_partial", chunks_unfinished=len(pending), chunks=len(chunks),
                 cells=len(flat), recognized=recognized)
        if not recognized:
            raise asyncio.TimeoutError("дедлайн задания: ничего не распознано")
    _quality_note("cells", len(flat))
    _quality_note("final_zero", sum(1 for r in flat if r["text"] == "0"))
    gc.collect()
    return flat
