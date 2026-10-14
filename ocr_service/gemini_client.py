"""Низкоуровневый REST-вызов Gemini через httpx (по одному клиенту на канал)."""
import asyncio
import base64
import os
from typing import Optional

import httpx

from .gemini_errors import GeminiAPIError, _classify_gemini_http_error, is_geo_blocked
from .proxies import ProxyGeoBlocked, ProxyUnavailable

# Ключ передаётся заголовком x-goog-api-key, а не в URL: так он не попадает в логи httpx.
GEMINI_REST_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Один httpx.AsyncClient на канал (keep-alive), а не новый TCP+TLS на каждый вызов.
_HTTPX_CLIENTS: dict = {}

# Жёсткий потолок на ВЕСЬ вызов. httpx-timeout не покрывает SOCKS5-handshake:
# прокси, принявший TCP и замолчавший, вешает запрос навсегда.
GEMINI_HARD_TIMEOUT = float(os.getenv("GEMINI_HARD_TIMEOUT", "45"))
# Таймаут чтения/записи и подключения для одной попытки. Обычный ответ 2-5с, поэтому 15с хватает с запасом,
# а зависший канал обнаруживается быстрее, и задание успевает сделать больше попыток до дедлайна.
GEMINI_HTTP_TIMEOUT = float(os.getenv("GEMINI_HTTP_TIMEOUT", "15"))
GEMINI_CONNECT_TIMEOUT = float(os.getenv("GEMINI_CONNECT_TIMEOUT", "8"))
TEMPLATE_PROXY_PREFLIGHT_URL = "https://generativelanguage.googleapis.com/"


async def preflight_template_proxy(proxy_url: Optional[str], timeout: Optional[float] = None) -> None:
    """Проверяет только транспорт прокси, не используя ключ Gemini и профиль короткого OCR."""
    if proxy_url is None:
        return
    from .config import TEMPLATE_PROXY_PREFLIGHT_TIMEOUT

    limit = max(0.1, float(timeout if timeout is not None else TEMPLATE_PROXY_PREFLIGHT_TIMEOUT))
    request_timeout = httpx.Timeout(limit, connect=min(limit, 3.0))
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=request_timeout, trust_env=False) as client:
            # Любой HTTP-ответ доказывает доступность канала; проверка не вызывает Gemini API.
            await asyncio.wait_for(client.get(TEMPLATE_PROXY_PREFLIGHT_URL), timeout=limit)
    except Exception as exc:
        # Не включаем URL прокси, credentials или upstream response в текст ошибки.
        raise ProxyUnavailable(f"proxy preflight failed ({type(exc).__name__})") from exc


def _get_http_client(proxy_url: Optional[str], profile: str = "short") -> httpx.AsyncClient:
    key = (proxy_url, profile)
    client = _HTTPX_CLIENTS.get(key)
    if client is None:
        if profile == "long":
            from .config import TEMPLATE_CONNECT_TIMEOUT, TEMPLATE_HTTP_READ_TIMEOUT
            timeout = httpx.Timeout(TEMPLATE_HTTP_READ_TIMEOUT, connect=TEMPLATE_CONNECT_TIMEOUT, write=30.0, pool=10.0)
        else:
            timeout = httpx.Timeout(GEMINI_HTTP_TIMEOUT, connect=GEMINI_CONNECT_TIMEOUT)
        client = httpx.AsyncClient(proxy=proxy_url, timeout=timeout)
        _HTTPX_CLIENTS[key] = client
    return client


async def close_http_clients() -> None:
    for client in _HTTPX_CLIENTS.values():
        await client.aclose()
    _HTTPX_CLIENTS.clear()


class GenerationTimeout(TimeoutError):
    """Обычный таймаут генерации, не являющийся отказом прокси."""


class StreamUnavailable(RuntimeError):
    """Потоковый endpoint недоступен; разрешён резервный generateContent."""


