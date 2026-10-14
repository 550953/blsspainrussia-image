"""Закрытые API-эндпоинты генерации и проверки HTML-писем."""
import asyncio
import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import (
    TEMPLATE_API_KEYS, TEMPLATE_ENABLED, TEMPLATE_HTML_ERROR_BYTES, TEMPLATE_MAX_ATTEMPTS,
    TEMPLATE_MAX_CONCURRENCY, TEMPLATE_MAX_HTML_BYTES, TEMPLATE_MAX_PENDING,
    TEMPLATE_BREAKER_FAILS, TEMPLATE_BREAKER_PAUSE_SECONDS, TEMPLATE_DAILY_CAP,
    TEMPLATE_DAILY_TOKEN_CAP, TEMPLATE_MODELS, TEMPLATE_QUEUE_MAX_WAIT_SECONDS, TEMPLATE_JOB_DEADLINE_SECONDS,
    TEMPLATE_RESULT_TTL_SECONDS, TEMPLATE_RESERVE_KEYS_FOR_OCR,
    TEMPLATE_KEEP_ATTEMPT_HTML, TEMPLATE_STORE_MAX, TEMPLATE_SUBMIT_RATE_PER_MIN, TEMPLATE_VALIDATE_RATE_PER_MIN, TEMPLATE_HISTORY,
)
from ..email_template.director import FONTS, HEADERS, INDICATORS, PALETTES, make_brief, set_history_size
from ..email_template.generate import run_template_job
from ..email_template.prompt import PROMPT_VERSION
from ..email_template.preview import render
from ..email_template.runner import run_template_selfcheck
from ..email_template.validator import rules, validate
from ..key_pool import gemini_pool
from ..telemetry import TEMPLATE_STATS, _job_log, ensure_template_day, record_template_run
from ..task_kinds import TaskKind, get as get_task_kind, register
from .status import require_status_auth

router = APIRouter()
TEMPLATE_JOBS = {}
TEMPLATE_TASKS = set()
_TEMPLATE_TASK_BY_ID = {}
_IDEMPOTENCY = {}
_RATE = defaultdict(deque)
_SEMAPHORE = None
_BREAKER_FAILURES = 0
_BREAKER_UNTIL = 0.0

set_history_size(TEMPLATE_HISTORY)


register(TaskKind(
    name="email_template",
    runner=run_template_job,
    deadline_s=lambda: TEMPLATE_JOB_DEADLINE_SECONDS,
    deadline_from="started",
    queue_max_wait_s=TEMPLATE_QUEUE_MAX_WAIT_SECONDS,
    watchdog_grace_s=15,
    result_ttl_s=lambda: TEMPLATE_RESULT_TTL_SECONDS,
    max_concurrency=lambda: effective_concurrency(),
    store_max=lambda: TEMPLATE_STORE_MAX,
    stats_prefix="tpl_",
))


class BriefOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    palette: Optional[str] = Field(None, max_length=180)
    fonts: Optional[str] = Field(None, max_length=60)
    header: Optional[str] = Field(None, max_length=180)
    indicator: Optional[str] = Field(None, max_length=180)


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field("", max_length=128)
    client_request_id: str = Field("", max_length=128)
    seed: Optional[int] = None
    currency_method: Optional[int] = Field(None, ge=1, le=20)
    brief_overrides: BriefOverrides = Field(default_factory=BriefOverrides)
    model: Optional[str] = Field(None, max_length=100)
    max_attempts: Optional[int] = Field(None, ge=1, le=5)


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html: str
    currency_method: Optional[int] = Field(None, ge=1, le=20)


def _clean_store():
    now = time.time()
    expired = [job_id for job_id, job in TEMPLATE_JOBS.items()
               if job.get("expires_at") and now >= job["expires_at"]]
    for job_id in expired:
        TEMPLATE_JOBS.pop(job_id, None)
        _job_log("tpl_expired", job_id, component="email_template")
    for key, job_id in list(_IDEMPOTENCY.items()):
        if job_id not in TEMPLATE_JOBS:
            _IDEMPOTENCY.pop(key, None)
    complete = sorted(
        ((float(job.get("finished_at") or 0), job_id) for job_id, job in TEMPLATE_JOBS.items()
         if job.get("status") in {"done", "error"}),
    )
    while len(complete) > TEMPLATE_STORE_MAX:
        _, job_id = complete.pop(0)
        TEMPLATE_JOBS.pop(job_id, None)
        _job_log("tpl_evicted", job_id, component="email_template", reason="store_limit")


