"""Данные для /status.json. Секретов здесь нет: только имена ключей и id клиентов."""
import os
import time
from collections import Counter
from typing import Optional

from .config import (
    BETTERSTACK_ENABLED,
    BETTERSTACK_SERVICE,
    GEMINI_SHEET_CHUNK_SIZE,
    GEMINI_VARIANT_RETRY_ENABLED,
    GEMINI_VARIANT_RETRY_MIN_VOTES,
    OCR_MAX_WORKERS,
    PROCESS_START_TIME,
)
from .jobs import JOB_RESULT_TTL_SECONDS, JOBS, _cleanup_expired_jobs
from .config import GEMINI_MODELS
from .key_pool import GEMINI_KEYS, gemini_model_name, gemini_pool
from .pipeline import GEMINI_SHEET_MAX_CONCURRENT_CHUNKS
from .proxies import _configured_proxies, _proxy_label, _proxy_source, smart_proxy_pool
from .telemetry import (
    ACCOUNTS,
    ATTEMPTS,
    JOB_STATS,
    KEY_LAST,
    LAST_EVENT,
    NO_ACCOUNT,
    QUALITY,
    _percentile,
    betterstack_queue_size,
)

STATS_WINDOW_SECONDS = 3600  # скользящее окно статистики (1 час)