def _generation_result(data, model):
    candidates = data.get("candidates") if isinstance(data, dict) else []
    candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    parts = ((candidate.get("content") or {}).get("parts") or [])
    text = "\n".join(
        str(part.get("text") or "") for part in parts
        if isinstance(part, dict) and part.get("text") and not part.get("thought")
    ).strip()
    usage = data.get("usageMetadata") or {} if isinstance(data, dict) else {}
    feedback = data.get("promptFeedback") or {} if isinstance(data, dict) else {}
    metadata = {
        "served_model": str(data.get("modelVersion") or model)[:100] if isinstance(data, dict) else model,
        "finish_reason": str(candidate.get("finishReason") or "")[:80],
        "prompt_block_reason": str(feedback.get("blockReason") or "")[:80],
        "usage": {
            "in": int(usage.get("promptTokenCount") or 0),
            "out": int(usage.get("candidatesTokenCount") or 0),
            "thoughts": int(usage.get("thoughtsTokenCount") or 0),
            "cached": int(usage.get("cachedContentTokenCount") or 0),
        },
    }
    return text, metadata


def _raise_http_error(response, data):
    error_data = data.get("error", {}) if isinstance(data, dict) else {}
    provider_status = str(error_data.get("status") or "") if isinstance(error_data, dict) else ""
    message = str(error_data.get("message") or response.text) if isinstance(error_data, dict) else response.text
    if is_geo_blocked(response.status_code, message):
        raise ProxyGeoBlocked("HTTP 400: User location is not supported")
    policy = _classify_gemini_http_error(response.status_code, provider_status, message)
    raise GeminiAPIError(response.status_code, provider_status, message, policy)


async def _call_gemini_stream(
    client, payload, model, key, timeout, connect_timeout, first_byte_timeout, stall_timeout,
):
    """Read Gemini's SSE stream; first-byte and inter-chunk waits are bounded separately."""
    stream_url = GEMINI_REST_URL.format(model=model).replace(
        ":generateContent", ":streamGenerateContent"
    )
    # read-timeout httpx действует и пока ждём заголовки ответа. Если он меньше first_byte_timeout
    # (stall=30 против 75), вызов обрывается раньше нужного (~31 с). Реальные пределы first-byte и
    # stall ниже проверяются явно через asyncio.wait_for, поэтому здесь берём максимум.
    request_timeout = httpx.Timeout(
        max(1.0, min(max(stall_timeout, first_byte_timeout), timeout)),
        connect=min(connect_timeout, max(1.0, timeout)),
        write=min(30.0, max(1.0, timeout)),
        pool=min(10.0, max(1.0, timeout)),
    )
    try:
        stream_method = client.stream
    except AttributeError as exc:
        raise StreamUnavailable("HTTP client does not support streaming") from exc
    stream_context = stream_method(
        "POST", stream_url, params={"alt": "sse"}, json=payload,
        headers={"x-goog-api-key": key}, timeout=request_timeout,
    )
    entered = False
    started = asyncio.get_running_loop().time()
    try:
        response = await asyncio.wait_for(
            stream_context.__aenter__(),
            timeout=min(first_byte_timeout, timeout),
        )
        entered = True
        if response.status_code != 200:
            body = await response.aread()
            try:
                data = response.json()
            except Exception:
                data = {}
            if not isinstance(data, dict) and body:
                try:
                    data = __import__("json").loads(body.decode("utf-8", "replace"))
                except Exception:
                    data = {}
            _raise_http_error(response, data)

        iterator = response.aiter_bytes().__aiter__()
        first_remaining = min(
            first_byte_timeout,
            timeout - (asyncio.get_running_loop().time() - started),
        )
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=max(0.01, first_remaining))
        except StopAsyncIteration:
            raise StreamUnavailable("Gemini stream ended before its first payload")
        except asyncio.TimeoutError as exc:
            raise GenerationTimeout("Gemini stream first byte timed out") from exc

        pending = ""
        text_parts = []
        latest = {}

        def consume_line(line):
            if not line.startswith("data:"):
                return
            data_line = line[5:].strip()
            if not data_line or data_line == "[DONE]":
                return
            try:
                event = __import__("json").loads(data_line)
            except Exception:
                return
            if not isinstance(event, dict):
                return
            candidates = event.get("candidates") or []
            first = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
            parts = ((first.get("content") or {}).get("parts") or [])
            for part in parts:
                if isinstance(part, dict) and part.get("text") and not part.get("thought"):
                    text_parts.append(str(part["text"]))
            if first.get("finishReason"):
                latest["finishReason"] = first["finishReason"]
            if event.get("usageMetadata"):
                latest["usageMetadata"] = event["usageMetadata"]
            if event.get("modelVersion"):
                latest["modelVersion"] = event["modelVersion"]
            if event.get("promptFeedback"):
                latest["promptFeedback"] = event["promptFeedback"]

        while True:
            pending += chunk.decode("utf-8", "replace")
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                consume_line(line.rstrip("\r"))
            elapsed = asyncio.get_running_loop().time() - started
            remaining = timeout - elapsed
            if remaining <= 0:
                raise GenerationTimeout("Gemini stream exceeded hard timeout")
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=min(stall_timeout, remaining),
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise GenerationTimeout("Gemini stream stalled") from exc
        if pending:
            consume_line(pending.rstrip("\r"))
        if not text_parts and not latest:
            raise StreamUnavailable("Gemini returned an empty SSE stream")
        data = dict(latest)
        data["candidates"] = [{
            "content": {"parts": [{"text": "".join(text_parts)}]},
            "finishReason": latest.get("finishReason", ""),
        }]
        return _generation_result(data, model)
    except GenerationTimeout:
        raise
    except asyncio.TimeoutError as exc:
        raise GenerationTimeout("Gemini stream timed out") from exc
    except (AttributeError, NotImplementedError) as exc:
        raise StreamUnavailable("HTTP client does not support streaming") from exc
    except httpx.ReadTimeout as exc:
        raise GenerationTimeout("Gemini stream read timed out") from exc
    except httpx.RequestError as exc:
        raise ProxyUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if entered:
            await stream_context.__aexit__(None, None, None)