def _auth(request: Request) -> str:
    if not TEMPLATE_API_KEYS:
        raise HTTPException(status_code=503, detail="TEMPLATE_API_KEYS не заданы")
    supplied = request.headers.get("X-API-Key", "")
    for value in TEMPLATE_API_KEYS:
        if secrets.compare_digest(supplied.encode("utf-8"), value.encode("utf-8")):
            return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    raise HTTPException(status_code=401, detail="Invalid API key")


def _rate_limit(namespace: str, limit: int):
    now = time.monotonic()
    items = _RATE[namespace]
    while items and now - items[0] >= 60:
        items.popleft()
    if len(items) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": "60"})
    items.append(now)


def _count_active():
    return sum(1 for job in TEMPLATE_JOBS.values() if job.get("status") in {"pending", "running"})


def _log_rejection(reason: str, *, limited: bool = False):
    fields = {"component": "email_template", "reason": reason}
    _job_log("tpl_rejected", **fields)
    if limited:
        _job_log("tpl_rejected_limit", **fields)


def _set_basic_error_detail(job: dict, code: str, category: str, safe_message: str):
    job.setdefault("error_detail", {
        "category": category,
        "http_status": None,
        "provider_status": "",
        "safe_message": safe_message[:200],
        "model": job.get("model") or (job.get("models") or [None])[0],
        "key_name": None,
        "api_attempts": 0,
        "proxy_preflight_failures": 0,
        "attempt_times_ms": [],
        "error_code": code,
    })


def effective_concurrency() -> int:
    live = sum(
        1 for idx in range(len(gemini_pool.keys))
        if not gemini_pool.dead[idx] and not gemini_pool._is_operator_hold(idx)
    )
    reserve = min(TEMPLATE_RESERVE_KEYS_FOR_OCR, max(live - 1, 0))
    return max(1, min(TEMPLATE_MAX_CONCURRENCY, max(live - reserve, 1)))


