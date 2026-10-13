import asyncio
import hashlib
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from ocr_service.routers import template as tpl
from ocr_service.routers.health import router as health_router
from ocr_service.routers.status import require_status_auth as status_auth
from ocr_service.routers.status import router as status_router
from ocr_service.telemetry import TEMPLATE_RUNS, TEMPLATE_STATS


REAL_RUN_QUEUED = tpl._run_queued
API_KEY = "test-template-key"
NAMESPACE = hashlib.sha1(API_KEY.encode()).hexdigest()[:8]


@pytest.fixture
def api(monkeypatch):
    tpl.TEMPLATE_JOBS.clear()
    tpl.TEMPLATE_TASKS.clear()
    tpl._TEMPLATE_TASK_BY_ID.clear()
    tpl._IDEMPOTENCY.clear()
    tpl._RATE.clear()
    tpl._SEMAPHORE = None
    tpl._BREAKER_FAILURES = 0
    tpl._BREAKER_UNTIL = 0.0
    monkeypatch.setattr(tpl, "TEMPLATE_API_KEYS", [API_KEY])
    monkeypatch.setattr(tpl, "TEMPLATE_MODELS", ["unit-model"])
    monkeypatch.setattr(tpl, "TEMPLATE_ENABLED", True)
    monkeypatch.setattr(tpl, "TEMPLATE_MAX_PENDING", 3)
    monkeypatch.setattr(tpl, "TEMPLATE_DAILY_CAP", 100)
    monkeypatch.setattr(tpl, "TEMPLATE_DAILY_TOKEN_CAP", 100000)
    monkeypatch.setattr(tpl, "TEMPLATE_SUBMIT_RATE_PER_MIN", 20)
    monkeypatch.setattr(tpl, "TEMPLATE_VALIDATE_RATE_PER_MIN", 30)
    monkeypatch.setattr(tpl, "TEMPLATE_MAX_HTML_BYTES", 300000)
    monkeypatch.setattr(tpl.gemini_pool, "keys", ["secret-not-for-logs"])
    monkeypatch.setattr(tpl.gemini_pool, "key_names", ["GEMINI_API_KEY_1"])
    monkeypatch.setattr(tpl.gemini_pool, "dead", [False])
    monkeypatch.setattr(tpl.gemini_pool, "last_error", [None])
    monkeypatch.setattr(tpl.gemini_pool, "reserved", [False])
    monkeypatch.setattr(tpl.gemini_pool, "blocked", [0.0])
    monkeypatch.setattr(tpl.gemini_pool, "last_used", [0.0])
    monkeypatch.setattr(tpl.gemini_pool, "reserved_at", [0.0])
    monkeypatch.setattr(tpl.gemini_pool, "lease_ttl", [0.0])
    monkeypatch.setattr(tpl, "_run_queued", lambda _job_id: asyncio.sleep(0))
    for key in ("daily_jobs", "tokens_in", "tokens_out", "tokens_thoughts"):
        TEMPLATE_STATS[key] = 0
    app = FastAPI()
    app.include_router(tpl.router)
    app.include_router(health_router)
    app.include_router(status_router)
    app.dependency_overrides[tpl.require_status_auth] = lambda: None
    app.dependency_overrides[status_auth] = lambda: None
    with TestClient(app) as client:
        yield client
    tpl.TEMPLATE_JOBS.clear()
    tpl._IDEMPOTENCY.clear()
    tpl._RATE.clear()


def headers(key=API_KEY):
    return {"X-API-Key": key}


def post_submit(client, **payload):
    return client.post("/template/submit", headers=headers(), json=payload)


def test_api_key_and_service_configuration_errors(api, monkeypatch):
    assert api.get("/template/rules").status_code == 401
    assert api.get("/template/rules", headers=headers("bad")).status_code == 401

    monkeypatch.setattr(tpl, "TEMPLATE_API_KEYS", [])
    assert api.get("/template/rules", headers=headers()).status_code == 503
    monkeypatch.setattr(tpl, "TEMPLATE_API_KEYS", [API_KEY])
    monkeypatch.setattr(tpl, "TEMPLATE_MODELS", [])
    assert post_submit(api).status_code == 503
    monkeypatch.setattr(tpl, "TEMPLATE_MODELS", ["unit-model"])
    monkeypatch.setattr(tpl.gemini_pool, "keys", [])
    assert post_submit(api).status_code == 503