async def _call_gemini_nonstream(client, payload, model, key, profile, timeout):
    from .config import TEMPLATE_CONNECT_TIMEOUT, TEMPLATE_HTTP_READ_TIMEOUT

    request_timeout = httpx.Timeout(
        min(TEMPLATE_HTTP_READ_TIMEOUT, max(1.0, timeout - 5.0)),
        connect=min(TEMPLATE_CONNECT_TIMEOUT, max(1.0, timeout)),
        write=min(30.0, max(1.0, timeout - 5.0)),
        pool=min(10.0, max(1.0, timeout - 5.0)),
    )
    url = GEMINI_REST_URL.format(model=model)
    try:
        response = await asyncio.wait_for(
            client.post(url, json=payload, headers={"x-goog-api-key": key}, timeout=request_timeout),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, httpx.ReadTimeout) as exc:
        if profile == "long":
            raise GenerationTimeout("Generation call timed out") from exc
        raise ProxyUnavailable(f"{type(exc).__name__}: {exc}") from exc
    except httpx.RequestError as exc:
        raise ProxyUnavailable(f"{type(exc).__name__}: {exc}") from exc
    try:
        data = response.json()
    except Exception:
        data = {}
    if response.status_code != 200:
        _raise_http_error(response, data)
    return _generation_result(data, model)


def _payload_text_chars(payload: dict) -> int:
    """Грубая оценка размера запроса по числу символов текста (системная инструкция + реплики)."""
    total = 0
    system = payload.get("systemInstruction") or {}
    for part in system.get("parts", ()) if isinstance(system, dict) else ():
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            total += len(part["text"])
    for item in payload.get("contents") or ():
        for part in item.get("parts", ()) if isinstance(item, dict) else ():
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                total += len(part["text"])
    return total


