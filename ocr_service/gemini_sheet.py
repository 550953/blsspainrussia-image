"""Один Gemini-вызов на чанк contact sheet + точечный повтор по одной картинке."""
import asyncio
import os
import time
from collections import Counter
from typing import List, Optional

from .config import (
    GEMINI_ACQUIRE_POLL,
    GEMINI_EMPTY_RESPONSE_COOLDOWN,
    GEMINI_MODELS,
    OCR_MAX_OUTPUT_TOKENS,
    OCR_THINKING_LEVEL,
    GEMINI_VARIANT_RETRY_BLUR_KSIZE,
    GEMINI_VARIANT_RETRY_COLS,
    GEMINI_VARIANT_RETRY_ENABLED,
    GEMINI_VARIANT_RETRY_MIN_VOTES,
    GEMINI_VARIANT_RETRY_SCALE,
    GEMINI_SHEET_MAX_IMAGES,
)
from .contact_sheet import (
    GEMINI_SHEET_PROMPT,
    GEMINI_VARIANT_RETRY_PROMPT_TEMPLATE,
    make_gemini_contact_sheet,
    parse_gemini_sheet_result,
)
from .gemini_client import _call_gemini_rest
from .gemini_errors import GeminiAPIError, _safe_gemini_message
from .key_pool import GEMINI_KEYS, gemini_model_name, gemini_pool
from .proxies import ProxyUnavailable, _proxy_label, smart_proxy_pool
from .telemetry import _job_log
from .utils import _score
from .variants import make_variants_for_retry


def ocr_thinking_config(model: str) -> Optional[dict]:
    """thinkingConfig для OCR: минимум размышлений, чтобы они не съели лимит ответа.
    lite-модели не трогаем: у них thinking и так почти выключен, и они уже работают."""
    name = (model or "").lower().split("/")[-1]
    if "lite" in name:
        return None
    if name.startswith("gemini-2.5-"):
        return {"thinkingBudget": 128 if "pro" in name else 0}
    level = OCR_THINKING_LEVEL
    if name == "gemini-4-argon" or name.startswith(("gemini-3.8-flash", "gemini-3.7-flash")):
        # эти модели не умеют minimal
        return {"thinkingLevel": "low" if level == "minimal" else level}
    if name.startswith(("gemini-3.", "gemini-3-")):
        return {"thinkingLevel": level}
    return None


