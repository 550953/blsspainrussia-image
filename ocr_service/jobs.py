"""Очередь заданий в стиле 2captcha: submit -> job_id -> poll.

JOBS хранится в памяти процесса: работает только при ОДНОМ воркере uvicorn
(как и gemini_pool / smart_proxy_pool). Готовый результат живёт JOB_RESULT_TTL_SECONDS,
чтобы потерянный HTTP-ответ можно было забрать повторно.
"""
import asyncio
import os
import time
from typing import List, Optional

from .key_pool import GEMINI_KEYS
from .pipeline import process_images_gemini_sheet
from .telemetry import JOB_STATS, _account_touch, _job_log

JOBS: dict = {}
JOB_RESULT_TTL_SECONDS = max(60, int(os.getenv("OCR_JOB_RESULT_TTL_SECONDS", "300")))
JOB_CLEANUP_INTERVAL_SECONDS = max(5, int(os.getenv("OCR_JOB_CLEANUP_INTERVAL_SECONDS", "30")))
_JOB_CLEANUP_TASK: Optional[asyncio.Task] = None

_JOB_SEMAPHORE = asyncio.Semaphore(max(len(GEMINI_KEYS), 1))


def _job_queue_metrics(job_id: str, job: dict, now: Optional[float] = None) -> dict:
    """Несекретное состояние очереди для одного задания."""
    now = time.time() if now is None else float(now)
    pending = sorted(
        (
            (str(candidate_id), candidate)
            for candidate_id, candidate in JOBS.items()
            if str(candidate.get("status") or "pending") == "pending"
        ),
        key=lambda item: (float(item[1].get("created_at") or 0.0), item[0]),
    )
    running_jobs = sum(str(candidate.get("status") or "") == "running" for candidate in JOBS.values())
    status = str(job.get("status") or "pending")
    queue_position = None
    if status == "pending":
        queue_position = next(
            (index for index, (candidate_id, _c) in enumerate(pending, start=1) if candidate_id == str(job_id)),
            None,
        )

    created_at = float(job.get("created_at") or now)
    started_at = job.get("started_at")
    finished_at = job.get("finished_at")
    queue_end = float(started_at) if started_at else now
    processing_end = float(finished_at) if finished_at else now
    queue_wait_ms = (
        round(max(0.0, queue_end - created_at) * 1000) if started_at or status == "pending" else None
    )
    processing_ms = round(max(0.0, processing_end - float(started_at)) * 1000) if started_at else None
    return {
        "stage": str(job.get("stage") or ("queued" if status == "pending" else status)),
        "queue_position": queue_position,
        "queued_jobs": len(pending),
        "running_jobs": running_jobs,
        "max_concurrency": max(len(GEMINI_KEYS), 1),
        "job_age_ms": round(max(0.0, now - created_at) * 1000),
        "queue_wait_ms": queue_wait_ms,
        "processing_ms": processing_ms,
    }


def _job_public_payload(job: dict) -> dict:
    status = str(job.get("status") or "pending")
    public_status = status if status in {"done", "error"} else "pending"
    payload = {
        # Публичный контракт прежний: старые клиенты видят "pending"; новые смотрят на stage.
        "status": public_status,
        **_job_queue_metrics(str(job.get("job_id") or ""), job),
    }
    if status == "done":
        payload["results"] = job.get("results") or []
        return payload
    if status == "error":
        payload["error"] = str(job.get("error") or "OCR job error")
    return payload


def _cleanup_expired_jobs() -> int:
    now = time.time()
    expired = [
        job_id for job_id, job in list(JOBS.items())
        if job.get("expires_at") and now >= float(job["expires_at"])
    ]
    for job_id in expired:
        job = JOBS.pop(job_id, None)
        if job is None:
            continue
        JOB_STATS["expired"] += 1
        _job_log(
            "expired",
            job_id,
            account_id=job.get("account_id", ""),
            client_request_id=job.get("client_request_id", ""),
            final_status=job.get("status", ""),
            age_ms=round(max(0.0, now - float(job.get("created_at", now))) * 1000),
            result_delivered=bool(job.get("result_delivered_at")),
        )
    return len(expired)


async def _job_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(JOB_CLEANUP_INTERVAL_SECONDS)
        _cleanup_expired_jobs()


def start_cleanup_loop() -> None:
    global _JOB_CLEANUP_TASK
    _JOB_CLEANUP_TASK = asyncio.create_task(_job_cleanup_loop())


def stop_cleanup_loop() -> None:
    global _JOB_CLEANUP_TASK
    if _JOB_CLEANUP_TASK is not None:
        _JOB_CLEANUP_TASK.cancel()
        _JOB_CLEANUP_TASK = None


async def _run_job(job_id: str, images: List[str]) -> None:
    job = JOBS.get(job_id)
    if job is None:
        return

    async with _JOB_SEMAPHORE:
        started_at = time.time()
        job["status"] = "running"
        job["started_at"] = started_at
        job["stage"] = "processing"
        JOB_STATS["started"] += 1
        _job_log(
            "started", job_id,
            account_id=job.get("account_id", ""),
            client_request_id=job.get("client_request_id", ""),
            **_job_queue_metrics(job_id, job, started_at),
        )
        try:
            results = await process_images_gemini_sheet(images)
            finished_at = time.time()
            zero_count = sum(1 for r in results if r.get("text") == "0")
            job["status"] = "done"
            job["stage"] = "finished"
            job["results"] = results
            job["zero_count"] = zero_count
            job["finished_at"] = finished_at
            job["expires_at"] = finished_at + JOB_RESULT_TTL_SECONDS
            JOB_STATS["done"] += 1
            if results and zero_count == len(results):
                # Задание «готово», но все ответы нули: главный сигнал слепой зоны.
                JOB_STATS["all_zero_jobs"] += 1
                _account_touch(job.get("account_id", ""), "zero_job", finished_at)
            _job_log(
                "done", job_id,
                account_id=job.get("account_id", ""),
                client_request_id=job.get("client_request_id", ""),
                total_ms=round(max(0.0, finished_at - float(job.get("created_at", finished_at))) * 1000),
                result_count=len(results or []),
                zero_count=zero_count,
                result_ttl_seconds=JOB_RESULT_TTL_SECONDS,
                **_job_queue_metrics(job_id, job, finished_at),
            )
        except Exception as e:
            finished_at = time.time()
            job["status"] = "error"
            job["stage"] = "finished"
            job["error"] = str(e)
            job["finished_at"] = finished_at
            job["expires_at"] = finished_at + JOB_RESULT_TTL_SECONDS
            JOB_STATS["error"] += 1
            _account_touch(job.get("account_id", ""), "error", finished_at)
            _job_log(
                "error", job_id,
                account_id=job.get("account_id", ""),
                client_request_id=job.get("client_request_id", ""),
                error_type=type(e).__name__,
                error=str(e)[:500],
                result_ttl_seconds=JOB_RESULT_TTL_SECONDS,
                **_job_queue_metrics(job_id, job, finished_at),
            )