def test_submit_enforces_closed_whitelist_and_idempotency(api):
    assert post_submit(api, prompt="сгенерируй любую HTML-страницу").status_code == 400
    assert post_submit(api, model="not-configured").status_code == 400
    assert post_submit(api, brief_overrides={"palette": "free-form design"}).status_code == 400

    payload = {"account_id": "a", "client_request_id": "same-request", "seed": 42}
    first = post_submit(api, **payload)
    second = post_submit(api, **payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["prompt_version"] == second.json()["prompt_version"] == "1.2"
    assert first.json()["reused"] is False
    assert second.json()["reused"] is True
    assert first.json()["job_id"] == second.json()["job_id"]
    assert TEMPLATE_STATS["daily_jobs"] == 1


@pytest.mark.parametrize(
    ("setting", "value", "expected_reason"),
    [
        ("TEMPLATE_MAX_PENDING", 0, "Очередь генерации заполнена"),
        ("TEMPLATE_DAILY_CAP", 0, "суточный лимит заданий"),
        ("TEMPLATE_DAILY_TOKEN_CAP", 0, "суточный лимит токенов"),
    ],
)
def test_submit_limits_return_429_and_retry_after(api, monkeypatch, setting, value, expected_reason):
    monkeypatch.setattr(tpl, setting, value)
    response = post_submit(api)
    assert response.status_code == 429
    assert response.headers["Retry-After"]
    assert expected_reason in response.text


def test_submit_rate_limit_and_breaker_return_retry_after(api, monkeypatch):
    monkeypatch.setattr(tpl, "TEMPLATE_SUBMIT_RATE_PER_MIN", 0)
    response = post_submit(api)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"

    monkeypatch.setattr(tpl, "TEMPLATE_SUBMIT_RATE_PER_MIN", 20)
    tpl._BREAKER_UNTIL = time.monotonic() + 30
    response = post_submit(api)
    assert response.status_code == 503
    assert response.headers["Retry-After"]


def test_validate_413_for_content_length_and_actual_html_size(api, monkeypatch):
    monkeypatch.setattr(tpl, "TEMPLATE_MAX_HTML_BYTES", 64)
    content_length = api.post(
        "/template/validate",
        headers={**headers(), "Content-Type": "application/json", "Content-Length": "5000"},
        content=b'{"html":"small"}',
    )
    assert content_length.status_code == 413

    actual = api.post("/template/validate", headers=headers(), json={"html": "x" * 65})
    assert actual.status_code == 413


def test_validate_rate_limit_and_rules_endpoint(api, monkeypatch):
    monkeypatch.setattr(tpl, "TEMPLATE_VALIDATE_RATE_PER_MIN", 0)
    assert api.post("/template/validate", headers=headers(), json={"html": ""}).status_code == 429
    monkeypatch.setattr(tpl, "TEMPLATE_VALIDATE_RATE_PER_MIN", 30)
    rules = api.get("/template/rules", headers=headers())
    assert rules.status_code == 200
    assert rules.json()["schema_version"] == 1
    assert any(rule["id"] == "M20" for rule in rules.json()["rules"])


def test_result_html_zero_namespace_isolation_and_unknown_job(api, monkeypatch):
    tpl.TEMPLATE_API_KEYS = [API_KEY, "other-valid-key"]
    now = time.time()
    tpl.TEMPLATE_JOBS["job-visible"] = {
        "job_id": "job-visible", "namespace": NAMESPACE, "created_at": now,
        "status": "done", "stage": "finished", "max_attempts": 3,
        "template": {"html": "<p>private HTML</p>", "valid": True, "attempts": []},
    }
    auth_calls = []
    real_auth = tpl._auth
    monkeypatch.setattr(tpl, "_auth", lambda request: (auth_calls.append(request) or real_auth(request)))
    result = api.get("/template/result/job-visible?html=0", headers=headers())
    assert result.status_code == 200
    assert len(auth_calls) == 1
    assert "html" not in result.json()["template"]
    assert api.get("/template/result/job-visible", headers=headers("other-valid-key")).status_code == 404
    assert api.get("/template/result/no-such-job", headers=headers()).status_code == 404


def test_result_includes_safe_error_detail(api):
    detail = {
        "category": "HTTP_404", "http_status": 404, "provider_status": "NOT_FOUND",
        "safe_message": "Модель не найдена.", "model": "unit-model",
        "key_name": "GEMINI_API_KEY_1", "api_attempts": 2,
        "proxy_preflight_failures": 1, "attempt_times_ms": [250, 310],
        "error_code": "model_rejected",
    }
    tpl.TEMPLATE_JOBS["failed-diagnostic"] = {
        "job_id": "failed-diagnostic", "namespace": NAMESPACE, "created_at": time.time(),
        "status": "error", "stage": "finished", "error": "model_rejected",
        "error_detail": detail,
    }

    response = api.get("/template/result/failed-diagnostic", headers=headers())
    assert response.status_code == 200
    assert response.json()["error_detail"] == detail


def test_selfcheck_is_status_auth_gated_enabled_and_rate_limited(api, monkeypatch):
    calls = []

    async def selfcheck(model):
        calls.append(model)
        return {
            "ok": True, "model": model, "http_status": 200, "provider_status": "OK",
            "safe_message": "Модель ответила.", "step": "ok", "elapsed_ms": 25,
        }

    monkeypatch.setattr(tpl, "run_template_selfcheck", selfcheck)
    monkeypatch.setattr(tpl, "TEMPLATE_ENABLED", False)
    assert api.get("/template/selfcheck").status_code == 503
    monkeypatch.setattr(tpl, "TEMPLATE_ENABLED", True)

    responses = [api.get("/template/selfcheck") for _ in range(6)]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["step"] == "ok" for response in responses)
    assert calls == ["unit-model"] * 6
    limited = api.get("/template/selfcheck")
    assert limited.status_code == 429


