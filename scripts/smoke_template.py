#!/usr/bin/env python3
"""Offline end-to-end smoke tests for email-template generation and proxy recovery.

Run from the repository root:
    PYTHONPATH=. python scripts/smoke_template.py

Uses the real template runner, generator, HTTP client, key pool, and queue semaphore.
Only the Gemini HTTP server and outbound HTTP proxies are local fakes; no external
API key, proxy, or network access is used.
"""
from __future__ import annotations

import asyncio
import copy
import http.client
import json
import os
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ocr_service import config, gemini_client, telemetry
from ocr_service.email_template import generate, runner
from ocr_service.key_pool import gemini_pool
from ocr_service.proxies import GeminiSmartProxyPool

# Some lightweight development images omit OpenCV, although deployment requirements
# include it. The OCR modules import cv2 at module load, but this smoke test never
# calls the OCR image path, so provide an import-only stub if the optional wheel is absent.
try:
    import cv2  # noqa: F401
except ImportError:
    sys.modules["cv2"] = types.ModuleType("cv2")
try:
    import numpy  # noqa: F401
except ImportError:
    numpy_stub = types.ModuleType("numpy")
    numpy_stub.ndarray = object
    numpy_stub.uint8 = object
    sys.modules["numpy"] = numpy_stub
try:
    import ddddocr  # noqa: F401
except ImportError:
    ddddocr_stub = types.ModuleType("ddddocr")
    ddddocr_stub.DdddOcr = type("DdddOcr", (), {"__init__": lambda self, *args, **kwargs: None})
    sys.modules["ddddocr"] = ddddocr_stub

from ocr_service.routers import template as template_router


ROOT = Path(__file__).resolve().parents[1]
GOOD_HTML = (ROOT / "tests" / "fixtures" / "good.html").read_text(encoding="utf-8")
SMOKE_API_KEY = "local-smoke-not-a-secret"
MODEL = "local-smoke-model"
SMOKE_READ_TIMEOUT = float(os.getenv("TEMPLATE_SMOKE_READ_TIMEOUT", "0.15"))

ACTIVE_EVENTS: list[tuple[float, str]] = []
ACTIVE_LOCK = threading.Lock()
FAKE_GENERATE_COUNT = 0
FAKE_503_REMAINING = 0
FAKE_ERROR_QUEUE: list[dict] = []
FAKE_TEXT_QUEUE: list[str] = []
FAKE_DELAY_QUEUE: list[float] = []
FAKE_REQUEST_SEEN = threading.Event()
UPSTREAM_PORT = 0
RUNNER_API_ATTEMPTS = 0


def event(message: str) -> None:
    with ACTIVE_LOCK:
        ACTIVE_EVENTS.append((time.monotonic(), message))


app = FastAPI()


@app.get("/health")
async def fake_health():
    event("fake Gemini GET /health → 200")
    return {"ok": True}


