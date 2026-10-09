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


def _get_http_client(proxy_url: Optional[str]) -> httpx.AsyncClient:
    client = _HTTPX_CLIENTS.get(proxy_url)
    if client is None:
        client = httpx.AsyncClient(proxy=proxy_url, timeout=httpx.Timeout(GEMINI_HTTP_TIMEOUT, connect=GEMINI_CONNECT_TIMEOUT))
        _HTTPX_CLIENTS[proxy_url] = client
    return client


async def close_http_clients() -> None:
    for client in _HTTPX_CLIENTS.values():
        await client.aclose()


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
