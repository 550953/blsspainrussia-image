"""Эндпоинты распознавания: очередь (submit/result) и синхронные /ocr, /ocr/batch, /ocr/captcha6."""
import asyncio
import time
import uuid

from fastapi import APIRouter, HTTPException

from ..jobs import JOBS, _cleanup_expired_jobs, _job_public_payload, _job_queue_metrics, _run_job
from ..pipeline import process_images_gemini_sheet
from ..schemas import (
    Captcha6Request,
    Captcha6Response,
    JobResultResponse,
    JobSubmitResponse,
    OCRRequest,
    OCRResponse,
)
from ..telemetry import JOB_STATS, _account_touch, _job_log
from ..utils import clean_base64
from ..mailru_captcha import recognize_mailru_captcha

router = APIRouter()


@router.post(
    "/ocr/submit",
    response_model=JobSubmitResponse,
    summary="Поставить пачку изображений в очередь на распознавание",
    description=(
        "Асинхронный режим (аналог 2captcha): принимает изображения, сразу возвращает job_id "
        "и НЕ дожидается распознавания. Результат забирать через GET /ocr/result/{job_id}. "
        "Несколько клиентов могут слать сюда одновременно: нагрузку разруливает пул ключей Gemini."
    ),
    tags=["async queue"],
)
async def ocr_submit(req: OCRRequest):
    _cleanup_expired_jobs()
    images = req.images if isinstance(req.images, list) else [req.images]
    if not images:
        raise HTTPException(status_code=400, detail="Пустой список изображений")
    if len(images) > 500:
        raise HTTPException(status_code=400, detail="Слишком много изображений за один запрос (лимит 500)")

    job_id = uuid.uuid4().hex
    created_at = time.time()
    JOBS[job_id] = {
        "status": "pending",
        "stage": "queued",
        "created_at": created_at,
        "account_id": str(req.account_id or "")[:128],
        "client_request_id": str(req.client_request_id or "")[:128],
        "image_count": len(images),
        "poll_count": 0,
        "first_poll_at": None,
        "last_poll_at": None,
        "last_poll_log_at": None,
        "last_poll_log_status": None,
        "result_delivered_at": None,
        "expires_at": None,
        "job_id": job_id,
    }
    JOB_STATS["accepted"] += 1
    _account_touch(JOBS[job_id]["account_id"], "submit", created_at)
    _job_log(
        "accepted", job_id,
        account_id=JOBS[job_id]["account_id"],
        client_request_id=JOBS[job_id]["client_request_id"],
        image_count=len(images),
        jobs_in_memory=len(JOBS),
        **_job_queue_metrics(job_id, JOBS[job_id], created_at),
    )
    asyncio.create_task(_run_job(job_id, images))
    return JobSubmitResponse(job_id=job_id, status="pending")


@router.get(
    "/ocr/result/{job_id}",
    response_model=JobResultResponse,
    responses={404: {"description": "job_id не найден (не существовал или истёк TTL)"}},
    summary="Забрать результат задания по job_id",
    description=(
        "Опрашивать раз в 1.5-2 сек, пока status не станет 'done' или 'error'. Финальный результат "
        "остаётся доступен несколько минут, чтобы потерянный HTTP-ответ можно было забрать повторно."
    ),
    tags=["async queue"],
)
async def ocr_result(job_id: str):
    _cleanup_expired_jobs()
    job = JOBS.get(job_id)
    if job is None:
        _job_log("not_found", job_id)
        raise HTTPException(status_code=404, detail="unknown job_id")

    now = time.time()
    job["poll_count"] = int(job.get("poll_count") or 0) + 1
    if not job.get("first_poll_at"):
        job["first_poll_at"] = now
    job["last_poll_at"] = now
    _account_touch(job.get("account_id", ""), "poll", now)

    status = str(job.get("status") or "pending")
    last_log_at = job.get("last_poll_log_at")
    last_log_status = job.get("last_poll_log_status")
    if last_log_at is None or last_log_status != status or now - float(last_log_at) >= 10.0:
        _job_log(
            "poll", job_id,
            account_id=job.get("account_id", ""),
            client_request_id=job.get("client_request_id", ""),
            status=status,
            poll_count=job["poll_count"],
            **_job_queue_metrics(job_id, job, now),
        )
        job["last_poll_log_at"] = now
        job["last_poll_log_status"] = status

    if status in ("done", "error") and not job.get("result_delivered_at"):
        job["result_delivered_at"] = now
        JOB_STATS["result_delivered"] += 1
        _account_touch(job.get("account_id", ""), "delivered", now)
        _job_log(
            "result_delivered", job_id,
            account_id=job.get("account_id", ""),
            client_request_id=job.get("client_request_id", ""),
            final_status=status,
            poll_count=job["poll_count"],
        )

    # Финальный ответ остаётся до TTL: повторный опрос идемпотентен.
    return _job_public_payload(job)


@router.post("/ocr", response_model=OCRResponse)
async def ocr_endpoint(req: OCRRequest):
    images = req.images if isinstance(req.images, list) else [req.images]
    if len(images) > 500:
        raise HTTPException(status_code=400, detail="Слишком много изображений за один запрос (лимит 500)")
    return OCRResponse(results=await process_images_gemini_sheet(images))


@router.post("/ocr/batch", response_model=OCRResponse)
async def ocr_batch_endpoint(req: OCRRequest):
    images = req.images if isinstance(req.images, list) else [req.images]
    if not images:
        raise HTTPException(status_code=400, detail="Пустой список изображений")
    if len(images) > 500:
        raise HTTPException(status_code=400, detail="Слишком много изображений за один запрос (лимит 500)")
    return OCRResponse(results=await process_images_gemini_sheet(images))


@router.post(
    "/ocr/captcha6",
    response_model=Captcha6Response,
    summary="Распознать капчу mail.ru (6 alphanumeric)",
    description=(
        "Синхронный эндпоинт для одной картинки mail.ru-капчи. "
        "Ожидает base64 в поле image. Поле url опционально (только для логов). "
        "Возвращает 6 символов A-Z0-9 или пустую строку при ошибке."
    ),
    tags=["mail.ru"],
)
async def ocr_captcha6(req: Captcha6Request):
    image_bytes = clean_base64(req.image)
    if len(image_bytes) < 100:
        raise HTTPException(status_code=400, detail="image too small")
    if len(image_bytes) > 2_000_000:
        raise HTTPException(status_code=400, detail="image too large")

    account_id = str(req.account_id or "")[:128]
    client_request_id = str(req.client_request_id or "")[:128]
    captcha_url = (req.url or "")[:512]

    _job_log(
        "mailru_accepted",
        account_id=account_id,
        client_request_id=client_request_id,
        captcha_url=captcha_url or None,
        image_bytes=len(image_bytes),
    )

    text, source = await recognize_mailru_captcha(image_bytes)

    _job_log(
        "mailru_done",
        account_id=account_id,
        client_request_id=client_request_id,
        captcha_url=captcha_url or None,
        text=text or None,
        source=source,
    )

    return Captcha6Response(text=text or "", source=source)