@app.post("/v1beta/models/{model}:generateContent")
async def fake_generate(model: str):
    global FAKE_GENERATE_COUNT, FAKE_503_REMAINING
    FAKE_REQUEST_SEEN.set()
    FAKE_GENERATE_COUNT += 1
    event(f"fake Gemini generateContent #{FAKE_GENERATE_COUNT} model={model}")
    if FAKE_DELAY_QUEUE:
        delay = FAKE_DELAY_QUEUE.pop(0)
        if delay:
            event(f"fake Gemini response delayed {delay:.2f}s")
            await asyncio.sleep(delay)
            event("fake Gemini delayed response completed")
    if FAKE_ERROR_QUEUE:
        response = FAKE_ERROR_QUEUE.pop(0)
        event(f"fake Gemini → {response['status']} {response['provider_status']}")
        return JSONResponse(
            status_code=response["status"],
            content={
                "error": {
                    "code": response["status"],
                    "status": response["provider_status"],
                    "message": response["message"],
                },
            },
        )
    if FAKE_503_REMAINING:
        FAKE_503_REMAINING -= 1
        event("fake Gemini → 503 UNAVAILABLE")
        return JSONResponse(
            status_code=503,
            content={"error": {"code": 503, "status": "UNAVAILABLE", "message": "local smoke overload"}},
        )
    text = FAKE_TEXT_QUEUE.pop(0) if FAKE_TEXT_QUEUE else GOOD_HTML + "\n№ 1"
    return {
        "modelVersion": model,
        "candidates": [{
            "content": {"parts": [{"text": text}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 880,
            "thoughtsTokenCount": 64,
            "cachedContentTokenCount": 0,
        },
    }


class LocalGeminiServer:
    def __init__(self):
        global UPSTREAM_PORT
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        UPSTREAM_PORT = probe.getsockname()[1]
        probe.close()
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=UPSTREAM_PORT, log_level="critical",
            access_log=False, lifespan="off",
        ))
        self.thread = threading.Thread(target=self.server.run, name="smoke-gemini", daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.server.started:
                return
            time.sleep(0.01)
        raise RuntimeError("local fake Gemini server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=3)


class LocalProxyHandler(BaseHTTPRequestHandler):
    behavior = "healthy"
    request_seen = None
    stall_seen = None
    post_count = 0
    post_count_lock = threading.Lock()

    def log_message(self, *_args):
        pass

    def _serve(self):
        target = urlsplit(self.path)
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        event(f"proxy {self.behavior}: {self.command} {path}")
        is_generate_content = (
            self.command == "POST"
            and ":generateContent" in target.path
            and ":streamGenerateContent" not in target.path
        )
        post_number = None
        if is_generate_content:
            handler_type = type(self)
            with handler_type.post_count_lock:
                post_number = handler_type.post_count
                handler_type.post_count += 1
            if self.request_seen is not None:
                self.request_seen.set()
        if self.behavior == "blackhole":
            time.sleep(1.5)
            return
        if self.behavior == "stall_generate" and is_generate_content and post_number >= 1:
            event("proxy stall_generate: second generateContent call held open")
            if self.stall_seen is not None:
                self.stall_seen.set()
            time.sleep(8)
            return
        if self.behavior == "dies_mid_response" and self.command == "POST":
            self.connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2048\r\nConnection: close\r\n\r\n{\"candidates\":"
            )
            return
        length = int(self.headers.get("content-length", "0") or 0)
        body = self.rfile.read(length) if length else None
        request_headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "proxy-connection", "transfer-encoding"}
        }
        conn = http.client.HTTPConnection("127.0.0.1", UPSTREAM_PORT, timeout=5)
        try:
            conn.request(self.command, path, body=body, headers=request_headers)
            response = conn.getresponse()
            response_body = response.read()
            self.send_response(response.status, response.reason)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(response_body)
            except (BrokenPipeError, ConnectionResetError):
                event("proxy client closed before delayed response was forwarded")
        finally:
            conn.close()

    do_GET = _serve
    do_POST = _serve


class LocalProxy:
    def __init__(self, behavior: str, request_seen=None, stall_seen=None):
        handler = type(
            f"SmokeProxyHandler_{behavior}",
            (LocalProxyHandler,),
            {
                "behavior": behavior, "request_seen": request_seen,
                "stall_seen": stall_seen, "post_count": 0,
                "post_count_lock": threading.Lock(),
            },
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name=f"proxy-{behavior}", daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


def closed_proxy_url() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def reset_keys(count: int = 1):
    gemini_pool.keys = [f"{SMOKE_API_KEY}-{index}" for index in range(count)]
    gemini_pool.key_names = [f"GEMINI_API_KEY_SMOKE_{index + 1}" for index in range(count)]
    gemini_pool.blocked = [0.0] * count
    gemini_pool.reserved = [False] * count
    gemini_pool.dead = [False] * count
    gemini_pool.last_used = [0.0] * count
    gemini_pool.reserved_at = [0.0] * count
    gemini_pool.last_error = [None] * count
    gemini_pool.lease_ttl = [300.0] * count
    gemini_pool.blackout_until = 0.0
    gemini_pool.rr_index = 0
    gemini_pool.model_blocked_until = {}
    gemini_pool.model_unavailable = set()
    gemini_pool.key_model_blocked = {}
    gemini_pool.key_model_daily_blocked = {}


def make_job(job_id: str, **overrides):
    job = {
        "job_id": job_id,
        "status": "pending",
        "stage": "queued",
        "namespace": "smoke",
        "created_at": time.time(),
        "deadline_s": 420,
        "max_attempts": 1,
        "models": [MODEL],
        "model": MODEL,
        "brief": {
            "currency_method": 1,
            "palette_family": "green",
            "fonts": "Georgia + Verdana",
            "header": "header",
            "indicator": "indicator",
            "avoid_palettes": [],
            "seed": 42,
        },
    }
    job.update(overrides)
    return job


def install_events():
    def log_event(name, job_id=None, **fields):
        detail = " ".join(
            f"{key}={value}" for key, value in fields.items()
            if key in {
                "category", "error_type", "attempt", "http_status",
                "provider_status", "key_name", "safe_message",
            }
        )
        event(f"{name} job={job_id}" + (f" {detail}" if detail else ""))
    runner._job_log = log_event
    generate._job_log = log_event
    template_router._job_log = log_event


REAL_GENERATION_CALL = runner.call_gemini_generate


async def tracked_generation_call(**kwargs):
    global RUNNER_API_ATTEMPTS
    RUNNER_API_ATTEMPTS += 1
    event(f"runner started Gemini attempt #{RUNNER_API_ATTEMPTS} after key lease")
    return await REAL_GENERATION_CALL(**kwargs)


def prepare_case(proxies: list[str], key_count: int = 1):
    global ACTIVE_EVENTS, FAKE_GENERATE_COUNT, FAKE_503_REMAINING
    global FAKE_ERROR_QUEUE, FAKE_TEXT_QUEUE, FAKE_DELAY_QUEUE
    global FAKE_REQUEST_SEEN, RUNNER_API_ATTEMPTS
    with ACTIVE_LOCK:
        ACTIVE_EVENTS = []
    FAKE_GENERATE_COUNT = 0
    FAKE_503_REMAINING = 0
    FAKE_ERROR_QUEUE = []
    FAKE_TEXT_QUEUE = []
    FAKE_DELAY_QUEUE = []
    FAKE_REQUEST_SEEN = threading.Event()
    RUNNER_API_ATTEMPTS = 0
    reset_keys(key_count)
    pool = GeminiSmartProxyPool([])
    pool.routes = list(proxies)
    pool.cooldown_until = {route: 0.0 for route in pool.routes}
    pool.failures = {route: 0 for route in pool.routes}
    pool.block_reason = {route: "" for route in pool.routes}
    pool.geo_hits = {route: 0 for route in pool.routes}
    pool.cursor = 0
    runner.smart_proxy_pool = pool
    runner.call_gemini_generate = tracked_generation_call
    runner.TEMPLATE_YIELD_TO_OCR = False
    runner.TEMPLATE_INFRA_RETRIES = 4
    runner.TEMPLATE_RESERVE_KEYS_FOR_OCR = 0
    runner.TEMPLATE_HARD_TIMEOUT = 5
    runner.TEMPLATE_PROXY_PREFLIGHT_TIMEOUT = 0.25
    runner.TEMPLATE_MIN_CALL_SECONDS = 0.1
    runner.TEMPLATE_DEADLINE_MARGIN = 0
    config.TEMPLATE_HTTP_READ_TIMEOUT = SMOKE_READ_TIMEOUT
    runner.TEMPLATE_MODEL_COOLDOWN = 0.01
    gemini_client.TEMPLATE_PROXY_PREFLIGHT_URL = "http://fake-gemini.invalid/health"
    gemini_client.GEMINI_REST_URL = "http://fake-gemini.invalid/v1beta/models/{model}:generateContent"
    generate.TEMPLATE_MAX_ATTEMPTS = 1
    telemetry.TEMPLATE_STATS["proxy_preflight_failed"] = 0
    telemetry.TEMPLATE_STATS["generation_timeout"] = 0
    telemetry.TEMPLATE_STATS["proxy_unavailable"] = 0
    template_router.TEMPLATE_JOBS.clear()
    template_router._SEMAPHORE = None
    install_events()
    event("scenario started")
    return pool


def assert_case(name: str, condition: bool, reason: str):
    if not condition:
        raise AssertionError(f"{name}: {reason}")


def report_case(name: str, started: float, job: dict, pool, expected: bool, extra: str = ""):
    elapsed = time.monotonic() - started
    reserved = any(gemini_pool.reserved)
    route_status = ",".join(
        f"{route.rsplit(':', 1)[-1]}={'cooldown' if pool.failures.get(route, 0) else 'clean'}"
        for route in pool.routes
    )
    event(
        f"final status={job.get('status')} error={job.get('error')} "
        f"api_attempts={RUNNER_API_ATTEMPTS} fake_gemini_requests={FAKE_GENERATE_COUNT} "
        f"key_released={not reserved} proxy_failures={sum(pool.failures.values())} {route_status} {extra}".rstrip()
    )
    print(f"\n{name} — {'PASS' if expected else 'FAIL'} ({elapsed:.3f}s)")
    for moment, message in sorted(ACTIVE_EVENTS, key=lambda item: item[0]):
        print(f"  +{moment - started:07.3f}s  {message}")
    return expected


async def run_generation_case(name: str, routes: list[str], expectation):
    started = time.monotonic()
    pool = prepare_case(routes)
    job = make_job(name.replace(" ", "-"))
    await generate.run_template_job(job["job_id"], job)
    try:
        valid = expectation(job, pool)
    except AssertionError as exc:
        event(f"assertion failed: {exc}")
        valid = False
    return report_case(name, started, job, pool, valid)


async def main():
    global FAKE_503_REMAINING, FAKE_ERROR_QUEUE, FAKE_TEXT_QUEUE, FAKE_DELAY_QUEUE
    server = LocalGeminiServer()
    proxies: list[LocalProxy] = []
    saved_pool_attrs = {
        name: copy.deepcopy(getattr(gemini_pool, name))
        for name in (
            "keys", "key_names", "blocked", "reserved", "dead", "last_used", "reserved_at",
            "last_error", "lease_ttl", "blackout_until", "rr_index", "model_blocked_until",
            "model_unavailable", "key_model_blocked", "key_model_daily_blocked",
        )
    }
    original_logs = (runner._job_log, generate._job_log, template_router._job_log)
    original_proxy_pool = runner.smart_proxy_pool
    original_reserve = runner.TEMPLATE_RESERVE_KEYS_FOR_OCR
    original_read_timeout = config.TEMPLATE_HTTP_READ_TIMEOUT
    server.start()
    healthy = LocalProxy("healthy")
    blackhole = LocalProxy("blackhole")
    partial = LocalProxy("dies_mid_response")
    proxies.extend([healthy, blackhole, partial])
    runner.TEMPLATE_PROXY_PREFLIGHT_TIMEOUT = 0.25
    all_passed = True

    try:
        # 1. Healthy route: preflight, real generateContent request, HTML validation.
        def success(job, pool):
            return (
                job.get("status") == "done"
                and job.get("template", {}).get("html")
                and RUNNER_API_ATTEMPTS == 1 and FAKE_GENERATE_COUNT == 1
                and not gemini_pool.reserved[0]
                and sum(pool.failures.values()) == 0
            )
        all_passed &= await run_generation_case("1 success", [healthy.url], success)

        # 2. Refused TCP connection: no key lease or API call; proxy is blamed.
        closed = closed_proxy_url()
        def closed_case(job, pool):
            return (
                job.get("status") == "error" and job.get("error") == "no_proxy"
                and RUNNER_API_ATTEMPTS == 0 and FAKE_GENERATE_COUNT == 0 and not gemini_pool.reserved[0]
                and pool.failures.get(closed, 0) >= 1
                and telemetry.TEMPLATE_STATS["proxy_preflight_failed"] >= 1
            )
        all_passed &= await run_generation_case("2 closed proxy", [closed], closed_case)

        # 3. TCP accepts but preflight never gets a response.
        def blackhole_case(job, pool):
            return (
                job.get("status") == "error" and job.get("error") == "no_proxy"
                and RUNNER_API_ATTEMPTS == 0 and FAKE_GENERATE_COUNT == 0 and not gemini_pool.reserved[0]
                and pool.failures.get(blackhole.url, 0) >= 1
            )
        all_passed &= await run_generation_case("3 TCP blackhole", [blackhole.url], blackhole_case)

        # 4. Proxy passes preflight, then closes a partial Gemini response.
        def partial_case(job, pool):
            return (
                job.get("status") == "error" and job.get("error") == "no_proxy"
                and RUNNER_API_ATTEMPTS == 1 and FAKE_GENERATE_COUNT == 0 and not gemini_pool.reserved[0]
                and pool.failures.get(partial.url, 0) >= 1
                and telemetry.TEMPLATE_STATS["proxy_unavailable"] >= 1
            )
        all_passed &= await run_generation_case("4 dies mid-response", [partial.url], partial_case)

        # 5. The fourth route is alive after three refused connections.
        dead_routes = [closed_proxy_url() for _ in range(3)]
        def fourth_alive(job, pool):
            return (
                job.get("status") == "done"
                and RUNNER_API_ATTEMPTS == 1 and FAKE_GENERATE_COUNT == 1 and not gemini_pool.reserved[0]
                and all(pool.failures.get(route, 0) >= 1 for route in dead_routes)
                and pool.failures.get(healthy.url, 0) == 0
            )
        all_passed &= await run_generation_case("5 first three dead, fourth alive", dead_routes + [healthy.url], fourth_alive)

        # 6. Four dead routes are excluded promptly rather than retried in a long API loop.
        all_dead = [closed_proxy_url(), blackhole.url, closed_proxy_url(), partial.url]
        def all_dead_bounded(job, pool):
            return (
                job.get("status") == "error" and job.get("error") == "no_proxy"
                and RUNNER_API_ATTEMPTS == 1 and FAKE_GENERATE_COUNT == 0 and not gemini_pool.reserved[0]
                and all(pool.failures.get(route, 0) >= 1 for route in all_dead)
                and time.monotonic() - started < 3.0
            )
        started = time.monotonic()
        all_passed &= await run_generation_case("6 all dead → bounded no_proxy", all_dead, all_dead_bounded)

        # 7. Provider 503s are API attempts, not proxy guilt; third request succeeds.
        started = time.monotonic()
        pool = prepare_case([healthy.url])
        FAKE_503_REMAINING = 2
        job = make_job("7-gemini-503-then-200")
        await generate.run_template_job(job["job_id"], job)
        valid = (
            job.get("status") == "done" and FAKE_GENERATE_COUNT == 3
            and RUNNER_API_ATTEMPTS == 3 and not gemini_pool.reserved[0]
            and pool.failures.get(healthy.url, 0) == 0
        )
        all_passed &= report_case("7 Gemini 503 twice, then 200", started, job, pool, valid)

        # 8–10. Same-model 400/403/404 on two distinct keys should stop immediately,
        # block only the key/model pairs, and keep the global keys healthy.
        rejection_cases = [
            (400, "INVALID_ARGUMENT", "Model is not valid for this project"),
            (403, "PERMISSION_DENIED", "Project has no permission for this model"),
            (404, "NOT_FOUND", "Model was not found"),
        ]
        for number, (status, provider_status, message) in enumerate(rejection_cases, start=8):
            started = time.monotonic()
            pool = prepare_case([healthy.url], key_count=2)
            FAKE_ERROR_QUEUE = [
                {"status": status, "provider_status": provider_status, "message": message},
                {"status": status, "provider_status": provider_status, "message": message},
            ]
            job = make_job(f"{number}-model-rejected-{status}")
            await generate.run_template_job(job["job_id"], job)
            detail = job.get("error_detail", {})
            valid = (
                job.get("status") == "error" and job.get("error") == "model_rejected"
                and detail.get("error_code") == "model_rejected"
                and detail.get("http_status") == status
                and detail.get("api_attempts") == 2
                and len(detail.get("attempt_times_ms", [])) == 2
                and RUNNER_API_ATTEMPTS == 2 and FAKE_GENERATE_COUNT == 2
                and gemini_pool.dead == [False, False]
                and gemini_pool.blocked == [0.0, 0.0]
                and gemini_pool.last_error == [None, None]
                and gemini_pool.blackout_until == 0.0
                and all((index, MODEL) in gemini_pool.key_model_blocked for index in (0, 1))
                and not gemini_pool.reserved[0] and not gemini_pool.reserved[1]
                and not gemini_pool.model_unavailable
                and pool.failures.get(healthy.url, 0) == 0
            )
            all_passed &= report_case(
                f"{number} Gemini {status} → fast model_rejected; keys remain healthy",
                started, job, pool, valid,
                extra=f"key_model_blocks={len(gemini_pool.key_model_blocked)}",
            )

        # 11. Preflight succeeds, first live provider response is slower than the
        # smoke-only read-timeout env; retry on the same healthy proxy and finish.
        started = time.monotonic()
        pool = prepare_case([healthy.url])
        slow_response_seconds = max(0.35, SMOKE_READ_TIMEOUT * 3)
        FAKE_DELAY_QUEUE = [slow_response_seconds]
        job = make_job("11-slow-live-response")
        await generate.run_template_job(job["job_id"], job)
        await asyncio.sleep(slow_response_seconds + 0.05)  # drain fake response after client timeout
        valid = (
            job.get("status") == "done" and job.get("template", {}).get("html")
            and RUNNER_API_ATTEMPTS == 2 and FAKE_GENERATE_COUNT == 2
            and telemetry.TEMPLATE_STATS["generation_timeout"] >= 1
            and not gemini_pool.dead[0] and gemini_pool.blocked[0] == 0.0
            and gemini_pool.last_error[0] is None and not gemini_pool.reserved[0]
            and pool.failures.get(healthy.url, 0) == 0
        )
        all_passed &= report_case(
            "11 slow-but-live response retries without proxy cooldown",
            started, job, pool, valid,
            extra=f"read_timeout_env={SMOKE_READ_TIMEOUT:.3f}s",
        )

        # 12. Generate a real invalid first draft, verify it is persisted, then cancel
        # while the second Gemini call is held by the local proxy.
        stall_seen = threading.Event()
        started = time.monotonic()
        stalled = LocalProxy("stall_generate", stall_seen=stall_seen)
        proxies.append(stalled)
        pool = prepare_case([stalled.url])
        draft_html = GOOD_HTML.replace(
            '<h2 style="margin:20px 0 12px;color:#24352d;font-size:20px;line-height:1.5">Покупка уже в работе</h2>',
            "",
        ).replace(
            "Мы подготовили свежие детали",
            "DRAFT-SAVED-BEFORE-CANCEL — Мы подготовили свежие детали",
        )
        FAKE_TEXT_QUEUE = [draft_html + "\n№ 1"]
        job = make_job("12-cancel-after-real-draft", max_attempts=2)
        template_router.TEMPLATE_JOBS[job["job_id"]] = job
        template_router._SEMAPHORE = asyncio.Semaphore(1)
        task = asyncio.create_task(template_router._run_queued(job["job_id"]))
        await asyncio.wait_for(asyncio.to_thread(stall_seen.wait, 5), timeout=6)
        draft_saved = bool(job.get("template", {}).get("html"))
        event(
            "verified generated draft persisted before cancellation "
            f"(valid={job.get('template', {}).get('valid')}, attempts={len(job.get('template', {}).get('attempts', []))})"
        )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        valid = (
            draft_saved and job.get("status") == "done"
            and "DRAFT-SAVED-BEFORE-CANCEL" in job.get("template", {}).get("html", "")
            and job.get("template", {}).get("valid") is False
            and job.get("template", {}).get("generation_error") == "cancelled"
            and RUNNER_API_ATTEMPTS == 2 and FAKE_GENERATE_COUNT == 1
            and not gemini_pool.reserved[0] and template_router._SEMAPHORE._value == 1
            and pool.failures.get(stalled.url, 0) == 0
        )
        all_passed &= report_case(
            "12 cancellation preserves the generated draft and releases key+semaphore",
            started, job, pool, valid,
            extra=f"draft_saved_before_cancel={draft_saved}; semaphore_available={template_router._SEMAPHORE._value}",
        )

        # 13. Cancellation before any model result must remain error=cancelled.
        started = time.monotonic()
        pool = prepare_case([healthy.url])
        job = make_job("13-cancel-before-any-result")
        template_router.TEMPLATE_JOBS[job["job_id"]] = job
        template_router._SEMAPHORE = asyncio.Semaphore(0)
        task = asyncio.create_task(template_router._run_queued(job["job_id"]))
        await asyncio.sleep(0.01)
        event("cancel queued task before generator/result was started")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        valid = (
            job.get("status") == "error" and job.get("error") == "cancelled"
            and not job.get("template") and job.get("error_detail", {}).get("error_code") == "cancelled"
            and RUNNER_API_ATTEMPTS == 0 and FAKE_GENERATE_COUNT == 0
            and not any(gemini_pool.reserved)
        )
        all_passed &= report_case(
            "13 cancellation before a result returns error=cancelled",
            started, job, pool, valid,
        )

    finally:
        await gemini_client.close_http_clients()
        for proxy in proxies:
            proxy.stop()
        server.stop()
        runner.smart_proxy_pool = original_proxy_pool
        runner.call_gemini_generate = REAL_GENERATION_CALL
        runner.TEMPLATE_RESERVE_KEYS_FOR_OCR = original_reserve
        config.TEMPLATE_HTTP_READ_TIMEOUT = original_read_timeout
        runner._job_log, generate._job_log, template_router._job_log = original_logs
        for name, value in saved_pool_attrs.items():
            setattr(gemini_pool, name, value)

    print(f"\nSMOKE RESULT: {'PASS' if all_passed else 'FAIL'} (13 scenarios)")
    if not all_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
