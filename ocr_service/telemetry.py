"""Логи (JSON в stdout + Better Stack) и статистика в памяти для /status.json.

Всё в памяти, окно скользящее. Сюда нельзя передавать картинки, ключи, пароли, полные URL прокси.
"""
import asyncio
import json
import os
import time
from collections import Counter, deque
from typing import Optional

import httpx

from .config import BETTERSTACK_BEARER, BETTERSTACK_ENABLED, BETTERSTACK_SERVICE, BETTERSTACK_URL

JOB_STATS = Counter()
TEMPLATE_RUNS: deque = deque(maxlen=2000)
TEMPLATE_STATS = Counter()
TEMPLATE_API_CALLS: deque = deque(maxlen=20000)
TEMPLATE_STATS["date_utc"] = time.strftime("%Y-%m-%d", time.gmtime())
TEMPLATE_PROXY_ERROR_CATEGORIES = (
    "proxy_preflight_failed", "generation_timeout", "proxy_unavailable",
)


def record_template_proxy_error(category: str) -> None:
    if category in TEMPLATE_PROXY_ERROR_CATEGORIES:
        TEMPLATE_STATS[category] += 1


def ensure_template_day() -> None:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if TEMPLATE_STATS.get("date_utc") != today:
        for key in ("tokens_in", "tokens_out", "tokens_thoughts", "daily_tokens", "daily_jobs"):
            TEMPLATE_STATS[key] = 0
        TEMPLATE_STATS["date_utc"] = today


def record_template_run(job: dict, error_code: str = "") -> None:
    """Хранит только сводные показатели письма, не сам HTML или промпт."""
    now = time.time()
    ensure_template_day()
    template = job.get("template") or {}
    attempts = template.get("attempts") or []
    token_in = sum(int(item.get("tokens", {}).get("in", 0) or 0) for item in attempts)
    token_out = sum(int(item.get("tokens", {}).get("out", 0) or 0) for item in attempts)
    token_thoughts = sum(int(item.get("tokens", {}).get("thoughts", 0) or 0) for item in attempts)
    stats = template.get("report", {}).get("stats", {})
    TEMPLATE_RUNS.append((
        now, str(job.get("job_id", ""))[:8], bool(template.get("valid")),
        len(attempts), int((job.get("finished_at", now) - job.get("started_at", now)) * 1000),
        token_in, token_out, str(template.get("model", "")), int(stats.get("bytes", 0)),
        error_code, tuple(
            item.get("rule", "") for item in template.get("report", {}).get("errors", [])[:20]
            if isinstance(item, dict)
        ),
    ))
    if error_code:
        TEMPLATE_STATS["tpl_error"] += 1
    elif template.get("valid"):
        TEMPLATE_STATS["tpl_done_valid"] += 1
    else:
        TEMPLATE_STATS["tpl_done_invalid"] += 1


def record_template_usage(usage: dict) -> None:
    """Учёт токенов сразу после ответа API: лимит действует между ремонтными попытками."""
    ensure_template_day()
    incoming = int(usage.get("in", 0) or 0)
    outgoing = int(usage.get("out", 0) or 0)
    thoughts = int(usage.get("thoughts", 0) or 0)
    TEMPLATE_STATS["tokens_in"] += incoming
    TEMPLATE_STATS["tokens_out"] += outgoing
    TEMPLATE_STATS["tokens_thoughts"] += thoughts
    TEMPLATE_STATS["daily_tokens"] += incoming + outgoing + thoughts

ATTEMPTS: deque = deque(maxlen=20000)   # (ts, key, proxy, category, latency_ms, images, zeros, model)
DAILY_QUOTA = "DAILY_QUOTA"             # категория в ATTEMPTS для 429 «дневная квота исчерпана»
QUALITY: deque = deque(maxlen=20000)    # (ts, kind, n)
KEY_LAST: dict = {}                     # key_name -> {used, ok, err, err_cat, err_http, err_msg}
LAST_EVENT = {"attempt": None, "ok": None, "err": None}
ACCOUNTS: dict = {}                     # account_id -> счётчики и метки времени
ACCOUNTS_MAX = 500
NO_ACCOUNT = "(без id)"


