"""Вызов Gemini с отдельным профилем таймаута и резервом ключей для OCR."""
import asyncio
import re
import time
from difflib import SequenceMatcher

from ..config import (
    GEMINI_ACQUIRE_POLL, TEMPLATE_CONNECT_TIMEOUT, TEMPLATE_DEADLINE_MARGIN,
    TEMPLATE_HARD_TIMEOUT, TEMPLATE_INFRA_RETRIES, TEMPLATE_MIN_CALL_SECONDS,
    TEMPLATE_HTTP_READ_TIMEOUT, TEMPLATE_LEASE_TTL, TEMPLATE_MODELS, TEMPLATE_PROXY_PREFLIGHT_TIMEOUT,
    TEMPLATE_MODEL_COOLDOWN, TEMPLATE_RESERVE_KEYS_FOR_OCR, TEMPLATE_THINKING_BUDGET,
    TEMPLATE_THINKING_LEVEL, TEMPLATE_QUOTA_BLOCK_SECONDS, TEMPLATE_YIELD_TO_OCR,
    TEMPLATE_SLOW_ROUTE_COOLDOWN, TEMPLATE_OVERLOAD_BACKOFF, TEMPLATE_OVERLOAD_BACKOFF_MAX,
    TEMPLATE_OVERLOAD_RETRIES,
)
from ..gemini_client import GenerationTimeout, call_gemini_generate, preflight_template_proxy
from ..gemini_errors import GeminiAPIError, _safe_gemini_message
from ..key_pool import gemini_pool
from ..proxies import ProxyGeoBlocked, ProxyUnavailable, _proxy_label, smart_proxy_pool
from ..telemetry import _job_log, record_template_api_call, record_template_proxy_error

SLOW_ROUTE_PENALTIES = {}


def _active_slow_penalties(now=None):
    now = time.monotonic() if now is None else now
    expired = [pair for pair, item in SLOW_ROUTE_PENALTIES.items() if item["until"] <= now]
    for pair in expired:
        SLOW_ROUTE_PENALTIES.pop(pair, None)
    return SLOW_ROUTE_PENALTIES


def slow_route_penalty_count():
    return len(_active_slow_penalties())


def _penalize_slow_route(key_index, proxy):
    pair = (int(key_index), proxy)
    previous = SLOW_ROUTE_PENALTIES.get(pair)
    hits = int(previous.get("hits", 0)) + 1 if previous else 1
    cooldown = float(TEMPLATE_SLOW_ROUTE_COOLDOWN) * (2 ** min(hits - 1, 8))
    SLOW_ROUTE_PENALTIES[pair] = {
        "hits": hits,
        "cooldown_s": cooldown,
        "until": time.monotonic() + cooldown,
    }
    return hits, cooldown


def _clear_slow_route(key_index, proxy):
    SLOW_ROUTE_PENALTIES.pop((int(key_index), proxy), None)
    _active_slow_penalties()


def _set_job_stage(job_id, stage):
    try:
        from ..routers.template import TEMPLATE_JOBS
        job = TEMPLATE_JOBS.get(job_id)
        if job is not None and job.get("status") == "running":
            job["stage"] = stage
    except Exception:
        pass


def _append_job_api_trace(job_id, trace):
    try:
        from ..routers.template import TEMPLATE_JOBS
        job = TEMPLATE_JOBS.get(job_id)
        if job is not None:
            rows = job.setdefault("api_trace", [])
            item = dict(trace)
            item["n"] = len(rows) + 1
            rows.append(item)
            return item
    except Exception:
        pass
    return trace


class GenerationDeadline(TimeoutError):
    """Недостаточно времени на безопасный вызов Gemini."""

    def __init__(self, code: str = "deadline"):
        self.code = code
        super().__init__(code)


class TemplateGenerationError(RuntimeError):
    def __init__(self, code: str, error_detail: dict | None = None):
        self.code = code
        self.error_detail = error_detail
        super().__init__(code)


