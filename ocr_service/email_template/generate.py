"""Асинхронная генерация письма, валидация и ограниченный цикл ремонта."""
import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

from ..config import (
    TEMPLATE_DAILY_TOKEN_CAP, TEMPLATE_MAX_ATTEMPTS, TEMPLATE_MAX_HTML_BYTES, TEMPLATE_MAX_OUTPUT_TOKENS,
    TEMPLATE_MODELS, TEMPLATE_REPAIR_MODEL, TEMPLATE_REPAIR_TEMPERATURE,
    TEMPLATE_RESULT_TTL_SECONDS,
    TEMPLATE_TEMPERATURE,
)
from ..telemetry import (
    TEMPLATE_STATS, _job_log, ensure_template_day, record_template_run,
    record_template_usage, template_success_p50_ms,
)
from . import autofix
from .extract import parse
from .prompt import PROMPT_VERSION, build_repair_message, build_user_message, base_prompt, prompt_sha1
from .runner import GenerationDeadline, TemplateGenerationError, generate as call_model
from .validator import validate


def _persistent_rules(attempts):
    if len(attempts) < 2:
        return set()
    previous = set(attempts[-2].get("errors", []))
    current = set(attempts[-1].get("errors", []))
    return previous & current


def _repair_model_candidate(setting, models, current_model):
    setting = str(setting or "").strip()
    if not setting:
        return None
    models = list(models or [])
    if setting.lower() in {"1", "true", "yes", "on", "next"}:
        try:
            position = models.index(current_model)
        except ValueError:
            return None
        return models[position + 1] if position + 1 < len(models) else None
    return setting if setting in models and setting != current_model else None


