"""Полный цикл распознавания пачки: чанки -> Gemini -> повтор -> ddddocr."""
import asyncio
import gc
import os
import threading
import time
from typing import List, Optional

from .config import GEMINI_SHEET_CHUNK_SIZE
from .contact_sheet import _chunk
from .dddd_ocr import EXECUTOR, recognize_one_sync_part
from .gemini_sheet import recognize_gemini_sheet_async, recognize_gemini_variant_retry
from .key_pool import GEMINI_KEYS
from . import pressure
from .telemetry import _quality_note, _job_log
from .utils import clean_base64

# Зависит от числа ключей, поэтому живёт здесь, а не в config.py.
GEMINI_SHEET_MAX_CONCURRENT_CHUNKS = int(
    os.getenv("GEMINI_SHEET_MAX_CONCURRENT_CHUNKS", str(max(len(GEMINI_KEYS), 1)))
)


async def _fill_unrecognized(images: List[bytes], results: List[dict], dddd_tried: bool = False) -> List[dict]:
    """Для каждой нераспознанной ячейки: 1) точечный Gemini-повтор, 2) ddddocr на оригинале."""
    missing_idx = [i for i, r in enumerate(results) if r["text"] == "0"]
    if not missing_idx:
        return results
    _quality_note("missing", len(missing_idx))

    # Последовательно, а не gather: не толкаться за один пул ключей ради нескольких картинок.
    for i in missing_idx:
        retry = await recognize_gemini_variant_retry(images[i])
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
    _quality_note("dddd_used", len(still_missing))

    loop = asyncio.get_running_loop()
    fallback_values = await asyncio.gather(*[
        loop.run_in_executor(EXECUTOR, recognize_one_sync_part, images[i]) for i in still_missing
    ])
    for i, value in zip(still_missing, fallback_values):
        if value and value != "0":
            results[i] = {"text": value, "source": f"{results[i]['source']}+ddddocr_fallback"}
            _quality_note("dddd_saved")
    return results


_HEDGE_SEM = asyncio.Semaphore(pressure.HEDGE_MAX_CHUNKS)


def _dddd_cell(image: bytes, stop: threading.Event) -> Optional[str]:
    """Одна ячейка в потоке; если чанк уже закрыт (Gemini успел), ячейку даже не начинаем."""
    if stop.is_set():
        return None
    return recognize_one_sync_part(image)


async def _dddd_chunk(images: List[bytes], stop: threading.Event) -> List[Optional[str]]:
    """ddddocr по всему чанку. Число одновременных чанков ограничено (CPU общий с остальными)."""
    async with _HEDGE_SEM:
        loop = asyncio.get_running_loop()
        return list(await asyncio.gather(*[
            loop.run_in_executor(EXECUTOR, _dddd_cell, img, stop) for img in images
        ]))


async def _cancel(task: Optional[asyncio.Task]) -> None:
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def _dddd_result(task: Optional[asyncio.Task], n: int) -> List[Optional[str]]:
    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return [None] * n
    return task.result()


async def _recognize_chunk(chunk: List[bytes]) -> tuple:
    """Gemini + (при давлении) ddddocr параллельно. Возвращает (results, dddd_tried).
    Gemini всегда в приоритете: ddddocr закрывает только ячейки, которые Gemini не отдал."""
    if not pressure.HEDGE_ENABLED:
        return await recognize_gemini_sheet_async(chunk), False

    n = len(chunk)
    started = time.monotonic()
    stop = threading.Event()
    d_task: Optional[asyncio.Task] = None
    d_done_at: Optional[float] = None
    with pressure.track_chunk():
        g_task = asyncio.create_task(recognize_gemini_sheet_async(chunk))
        try:
            cutover = False
            while not g_task.done():
                elapsed = time.monotonic() - started
                if d_task is None and pressure.should_hedge(elapsed):
                    d_task = asyncio.create_task(_dddd_chunk(chunk, stop))
                    snap = pressure.snapshot()
                    _quality_note("hedge_start")
                    _job_log("hedge_start", pressure_index=snap["index"], fail_rate=snap["fail_rate"],
                             chunk_wait_s=round(elapsed, 1), images=n)
                if d_task is not None and d_task.done() and d_done_at is None:
                    d_done_at = time.monotonic()
                grace_over = d_done_at is not None and time.monotonic() - d_done_at >= pressure.HEDGE_GRACE_S
                if elapsed >= pressure.HEDGE_DEADLINE_S or grace_over:
                    cutover = True
                    break
                await asyncio.wait({g_task}, timeout=0.5)

            if cutover:
                # Gemini не уложился: отменяем (ключ вернётся через finally), добираем ddddocr.
                await _cancel(g_task)
                if d_task is None:
                    d_task = asyncio.create_task(_dddd_chunk(chunk, stop))
                try:
                    await asyncio.wait_for(asyncio.shield(d_task), timeout=pressure.HEDGE_FINISH_S)
                except (asyncio.TimeoutError, Exception):
                    pass
                stop.set()
                await _cancel(d_task)
                vals = _dddd_result(d_task, n)
                _quality_note("hedge_cut")
                results = [{"text": v if v and v != "0" else "0", "source": "ddddocr_hedge_cutover"} for v in vals]
                _quality_note("hedge_used", sum(1 for r in results if r["text"] != "0"))
                return results, True

            results = g_task.result()
            if d_task is None:
                return results, False

            has_zero = any(r["text"] == "0" for r in results)
            if has_zero and not d_task.done():
                left = max(0.0, pressure.HEDGE_DEADLINE_S - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(asyncio.shield(d_task), timeout=min(left, pressure.HEDGE_FINISH_S))
                except (asyncio.TimeoutError, Exception):
                    pass
            stop.set()
            await _cancel(d_task)
            vals = _dddd_result(d_task, n)
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


async def process_images_gemini_sheet(images_b64: List[str]) -> List[dict]:
    """Бьёт вход на чанки, гонит их параллельно (семафор по числу ключей) и добивает
    нераспознанные ячейки (Gemini-повтор -> ddddocr)."""
    images = [clean_base64(b64) for b64 in images_b64]
    chunks = _chunk(images, GEMINI_SHEET_CHUNK_SIZE)
    semaphore = asyncio.Semaphore(GEMINI_SHEET_MAX_CONCURRENT_CHUNKS)

    async def _run_chunk(chunk: List[bytes]) -> List[dict]:
        async with semaphore:
            chunk_results, dddd_tried = await _recognize_chunk(chunk)
            return await _fill_unrecognized(chunk, chunk_results, dddd_tried=dddd_tried)

    chunk_results = await asyncio.gather(*[_run_chunk(c) for c in chunks])
    flat: List[dict] = []
    for r in chunk_results:
        flat.extend(r)
    _quality_note("cells", len(flat))
    _quality_note("final_zero", sum(1 for r in flat if r["text"] == "0"))
    gc.collect()
    return flat