def _safe_attempt_message(error, contents=None, system_instruction="") -> str:
    if isinstance(error, GeminiAPIError):
        message = _safe_gemini_message(error.message)
        prompt_texts = [system_instruction or ""]
        for item in contents or ():
            for part in item.get("parts", ()) if isinstance(item, dict) else ():
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    prompt_texts.append(part["text"])
        for source in prompt_texts:
            if len(source) < 12:
                continue
            for _ in range(3):
                match = SequenceMatcher(None, message, source, autojunk=False).find_longest_match(
                    0, len(message), 0, len(source),
                )
                if match.size < 12:
                    break
                message = (
                    message[:match.a] + "[содержимое запроса скрыто]"
                    + message[match.a + match.size:]
                )
        message = re.sub(
            r"(?i)\b(?:https?|socks4a?|socks5h?)://[^\s\"'<>]+",
            "[URL скрыт]",
            message,
        )
        return message[:200]
    if isinstance(error, GenerationTimeout):
        return "Gemini не ответил до истечения таймаута."
    if isinstance(error, ProxyUnavailable):
        return "Сетевой канал недоступен."
    return _safe_gemini_message("Не удалось выполнить вызов Gemini.")[:200]


def _public_error_code(error) -> str:
    if isinstance(error, GeminiAPIError):
        if (
            error.http_status == 401
            or error.category in {"INVALID_API_KEY", "PROJECT_ACCESS_DENIED"}
        ):
            return "key_rejected"
        if (
            error.http_status in (402, 429)
            or error.category == "RATE_LIMIT_OR_QUOTA"
            or error.category.startswith("BILLING_")
        ):
            return "quota"
        if error.http_status in (403, 404):
            return "model_rejected"
        if error.http_status == 400:
            return "invalid_request"
        if error.http_status >= 500 or error.category == "UPSTREAM_UNAVAILABLE":
            return "upstream_unavailable"
    if isinstance(error, GenerationTimeout):
        return "upstream_unavailable"
    return "generation_unavailable"


def _error_detail(
    category: str,
    error=None,
    *,
    model=None,
    key_name=None,
    api_attempts=0,
    proxy_preflight_failures=0,
    attempt_times_ms=(),
    contents=None,
    system_instruction="",
    error_code=None,
    api_trace=(),
) -> dict:
    api_error = error if isinstance(error, GeminiAPIError) else None
    provider_status = (
        re.sub(r"[^A-Za-z0-9_.-]", "", api_error.provider_status or "")[:80]
        if api_error else ""
    )
    return {
        "category": (api_error.category if api_error else category or "UNKNOWN")[:80],
        "http_status": api_error.http_status if api_error else None,
        "provider_status": provider_status,
        "safe_message": _safe_attempt_message(error, contents, system_instruction),
        "model": str(model)[:100] if model else None,
        "key_name": str(key_name)[:128] if key_name else None,
        "api_attempts": int(api_attempts),
        "proxy_preflight_failures": int(proxy_preflight_failures),
        "attempt_times_ms": [max(0, int(value)) for value in attempt_times_ms],
        "api_trace": [dict(item) for item in api_trace],
        **({"error_code": error_code} if error_code else {}),
    }


def thinking_config_for_model(model: str) -> tuple[dict | None, str]:
    """Выбирает только ту форму thinkingConfig, которую понимает конкретная модель."""
    name = (model or "").lower().split("/")[-1]
    if name.startswith("gemini-2.5-"):
        budget = TEMPLATE_THINKING_BUDGET
        if "pro" in name:
            if budget == 0:
                return None, "model-default (2.5 Pro cannot disable thinking)"
            if budget != -1 and not 128 <= budget <= 32768:
                return None, "model-default (unsupported 2.5 Pro budget)"
        elif budget != -1 and not 0 <= budget <= 24576:
            return None, "model-default (unsupported 2.5 Flash budget)"
        return {"thinkingBudget": budget}, f"budget:{budget}"

    if name == "gemini-4-argon":
        supported = {"low", "medium", "high"}
    elif name.startswith("gemini-3.8-flash") or name.startswith("gemini-3.7-flash"):
        supported = {"low", "medium", "high"}
    elif (
        name.startswith("gemini-3.")
        or name.startswith("gemini-3-")
    ):
        supported = {"minimal", "low", "medium", "high"}
    else:
        return None, "model-default"
    if TEMPLATE_THINKING_LEVEL not in supported:
        return None, f"model-default (unsupported level:{TEMPLATE_THINKING_LEVEL})"
    return {"thinkingLevel": TEMPLATE_THINKING_LEVEL}, f"level:{TEMPLATE_THINKING_LEVEL}"