async def _run_queued(job_id: str):
    global _SEMAPHORE, _BREAKER_FAILURES, _BREAKER_UNTIL
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(effective_concurrency())
    job = TEMPLATE_JOBS.get(job_id)
    if job is None:
        return
    job["stage"] = "queued"
    wait_seconds = max(0.1, TEMPLATE_QUEUE_MAX_WAIT_SECONDS)
    try:
        await asyncio.wait_for(_SEMAPHORE.acquire(), timeout=wait_seconds)
    except asyncio.TimeoutError:
        job.update(status="error", stage="finished", error="queue_timeout",
                   finished_at=time.time(), expires_at=time.time() + TEMPLATE_RESULT_TTL_SECONDS)
        _set_basic_error_detail(job, "queue_timeout", "QUEUE_TIMEOUT", "Задание не попало в очередь вовремя.")
        record_template_run(job, "queue_timeout")
        _job_log("tpl_error", job_id, component="email_template", error="queue_timeout")
        return
    except asyncio.CancelledError:
        job.update(status="error", stage="finished", error="cancelled",
                   finished_at=time.time(), expires_at=time.time() + TEMPLATE_RESULT_TTL_SECONDS)
        _set_basic_error_detail(job, "cancelled", "CANCELLED", "Задание отменено.")
        _job_log("tpl_error", job_id, component="email_template", error="cancelled")
        return
    try:
        job["started_at"] = time.time()
        kind = get_task_kind("email_template")
        await asyncio.wait_for(kind.runner(job_id, job), timeout=max(1.0, kind.deadline_s()))
    except asyncio.TimeoutError:
        if job.get("status") not in {"done", "error"}:
            now = time.time()
            if job.get("template"):
                job["template"].setdefault("generation_error", "deadline")
                job.update(status="done", stage="finished", error=None,
                           finished_at=now, expires_at=now + TEMPLATE_RESULT_TTL_SECONDS)
                record_template_run(job)
                _job_log("tpl_done", job_id, component="email_template",
                         valid=job["template"].get("valid", False),
                         attempts=len(job["template"].get("attempts", [])),
                         bytes=len((job["template"].get("html") or "").encode("utf-8")))
            else:
                job.update(status="error", stage="finished", error="deadline",
                           finished_at=now, expires_at=now + TEMPLATE_RESULT_TTL_SECONDS)
                _set_basic_error_detail(job, "deadline", "DEADLINE", "Истёк срок выполнения задания.")
                record_template_run(job, "deadline")
                _job_log("tpl_error", job_id, component="email_template", error="deadline")
    except asyncio.CancelledError:
        if job.get("status") not in {"done", "error"}:
            now = time.time()
            if job.get("template"):
                job["template"].setdefault("generation_error", "cancelled")
                job.update(status="done", stage="finished", error=None,
                           finished_at=now, expires_at=now + TEMPLATE_RESULT_TTL_SECONDS)
                record_template_run(job)
            else:
                job.update(status="error", stage="finished", error="cancelled",
                           finished_at=now, expires_at=now + TEMPLATE_RESULT_TTL_SECONDS)
                _set_basic_error_detail(job, "cancelled", "CANCELLED", "Задание отменено.")
                record_template_run(job, "cancelled")
        raise
    except Exception as exc:
        if job.get("status") not in {"done", "error"}:
            now = time.time()
            _set_basic_error_detail(
                job, "generation_unavailable", "UNKNOWN", "Неизвестная ошибка генерации.",
            )
            if job.get("template"):
                job["template"].setdefault("generation_error", "generation_unavailable")
                job.update(status="done", stage="finished", error=None,
                           finished_at=now, expires_at=now + TEMPLATE_RESULT_TTL_SECONDS)
                record_template_run(job)
            else:
                job.update(status="error", stage="finished", error="generation_unavailable",
                           finished_at=now, expires_at=now + TEMPLATE_RESULT_TTL_SECONDS)
                record_template_run(job, "generation_unavailable")
        if job.get("status") == "done":
            _job_log("tpl_done", job_id, component="email_template",
                     valid=job.get("template", {}).get("valid", False),
                     attempts=len(job.get("template", {}).get("attempts", [])),
                     bytes=len((job.get("template", {}).get("html") or "").encode("utf-8")))
        else:
            _job_log("tpl_error", job_id, component="email_template", error_type=type(exc).__name__)
    finally:
        _SEMAPHORE.release()
    if job.get("status") == "error":
        _BREAKER_FAILURES += 1
        if _BREAKER_FAILURES >= TEMPLATE_BREAKER_FAILS:
            _BREAKER_UNTIL = time.monotonic() + TEMPLATE_BREAKER_PAUSE_SECONDS
    elif job.get("status") == "done":
        _BREAKER_FAILURES = 0
        _BREAKER_UNTIL = 0.0


async def _read_model(request: Request, model, max_bytes: int):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Request body exceeds the endpoint size limit")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="Request body exceeds the endpoint size limit")
    try:
        if hasattr(model, "model_validate_json"):
            return model.model_validate_json(raw)
        return model.parse_raw(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors(include_input=False, include_url=False))