async def call_gemini_generate(
    contents: list,
    system_instruction: str,
    model: str,
    key: str,
    proxy_url: Optional[str],
    generation_config: Optional[dict] = None,
    profile: str = "long",
    hard_timeout: Optional[float] = None,
) -> tuple:
    """Текстовая генерация Gemini; не меняет payload старого OCR-вызова."""
    from .config import TEMPLATE_CONNECT_TIMEOUT, TEMPLATE_HARD_TIMEOUT

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": generation_config or {},
    }
    client = _get_http_client(proxy_url, profile=profile)
    timeout = float(hard_timeout or TEMPLATE_HARD_TIMEOUT)
    if profile == "long":
        from .config import (
            TEMPLATE_LARGE_REQUEST_CHARS, TEMPLATE_REPAIR_FIRST_BYTE_TIMEOUT,
            TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT, TEMPLATE_STREAM_STALL_TIMEOUT,
        )
        first_byte_timeout = TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT
        if _payload_text_chars(payload) > TEMPLATE_LARGE_REQUEST_CHARS:
            # Ремонтный (многоходовой) запрос: первый байт приходит позже, чем у первой генерации.
            first_byte_timeout = max(first_byte_timeout, TEMPLATE_REPAIR_FIRST_BYTE_TIMEOUT)
        try:
            return await asyncio.wait_for(
                _call_gemini_stream(
                    client, payload, model, key, timeout, TEMPLATE_CONNECT_TIMEOUT,
                    first_byte_timeout, TEMPLATE_STREAM_STALL_TIMEOUT,
                ),
                timeout=timeout,
            )
        except GenerationTimeout:
            raise
        except asyncio.TimeoutError as exc:
            raise GenerationTimeout("Gemini stream exceeded hard timeout") from exc
        except GeminiAPIError as exc:
            if exc.http_status not in {400, 404, 405, 501}:
                raise
        except StreamUnavailable:
            pass
    return await _call_gemini_nonstream(client, payload, model, key, profile, timeout)


async def _call_gemini_rest(
    png_bytes: bytes,
    prompt: str,
    model: str,
    api_key: str,
    proxy_url: Optional[str],
    generation_config: Optional[dict] = None,
) -> tuple:
    """Прямой REST-вызов Gemini; прокси задаётся на каждую попытку отдельно."""
    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(png_bytes).decode("utf-8")}},
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 10, **(generation_config or {})},
    }
    url = GEMINI_REST_URL.format(model=model)
    http_client = _get_http_client(proxy_url)

    try:
        response = await asyncio.wait_for(
            http_client.post(url, json=payload, headers={"x-goog-api-key": api_key}),
            timeout=GEMINI_HARD_TIMEOUT,
        )
    except asyncio.TimeoutError as e:
        raise ProxyUnavailable(f"HardTimeout>{GEMINI_HARD_TIMEOUT:.0f}s: канал завис, ответа нет") from e
    except httpx.RequestError as e:
        raise ProxyUnavailable(f"{type(e).__name__}: {e}") from e
    except Exception as e:
        msg = str(e).lower()
        if "socksio" in msg or "unsupported proxy" in msg or "sockshandler" in msg:
            raise ProxyUnavailable(f"{type(e).__name__}: {e}") from e
        raise

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code != 200:
        error_data = data.get("error", {}) if isinstance(data, dict) else {}
        provider_status = str(error_data.get("status") or "") if isinstance(error_data, dict) else ""
        message = str(error_data.get("message") or response.text) if isinstance(error_data, dict) else response.text
        if is_geo_blocked(response.status_code, message):
            # Виноват выходной IP канала: ключ не трогаем, канал в карантин, запрос уйдёт на другой канал.
            raise ProxyGeoBlocked("HTTP 400: User location is not supported (гео-блок выходного IP)")
        policy = _classify_gemini_http_error(response.status_code, provider_status, message)
        raise GeminiAPIError(response.status_code, provider_status, message, policy)

    if not isinstance(data, dict):
        message = "Gemini returned a non-object response."
        raise GeminiAPIError(
            response.status_code, "INVALID_RESPONSE", message,
            _classify_gemini_http_error(response.status_code, "INVALID_RESPONSE", message),
        )
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    first_candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    try:
        text = first_candidate["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        text = ""
    prompt_feedback = data.get("promptFeedback") or {}
    if not isinstance(prompt_feedback, dict):
        prompt_feedback = {}
    metadata = {
        "served_model": str(data.get("modelVersion") or model)[:100],
        "usage_metadata": data.get("usageMetadata") or {},
        "finish_reason": str(first_candidate.get("finishReason") or "")[:80],
        "prompt_block_reason": str(prompt_feedback.get("blockReason") or "")[:80],
        "candidate_count": len(candidates),
        "text_chars": len(text),
    }
    return text, metadata