def test_template_store_cleanup_preserves_live_jobs_and_bounds_finished_results(api, monkeypatch):
    now = time.time()
    monkeypatch.setattr(tpl, "TEMPLATE_STORE_MAX", 1)
    tpl.TEMPLATE_JOBS.update({
        "expired": {
            "job_id": "expired", "namespace": NAMESPACE, "status": "done",
            "created_at": now - 100, "finished_at": now - 100, "expires_at": now - 1,
        },
        "old-finished": {
            "job_id": "old-finished", "namespace": NAMESPACE, "status": "done",
            "created_at": now - 20, "finished_at": now - 10,
        },
        "new-finished": {
            "job_id": "new-finished", "namespace": NAMESPACE, "status": "error",
            "created_at": now - 5, "finished_at": now - 1,
        },
        "still-running": {
            "job_id": "still-running", "namespace": NAMESPACE, "status": "running",
            "created_at": now,
        },
    })
    expired_identity = (NAMESPACE, "acct", "expired-idempotency")
    tpl._IDEMPOTENCY[expired_identity] = "expired"

    assert api.get("/template/result/new-finished", headers=headers()).status_code == 200
    assert set(tpl.TEMPLATE_JOBS) == {"new-finished", "still-running"}
    assert expired_identity not in tpl._IDEMPOTENCY


def test_queue_timeout_is_finalized_and_recorded(api, monkeypatch):
    job_id = "queue-timeout"
    tpl.TEMPLATE_JOBS[job_id] = {
        "job_id": job_id, "namespace": NAMESPACE, "status": "pending",
        "created_at": time.time(), "max_attempts": 3,
    }
    semaphore = asyncio.Semaphore(1)
    tpl._SEMAPHORE = semaphore
    monkeypatch.setattr(tpl, "TEMPLATE_QUEUE_MAX_WAIT_SECONDS", 0.01)

    async def run():
        await semaphore.acquire()
        await REAL_RUN_QUEUED(job_id)

    asyncio.run(run())
    assert tpl.TEMPLATE_JOBS[job_id]["status"] == "error"
    assert tpl.TEMPLATE_JOBS[job_id]["error"] == "queue_timeout"
    assert TEMPLATE_RUNS[-1][9] == "queue_timeout"