@router.post("/template/submit")
async def submit(request: Request):
    namespace = _auth(request)
    req = await _read_model(request, SubmitRequest, 4096)
    if not TEMPLATE_ENABLED:
        _log_rejection("disabled")
        raise HTTPException(status_code=503, detail="Генерация шаблонов выключена")
    if not TEMPLATE_MODELS:
        _log_rejection("models_not_configured")
        raise HTTPException(status_code=503, detail="TEMPLATE_MODELS не настроены")
    if not gemini_pool.keys:
        _log_rejection("no_keys")
        raise HTTPException(status_code=503, detail="Нет доступных ключей Gemini")
    if req.max_attempts and req.max_attempts > TEMPLATE_MAX_ATTEMPTS:
        raise HTTPException(status_code=400, detail="max_attempts превышает TEMPLATE_MAX_ATTEMPTS")
    if req.model and req.model not in TEMPLATE_MODELS:
        raise HTTPException(status_code=400, detail="Модель отсутствует в белом списке TEMPLATE_MODELS")
    overrides = req.brief_overrides.model_dump(exclude_none=True)
    pools = {"palette": PALETTES, "fonts": FONTS, "header": HEADERS, "indicator": INDICATORS}
    for field, value in overrides.items():
        pool = pools[field]
        allowed = (set(pool) | set(pool.values())) if isinstance(pool, dict) else set(pool)
        if value not in allowed:
            raise HTTPException(status_code=400, detail="Недопустимый параметр дизайна: " + field)
    _clean_store()
    identity = (namespace, req.account_id, req.client_request_id) if req.client_request_id else None
    if identity and identity in _IDEMPOTENCY:
        job = TEMPLATE_JOBS.get(_IDEMPOTENCY[identity])
        if job and (job.get("status") in {"pending", "running"} or
                    (job.get("expires_at") and time.time() < job["expires_at"])):
            return {
                "job_id": job["job_id"], "status": job["status"], "kind": "email_template",
                "reused": True,
                "brief": {key: value for key, value in job["brief"].items() if key != "avoid_palettes"},
                "prompt_version": PROMPT_VERSION, "schema_version": 1,
            }
    if time.monotonic() < _BREAKER_UNTIL:
        remaining = max(1, int(_BREAKER_UNTIL - time.monotonic()))
        _log_rejection("breaker_open")
        raise HTTPException(status_code=503, detail="Генерация временно приостановлена после серии ошибок",
                            headers={"Retry-After": str(remaining)})
    if _count_active() >= TEMPLATE_MAX_PENDING:
        _log_rejection("queue_full", limited=True)
        raise HTTPException(status_code=429, detail="Очередь генерации заполнена", headers={"Retry-After": "30"})
    ensure_template_day()
    if int(TEMPLATE_STATS.get("daily_jobs", 0)) >= TEMPLATE_DAILY_CAP:
        _log_rejection("daily_job_cap", limited=True)
        raise HTTPException(status_code=429, detail="Достигнут суточный лимит заданий", headers={"Retry-After": "3600"})
    token_usage = sum(int(TEMPLATE_STATS.get(key, 0)) for key in ("tokens_in", "tokens_out", "tokens_thoughts"))
    if token_usage >= TEMPLATE_DAILY_TOKEN_CAP:
        _log_rejection("daily_token_cap", limited=True)
        raise HTTPException(status_code=429, detail={"reason": "daily_token_cap", "message": "Достигнут суточный лимит токенов"},
                            headers={"Retry-After": "3600"})
    try:
        _rate_limit("submit:" + namespace, TEMPLATE_SUBMIT_RATE_PER_MIN)
    except HTTPException:
        _log_rejection("submit_rate_limit", limited=True)
        raise
    try:
        brief = make_brief(req.seed, req.currency_method, overrides)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    job_id = uuid.uuid4().hex
    now = time.time()
    job = {
        "job_id": job_id, "status": "pending", "stage": "queued", "kind": "email_template",
        "created_at": now, "expires_at": None, "account_id": req.account_id,
        "client_request_id": req.client_request_id, "namespace": namespace, "brief": brief,
        "models": list(TEMPLATE_MODELS), "model": req.model,
        "max_attempts": min(req.max_attempts or TEMPLATE_MAX_ATTEMPTS, TEMPLATE_MAX_ATTEMPTS),
        "deadline_s": TEMPLATE_JOB_DEADLINE_SECONDS,
    }
    TEMPLATE_JOBS[job_id] = job
    if identity:
        _IDEMPOTENCY[identity] = job_id
    TEMPLATE_STATS["tpl_accepted"] += 1
    TEMPLATE_STATS["daily_jobs"] += 1
    task = asyncio.create_task(_run_queued(job_id))
    TEMPLATE_TASKS.add(task)
    _TEMPLATE_TASK_BY_ID[job_id] = task

    def _finished(completed, item_id=job_id):
        TEMPLATE_TASKS.discard(completed)
        if _TEMPLATE_TASK_BY_ID.get(item_id) is completed:
            _TEMPLATE_TASK_BY_ID.pop(item_id, None)

    task.add_done_callback(_finished)
    _job_log("tpl_accepted", job_id, component="email_template", account_id=req.account_id,
             client_request_id=req.client_request_id, model=req.model or TEMPLATE_MODELS[0])
    return {
        "job_id": job_id, "status": "pending", "kind": "email_template", "reused": False,
        "brief": {key: value for key, value in brief.items() if key != "avoid_palettes"},
        "prompt_version": PROMPT_VERSION, "schema_version": 1,
    }