def _record_attempt(p: dict) -> None:
    now = time.time()
    key = str(p.get("key_profile") or "?")
    category = str(p.get("category") or "?")
    if category == "RATE_LIMIT_OR_QUOTA" and p.get("daily_quota"):
        # Дневная квота: ожидаемый отказ, пара ключ+модель уже закрыта до сброса. Отдельная категория,
        # чтобы такие 429 не портили процент успеха и индекс давления.
        category = DAILY_QUOTA
    ATTEMPTS.append((
        now, key, str(p.get("proxy") or "DIRECT"), category, p.get("latency_ms"),
        int(p.get("image_count") or 0), int(p.get("zero_count") or 0),
        str(p.get("served_model") or p.get("model") or "?"),
    ))
    LAST_EVENT["attempt"] = now
    last = KEY_LAST.setdefault(key, {})
    last["used"] = now
    if category == "SUCCESS":
        last["ok"] = now
        LAST_EVENT["ok"] = now
        return
    LAST_EVENT["err"] = now
    if category != "PROXY_UNAVAILABLE":  # обрыв прокси это вина канала, а не ключа
        last["err"] = now
        last["err_cat"] = category
        last["err_http"] = p.get("http_status")
        last["err_msg"] = str(p.get("api_message") or "")[:300]


def _quality_note(kind: str, n: int = 1) -> None:
    if n:
        QUALITY.append((time.time(), kind, int(n)))


def _account_touch(account_id: str, event: str, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    acc_id = account_id or NO_ACCOUNT
    acc = ACCOUNTS.get(acc_id)
    if acc is None:
        if len(ACCOUNTS) >= ACCOUNTS_MAX:
            oldest = min(ACCOUNTS, key=lambda k: ACCOUNTS[k].get("last_seen") or 0)
            ACCOUNTS.pop(oldest, None)
        acc = ACCOUNTS[acc_id] = {
            "jobs": 0, "errors": 0, "zero_jobs": 0,
            "submit": None, "poll": None, "delivered": None, "last_seen": now,
        }
    acc["last_seen"] = now
    if event == "submit":
        acc["submit"] = now
        acc["jobs"] += 1
    elif event == "poll":
        acc["poll"] = now
    elif event == "delivered":
        acc["delivered"] = now
    elif event == "error":
        acc["errors"] += 1
    elif event == "zero_job":
        acc["zero_jobs"] += 1


def _percentile(values: list, q: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    return int(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))])


def record_template_api_call(fields: dict) -> None:
    """Keep only bounded, secret-free timing fields for actual template API calls."""
    row = {
        "ts": time.time(),
        "n": int(fields.get("n") or 0),
        "wait_ocr_ms": max(0, int(fields.get("wait_ocr_ms") or 0)),
        "wait_key_ms": max(0, int(fields.get("wait_key_ms") or 0)),
        "preflight_ms": max(0, int(fields.get("preflight_ms") or 0)),
        "call_ms": max(0, int(fields.get("call_ms") or 0)),
        "key_name": str(fields.get("key_name") or "")[:128],
        "proxy": str(fields.get("proxy") or "DIRECT")[:160],
        "model": str(fields.get("model") or "")[:100],
        "outcome": str(fields.get("outcome") or "unknown")[:40],
        "http_status": fields.get("http_status"),
    }
    TEMPLATE_API_CALLS.append(row)


def template_success_p50_ms(model: str) -> Optional[int]:
    cutoff = time.time() - 86400
    values = [
        item["call_ms"] for item in TEMPLATE_API_CALLS
        if item["ts"] >= cutoff and item["model"] == str(model)
        and item["outcome"] == "success" and item["call_ms"] > 0
    ][-200:]
    return _percentile(values, 0.5)


def _utc_iso(timestamp: Optional[float] = None) -> str:
    value = time.time() if timestamp is None else float(timestamp)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(value)) + f".{int(value * 1000) % 1000:03d}Z"


