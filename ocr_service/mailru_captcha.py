"""Распознавание капчи mail.ru (6 alphanumeric + волнистая линия).

Отдельный путь от BLS contact-sheet: одна картинка, другой промпт,
другой парсер. Использует тот же пул ключей/прокси.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional, Tuple

from .gemini_client import _call_gemini_rest
from .gemini_errors import GeminiAPIError, _safe_gemini_message
from .key_pool import GEMINI_KEYS, gemini_model_name, gemini_pool
from .proxies import ProxyUnavailable, _proxy_label, smart_proxy_pool
from .telemetry import _job_log
from .config import GEMINI_ACQUIRE_POLL

MAILRU_CAPTCHA_PROMPT = """
На картинке капча mail.ru: ровно 6 символов (заглавные латинские буквы A-Z и цифры 0-9).
Через картинку часто проходит чёрная волнистая линия — её полностью игнорируй.
Распознай только 6 символов.

Верни строго JSON без markdown и пояснений:
{"text":"5XM806"}

Правила:
- значение "text" — строка ровно из 6 символов, только A-Z и 0-9;
- если не уверен или не читается — верни {"text":null};
- ничего не придумывай, не добавляй пробелы и не меняй регистр.
""".strip()


def _parse_mailru_text(raw: str) -> Optional[str]:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    text: Optional[str] = None
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            text = data.get("text")
        elif isinstance(data, str):
            text = data
    except json.JSONDecodeError:
        # fallback: вытащить первую последовательность из 6 alphanumeric
        m = re.search(r"[A-Z0-9]{6}", cleaned.upper())
        if m:
            text = m.group(0)

    if text is None:
        return None
    text = str(text).strip().upper()
    text = re.sub(r"[^A-Z0-9]", "", text)
    if len(text) == 6:
        return text
    return None


async def recognize_mailru_captcha(image_bytes: bytes) -> Tuple[str, str]:
    """Возвращает (text, source). text='' если не удалось."""
    if not GEMINI_KEYS or gemini_model_name is None:
        return "", "mailru_gemini_unavailable"

    max_attempts = max(4, min(16, len(GEMINI_KEYS) * 2 + len(smart_proxy_pool.routes)))
    attempted_routes: set = set()
    last_error: Optional[str] = None
    attempt_no = 0

    for _ in range(max_attempts):
        idx, key_name = gemini_pool.acquire()
        if idx is None:
            current = gemini_pool.grouped_keys()
            if not current["ready"] and not current["in_use"] and not current["cooldown"]:
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

        model = gemini_pool.pick_model()
        t0 = time.monotonic()
        try:
            raw_text, response_metadata = await _call_gemini_rest(
                image_bytes,
                MAILRU_CAPTCHA_PROMPT,
                model,
                gemini_pool.keys[idx],
                proxy_url,
                generation_config={
                    "temperature": 0.0,
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 32,
                },
            )
            parsed = _parse_mailru_text(raw_text)
            smart_proxy_pool.mark_ok(proxy_url)
            gemini_pool.release(idx, None)

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
                image_count=1,
                zero_count=0 if parsed else 1,
                usage={
                    field: int(usage[field])
                    for field in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
                    if isinstance(usage, dict) and isinstance(usage.get(field), (int, float))
                },
                response_state={
                    "finish_reason": response_metadata.get("finish_reason", ""),
                    "prompt_block_reason": response_metadata.get("prompt_block_reason", ""),
                    "candidate_count": response_metadata.get("candidate_count", 0),
                    "text_chars": response_metadata.get("text_chars", 0),
                },
                captcha_type="mailru",
            )
            print(
                f"[pid={os.getpid()}][mailru] OK key={key_name} "
                f"proxy={_proxy_label(proxy_url)} text={parsed or 'null'}"
            )
            if parsed:
                return parsed, "mailru_gemini"
            # распознало null — считаем ошибкой и пробуем другой ключ/прокси
            last_error = "parsed_null"
            continue

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
                image_count=1,
                error_type=type(exc).__name__,
                captcha_type="mailru",
            )
        except GeminiAPIError as exc:
            last_error = str(exc)
            smart_proxy_pool.mark_ok(proxy_url)
            if gemini_pool.is_model_side_error(exc):
                # 503 и подобное: перегружена МОДЕЛЬ, а не ключ. Ключ не штрафуем, модель обходим.
                gemini_pool.mark_model_overloaded(model)
                state = gemini_pool.release(idx, None)
            else:
                state = gemini_pool.release(idx, exc)
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
                image_count=1,
                api_message=exc.message,
                captcha_type="mailru",
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
                image_count=1,
                api_message=api_error.message,
                captcha_type="mailru",
            )
        finally:
            # CancelledError не ловится "except Exception": без finally ключ терялся навсегда.
            gemini_pool.ensure_released(idx)

    print(
        f"[pid={os.getpid()}][mailru] fail: {last_error or 'нет доступного ключа/канала'}"
    )
    return "", "mailru_gemini_error"
