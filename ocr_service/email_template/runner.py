"""Вызов Gemini с отдельным профилем таймаута и резервом ключей для OCR."""
import asyncio
import time

from ..config import (
    GEMINI_ACQUIRE_POLL, TEMPLATE_CONNECT_TIMEOUT, TEMPLATE_DEADLINE_MARGIN,
    TEMPLATE_HARD_TIMEOUT, TEMPLATE_INFRA_RETRIES, TEMPLATE_MIN_CALL_SECONDS,
    TEMPLATE_HTTP_READ_TIMEOUT, TEMPLATE_LEASE_TTL, TEMPLATE_MODELS, TEMPLATE_PROXY_PREFLIGHT_TIMEOUT,
    TEMPLATE_MODEL_COOLDOWN, TEMPLATE_RESERVE_KEYS_FOR_OCR, TEMPLATE_THINKING_BUDGET,
    TEMPLATE_THINKING_LEVEL, TEMPLATE_YIELD_TO_OCR,
)
from ..gemini_client import GenerationTimeout, call_gemini_generate, preflight_template_proxy
from ..gemini_errors import GeminiAPIError
from ..key_pool import gemini_pool
from ..proxies import ProxyGeoBlocked, ProxyUnavailable, smart_proxy_pool
from ..telemetry import _job_log, record_template_proxy_error


class GenerationDeadline(TimeoutError):
    """Недостаточно времени на безопасный вызов Gemini."""

    def __init__(self, code: str = "deadline"):
        self.code = code
        super().__init__(code)


class TemplateGenerationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


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
    api_attempts = 0
    yielded_to_ocr = False
    excluded_proxies = set()
    preflight_passed = set()
    while api_attempts < TEMPLATE_INFRA_RETRIES:
        remaining = deadline - time.monotonic() - TEMPLATE_DEADLINE_MARGIN
        if remaining < TEMPLATE_MIN_CALL_SECONDS:
            raise TemplateGenerationError("ocr_priority_timeout" if yielded_to_ocr else "deadline")
        if TEMPLATE_YIELD_TO_OCR:
            while _ocr_busy():
                yielded_to_ocr = True
                if time.monotonic() + TEMPLATE_MIN_CALL_SECONDS >= deadline - TEMPLATE_DEADLINE_MARGIN:
                    raise TemplateGenerationError("ocr_priority_timeout")
                await asyncio.sleep(min(
                    2.0,
                    max(0.1, deadline - time.monotonic() - TEMPLATE_DEADLINE_MARGIN - TEMPLATE_MIN_CALL_SECONDS),
                ))
        remaining = deadline - time.monotonic() - TEMPLATE_DEADLINE_MARGIN
        if remaining < TEMPLATE_MIN_CALL_SECONDS:
            raise TemplateGenerationError("ocr_priority_timeout" if yielded_to_ocr else "deadline")
        try:
            proxy = smart_proxy_pool.next(excluded=excluded_proxies)
        except LookupError as exc:
            raise TemplateGenerationError("no_proxy") from exc
        if proxy is not None and proxy not in preflight_passed:
            preflight_started = time.monotonic()
            try:
                await preflight_template_proxy(proxy, timeout=TEMPLATE_PROXY_PREFLIGHT_TIMEOUT)
                preflight_passed.add(proxy)
            except ProxyUnavailable as exc:
                excluded_proxies.add(proxy)
                smart_proxy_pool.mark_failed(proxy, exc)
                record_template_proxy_error("proxy_preflight_failed")
                _job_log(
                    "tpl_gemini_error", job_id, component="email_template",
                    error_type=type(exc).__name__, category="proxy_preflight_failed",
                    latency_ms=round((time.monotonic() - preflight_started) * 1000),
                )
                continue
        live = sum(1 for idx in range(len(gemini_pool.keys)) if not gemini_pool.dead[idx] and not gemini_pool._is_operator_hold(idx))
        reserve = min(TEMPLATE_RESERVE_KEYS_FOR_OCR, max(live - 1, 0))
        idx, key_name = gemini_pool.acquire(
            lease_ttl=TEMPLATE_LEASE_TTL, reserve=reserve, model_chain=model_chain,
            respect_key_cooldown=True,
        )
        if idx is None:
            if not gemini_pool.keys:
                raise TemplateGenerationError("no_keys")
            live_keys = [
                i for i in range(len(gemini_pool.keys))
                if not gemini_pool.dead[i] and not gemini_pool._is_operator_hold(i)
            ]
            if not live_keys:
                raise TemplateGenerationError("no_live_keys")
            allowed = [
                (i, model) for i in live_keys for model in model_chain
                if gemini_pool.model_allowed(i, model, respect_key_cooldown=False)
            ]
            available = [
                pair for pair in allowed
                if gemini_pool.model_allowed(pair[0], pair[1], respect_key_cooldown=True)
            ]
            if allowed and not available and all(
                time.monotonic() < gemini_pool.key_model_daily_blocked.get(pair, 0.0)
                for pair in allowed
            ):
                raise TemplateGenerationError("quota")
            if not allowed:
                raise TemplateGenerationError("model_unavailable")
            if _ocr_busy():
                yielded_to_ocr = True
            await asyncio.sleep(min(GEMINI_ACQUIRE_POLL, max(0.05, deadline - time.monotonic())))
            continue
        api_attempts += 1
        model = gemini_pool.pick_model(idx, chain=model_chain, respect_key_cooldown=True)
        started = time.monotonic()
        error = None
        error_category = None
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
            return text, metadata, model
        except GenerationTimeout as exc:
            last_error = exc
            error_category = "generation_timeout"
            # Не удерживаем ключ на дополнительную проверку канала.
            gemini_pool.release(idx)
            if proxy is not None:
                try:
                    await preflight_template_proxy(proxy, timeout=TEMPLATE_PROXY_PREFLIGHT_TIMEOUT)
                except ProxyUnavailable as probe_exc:
                    last_error = probe_exc
                    error_category = "proxy_preflight_failed"
                    excluded_proxies.add(proxy)
                    smart_proxy_pool.mark_failed(proxy, probe_exc)
        except ProxyUnavailable as exc:
            last_error = exc
            error_category = "proxy_unavailable"
            excluded_proxies.add(proxy)
            smart_proxy_pool.mark_failed(proxy, exc)
        except GeminiAPIError as exc:
            last_error = exc
            if exc.category == "RATE_LIMIT_OR_QUOTA":
                gemini_pool.release(idx, error=exc, model=model)
            elif exc.http_status in (404, 500, 502, 503, 504) or exc.category == "UPSTREAM_UNAVAILABLE":
                gemini_pool.release(idx)
            else:
                gemini_pool.release(idx, error=exc)
            if exc.http_status == 404:
                gemini_pool.mark_model_unavailable(idx, model)
            elif exc.http_status in (500, 502, 503, 504):
                gemini_pool.mark_model_overloaded(model, TEMPLATE_MODEL_COOLDOWN)
        finally:
            gemini_pool.ensure_released(idx)
        if error_category:
            record_template_proxy_error(error_category)
        _job_log(
            "tpl_gemini_error", job_id, component="email_template", model=model,
            latency_ms=round((time.monotonic() - started) * 1000),
            error_type=type(last_error).__name__ if last_error else "unknown",
            category=(error_category or
                      ("DAILY_QUOTA" if isinstance(last_error, GeminiAPIError) and last_error.daily_quota
                       else (last_error.category if isinstance(last_error, GeminiAPIError) else "INFRASTRUCTURE"))),
        )
    if isinstance(last_error, ProxyUnavailable):
        raise TemplateGenerationError("no_proxy") from last_error
    if time.monotonic() + TEMPLATE_MIN_CALL_SECONDS >= deadline - TEMPLATE_DEADLINE_MARGIN:
        raise TemplateGenerationError("ocr_priority_timeout" if yielded_to_ocr else "deadline") from last_error
    raise RuntimeError("generation_unavailable:" + (type(last_error).__name__ if last_error else "no_key"))