@router.get("/template/result/{job_id}")
async def result(job_id: str, request: Request, html: int = 1, attempts_html: int = 0):
    namespace = _auth(request)
    _clean_store()
    job = TEMPLATE_JOBS.get(job_id)
    if job is None or job.get("namespace") != namespace:
        raise HTTPException(status_code=404, detail="unknown job_id")
    status = job.get("status", "pending")
    payload = {
        "status": status, "kind": "email_template", "stage": job.get("stage", "queued"),
        "attempt": job.get("attempt", 0), "max_attempts": job.get("max_attempts"),
        "job_id": job_id, "schema_version": 1,
        "poll_after_ms": 4000 if status == "running" else (2000 if status == "pending" else None),
        "queue_position": sum(1 for other in TEMPLATE_JOBS.values()
                              if other.get("status") == "pending" and other.get("created_at", 0) < job.get("created_at", 0)) + 1
        if status == "pending" else None,
        "queued_jobs": sum(1 for other in TEMPLATE_JOBS.values() if other.get("status") == "pending"),
        "running_jobs": sum(1 for other in TEMPLATE_JOBS.values() if other.get("status") == "running"),
        "max_concurrency": effective_concurrency(),
        "job_age_ms": round((time.time() - float(job.get("created_at", time.time()))) * 1000),
        "queue_wait_ms": round((float(job.get("started_at", time.time())) - float(job.get("created_at", time.time()))) * 1000)
        if job.get("started_at") else None,
        "processing_ms": round((float(job.get("finished_at", time.time())) - float(job.get("started_at", time.time()))) * 1000)
        if job.get("started_at") else None,
        "template": job.get("template"),
        "error": job.get("error"),
        "error_detail": job.get("error_detail"),
    }
    if html == 0 and payload["template"]:
        payload["template"] = dict(payload["template"])
        payload["template"].pop("html", None)
        payload["template"].pop("text", None)
    if attempts_html and TEMPLATE_KEEP_ATTEMPT_HTML and job.get("attempts_html"):
        payload["attempts_html"] = job["attempts_html"]
    return payload


@router.get("/template/selfcheck", dependencies=[Depends(require_status_auth)])
async def selfcheck():
    if not TEMPLATE_ENABLED:
        raise HTTPException(status_code=503, detail="Генерация шаблонов выключена")
    _rate_limit("selfcheck", 6)
    return await run_template_selfcheck(TEMPLATE_MODELS[0] if TEMPLATE_MODELS else "")


@router.post("/template/cancel/{job_id}")
async def cancel(job_id: str, request: Request):
    namespace = _auth(request)
    job = TEMPLATE_JOBS.get(job_id)
    if job is None or job.get("namespace") != namespace:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job.get("status") not in {"pending", "running"}:
        return {"job_id": job_id, "status": job.get("status"), "schema_version": 1}
    task = _TEMPLATE_TASK_BY_ID.get(job_id)
    was_started = bool(job.get("started_at"))
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if job.get("status") in {"pending", "running"}:
        now = time.time()
        job.update(status="error", stage="finished", error="cancelled",
                   finished_at=now, expires_at=now + TEMPLATE_RESULT_TTL_SECONDS)
        _set_basic_error_detail(job, "cancelled", "CANCELLED", "Задание отменено.")
    if not was_started and job.get("status") == "error" and job.get("error") == "cancelled":
        record_template_run(job, "cancelled")
    _job_log("tpl_cancelled", job_id, component="email_template", status=job.get("status"))
    return {
        "job_id": job_id, "status": job.get("status"), "error": job.get("error"),
        "schema_version": 1,
    }


@router.post("/template/validate")
async def validate_html(request: Request):
    namespace = _auth(request)
    _rate_limit("validate:" + namespace, TEMPLATE_VALIDATE_RATE_PER_MIN)
    body = await _read_model(request, ValidateRequest, TEMPLATE_MAX_HTML_BYTES + 1024)
    size = len(body.html.encode("utf-8"))
    if size > TEMPLATE_MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail="HTML exceeds maximum size")
    report = await asyncio.to_thread(validate, body.html, body.currency_method)
    return {"report": report.to_dict(), "schema_version": 1}


@router.get("/template/rules")
async def get_rules(request: Request):
    _auth(request)
    return {"rules": rules(), "schema_version": 1}


@router.get("/template/result/{job_id}/preview")
async def preview(job_id: str, raw: int = 0, _auth=Depends(require_status_auth)):
    _clean_store()
    job = TEMPLATE_JOBS.get(job_id)
    if job is None or not job.get("template"):
        raise HTTPException(status_code=404, detail="Template not found")
    template = job["template"]
    source = template.get("html") or ""
    if not raw:
        source = render(source)
    response_headers = {
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        "X-Template-Valid": "1" if template.get("valid") else "0",
    }
    return HTMLResponse(source, headers=response_headers)
