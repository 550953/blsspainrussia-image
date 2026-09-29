"""/health (для Render и мониторинга) и /favicon.ico."""
import json
import os
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import (
    BASE_DIR,
    BETTERSTACK_CONFIG_SOURCE,
    BETTERSTACK_ENABLED,
    BETTERSTACK_SERVICE,
    GEMINI_SHEET_CHUNK_SIZE,
    GEMINI_VARIANT_RETRY_COLS,
    GEMINI_VARIANT_RETRY_ENABLED,
    GEMINI_VARIANT_RETRY_MIN_VOTES,
    OCR_MAX_WORKERS,
    STARTUP_TIMING_FILE,
    STATUS_PASSWORD,
    STATUS_USER,
)
from ..jobs import JOB_RESULT_TTL_SECONDS, JOBS, _cleanup_expired_jobs
from ..key_pool import GEMINI_KEYS, gemini_model_name, gemini_pool
from ..pipeline import GEMINI_SHEET_MAX_CONCURRENT_CHUNKS
from ..proxies import GEMINI_PROXIES, _configured_proxies, _proxy_source
from ..telemetry import JOB_STATS, betterstack_queue_size

router = APIRouter()


@router.get("/health")
async def health():
    _cleanup_expired_jobs()
    startup_info = None
    if STARTUP_TIMING_FILE.exists():
        try:
            startup_info = json.loads(STARTUP_TIMING_FILE.read_text())
        except Exception:
            startup_info = None

    overloaded, remaining = gemini_pool.is_overloaded()
    now = time.monotonic()
    wall_now = time.time()
    job_values = list(JOBS.values())
    pending_jobs = [job for job in job_values if job.get("status") == "pending"]
    running_jobs = [job for job in job_values if job.get("status") == "running"]

    def oldest_age(jobs):
        if not jobs:
            return 0
        return round(max(0.0, wall_now - min(float(job.get("created_at", wall_now)) for job in jobs)), 1)

    return {
        "status": "ok",
        "pid": os.getpid(),
        "gemini_keys_count": len(GEMINI_KEYS),
        "gemini_keys_by_state": gemini_pool.grouped_keys(now=now),
        "gemini_model": gemini_model_name,
        "gemini_pool_overloaded": overloaded,
        "gemini_pool_overloaded_seconds_left": round(remaining, 1) if overloaded else 0,
        "gemini_proxy_channels": len(GEMINI_PROXIES),
        "gemini_proxy_mode": "proxied+direct_fallback" if _configured_proxies else "direct_only",
        "gemini_proxy_source": _proxy_source,
        "gemini_sheet_chunk_size": GEMINI_SHEET_CHUNK_SIZE,
        "gemini_sheet_max_concurrent_chunks": GEMINI_SHEET_MAX_CONCURRENT_CHUNKS,
        "gemini_variant_retry_enabled": GEMINI_VARIANT_RETRY_ENABLED,
        "gemini_variant_retry_cols": GEMINI_VARIANT_RETRY_COLS,
        "gemini_variant_retry_min_votes": GEMINI_VARIANT_RETRY_MIN_VOTES,
        "ocr_max_workers": OCR_MAX_WORKERS,
        "ocr_jobs_pending": len(pending_jobs),
        "ocr_jobs_in_memory": len(JOBS),
        "ocr_jobs_running": len(running_jobs),
        "ocr_oldest_pending_age_seconds": oldest_age(pending_jobs),
        "ocr_oldest_running_age_seconds": oldest_age(running_jobs),
        "ocr_job_result_ttl_seconds": JOB_RESULT_TTL_SECONDS,
        "ocr_job_stats": dict(JOB_STATS),
        "betterstack_enabled": BETTERSTACK_ENABLED,
        "betterstack_service": BETTERSTACK_SERVICE,
        "betterstack_config_source": BETTERSTACK_CONFIG_SOURCE,
        "betterstack_queue_size": betterstack_queue_size(),
        "status_page_enabled": bool(STATUS_USER and STATUS_PASSWORD),
        "startup_timing": startup_info,
    }


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = BASE_DIR / "favicon.ico"  # лежит рядом с main.py, как и раньше
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(favicon_path)