async def run_template_selfcheck(model: str) -> dict:
    """Один короткий проверочный вызов; независимо от ответа ключ освобождается здоровым."""
    started = time.monotonic()
    proxy = None
    idx = None
    step = "proxy"
    http_status = None
    provider_status = ""
    safe_message = ""
    try:
        if not model:
            step = "model"
            safe_message = "TEMPLATE_MODELS не настроены."
            return {
                "ok": False, "model": None, "http_status": None, "provider_status": "",
                "safe_message": safe_message, "step": step,
                "time": round(time.monotonic() - started, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        try:
            proxy = smart_proxy_pool.next()
            await preflight_template_proxy(
                proxy, timeout=min(TEMPLATE_PROXY_PREFLIGHT_TIMEOUT, 19.0),
            )
        except (ProxyUnavailable, LookupError) as exc:
            if proxy is not None and isinstance(exc, ProxyUnavailable):
                smart_proxy_pool.mark_failed(proxy, exc)
            safe_message = _safe_attempt_message(exc)
            return {
                "ok": False, "model": model, "http_status": None, "provider_status": "",
                "safe_message": safe_message, "step": "proxy",
                "time": round(time.monotonic() - started, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }

        step = "key"
        idx, _key_name = gemini_pool.acquire(
            lease_ttl=30, reserve=0, model_chain=[model], respect_key_cooldown=True,
        )
        if idx is None:
            safe_message = "Нет свободного Gemini-ключа для выбранной модели."
            return {
                "ok": False, "model": model, "http_status": None, "provider_status": "",
                "safe_message": safe_message, "step": step,
                "time": round(time.monotonic() - started, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }

        step = "model"
        try:
            remaining = max(1.0, 20.0 - (time.monotonic() - started))
            text, _metadata = await call_gemini_generate(
                contents=[{"role": "user", "parts": [{"text": "Ответь: ok"}]}],
                system_instruction="",
                model=model,
                key=gemini_pool.keys[idx],
                proxy_url=proxy,
                generation_config={"maxOutputTokens": 16},
                profile="long",
                hard_timeout=remaining,
            )
            http_status = 200
            provider_status = "OK"
            safe_message = "Модель ответила." if text.strip() else "Gemini вернул пустой ответ."
            if proxy is not None:
                smart_proxy_pool.mark_ok(proxy)
            return {
                "ok": bool(text.strip()), "model": model, "http_status": http_status,
                "provider_status": provider_status, "safe_message": safe_message,
                "step": "ok" if text.strip() else step,
                "time": round(time.monotonic() - started, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        except GeminiAPIError as exc:
            http_status = exc.http_status
            provider_status = re.sub(r"[^A-Za-z0-9_.-]", "", exc.provider_status or "")[:80]
            safe_message = _safe_attempt_message(
                exc, [{"role": "user", "parts": [{"text": "Ответь: ok"}]}], "",
            )
            # HTTP-ответ доказывает, что канал доставил запрос; self-check не штрафует ключ.
            if proxy is not None:
                smart_proxy_pool.mark_ok(proxy)
            return {
                "ok": False, "model": model, "http_status": http_status,
                "provider_status": provider_status, "safe_message": safe_message,
                "step": step, "time": round(time.monotonic() - started, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        except GenerationTimeout as exc:
            safe_message = _safe_attempt_message(exc)
            return {
                "ok": False, "model": model, "http_status": None, "provider_status": "",
                "safe_message": safe_message, "step": step,
                "time": round(time.monotonic() - started, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        except ProxyUnavailable as exc:
            safe_message = _safe_attempt_message(exc)
            if proxy is not None:
                smart_proxy_pool.mark_failed(proxy, exc)
            return {
                "ok": False, "model": model, "http_status": None, "provider_status": "",
                "safe_message": safe_message, "step": step,
                "time": round(time.monotonic() - started, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
    finally:
        if idx is not None:
            gemini_pool.release(idx)


def _ocr_busy() -> bool:
    from ..jobs import JOBS as OCR_JOBS

    queued = any(
        job.get("status") == "pending" and job.get("kind", "ocr_sheet") == "ocr_sheet"
        for job in OCR_JOBS.values()
    )
    running = sum(
        1 for job in OCR_JOBS.values()
        if job.get("status") == "running" and job.get("kind", "ocr_sheet") == "ocr_sheet"
    )
    live = sum(1 for idx in range(len(gemini_pool.keys)) if not gemini_pool.dead[idx] and not gemini_pool._is_operator_hold(idx))
    return queued or (live == 1 and running > 0)


async def generate(contents, system_instruction, model_chain, job_id, generation_config, deadline_ts=None):
    deadline = deadline_ts or (time.monotonic() + TEMPLATE_HARD_TIMEOUT)
    last_error = None
    last_error_detail = None
    api_attempts = 0
    yielded_to_ocr = False
    excluded_proxies = set()
    preflight_passed = set()
    attempt_times_ms = []
    api_trace = []
    wait_ocr_ms = 0
    wait_key_ms = 0
    previous_slow_key = None
    previous_slow_proxy = None
    previous_model_rejection = None
    dead_key_refunds = 0   # ключи с PROJECT_ACCESS_DENIED не тратят бюджет повторов (до 3 раз за вызов)
    timeout_hits = 0       # таймауты в рамках одного вызова generate()
    overload_refunds = 0   # 503/5xx «высокий спрос» не тратят бюджет повторов (до TEMPLATE_OVERLOAD_RETRIES)
    pending_overload_sleep = 0.0

    def detail_for(code, category=None, error=None, model=None, key_name=None):
        return _error_detail(
            category or "UNKNOWN", error, model=model, key_name=key_name,
            api_attempts=api_attempts,
            proxy_preflight_failures=len(excluded_proxies),
            attempt_times_ms=attempt_times_ms,
            contents=contents, system_instruction=system_instruction,
            error_code=code, api_trace=api_trace,
        )

    while api_attempts - dead_key_refunds - overload_refunds < TEMPLATE_INFRA_RETRIES:
        remaining = deadline - time.monotonic() - TEMPLATE_DEADLINE_MARGIN
        if remaining < TEMPLATE_MIN_CALL_SECONDS:
            code = "ocr_priority_timeout" if yielded_to_ocr else "deadline"
            detail = dict(last_error_detail or detail_for(code, "DEADLINE", model=(model_chain or [None])[0]))
            detail.update(error_code=code, api_attempts=api_attempts,
                          proxy_preflight_failures=len(excluded_proxies),
                          attempt_times_ms=list(attempt_times_ms))
            raise TemplateGenerationError(code, detail)
        if TEMPLATE_YIELD_TO_OCR:
            while _ocr_busy():
                yielded_to_ocr = True
                _set_job_stage(job_id, "waiting_ocr")
                if time.monotonic() + TEMPLATE_MIN_CALL_SECONDS >= deadline - TEMPLATE_DEADLINE_MARGIN:
                    code = "ocr_priority_timeout"
                    detail = dict(last_error_detail or detail_for(code, "OCR_PRIORITY_TIMEOUT",
                                                                   model=(model_chain or [None])[0]))
                    detail.update(error_code=code, api_attempts=api_attempts,
                                  proxy_preflight_failures=len(excluded_proxies),
                                  attempt_times_ms=list(attempt_times_ms))
                    raise TemplateGenerationError(code, detail)
                wait_started = time.monotonic()
                await asyncio.sleep(min(
                    2.0,
                    max(0.1, deadline - time.monotonic() - TEMPLATE_DEADLINE_MARGIN - TEMPLATE_MIN_CALL_SECONDS),
                ))
                wait_ocr_ms += round((time.monotonic() - wait_started) * 1000)
        remaining = deadline - time.monotonic() - TEMPLATE_DEADLINE_MARGIN
        if remaining < TEMPLATE_MIN_CALL_SECONDS:
            code = "ocr_priority_timeout" if yielded_to_ocr else "deadline"
            detail = dict(last_error_detail or detail_for(code, "DEADLINE", model=(model_chain or [None])[0]))
            detail.update(error_code=code, api_attempts=api_attempts,
                          proxy_preflight_failures=len(excluded_proxies),
                          attempt_times_ms=list(attempt_times_ms))
            raise TemplateGenerationError(code, detail)
        route_exclusions = set(excluded_proxies)
        if previous_slow_key is not None and len(smart_proxy_pool.routes) > 1:
            route_exclusions.add(previous_slow_proxy)
        try:
            proxy = smart_proxy_pool.next(excluded=route_exclusions)
        except LookupError as exc:
            if route_exclusions != excluded_proxies:
                proxy = smart_proxy_pool.next(excluded=excluded_proxies)
            else:
                code = "no_proxy"
                detail = dict(last_error_detail or detail_for(code, "NO_PROXY", model=(model_chain or [None])[0]))
                detail.update(error_code=code, api_attempts=api_attempts,
                              proxy_preflight_failures=len(excluded_proxies),
                              attempt_times_ms=list(attempt_times_ms))
                raise TemplateGenerationError(code, detail) from exc
        preflight_ms = 0
        if proxy is not None and proxy not in preflight_passed:
            preflight_started = time.monotonic()
            try:
                await preflight_template_proxy(proxy, timeout=TEMPLATE_PROXY_PREFLIGHT_TIMEOUT)
                preflight_passed.add(proxy)
                preflight_ms = round((time.monotonic() - preflight_started) * 1000)
            except ProxyUnavailable as exc:
                excluded_proxies.add(proxy)
                smart_proxy_pool.mark_failed(proxy, exc)
                record_template_proxy_error("proxy_preflight_failed")
                last_error = exc
                last_error_detail = detail_for(
                    "no_proxy", "PROXY_PREFLIGHT_FAILED", exc,
                    model=(model_chain or [None])[0],
                )
                _job_log(
                    "tpl_gemini_error", job_id, component="email_template",
                    error_type=type(exc).__name__, category="proxy_preflight_failed",
                    latency_ms=round((time.monotonic() - preflight_started) * 1000),
                    http_status=None, provider_status="", key_name=None,
                    safe_message=last_error_detail["safe_message"],
                )
                continue
        live = sum(1 for idx in range(len(gemini_pool.keys)) if not gemini_pool.dead[idx] and not gemini_pool._is_operator_hold(idx))
        reserve = min(TEMPLATE_RESERVE_KEYS_FOR_OCR, max(live - 1, 0))
        wait_key_started = time.monotonic()
        _set_job_stage(job_id, "waiting_key")
        active_penalties = _active_slow_penalties()
        excluded_keys = {
            key_idx for (key_idx, route), penalty in active_penalties.items()
            if route == proxy and penalty["until"] > time.monotonic()
        }
        if previous_slow_key is not None:
            excluded_keys.add(previous_slow_key)
        idx, key_name = gemini_pool.acquire(
            lease_ttl=TEMPLATE_LEASE_TTL, reserve=reserve, model_chain=model_chain,
            respect_key_cooldown=True, exclude_indices=excluded_keys,
        )
        if idx is None and excluded_keys:
            idx, key_name = gemini_pool.acquire(
                lease_ttl=TEMPLATE_LEASE_TTL, reserve=reserve, model_chain=model_chain,
                respect_key_cooldown=True,
            )
        wait_key_ms += round((time.monotonic() - wait_key_started) * 1000)
        if idx is None:
            if not gemini_pool.keys:
                detail = dict(last_error_detail or detail_for(
                    "key_rejected", "NO_KEYS", model=(model_chain or [None])[0],
                ))
                detail.update(error_code="key_rejected", safe_message="Нет настроенных ключей Gemini.")
                raise TemplateGenerationError("key_rejected", detail)
            live_keys = [
                i for i in range(len(gemini_pool.keys))
                if not gemini_pool.dead[i] and not gemini_pool._is_operator_hold(i)
            ]
            if not live_keys:
                detail = dict(last_error_detail or detail_for(
                    "key_rejected", "NO_LIVE_KEYS", model=(model_chain or [None])[0],
                ))
                detail.update(error_code="key_rejected", safe_message="Нет активных ключей Gemini.")
                raise TemplateGenerationError("key_rejected", detail)
            allowed = [
                (i, model) for i in live_keys for model in model_chain
                if gemini_pool.model_allowed(i, model, respect_key_cooldown=False)
            ]
            available = [
                pair for pair in allowed
                if gemini_pool.model_allowed(pair[0], pair[1], respect_key_cooldown=True)
            ]
            if allowed and not available and previous_model_rejection:
                detail = dict(previous_model_rejection["detail"])
                code = detail.get("error_code") or "model_rejected"
                detail.update(error_code=code, api_attempts=api_attempts,
                              proxy_preflight_failures=len(excluded_proxies),
                              attempt_times_ms=list(attempt_times_ms))
                raise TemplateGenerationError(code, detail)
            if allowed and not available and all(
                time.monotonic() < gemini_pool.key_model_daily_blocked.get(pair, 0.0)
                for pair in allowed
            ):
                detail = dict(last_error_detail or detail_for(
                    "quota", "DAILY_QUOTA", model=(model_chain or [None])[0],
                ))
                detail.update(error_code="quota")
                raise TemplateGenerationError("quota", detail)
            if not allowed:
                detail = dict(last_error_detail or detail_for(
                    "model_rejected", "MODEL_UNAVAILABLE", model=(model_chain or [None])[0],
                ))
                detail.update(error_code="model_rejected",
                              safe_message="Нет ключа Gemini, которому доступна выбранная модель.")
                raise TemplateGenerationError("model_rejected", detail)
            ocr_busy_now = _ocr_busy()
            if ocr_busy_now:
                yielded_to_ocr = True
                _set_job_stage(job_id, "waiting_ocr")
            else:
                _set_job_stage(job_id, "waiting_key")
            wait_started = time.monotonic()
            await asyncio.sleep(min(GEMINI_ACQUIRE_POLL, max(0.05, deadline - time.monotonic())))
            waited = round((time.monotonic() - wait_started) * 1000)
            wait_key_ms += waited
            if ocr_busy_now:
                wait_ocr_ms += waited
            continue
        api_attempts += 1
        model = gemini_pool.pick_model(idx, chain=model_chain, respect_key_cooldown=True)
        started = time.monotonic()
        request_error = None
        error_category = None
        model_rejected_twice = False
        succeeded = False
        text = ""
        metadata = {}
        http_status = None
        timed_out = False
        cancelled = False
        _set_job_stage(job_id, "calling")
        try:
            timeout = min(TEMPLATE_HARD_TIMEOUT, remaining)
            request_config = dict(generation_config or {})
            thinking_config, thinking_setting = thinking_config_for_model(model)
            request_config.pop("thinkingConfig", None)
            if thinking_config is not None:
                request_config["thinkingConfig"] = thinking_config
            text, metadata = await call_gemini_generate(
                contents=contents, system_instruction=system_instruction, model=model,
                key=gemini_pool.keys[idx], proxy_url=proxy, generation_config=request_config,
                profile="long", hard_timeout=timeout,
            )
            metadata["thinking_setting"] = thinking_setting
            smart_proxy_pool.mark_ok(proxy)
            gemini_pool.release(idx)
            _clear_slow_route(idx, proxy)
            succeeded = True
        except GenerationTimeout as exc:
            last_error = exc
            request_error = exc
            error_category = "generation_timeout"
            timed_out = True
            record_template_proxy_error("generation_timeout")
            previous_slow_key, previous_slow_proxy = idx, proxy
            timeout_hits += 1
            if timeout_hits <= 1:
                penalty_hits, penalty_seconds = _penalize_slow_route(idx, proxy)
                _job_log(
                    "tpl_slow_route_penalty", job_id, component="email_template",
                    key_name=key_name, proxy=_proxy_label(proxy),
                    hits=penalty_hits, cooldown_s=round(penalty_seconds),
                    slow_route_penalties=slow_route_penalty_count(),
                )
            else:
                # Тот же запрос завис и на другом маршруте: причина в модели/размере запроса,
                # а не в канале. Маршрут не штрафуем, но ротацию ключа и канала сохраняем.
                _job_log(
                    "tpl_slow_route_skip_penalty", job_id, component="email_template",
                    key_name=key_name, proxy=_proxy_label(proxy), timeouts_in_call=timeout_hits,
                )
            # Не удерживаем ключ на дополнительную проверку канала.
            gemini_pool.release(idx)
            if proxy is not None:
                try:
                    await preflight_template_proxy(proxy, timeout=TEMPLATE_PROXY_PREFLIGHT_TIMEOUT)
                except ProxyUnavailable as probe_exc:
                    last_error = probe_exc
                    request_error = probe_exc
                    error_category = "proxy_preflight_failed"
                    excluded_proxies.add(proxy)
                    smart_proxy_pool.mark_failed(proxy, probe_exc)
        except ProxyUnavailable as exc:
            last_error = exc
            request_error = exc
            error_category = "proxy_unavailable"
            excluded_proxies.add(proxy)
            smart_proxy_pool.mark_failed(proxy, exc)
        except GeminiAPIError as exc:
            last_error = exc
            request_error = exc
            http_status = exc.http_status
            if exc.category == "PROJECT_ACCESS_DENIED" and dead_key_refunds < 3:
                dead_key_refunds += 1
            if exc.category == "RATE_LIMIT_OR_QUOTA":
                gemini_pool.release(idx, error=exc, model=model)
            elif (
                (
                    exc.http_status in (400, 404)
                    and exc.category not in {
                        "INVALID_API_KEY", "BILLING_CREDITS_DEPLETED",
                        "BILLING_PAYMENT_REQUIRED", "BILLING_SPEND_CAP_BREACHED",
                        "PROJECT_ACCESS_DENIED",
                    }
                )
                or (exc.http_status == 403 and exc.category == "PROJECT_PERMISSION_DENIED")
            ):
                gemini_pool.block_model_for_key(idx, model, TEMPLATE_QUOTA_BLOCK_SECONDS)
                gemini_pool.release(idx)
            elif exc.http_status in (500, 502, 503, 504) or exc.category == "UPSTREAM_UNAVAILABLE":
                gemini_pool.release(idx)
            else:
                gemini_pool.release(idx, error=exc)
            if exc.http_status in (500, 502, 503, 504):
                gemini_pool.mark_model_overloaded(model, TEMPLATE_MODEL_COOLDOWN)
            if (
                (exc.http_status in (500, 502, 503, 504) or exc.category == "UPSTREAM_UNAVAILABLE")
                and overload_refunds < TEMPLATE_OVERLOAD_RETRIES
            ):
                overload_refunds += 1
                pending_overload_sleep = min(
                    float(TEMPLATE_OVERLOAD_BACKOFF_MAX), float(TEMPLATE_OVERLOAD_BACKOFF) * overload_refunds,
                )
        except asyncio.CancelledError as exc:
            request_error = exc
            cancelled = True
        finally:
            gemini_pool.ensure_released(idx)
            call_ms = round((time.monotonic() - started) * 1000)
            attempt_times_ms.append(call_ms)
        trace_item = {
            "n": api_attempts, "wait_ocr_ms": wait_ocr_ms,
            "wait_key_ms": wait_key_ms, "preflight_ms": preflight_ms,
            "call_ms": call_ms, "key_name": key_name or "",
            "proxy": _proxy_label(proxy), "model": model,
            "outcome": "success" if succeeded else "cancelled" if cancelled else (
                "timeout" if timed_out
                else "api_error" if isinstance(request_error, GeminiAPIError)
                else "proxy_error"
            ),
            "http_status": http_status,
        }
        trace_item = _append_job_api_trace(job_id, trace_item)
        api_trace.append(trace_item)
        record_template_api_call(trace_item)
        _job_log("tpl_api_attempt", job_id, component="email_template", **trace_item)
        wait_ocr_ms = 0
        wait_key_ms = 0
        if cancelled:
            raise asyncio.CancelledError
        if succeeded:
            metadata["api_trace"] = list(api_trace)
            _set_job_stage(job_id, "retrying")
            return text, metadata, model
        if error_category and error_category != "generation_timeout":
            record_template_proxy_error(error_category)
        code = _public_error_code(request_error) if request_error else "generation_unavailable"
        key_level_error = isinstance(request_error, GeminiAPIError) and (
            request_error.category in {"INVALID_API_KEY", "PROJECT_ACCESS_DENIED"}
            or request_error.category.startswith("BILLING_")
        )
        if isinstance(request_error, GeminiAPIError):
            if key_level_error:
                # Отказ касается ключа/проекта, а не модели: два «залоченных» ключа подряд
                # не должны давать быстрый model_rejected.
                pass
            elif request_error.http_status in (400, 403, 404):
                if (
                    previous_model_rejection
                    and previous_model_rejection["model"] == model
                    and previous_model_rejection["idx"] != idx
                ):
                    code = "model_rejected"
                    model_rejected_twice = True
                previous_model_rejection = {"model": model, "idx": idx, "detail": None}
            else:
                previous_model_rejection = None
        last_error_detail = _error_detail(
            request_error.category if isinstance(request_error, GeminiAPIError) else (
                error_category or type(request_error).__name__.upper() if request_error else "UNKNOWN"
            ),
            request_error, model=model, key_name=key_name,
            api_attempts=api_attempts, proxy_preflight_failures=len(excluded_proxies),
            attempt_times_ms=attempt_times_ms, contents=contents,
            system_instruction=system_instruction, error_code=code,
            api_trace=api_trace,
        )
        _set_job_stage(job_id, "retrying")
        if (
            previous_model_rejection
            and previous_model_rejection["model"] == model
            and previous_model_rejection["idx"] == idx
        ):
            previous_model_rejection["detail"] = last_error_detail
        _job_log(
            "tpl_gemini_error", job_id, component="email_template", model=model,
            latency_ms=attempt_times_ms[-1],
            error_type=type(request_error).__name__ if request_error else "unknown",
            category=last_error_detail["category"],
            http_status=last_error_detail["http_status"],
            provider_status=last_error_detail["provider_status"],
            key_name=key_name,
            safe_message=last_error_detail["safe_message"],
        )
        if model_rejected_twice:
            raise TemplateGenerationError("model_rejected", last_error_detail) from request_error
        if pending_overload_sleep > 0:
            # Пауза между повторами при перегрузке модели; не дольше остатка дедлайна.
            left = deadline - time.monotonic() - TEMPLATE_DEADLINE_MARGIN - TEMPLATE_MIN_CALL_SECONDS
            await asyncio.sleep(max(0.0, min(pending_overload_sleep, left)))
            pending_overload_sleep = 0.0
    if isinstance(last_error, ProxyUnavailable):
        detail = dict(last_error_detail or detail_for("no_proxy", "PROXY_UNAVAILABLE", last_error))
        detail.update(error_code="no_proxy", api_attempts=api_attempts,
                      proxy_preflight_failures=len(excluded_proxies),
                      attempt_times_ms=list(attempt_times_ms))
        raise TemplateGenerationError("no_proxy", detail) from last_error
    if time.monotonic() + TEMPLATE_MIN_CALL_SECONDS >= deadline - TEMPLATE_DEADLINE_MARGIN:
        code = "ocr_priority_timeout" if yielded_to_ocr else "deadline"
        detail = dict(last_error_detail or detail_for(code, "DEADLINE", last_error))
        detail.update(error_code=code, api_attempts=api_attempts,
                      proxy_preflight_failures=len(excluded_proxies),
                      attempt_times_ms=list(attempt_times_ms))
        raise TemplateGenerationError(code, detail) from last_error
    code = _public_error_code(last_error) if last_error else "generation_unavailable"
    detail = dict(last_error_detail or detail_for(code, "UNKNOWN", last_error))
    detail.update(error_code=code, api_attempts=api_attempts,
                  proxy_preflight_failures=len(excluded_proxies),
                  attempt_times_ms=list(attempt_times_ms))
    raise TemplateGenerationError(code, detail) from last_error