async def recognize_gemini_sheet_async(
    images: List[bytes],
    cols: Optional[int] = None,
    prompt: Optional[str] = None,
) -> List[dict]:
    """Один Gemini-вызов на ОДИН чанк (<=GEMINI_SHEET_MAX_IMAGES) с direct-first failover.
    prompt: переопределить промпт (точечный повтор)."""
    if not GEMINI_KEYS or gemini_model_name is None:
        return [{"text": "0", "source": "gemini_sheet_unavailable"} for _ in images]

    key_groups = gemini_pool.grouped_keys()
    if not key_groups["ready"] and not key_groups["in_use"] and not key_groups["cooldown"]:
        all_invalid = bool(key_groups["invalid_key"]) and not (
            key_groups["billing_hold"] or key_groups["project_hold"] or key_groups["quarantine"]
        )
        category = "ALL_KEYS_INVALID" if all_invalid else "ALL_KEYS_ON_HOLD"
        source = "gemini_sheet_keys_invalid" if all_invalid else "gemini_sheet_keys_held"
        _job_log(
            "gemini_keys_unavailable",
            key_profile="pool",
            category=category,
            action="returned_zero_without_retry",
            invalid_key_count=len(key_groups["invalid_key"]),
            billing_hold_count=sum(len(names) for names in key_groups["billing_hold"].values()),
            project_hold_count=sum(len(names) for names in key_groups["project_hold"].values()),
            quarantine_count=sum(len(names) for names in key_groups["quarantine"].values()),
        )
        return [{"text": "0", "source": source} for _ in images]

    try:
        png_bytes = make_gemini_contact_sheet(images, cols=cols)
    except ValueError as exc:
        print(f"[pid={os.getpid()}][gemini_sheet] {exc}")
        return [{"text": "0", "source": "gemini_sheet_error"} for _ in images]

    effective_prompt = prompt if prompt is not None else GEMINI_SHEET_PROMPT.replace(
        f'"{GEMINI_SHEET_MAX_IMAGES}"', f'"{len(images)}"'
    )

    max_attempts = max(4, min(24, len(GEMINI_KEYS) * 2 + len(smart_proxy_pool.routes)))
    attempted_routes: set = set()
    last_error: Optional[str] = None
    attempt_no = 0

    for _ in range(max_attempts):
        idx, key_name = gemini_pool.acquire(model_chain=GEMINI_MODELS, respect_key_cooldown=True)
        if idx is None:
            current_groups = gemini_pool.grouped_keys()
            if not current_groups["ready"] and not current_groups["in_use"] and not current_groups["cooldown"]:
                break
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue

        proxy_url = smart_proxy_pool.next()
        route_key = (idx, proxy_url)
        if route_key in attempted_routes:
            gemini_pool.release(idx)
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue
        attempted_routes.add(route_key)
        attempt_no += 1

        model = gemini_pool.pick_model(idx, respect_key_cooldown=True)
        t0 = time.monotonic()
        response_metadata: dict = {}
        try:
            generation_config = {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "maxOutputTokens": max(OCR_MAX_OUTPUT_TOKENS, len(images) * 12),
            }
            thinking_config = ocr_thinking_config(model)
            if thinking_config is not None:
                generation_config["thinkingConfig"] = thinking_config
            text, response_metadata = await _call_gemini_rest(
                png_bytes,
                effective_prompt,
                model,
                gemini_pool.keys[idx],
                proxy_url,
                generation_config=generation_config,
            )
            values = parse_gemini_sheet_result(text, len(images))
            smart_proxy_pool.mark_ok(proxy_url)
            gemini_pool.release(idx, None, model=model)
            usage = response_metadata.get("usage_metadata") or {}
            _job_log(
                "gemini_attempt",
                key_profile=key_name,
                model=model,
                served_model=response_metadata.get("served_model", model),
                proxy=_proxy_label(proxy_url),
                attempt=attempt_no,
                latency_ms=round((time.monotonic() - t0) * 1000),
                http_status=200,
                category="SUCCESS",
                action="key_released",
                image_count=len(images),
                zero_count=sum(value == "0" for value in values),
                usage={
                    field: int(usage[field])
                    for field in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount", "thoughtsTokenCount")
                    if isinstance(usage, dict) and isinstance(usage.get(field), (int, float))
                },
                response_state={
                    "finish_reason": response_metadata.get("finish_reason", ""),
                    "prompt_block_reason": response_metadata.get("prompt_block_reason", ""),
                    "candidate_count": response_metadata.get("candidate_count", 0),
                    "text_chars": response_metadata.get("text_chars", 0),
                },
            )
            print(
                f"[pid={os.getpid()}][gemini_sheet] OK key={key_name} "
                f"proxy={_proxy_label(proxy_url)} images={len(images)}"
            )
            return [{"text": value, "source": "gemini_sheet"} for value in values]
        except ProxyUnavailable as exc:
            last_error = f"{type(exc).__name__}: {_safe_gemini_message(str(exc))}"
            smart_proxy_pool.mark_failed(proxy_url, exc)
            gemini_pool.release(idx, None)
            _job_log(
                "gemini_proxy_error",
                key_profile=key_name,
                model=model,
                proxy=_proxy_label(proxy_url),
                attempt=attempt_no,
                latency_ms=round((time.monotonic() - t0) * 1000),
                category="PROXY_UNAVAILABLE",
                action="key_released_without_cooldown",
                image_count=len(images),
                error_type=type(exc).__name__,
            )
        except GeminiAPIError as exc:
            last_error = str(exc)
            smart_proxy_pool.mark_ok(proxy_url)
            if exc.http_status == 404:
                # Модель закрыта для этого ключа (например 2.5 для новых проектов): ключ не штрафуем.
                gemini_pool.mark_model_unavailable(idx, model)
                state = gemini_pool.release(idx, None)
            elif gemini_pool.is_model_side_error(exc):
                # 503 и подобное: перегружена МОДЕЛЬ, а не ключ. Ключ не штрафуем, модель обходим.
                gemini_pool.mark_model_overloaded(model)
                state = gemini_pool.release(idx, None)
            else:
                state = gemini_pool.release(idx, exc, model=model)
            _job_log(
                "gemini_api_error",
                key_profile=key_name,
                model=model,
                proxy=_proxy_label(proxy_url),
                attempt=attempt_no,
                latency_ms=round((time.monotonic() - t0) * 1000),
                http_status=exc.http_status,
                provider_status=exc.provider_status,
                category=exc.category,
                action=exc.action,
                cooldown_seconds=exc.cooldown_seconds,
                hold_until_restart=exc.hold_until_restart,
                key_state=state["state"],
                image_count=len(images),
                api_message=exc.message,
            )
        except ValueError as exc:
            # Модель вернула не JSON/битый JSON (JSONDecodeError - подкласс ValueError): ключ и канал
            # исправны, штрафовать нечего. Освобождаем ключ без паузы, следующая попытка пойдёт дальше.
            last_error = f"{type(exc).__name__}: {_safe_gemini_message(str(exc))}"
            smart_proxy_pool.mark_ok(proxy_url)
            state = gemini_pool.release(idx, None)
            usage = response_metadata.get("usage_metadata") or {}
            empty_reply = not response_metadata.get("text_chars", 0)
            if response_metadata and (empty_reply or response_metadata.get("finish_reason") == "MAX_TOKENS"):
                # Модель отвечает, но пусто/обрезанно: повторять на ней бессмысленно, уводим трафик на другие.
                gemini_pool.mark_model_overloaded(model, GEMINI_EMPTY_RESPONSE_COOLDOWN)
            _job_log(
                "gemini_api_error",
                key_profile=key_name,
                model=model,
                proxy=_proxy_label(proxy_url),
                attempt=attempt_no,
                latency_ms=round((time.monotonic() - t0) * 1000),
                http_status=0,
                provider_status="CLIENT_RESPONSE_ERROR",
                category="MODEL_RESPONSE_INVALID",
                action="key_released_without_cooldown; response is not valid JSON",
                cooldown_seconds=0.0,
                key_state=state["state"],
                image_count=len(images),
                api_message=last_error,
                usage={
                    field: int(usage[field])
                    for field in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount", "thoughtsTokenCount")
                    if isinstance(usage, dict) and isinstance(usage.get(field), (int, float))
                },
                response_state={
                    "finish_reason": response_metadata.get("finish_reason", ""),
                    "prompt_block_reason": response_metadata.get("prompt_block_reason", ""),
                    "candidate_count": response_metadata.get("candidate_count", 0),
                    "text_chars": response_metadata.get("text_chars", 0),
                },
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {_safe_gemini_message(str(exc))}"
            smart_proxy_pool.mark_ok(proxy_url)
            policy = {
                "category": "MODEL_OR_RESPONSE_ERROR",
                "cooldown_seconds": 15.0,
                "permanent_key_failure": False,
                "hold_until_restart": False,
                "action": "temporary_key_pause; proxy is healthy",
            }
            api_error = GeminiAPIError(0, "CLIENT_RESPONSE_ERROR", last_error, policy)
            state = gemini_pool.release(idx, api_error)
            _job_log(
                "gemini_api_error",
                key_profile=key_name,
                model=model,
                proxy=_proxy_label(proxy_url),
                attempt=attempt_no,
                latency_ms=round((time.monotonic() - t0) * 1000),
                http_status=0,
                provider_status="CLIENT_RESPONSE_ERROR",
                category=api_error.category,
                action=api_error.action,
                cooldown_seconds=api_error.cooldown_seconds,
                key_state=state["state"],
                image_count=len(images),
                api_message=api_error.message,
            )
        finally:
            # CancelledError не ловится "except Exception": без finally ключ терялся навсегда.
            gemini_pool.ensure_released(idx)

    print(
        f"[pid={os.getpid()}][gemini_sheet] не удалось выполнить запрос: "
        f"{last_error or 'нет доступного ключа/канала'}"
    )
    return [{"text": "0", "source": "gemini_sheet_error"} for _ in images]


async def recognize_gemini_variant_retry(image_bytes: bytes) -> Optional[tuple]:
    """Повторный запрос в Gemini ТОЛЬКО по этой картинке: несколько вариантов контраста,
    один небольшой вызов, голосование. Возвращает (текст, голосов) или None."""
    if not GEMINI_VARIANT_RETRY_ENABLED or not GEMINI_KEYS:
        return None

    variants = make_variants_for_retry(
        image_bytes, scale=GEMINI_VARIANT_RETRY_SCALE, blur_ksize=GEMINI_VARIANT_RETRY_BLUR_KSIZE
    )
    if not variants:
        return None

    prompt = GEMINI_VARIANT_RETRY_PROMPT_TEMPLATE.format(n=len(variants))
    sheet_results = await recognize_gemini_sheet_async(variants, cols=GEMINI_VARIANT_RETRY_COLS, prompt=prompt)
    votes = Counter(r["text"] for r in sheet_results if r["text"] != "0" and r["source"] == "gemini_sheet")
    if not votes:
        return None

    top_text, top_freq = sorted(votes.items(), key=_score, reverse=True)[0]
    if top_freq < GEMINI_VARIANT_RETRY_MIN_VOTES:
        return None
    return top_text, top_freq