# ---------------------------------------------------------------------------
# Better Stack: очередь + фоновая задача. Создаются в start_betterstack() при старте приложения.
# ---------------------------------------------------------------------------
_BETTERSTACK_QUEUE: Optional[asyncio.Queue] = None
_BETTERSTACK_TASK: Optional[asyncio.Task] = None
_BETTERSTACK_CLIENT: Optional[httpx.AsyncClient] = None


async def _betterstack_loop() -> None:
    """Пересылает структурные события, не блокируя OCR."""
    if _BETTERSTACK_QUEUE is None or _BETTERSTACK_CLIENT is None:
        return
    while True:
        payload = await _BETTERSTACK_QUEUE.get()
        try:
            await _BETTERSTACK_CLIENT.post(
                BETTERSTACK_URL,
                headers={"Authorization": f"Bearer {BETTERSTACK_BEARER}", "Content-Type": "application/json"},
                json=payload,
            )
        except Exception:
            pass  # сбой телеметрии не должен ломать OCR и порождать новые события
        finally:
            _BETTERSTACK_QUEUE.task_done()


def start_betterstack() -> None:
    global _BETTERSTACK_QUEUE, _BETTERSTACK_TASK, _BETTERSTACK_CLIENT
    if BETTERSTACK_ENABLED:
        _BETTERSTACK_QUEUE = asyncio.Queue(maxsize=2000)
        _BETTERSTACK_CLIENT = httpx.AsyncClient(timeout=5.0)
        _BETTERSTACK_TASK = asyncio.create_task(_betterstack_loop())


async def stop_betterstack() -> None:
    global _BETTERSTACK_QUEUE, _BETTERSTACK_TASK, _BETTERSTACK_CLIENT
    if _BETTERSTACK_TASK is not None:
        _BETTERSTACK_TASK.cancel()
        _BETTERSTACK_TASK = None
    if _BETTERSTACK_CLIENT is not None:
        await _BETTERSTACK_CLIENT.aclose()
        _BETTERSTACK_CLIENT = None
    _BETTERSTACK_QUEUE = None


def betterstack_queue_size() -> int:
    return _BETTERSTACK_QUEUE.qsize() if _BETTERSTACK_QUEUE is not None else 0


_SENSITIVE_LOG_FIELDS = {
    "where", "html", "raw_html", "prompt", "prompt_text", "response",
    "response_text", "contents", "body", "request_body", "api_key",
    "key_value", "token", "tokens_text", "proxy_url", "full_url",
}


def _safe_log_fields(value):
    """Удаляет сырой контент и секреты даже если новый call site передал их случайно."""
    if isinstance(value, dict):
        return {
            key: _safe_log_fields(item)
            for key, item in value.items()
            if str(key).casefold() not in _SENSITIVE_LOG_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_log_fields(item) for item in value]
    return value


def _job_log(event: str, job_id: str = "", **fields) -> None:
    """Одна компактная JSON-строка. Никогда не передавать сюда картинки, ключи, пароли, полные URL прокси."""
    if not event.startswith("tpl_"):
        JOB_STATS[f"event_{event}"] += 1
    payload = {
        "ts": _utc_iso(),
        "pid": os.getpid(),
        "component": "ocr_job",
        "event": event,
        "service": BETTERSTACK_SERVICE,
        "level": "error" if event in {"error", "gemini_api_error"} else "info",
    }
    if job_id:
        payload["job_id"] = job_id
    payload.update(_safe_log_fields(fields))
    payload["message"] = f"{event} job={job_id}" if job_id else event
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)

    if event in ("gemini_attempt", "gemini_api_error", "gemini_proxy_error"):
        try:
            _record_attempt(payload)
        except Exception:
            pass  # статистика не должна ломать OCR

    if _BETTERSTACK_QUEUE is not None:
        try:
            _BETTERSTACK_QUEUE.put_nowait(dict(payload))
        except asyncio.QueueFull:
            JOB_STATS["betterstack_dropped"] += 1