def build_status() -> dict:
    _cleanup_expired_jobs()
    wall = time.time()
    mono = time.monotonic()
    cutoff = wall - STATS_WINDOW_SECONDS

    def ago(ts):
        return round(wall - ts, 1) if ts else None

    # --- попытки Gemini за окно: по ключам и по прокси ---
    per_key: dict = {}
    per_proxy: dict = {}
    per_model: dict = {}
    win = {"ok": 0, "api_err": 0, "n429": 0, "proxy_err": 0}
    for ts, key, proxy, category, latency, _images, _zeros, model in ATTEMPTS:
        if ts < cutoff:
            continue
        k = per_key.setdefault(key, {"ok": 0, "err": 0})
        p = per_proxy.setdefault(proxy, {"n": 0, "ok": 0, "n429": 0, "perr": 0, "lat": []})
        p["n"] += 1
        if category != "PROXY_UNAVAILABLE":  # до модели запрос не дошёл: ей не засчитываем
            m = per_model.setdefault(model, {"n": 0, "ok": 0, "n429": 0, "n5xx": 0, "lat": []})
            m["n"] += 1
            if category == "SUCCESS":
                m["ok"] += 1
                if latency is not None:
                    m["lat"].append(latency)
            elif category == "RATE_LIMIT_OR_QUOTA":
                m["n429"] += 1
            elif category == "UPSTREAM_UNAVAILABLE" or category in ("HTTP_500", "HTTP_502", "HTTP_504"):
                m["n5xx"] += 1
        if category == "SUCCESS":
            win["ok"] += 1
            k["ok"] += 1
            p["ok"] += 1
            if latency is not None:
                p["lat"].append(latency)
        elif category == "PROXY_UNAVAILABLE":
            win["proxy_err"] += 1
            p["perr"] += 1
        else:
            win["api_err"] += 1
            k["err"] += 1
            if category == "RATE_LIMIT_OR_QUOTA":
                win["n429"] += 1
                p["n429"] += 1
    win_total = win["ok"] + win["api_err"] + win["proxy_err"]

    # --- график за час: 12 корзин по 5 минут (старые слева) и последние ошибки ---
    BUCKET = 300
    timeline = [{"ok": 0, "err": 0, "proxy_err": 0} for _ in range(STATS_WINDOW_SECONDS // BUCKET)]
    recent_errors = []
    for ts, key, proxy, category, latency, _images, _zeros, model in ATTEMPTS:
        if ts < cutoff:
            continue
        i = min(len(timeline) - 1, int((ts - cutoff) // BUCKET))
        if category == "SUCCESS":
            timeline[i]["ok"] += 1
        elif category == "PROXY_UNAVAILABLE":
            timeline[i]["proxy_err"] += 1
        else:
            timeline[i]["err"] += 1
        if category != "SUCCESS":
            recent_errors.append({"ago": ago(ts), "key": key, "proxy": proxy, "category": category, "model": model})
    recent_errors = recent_errors[-12:][::-1]

    # --- ключи ---
    keys = []
    counts = Counter()
    for idx, name in enumerate(gemini_pool.key_names):
        state, category, cooldown_left = gemini_pool.key_state(idx, mono)
        counts[state] += 1
        last = KEY_LAST.get(name, {})
        stats = per_key.get(name, {"ok": 0, "err": 0})
        keys.append({
            "name": name, "state": state, "category": category, "cooldown_left": cooldown_left,
            "used_ago": ago(last.get("used")), "ok_ago": ago(last.get("ok")), "err_ago": ago(last.get("err")),
            "err_cat": last.get("err_cat", ""), "err_http": last.get("err_http"), "err_msg": last.get("err_msg", ""),
            "ok_1h": stats["ok"], "err_1h": stats["err"],
        })

    # --- прокси ---
    proxies = []
    for route in smart_proxy_pool.routes:
        label = _proxy_label(route)
        p = per_proxy.get(label, {"n": 0, "ok": 0, "n429": 0, "perr": 0, "lat": []})
        left = max(0.0, smart_proxy_pool.cooldown_until[route] - mono)
        reason = smart_proxy_pool.block_reason.get(route, "") if left > 0 else ""
        proxies.append({
            "label": label,
            "state": ("quarantine" if reason == "geo" else "cooldown") if left > 0 else "ready",
            "reason": reason, "geo_hits": smart_proxy_pool.geo_hits.get(route, 0),
            "cooldown_left": round(left, 1),
            "failures": smart_proxy_pool.failures[route], "attempts": p["n"],
            "ok_pct": round(100.0 * p["ok"] / p["n"], 1) if p["n"] else None,
            "n429": p["n429"], "proxy_err": p["perr"],
            "p50_ms": _percentile(p["lat"], 0.5), "p95_ms": _percentile(p["lat"], 0.95),
        })

    # --- модели: цепочка + всё, что реально отвечало за окно ---
    models = []
    names = list(GEMINI_MODELS) + [m for m in per_model if m not in GEMINI_MODELS and m != "?"]
    for name in names:
        m = per_model.get(name, {"n": 0, "ok": 0, "n429": 0, "n5xx": 0, "lat": []})
        left = max(0.0, gemini_pool.model_blocked_until.get(name, 0.0) - mono)
        models.append({
            "name": name, "in_chain": name in GEMINI_MODELS,
            "state": "bypass" if left > 0 else "ready", "bypass_left": round(left, 1),
            "attempts": m["n"], "ok_pct": round(100.0 * m["ok"] / m["n"], 1) if m["n"] else None,
            "n5xx": m["n5xx"], "n429": m["n429"], "p50_ms": _percentile(m["lat"], 0.5),
        })

    # --- качество за окно ---
    q = Counter()
    for ts, kind, n in QUALITY:
        if ts >= cutoff:
            q[kind] += n

    # --- задания ---
    pending = sorted(
        (j for j in JOBS.values() if str(j.get("status") or "pending") == "pending"),
        key=lambda j: (float(j.get("created_at") or 0.0), str(j.get("job_id"))),
    )
    position = {j["job_id"]: i for i, j in enumerate(pending, start=1)}
    running = [j for j in JOBS.values() if j.get("status") == "running"]

    def oldest_age(jobs):
        return round(max(0.0, wall - min(float(j.get("created_at", wall)) for j in jobs)), 1) if jobs else 0

    jobs = []
    undelivered = Counter()
    for job in sorted(JOBS.values(), key=lambda j: float(j.get("created_at") or 0.0), reverse=True)[:150]:
        status = str(job.get("status") or "pending")
        created = float(job.get("created_at") or wall)
        started = job.get("started_at")
        finished = job.get("finished_at")
        jobs.append({
            "id": str(job.get("job_id"))[:8],
            "account": job.get("account_id") or NO_ACCOUNT,
            "request": job.get("client_request_id") or "",
            "stage": {"pending": "queued", "running": "processing"}.get(status, status),
            "queue_position": position.get(job.get("job_id")),
            "images": job.get("image_count"),
            "zeros": job.get("zero_count"),
            "queue_wait_ms": round(((started or wall) - created) * 1000) if (started or status == "pending") else None,
            "processing_ms": round(((finished or wall) - float(started)) * 1000) if started else None,
            "age_s": round(wall - created, 1),
            "polls": int(job.get("poll_count") or 0),
            "last_poll_ago": ago(job.get("last_poll_at")),
            "delivered_ago": ago(job.get("result_delivered_at")),
            "expires_in": round(float(job["expires_at"]) - wall, 1) if job.get("expires_at") else None,
            "error": str(job.get("error") or "")[:200],
        })
    for job in JOBS.values():
        if job.get("status") in ("done", "error") and not job.get("result_delivered_at"):
            if wall - float(job.get("finished_at") or wall) > 30:  # 30 с на то, чтобы клиент успел прийти
                undelivered[job.get("account_id") or NO_ACCOUNT] += 1

    # --- клиенты ---
    clients = []
    for acc_id, acc in ACCOUNTS.items():
        seen = max((t for t in (acc["submit"], acc["poll"], acc["delivered"]) if t), default=None)
        if undelivered[acc_id]:
            status = "не забирает результат"
        elif seen is None or wall - seen > 900:
            status = "молчит"
        else:
            status = "активен"
        clients.append({
            "id": acc_id, "submit_ago": ago(acc["submit"]), "poll_ago": ago(acc["poll"]),
            "delivered_ago": ago(acc["delivered"]), "jobs": acc["jobs"], "errors": acc["errors"],
            "zero_jobs": acc["zero_jobs"], "undelivered": undelivered[acc_id], "status": status,
            "_seen": seen or 0,
        })
    clients.sort(key=lambda c: c["_seen"], reverse=True)
    for c in clients:
        c.pop("_seen")

    overloaded, overloaded_left = gemini_pool.is_overloaded()
    return {
        "now": wall,
        "pid": os.getpid(),
        "uptime_s": round(wall - PROCESS_START_TIME),
        "model": gemini_model_name,
        "window_s": STATS_WINDOW_SECONDS,
        "keys": {
            "total": len(keys),
            "usable": counts["ready"] + counts["in_use"],
            "by_state": dict(counts),
            "list": keys,
        },
        "queue": {
            "pending": len(pending), "running": len(running),
            "oldest_pending_s": oldest_age(pending), "oldest_running_s": oldest_age(running),
            "max_concurrency": max(len(GEMINI_KEYS), 1), "in_memory": len(JOBS),
            "result_ttl_s": JOB_RESULT_TTL_SECONDS,
            "pool_overloaded": overloaded, "pool_overloaded_left": round(overloaded_left, 1),
        },
        "attempts": {
            **win, "total": win_total,
            "ok_pct": round(100.0 * win["ok"] / win_total, 1) if win_total else None,
            "last_attempt_ago": ago(LAST_EVENT["attempt"]),
            "last_ok_ago": ago(LAST_EVENT["ok"]),
            "last_err_ago": ago(LAST_EVENT["err"]),
        },
        "quality": {
            "cells": q["cells"], "first_pass_zero": q["missing"], "variant_saved": q["variant_saved"],
            "dddd_used": q["dddd_used"], "dddd_saved": q["dddd_saved"], "final_zero": q["final_zero"],
            "all_zero_jobs_total": JOB_STATS["all_zero_jobs"],
        },
        "timeline": timeline,
        "recent_errors": recent_errors,
        "jobs": jobs,
        "clients": clients,
        "models": models,
        "proxies": {
            "mode": "proxied+direct_fallback" if _configured_proxies else "direct_only",
            "source": _proxy_source, "channels": len(smart_proxy_pool.routes), "list": proxies,
            "quarantined": sum(1 for x in proxies if x["state"] == "quarantine"),
        },
        "counters": {k: v for k, v in JOB_STATS.items() if not k.startswith("event_")},
        "config": {
            "chunk_size": GEMINI_SHEET_CHUNK_SIZE, "max_concurrent_chunks": GEMINI_SHEET_MAX_CONCURRENT_CHUNKS,
            "variant_retry": GEMINI_VARIANT_RETRY_ENABLED, "variant_min_votes": GEMINI_VARIANT_RETRY_MIN_VOTES,
            "ocr_max_workers": OCR_MAX_WORKERS,
            "betterstack": BETTERSTACK_ENABLED, "betterstack_service": BETTERSTACK_SERVICE,
            "betterstack_queue": betterstack_queue_size(),
        },
        "supabase": {"enabled": False},
    }
