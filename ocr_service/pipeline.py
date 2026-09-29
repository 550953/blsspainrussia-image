"""Полный цикл распознавания пачки: чанки -> Gemini -> повтор -> ddddocr."""
import asyncio
import gc
import os
from typing import List

from .config import GEMINI_SHEET_CHUNK_SIZE
from .contact_sheet import _chunk
from .dddd_ocr import EXECUTOR, recognize_one_sync_part
from .gemini_sheet import recognize_gemini_sheet_async, recognize_gemini_variant_retry
from .key_pool import GEMINI_KEYS
from .telemetry import _quality_note
from .utils import clean_base64

# Зависит от числа ключей, поэтому живёт здесь, а не в config.py.
GEMINI_SHEET_MAX_CONCURRENT_CHUNKS = int(
    os.getenv("GEMINI_SHEET_MAX_CONCURRENT_CHUNKS", str(max(len(GEMINI_KEYS), 1)))
)


async def _fill_unrecognized(images: List[bytes], results: List[dict]) -> List[dict]:
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
    if not still_missing:
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


async def process_images_gemini_sheet(images_b64: List[str]) -> List[dict]:
    """Бьёт вход на чанки, гонит их параллельно (семафор по числу ключей) и добивает
    нераспознанные ячейки (Gemini-повтор -> ddddocr)."""
    images = [clean_base64(b64) for b64 in images_b64]
    chunks = _chunk(images, GEMINI_SHEET_CHUNK_SIZE)
    semaphore = asyncio.Semaphore(GEMINI_SHEET_MAX_CONCURRENT_CHUNKS)

    async def _run_chunk(chunk: List[bytes]) -> List[dict]:
        async with semaphore:
            chunk_results = await recognize_gemini_sheet_async(chunk)
            return await _fill_unrecognized(chunk, chunk_results)

    chunk_results = await asyncio.gather(*[_run_chunk(c) for c in chunks])
    flat: List[dict] = []
    for r in chunk_results:
        flat.extend(r)
    _quality_note("cells", len(flat))
    _quality_note("final_zero", sum(1 for r in flat if r["text"] == "0"))
    gc.collect()
    return flat