async def run_template_job(job_id: str, job: dict):
    job["status"] = "running"
    job["stage"] = "generating"
    job["started_at"] = time.time()
    _job_log("tpl_started", job_id, component="email_template", attempt=1, model=job.get("model"))
    started = time.monotonic()
    deadline_ts = started + max(1.0, float(job.get("deadline_s", 420)) - 10.0)
    job["deadline_monotonic"] = deadline_ts
    candidate_models = [job.get("model")] if job.get("model") else list(job.get("models") or [""])
    candidate_p50s = [template_success_p50_ms(model) for model in candidate_models if model]
    model_p50_ms = max((value for value in candidate_p50s if value), default=None)
    expected_model = candidate_models[0] if candidate_models else ""
    repair_budget_seconds = max(60.0, (model_p50_ms / 1000.0) if model_p50_ms else 0.0)
    first_generation_deadline = max(started + 1.0, deadline_ts - repair_budget_seconds)
    system = base_prompt()
    initial = build_user_message(job["brief"])
    contents = [{"role": "user", "parts": [{"text": initial}]}]
    history = []
    attempts = []
    best = None
    generation_error = None
    no_progress = 0
    force_final_attempt = False
    last_report_errors = []
    max_attempts = int(job.get("max_attempts") or TEMPLATE_MAX_ATTEMPTS)
    try:
        for number in range(1, max_attempts + 1):
            if force_final_attempt and number < max_attempts:
                continue
            remaining = deadline_ts - time.monotonic()
            minimum_repair_seconds = max(60.0, (model_p50_ms / 1000.0) if model_p50_ms else 0.0)
            if number > 1 and remaining < minimum_repair_seconds:
                generation_error = "repair_skipped_no_time"
                _job_log(
                    "tpl_repair_skipped", job_id, component="email_template",
                    attempt=number, reason="repair_skipped_no_time",
                    remaining_s=round(max(0.0, remaining), 1),
                    required_s=round(minimum_repair_seconds, 1),
                    model=attempts[-1].get("model") if attempts else expected_model,
                )
                break
            if remaining <= 0:
                break
            job["stage"] = "generating" if number == 1 else "repairing"
            job["attempt"] = number
            generation_config = {
                "temperature": TEMPLATE_TEMPERATURE if number == 1 else TEMPLATE_REPAIR_TEMPERATURE,
                "maxOutputTokens": TEMPLATE_MAX_OUTPUT_TOKENS,
            }
            request_models = [job.get("model")] if job.get("model") else list(job["models"])
            persistent = _persistent_rules(attempts) if number == max_attempts else set()
            repair_strategy = None
            if persistent:
                setting = TEMPLATE_REPAIR_MODEL
                if setting and setting.lower() not in {"1", "true", "yes", "on", "next"}:
                    candidates = list(job.get("models") or [])
                    if setting not in candidates and setting in TEMPLATE_MODELS:
                        candidates.append(setting)
                else:
                    candidates = list(job.get("models") or [])
                alternate_model = _repair_model_candidate(setting, candidates, attempts[-1].get("model"))
                if alternate_model:
                    request_models = [alternate_model]
                    repair_strategy = "alternate_model"
                else:
                    repair_strategy = "compact_restart"
                issues = [item for item in last_report_errors if item.get("rule") in persistent]
                repair = build_repair_message(issues, compact=True)
                contents = [{"role": "user", "parts": [{"text": initial + "\n\n" + repair}]}]
                _job_log(
                    "tpl_repair_strategy", job_id, component="email_template", attempt=number,
                    strategy=repair_strategy, model=request_models[0] if repair_strategy == "alternate_model" else attempts[-1].get("model"),
                    persistent_rule_ids=sorted(persistent)[:12],
                )
            call_started = time.monotonic()
            call_deadline = first_generation_deadline if number == 1 and max_attempts > 1 else deadline_ts
            try:
                text, metadata, model = await call_model(
                    contents, system, request_models,
                    job_id, generation_config, deadline_ts=call_deadline,
                )
            except GenerationDeadline:
                generation_error = "deadline"
                attempts.append({
                    "n": number, "model": job.get("model") or "",
                    "latency_ms": round((time.monotonic() - call_started) * 1000),
                    "api_trace": [],
                    "finish_reason": "DEADLINE", "tokens": {"in": 0, "out": 0, "thoughts": 0, "cached": 0},
                    "raw_errors": [], "autofixes": [], "errors": [], "warnings": [],
                })
                break
            except TemplateGenerationError as exc:
                generation_error = exc.code
                if exc.error_detail:
                    job["error_detail"] = dict(exc.error_detail)
                attempts.append({
                    "n": number, "model": job.get("model") or "",
                    "latency_ms": round((time.monotonic() - call_started) * 1000),
                    "api_trace": list((exc.error_detail or {}).get("api_trace") or []),
                    "finish_reason": exc.code.upper(),
                    "tokens": {"in": 0, "out": 0, "thoughts": 0, "cached": 0},
                    "raw_errors": [], "autofixes": [], "errors": [], "warnings": [],
                })
                break
            latency_ms = round((time.monotonic() - call_started) * 1000)
            usage = metadata.get("usage", {})
            record_template_usage(usage)
            model_p50_ms = template_success_p50_ms(model) or model_p50_ms
            _job_log(
                "tpl_gemini_attempt", job_id, component="email_template", attempt=number,
                model=model, latency_ms=latency_ms, tokens=usage,
                thoughts=int(usage.get("thoughts", 0) or 0),
                thinking_setting=metadata.get("thinking_setting", "model-default"),
            )
            parsed = parse(text, metadata.get("finish_reason", ""), metadata.get("prompt_block_reason", ""))
            if parsed.html and not re.search(
                r"№\s*" + str(job["brief"]["currency_method"]) + r"\b",
                parsed.tail,
                re.I,
            ):
                parsed.warnings.append({
                    "rule": "V10",
                    "message": "Строка после HTML должна указывать выбранный способ оформления валюты",
                })
            raw_html = parsed.html
            fixes = []
            report = None
            raw_report = None
            output_bytes = len(raw_html.encode("utf-8"))
            if raw_html and output_bytes > TEMPLATE_MAX_HTML_BYTES:
                parsed.errors.append({
                    "rule": "T23",
                    "message": "Размер HTML превышает установленный лимит; содержимое не сохраняется",
                })
                raw_html = ""
            if raw_html and len(raw_html.encode("utf-8")) <= TEMPLATE_MAX_HTML_BYTES:
                job["stage"] = "validating"
                if len(raw_html.encode("utf-8")) > 40000:
                    fixed, fixes = await asyncio.to_thread(autofix.apply, raw_html)
                    raw_report = await asyncio.to_thread(validate, raw_html, job["brief"]["currency_method"])
                    after = await asyncio.to_thread(validate, fixed, job["brief"]["currency_method"])
                else:
                    fixed, fixes = autofix.apply(raw_html)
                    raw_report = validate(raw_html, job["brief"]["currency_method"])
                    after = validate(fixed, job["brief"]["currency_method"])
                if len(after.errors) > len(raw_report.errors):
                    fixed, fixes, after = raw_html, [], raw_report
                report = after
                if fixes:
                    _job_log("tpl_autofix", job_id, component="email_template", attempt=number, fix_ids=fixes)
            report_errors = [dict(rule=x["rule"], message=x["message"], where=x.get("where", "")) for x in parsed.errors]
            raw_errors = list(report_errors)
            if raw_report is not None:
                raw_errors.extend(
                    {"rule": issue.rule, "message": issue.message, "where": issue.where}
                    for issue in raw_report.errors
                )
            if report is not None:
                report_errors.extend({"rule": issue.rule, "message": issue.message, "where": issue.where} for issue in report.errors)
            report_warnings = list(parsed.warnings)
            if report is not None:
                report_warnings.extend({"rule": issue.rule, "message": issue.message, "where": issue.where} for issue in report.warnings)
            last_report_errors = list(report_errors)
            current = {
                "n": number, "model": model, "latency_ms": latency_ms,
                "api_trace": list(metadata.get("api_trace") or []),
                "finish_reason": metadata.get("finish_reason", ""),
                "thinking": metadata.get("thinking_setting", "model-default"),
                "tokens": metadata.get("usage", {}),
                "raw_errors": sorted(set(issue["rule"] for issue in raw_errors)),
                "autofixes": fixes, "errors": sorted(set(issue["rule"] for issue in report_errors)),
                "warnings": sorted(set(issue["rule"] for issue in report_warnings)),
            }
            attempts.append(current)
            if number > 1 and len(current["errors"]) >= len(attempts[-2].get("errors", [])):
                no_progress += 1
            else:
                no_progress = 0
            valid = bool(report is not None and report.ok and not parsed.errors)
            result = {
                "html": fixed if report is not None else raw_html,
                "bytes": len((fixed if report is not None else raw_html).encode("utf-8")),
                "sha1": hashlib.sha1((fixed if report is not None else raw_html).encode("utf-8")).hexdigest(),
                "valid": valid,
                "concept": parsed.concept,
                "brief": {key: value for key, value in job["brief"].items() if key != "avoid_palettes"},
                "prompt_version": PROMPT_VERSION,
                "prompt_sha1": prompt_sha1(),
                "model": model,
                "attempts": list(attempts),
                "report": report.to_dict() if report is not None else {
                    "ok": False, "errors": report_errors, "warnings": report_warnings,
                    "stats": {"bytes": output_bytes},
                },
                "tail": parsed.tail,
                "api_trace": [
                    trace for item in attempts for trace in item.get("api_trace", [])
                ],
            }
            score = (len(report_errors), len(report_warnings), number)
            if best is None or score < best[0]:
                best = (score, result)
            # Keep the best completed attempt available while repairs continue.
            # A router timeout or cancellation must not erase useful HTML.
            if best is not None:
                best[1]["attempts"] = list(attempts)
                best[1]["api_trace"] = [
                    trace for item in attempts for trace in item.get("api_trace", [])
                ]
                job["template"] = best[1]
            _job_log(
                "tpl_validated", job_id, component="email_template", attempt=number,
                model=model, bytes=len((result["html"] or "").encode("utf-8")),
                valid=valid, rule_ids=current["errors"][:20],
            )
            if valid:
                break
            if no_progress >= 2:
                if _persistent_rules(attempts) and number < max_attempts:
                    force_final_attempt = True
                else:
                    generation_error = "repair_no_progress"
                    break
            ensure_template_day()
            token_usage = sum(int(TEMPLATE_STATS.get(key, 0)) for key in ("tokens_in", "tokens_out", "tokens_thoughts"))
            if token_usage >= TEMPLATE_DAILY_TOKEN_CAP:
                break
            if number < max_attempts and report_errors:
                remaining = deadline_ts - time.monotonic()
                if remaining < minimum_repair_seconds:
                    generation_error = "repair_skipped_no_time"
                    _job_log(
                        "tpl_repair_skipped", job_id, component="email_template",
                        attempt=number + 1, reason="repair_skipped_no_time",
                        remaining_s=round(max(0.0, remaining), 1),
                        required_s=round(minimum_repair_seconds, 1), model=model,
                    )
                    break
                priority = {"T": 0, "S": 0, "V": 0, "L": 1, "A": 2, "X": -1}
                report_errors.sort(key=lambda issue: (priority.get(issue["rule"][:1], 3), issue["rule"]))
                repair = build_repair_message(
                    report_errors,
                    any(x["rule"] == "X02" for x in parsed.errors),
                )
                if any(x["rule"] == "X03" for x in parsed.errors):
                    history = []
                    contents = [{"role": "user", "parts": [{"text": initial + "\n\n" + repair}]}]
                else:
                    _job_log(
                        "tpl_repair", job_id, component="email_template", attempt=number,
                        rule_ids=sorted(set(issue["rule"] for issue in report_errors))[:12],
                    )
                    history.extend([
                        {"role": "model", "parts": [{"text": text}]},
                        {"role": "user", "parts": [{"text": repair}]},
                    ])
                    contents = [{"role": "user", "parts": [{"text": initial}]}] + history
            else:
                break
        finished = time.time()
        if best:
            result = best[1]
            result["attempts"] = attempts
            result["api_trace"] = [
                trace for item in attempts for trace in item.get("api_trace", [])
            ]
            if generation_error:
                result["generation_error"] = generation_error
            job["template"] = result
            job["status"] = "done"
            job["stage"] = "finished"
            job["finished_at"] = finished
            job["expires_at"] = finished + TEMPLATE_RESULT_TTL_SECONDS
            _job_log("tpl_done", job_id, component="email_template", valid=result["valid"], attempts=len(attempts),
                     bytes=len((result["html"] or "").encode("utf-8")))
            record_template_run(job)
        else:
            job["status"] = "error"
            job["stage"] = "finished"
            job["error"] = generation_error or ("deadline" if time.monotonic() >= deadline_ts else "generation_failed")
            job["finished_at"] = finished
            job["expires_at"] = finished + TEMPLATE_RESULT_TTL_SECONDS
            _job_log("tpl_error", job_id, component="email_template", error=job["error"])
            record_template_run(job, job["error"])
    except asyncio.TimeoutError:
        error_code = "deadline"
        if job.get("template"):
            job["template"]["generation_error"] = error_code
            job["template"]["api_trace"] = list(job.get("api_trace") or job["template"].get("api_trace") or [])
            job["status"] = "done"
            job["error"] = None
            _job_log("tpl_done", job_id, component="email_template",
                     valid=job["template"].get("valid", False),
                     attempts=len(job["template"].get("attempts", [])),
                     bytes=len((job["template"].get("html") or "").encode("utf-8")))
            record_template_run(job)
        else:
            job["status"] = "error"
            job["error"] = error_code
            record_template_run(job, error_code)
        job["stage"] = "finished"
        job["finished_at"] = time.time()
        job["expires_at"] = job["finished_at"] + TEMPLATE_RESULT_TTL_SECONDS
    except asyncio.CancelledError:
        if job.get("template"):
            job["template"]["generation_error"] = "cancelled"
            job["template"]["api_trace"] = list(job.get("api_trace") or job["template"].get("api_trace") or [])
            job["status"] = "done"
            job["error"] = None
            _job_log("tpl_done", job_id, component="email_template",
                     valid=job["template"].get("valid", False),
                     attempts=len(job["template"].get("attempts", [])),
                     bytes=len((job["template"].get("html") or "").encode("utf-8")))
            record_template_run(job)
        else:
            job["status"] = "error"
            job["error"] = "cancelled"
            record_template_run(job, job["error"])
        job["stage"] = "finished"
        job["finished_at"] = time.time()
        job["expires_at"] = job["finished_at"] + TEMPLATE_RESULT_TTL_SECONDS
        raise
    except Exception as exc:
        job["stage"] = "finished"
        job["error_detail"] = {
            "category": "UNKNOWN",
            "http_status": None,
            "provider_status": "",
            "safe_message": "Неизвестная ошибка генерации.",
            "model": job.get("model") or (job.get("models") or [None])[0],
            "key_name": None,
            "api_attempts": 0,
            "proxy_preflight_failures": 0,
            "attempt_times_ms": [],
            "error_code": "generation_unavailable",
        }
        if job.get("template"):
            job["template"]["generation_error"] = "generation_unavailable"
            job["status"] = "done"
            job["error"] = None
            record_template_run(job)
        else:
            job["status"] = "error"
            # Не выдаём наружу текст исключения: там могут быть данные провайдера.
            job["error"] = "generation_unavailable"
            record_template_run(job, job["error"])
        job["finished_at"] = time.time()
        job["expires_at"] = job["finished_at"] + TEMPLATE_RESULT_TTL_SECONDS
        if job.get("status") == "done":
            _job_log("tpl_done", job_id, component="email_template",
                     valid=job["template"].get("valid", False),
                     attempts=len(job["template"].get("attempts", [])),
                     bytes=len((job["template"].get("html") or "").encode("utf-8")))
        else:
            _job_log("tpl_error", job_id, component="email_template", error_type=type(exc).__name__)
