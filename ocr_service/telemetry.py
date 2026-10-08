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

ATTEMPTS: deque = deque(maxlen=20000)   # (ts, key, proxy, category, latency_ms, images, zeros, model)
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


def _job_log(event: str, job_id: str = "", **fields) -> None:
    """Одна компактная JSON-строка. Никогда не передавать сюда картинки, ключи, пароли, полные URL прокси."""
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
    payload.update(fields)
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
