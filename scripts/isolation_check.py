#!/usr/bin/env python3
"""Сравнивает синхронный OCR до/во время двух template jobs и проверяет резерв ключей."""
import argparse
import base64
import json
import time
import uuid
from math import isfinite
from pathlib import Path

import httpx


def percentile(values, fraction=0.95):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))] if ordered else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--image", required=True, help="PNG/JPEG для контрольных запросов POST /ocr")
    parser.add_argument("--baseline-samples", type=int, default=5)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--deadline", type=float, default=900)
    parser.add_argument("--model")
    parser.add_argument("--out", default="isolation_summary.json")
    args = parser.parse_args()
    image = base64.b64encode(Path(args.image).read_bytes()).decode("ascii")
    base = args.base_url.rstrip("/")
    baseline = []
    violations = []
    during = []
    job_ids = []
    ready_key_counts = []
    reserve_counts = []

    with httpx.Client(timeout=60) as client:
        for _ in range(max(1, args.baseline_samples)):
            started = time.monotonic()
            response = client.post(base + "/ocr", json={"images": image})
            response.raise_for_status()
            baseline.append(time.monotonic() - started)

        for index in range(2):
            body = {
                "account_id": "template-isolation-check",
                "client_request_id": "isolation-" + uuid.uuid4().hex,
                "seed": 80000 + index,
            }
            if args.model:
                body["model"] = args.model
            response = client.post(
                base + "/template/submit",
                headers={"X-API-Key": args.api_key},
                json=body,
            )
            response.raise_for_status()
            job_ids.append(response.json()["job_id"])

        started_run = time.monotonic()
        final_status = {}
        while time.monotonic() - started_run < args.deadline:
            tick = time.monotonic()
            ocr_started = time.monotonic()
            response = client.post(base + "/ocr", json={"images": image})
            response.raise_for_status()
            during.append(time.monotonic() - ocr_started)

            health = client.get(base + "/health")
            health.raise_for_status()
            state = health.json()
            template_config = state.get("template") or {}
            reserve = int(template_config.get("ocr_key_reserve", 0))
            free_keys = len((state.get("gemini_keys_by_state") or {}).get("ready", []))
            ready_key_counts.append(free_keys)
            reserve_counts.append(reserve)
            if free_keys < reserve:
                violations.append({
                    "elapsed_seconds": round(time.monotonic() - started_run, 2),
                    "ready_keys": free_keys,
                    "required_reserve": reserve,
                })

            for job_id in job_ids:
                if final_status.get(job_id) in {"done", "error"}:
                    continue
                result = client.get(
                    base + "/template/result/" + job_id,
                    headers={"X-API-Key": args.api_key},
                    params={"html": 0},
                )
                result.raise_for_status()
                payload = result.json()
                final_status[job_id] = payload["status"]
            if all(status in {"done", "error"} for status in final_status.values()) and len(final_status) == len(job_ids):
                break
            time.sleep(max(0.0, args.interval - (time.monotonic() - tick)))

    baseline_p95 = percentile(baseline)
    concurrent_p95 = percentile(during)
    p95_change = (
        round((concurrent_p95 / baseline_p95 - 1) * 100, 2)
        if baseline_p95 and concurrent_p95 is not None else None
    )
    all_jobs_finished = len(final_status) == len(job_ids) and all(
        status in {"done", "error"} for status in final_status.values()
    )
    summary = {
        "template_job_ids": job_ids,
        "template_status": final_status,
        "ocr_samples_baseline": len(baseline),
        "ocr_samples_during_templates": len(during),
        "ocr_p95_baseline_seconds": round(baseline_p95, 3) if baseline_p95 is not None else None,
        "ocr_p95_during_seconds": round(concurrent_p95, 3) if concurrent_p95 is not None else None,
        "ocr_p95_change_percent": p95_change,
        "ready_keys_min": min(ready_key_counts) if ready_key_counts else None,
        "ocr_reserve_max": max(reserve_counts) if reserve_counts else None,
        "ready_keys_at_or_above_reserve": bool(
            ready_key_counts and not violations
            and all(ready >= reserve for ready, reserve in zip(ready_key_counts, reserve_counts))
        ),
        "ocr_reserve_violations": violations,
        "all_template_jobs_finished": all_jobs_finished,
        "pass": bool(
            baseline and during and not violations and all_jobs_finished
            and baseline_p95 is not None and concurrent_p95 is not None
            and isfinite(baseline_p95) and isfinite(concurrent_p95)
        ),
    }
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