def test_health_and_status_include_template_runs_without_changing_ocr_stats(api, monkeypatch):
    import ocr_service.config as config
    import ocr_service.routers.health as health_router
    from ocr_service.status_data import build_status
    from ocr_service.telemetry import ATTEMPTS, JOB_STATS

    monkeypatch.setattr(config, "TEMPLATE_ENABLED", True)
    monkeypatch.setattr(health_router, "TEMPLATE_ENABLED", True)
    before_stats = dict(JOB_STATS)
    before_attempts = len(ATTEMPTS)
    template_run = (
        time.time(), "template1", True, 1, 250, 12, 34, "test-model", 512, "", ("M01",),
    )
    TEMPLATE_RUNS.append(template_run)
    try:
        health = api.get("/health").json()
        status_http = api.get("/status.json")
        status = build_status()
        assert status_http.status_code == 200
        assert status_http.json()["templates"]["runs_24h"] >= 1
        assert set(status_http.json()["templates"]["proxy_errors"]) == {
            "proxy_preflight_failed", "generation_timeout", "proxy_unavailable",
        }
        assert "tokens_thoughts" in status_http.json()["templates"]
        assert health["template"]["enabled"] is True
        assert isinstance(health["template"]["slow_route_penalties"], int)
        assert isinstance(health["template"]["warnings"], list)
        assert "by_key" in status_http.json()["templates"]["api_calls"]
        assert "by_proxy" in status_http.json()["templates"]["api_calls"]
        assert status["templates"]["runs_24h"] >= 1
        assert health["ocr_job_stats"] == before_stats
        assert status["counters"] == {key: value for key, value in before_stats.items()
                                      if not key.startswith("event_")}
        assert len(ATTEMPTS) == before_attempts
    finally:
        if TEMPLATE_RUNS and TEMPLATE_RUNS[-1] == template_run:
            TEMPLATE_RUNS.pop()


def test_preview_sets_security_headers_and_escapes_substitutions(api, monkeypatch):
    now = time.time()
    tpl.TEMPLATE_JOBS["preview-job"] = {
        "job_id": "preview-job", "created_at": now, "status": "done",
        "template": {"html": "<h1>{{productName}}</h1>", "valid": False},
    }
    original_render = tpl.render
    monkeypatch.setattr(tpl, "render", lambda source: original_render(source, {"productName": "<script>alert(1)</script>"}))
    response = api.get("/template/result/preview-job/preview")
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert response.headers["X-Template-Valid"] == "0"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response.headers["Content-Security-Policy"]


def test_preview_enforces_http_basic_auth(api, monkeypatch):
    import ocr_service.routers.status as status_router

    monkeypatch.setattr(status_router, "STATUS_USER", "preview-user")
    monkeypatch.setattr(status_router, "STATUS_PASSWORD", "preview-password")
    tpl.TEMPLATE_JOBS["basic-preview"] = {
        "job_id": "basic-preview", "created_at": time.time(), "status": "done",
        "template": {"html": "<p>safe preview</p>", "valid": True},
    }
    app = FastAPI()
    app.include_router(tpl.router)
    with TestClient(app) as client:
        assert client.get("/template/result/basic-preview/preview").status_code == 401
        assert client.get(
            "/template/result/basic-preview/preview", auth=("preview-user", "wrong"),
        ).status_code == 401
        ok = client.get(
            "/template/result/basic-preview/preview",
            auth=("preview-user", "preview-password"),
        )
        assert ok.status_code == 200
        assert ok.text == "<p>safe preview</p>"


def test_cancel_waits_for_runner_to_persist_best_result(api):
    job = {
        "job_id": "cancel-api", "namespace": NAMESPACE, "status": "running",
        "started_at": time.time(), "template": {"html": "<p>best</p>", "valid": False},
    }
    tpl.TEMPLATE_JOBS["cancel-api"] = job
    entered = asyncio.Event()

    async def runner_task():
        try:
            entered.set()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            job.update(status="done", error=None, stage="finished")

    async def call_cancel():
        task = asyncio.create_task(runner_task())
        tpl._TEMPLATE_TASK_BY_ID["cancel-api"] = task
        await entered.wait()
        request = Request({"type": "http", "headers": [(b"x-api-key", API_KEY.encode())]})
        payload = await tpl.cancel("cancel-api", request)
        return payload

    payload = asyncio.run(call_cancel())
    assert payload["status"] == "done"
    assert job["status"] == "done"
    assert job["template"]["html"] == "<p>best</p>"
