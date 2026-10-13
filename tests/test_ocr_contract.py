import base64
import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ocr_service import gemini_client, jobs, status_data
from ocr_service.routers import ocr
from ocr_service.schemas import JobResultResponse


GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def require_original_ocr_dependencies():
    for name in ("cv2", "numpy", "ddddocr"):
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            pytest.skip(f"E0 OCR snapshot requires installed {name}; stubs are not valid for this contract")
        pytest.importorskip(name, reason=f"E0 OCR snapshot requires installed {name}")


def read_golden(name):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def normalize_times(payload):
    result = dict(payload)
    for key in ("job_age_ms", "queue_wait_ms", "processing_ms"):
        if isinstance(result.get(key), (int, float)):
            result[key] = "<TIME>"
    return result


def test_ocr_request_body_matches_v46_golden(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "candidates": [{"content": {"parts": [{"text": "recognized"}]}, "finishReason": "STOP"}],
                "usageMetadata": {},
            }

    class Client:
        async def post(self, url, json=None, headers=None):
            captured.update(url=url, json=json)
            assert headers == {"x-goog-api-key": "dummy-test-key"}
            return Response()

    monkeypatch.setattr(gemini_client, "_get_http_client", lambda *args, **kwargs: Client())
    import asyncio
    asyncio.run(gemini_client._call_gemini_rest(
        b"e0-png", "E0 OCR prompt", "unit-model", "dummy-test-key", "http://proxy.test",
    ))
    assert captured == read_golden("gemini_ocr_request.json")


def test_queue_metrics_and_job_response_match_v46_golden(monkeypatch):
    jobs.JOBS.clear()
    monkeypatch.setattr(jobs, "GEMINI_KEYS", [])
    jobs.JOBS.update({
        "job-a": {"job_id": "job-a", "status": "pending", "stage": "queued", "created_at": 100.0},
        "job-b": {"job_id": "job-b", "status": "pending", "stage": "queued", "created_at": 101.0},
        "job-c": {
            "job_id": "job-c", "status": "running", "stage": "processing",
            "created_at": 99.0, "started_at": 103.0,
        },
    })
    metrics = jobs._job_queue_metrics("job-a", jobs.JOBS["job-a"], now=105.0)
    assert metrics == read_golden("job_queue_metrics.json")
    response = JobResultResponse(status="pending", **metrics)
    dumped = response.model_dump(exclude_none=True) if hasattr(response, "model_dump") else response.dict(exclude_none=True)
    assert dumped == read_golden("job_result_response.json")
    jobs.JOBS.clear()


def test_build_status_matches_v46_snapshot_when_templates_are_disabled(monkeypatch):
    from ocr_service import config
    from collections import Counter

    jobs.JOBS.clear()
    monkeypatch.setattr(config, "TEMPLATE_ENABLED", False)
    monkeypatch.setattr(status_data, "ATTEMPTS", [])
    monkeypatch.setattr(status_data, "QUALITY", [])
    monkeypatch.setattr(status_data, "TEMPLATE_RUNS", [])
    monkeypatch.setattr(status_data, "TEMPLATE_STATS", {})
    monkeypatch.setattr(status_data, "ACCOUNTS", {})
    monkeypatch.setattr(status_data, "KEY_LAST", {})
    monkeypatch.setattr(status_data, "LAST_EVENT", {"attempt": None, "ok": None, "err": None})
    monkeypatch.setattr(status_data, "JOB_STATS", Counter({"all_zero_jobs": 0}))
    monkeypatch.setattr(status_data, "GEMINI_KEYS", [])
    monkeypatch.setattr(status_data.gemini_pool, "keys", [])
    monkeypatch.setattr(status_data.gemini_pool, "key_names", [])
    payload = status_data.build_status()
    actual = {
        "top_keys": sorted(payload),
        "keys": {key: payload["keys"][key] for key in ("total", "usable", "by_state", "list")},
        "queue": {key: payload["queue"][key] for key in
                  ("pending", "running", "max_concurrency", "in_memory", "result_ttl_s")},
        "attempts": {key: payload["attempts"][key] for key in
                     ("ok", "api_err", "n429", "proxy_err", "total", "ok_pct")},
        "quality": payload["quality"],
        "has_template_fields": "queue_by_kind" in payload or "templates" in payload,
    }
    assert actual == read_golden("build_status.json")


def test_ocr_http_submit_pending_done_error_and_unknown_job_match_v46(
    monkeypatch, require_original_ocr_dependencies,
):
    app = FastAPI()
    app.include_router(ocr.router)
    monkeypatch.setattr(ocr, "spawn_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ocr, "_job_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ocr, "_account_touch", lambda *_args, **_kwargs: None)
    jobs.JOBS.clear()
    with TestClient(app) as client:
        submit = client.post("/ocr/submit", json={"images": "cG5n"})
        actual_submit = submit.json()
        actual_submit["job_id"] = "<JOB_ID>"
        assert actual_submit == read_golden("ocr_submit.json")

        now = time.time()
        jobs.JOBS["pending"] = {
            "job_id": "pending", "status": "pending", "stage": "queued", "created_at": now,
        }
        pending = client.get("/ocr/result/pending").json()
        assert normalize_times(pending) == read_golden("ocr_result_pending.json")

        jobs.JOBS.clear()
        jobs.JOBS["done"] = {
            "job_id": "done", "status": "done", "stage": "finished",
            "created_at": now - 3, "started_at": now - 2, "finished_at": now - 1,
            "results": [{"text": "recognized text", "source": "gemini"}],
        }
        done = client.get("/ocr/result/done").json()
        assert normalize_times(done) == read_golden("ocr_result_done.json")

        jobs.JOBS.clear()
        jobs.JOBS["error"] = {
            "job_id": "error", "status": "error", "stage": "finished",
            "created_at": now - 3, "started_at": now - 2, "finished_at": now - 1,
            "error": "upstream unavailable",
        }
        error = client.get("/ocr/result/error").json()
        assert normalize_times(error) == read_golden("ocr_result_error.json")

        unknown = client.get("/ocr/result/unknown")
        assert {"status_code": unknown.status_code, "body": unknown.json()} == read_golden("ocr_unknown_job.json")
    jobs.JOBS.clear()
