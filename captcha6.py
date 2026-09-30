"""Mail.ru-капча (ровно 6 символов 0-9a-z): промпт, скачивание, нормализация, fallback.

Отдельный модуль: существующие 3-значные пайплайны (contact sheet, jobs) не затронуты.
"""
import asyncio
import io
import ipaddress
import re
from typing import Tuple
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException
from PIL import Image

from .dddd_ocr import EXECUTOR, ocr_dddd
from .gemini_sheet import recognize_single_image
from .proxies import ProxyUnavailable, smart_proxy_pool

CAPTCHA_6_LENGTH = 6
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
DOWNLOAD_ATTEMPTS = 4

CAPTCHA_6_PROMPT = """Ты — высокоточный OCR для капчи Mail.ru / похожих.

ЗАДАЧА: прочитать РОВНО 6 символов с картинки.

ФАКТЫ О КАРТИНКЕ:
- Всегда ровно 6 символов.
- Алфавит ТОЛЬКО: 0-9 + a-z (строчные). Регистр игнорируй, возвращай всегда в нижнем регистре.
- Поверх символов идёт толстая чёрная волнистая линия-шум. Она часто пересекает, зачёркивает или полностью закрывает нижнюю часть символов. Это НЕ часть символа.
- Символы серые/контурные на белом фоне.
- Символы под линией всё ещё имеют читаемую форму: смотри на верхние/средние части, петли, хвостики, углы, наклон.

СТРОГИЕ ПРАВИЛА:
1. Ответ — ТОЛЬКО строка из ровно 6 символов [0-9a-z]. Никаких пробелов, кавычек, markdown, пояснений, "я думаю", "?" и т.д.
2. Если линия перекрывает символ — восстанавливай его по видимой форме + контексту соседних символов.
3. Классические путаницы (выбирай по начертанию):
   - 0 ≠ o
   - 1 ≠ l ≠ i
   - 5 ≠ s
   - 8 ≠ b
   - 2 ≠ z
   - 6 ≠ b / g
   - 9 ≠ g / q
4. Если совсем не уверен в одном символе — всё равно выдай наиболее вероятный вариант (не ставь "?").
5. Никогда не добавляй лишние символы и не обрезай.

ПРИМЕРЫ ПРАВИЛЬНЫХ ОТВЕТОВ (каждый — ровно 6 символов):
9t0aky
cahycx
bk2t50
opkxlk
ap6663
eaykak

ТЕПЕРЬ СМОТРИ НА КАРТИНКУ И ВЕРНИ ТОЛЬКО 6 СИМВОЛОВ."""

_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    "Referer": "https://e.mail.ru/",
}


# --------------------------------------------------------------------------- нормализация

def _clean_captcha6(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (text or "").lower())


def normalize_captcha6(text: str, expected_length: int = CAPTCHA_6_LENGTH) -> str:
    """Нижний регистр, только [0-9a-z], обрезка/добивка '?' до expected_length."""
    cleaned = _clean_captcha6(text)
    if len(cleaned) > expected_length:
        return cleaned[:expected_length]
    return cleaned.ljust(expected_length, "?")


# --------------------------------------------------------------------------- скачивание

class _RetryableStatus(Exception):
    """Временный HTTP-статус (403/429/5xx): имеет смысл повторить через другой канал."""


async def _assert_public_url(url: str) -> None:
    """Защита от SSRF: только http(s) и только публичные IP (сервис доступен из интернета)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=400, detail="image_url: разрешены только http(s) URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"image_url: не удалось резолвить хост ({exc})")
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise HTTPException(status_code=400, detail="image_url: адрес не публичный")


async def _fetch_once(url: str, proxy: str | None, timeout: float) -> bytes:
    async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=False) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await _assert_public_url(current)
            async with client.stream("GET", current, headers=_DOWNLOAD_HEADERS) as resp:
                location = resp.headers.get("location")
                if resp.status_code in (301, 302, 303, 307, 308) and location:
                    current = urljoin(current, location)
                    continue
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    raise _RetryableStatus(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise HTTPException(status_code=400, detail=f"image_url: HTTP {resp.status_code}")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_IMAGE_BYTES:
                        raise HTTPException(status_code=400, detail="image_url: файл слишком большой")
                if len(buf) < 100:
                    raise HTTPException(status_code=400, detail="image_url: слишком маленький ответ")
                return bytes(buf)
    raise HTTPException(status_code=400, detail="image_url: слишком много редиректов")


async def download_image(url: str, timeout: float = 12.0) -> bytes:
    """Скачивает картинку через общий пул каналов (DIRECT + прокси).

    Канал штрафуется только за сетевые сбои: 404 и подобное — проблема URL, а не канала,
    иначе мусорные ссылки уводили бы в cooldown каналы, общие с Gemini.
    """
    last_err = "нет попыток"
    for _ in range(DOWNLOAD_ATTEMPTS):
        proxy = smart_proxy_pool.next()
        try:
            data = await _fetch_once(url, proxy, timeout)
        except httpx.RequestError as exc:
            smart_proxy_pool.mark_failed(proxy, ProxyUnavailable(type(exc).__name__))
            last_err = type(exc).__name__
            continue
        except _RetryableStatus as exc:
            smart_proxy_pool.mark_ok(proxy)
            last_err = str(exc)
            continue
        except HTTPException:
            smart_proxy_pool.mark_ok(proxy)
            raise
        smart_proxy_pool.mark_ok(proxy)
        return data
    raise HTTPException(status_code=502, detail=f"Не удалось скачать картинку: {last_err}")


# --------------------------------------------------------------------------- подготовка картинки

def prepare_png(image_bytes: bytes) -> bytes:
    """Любой формат (png/jpg/gif/webp) -> PNG на белом фоне: клиент Gemini всегда шлёт image/png."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGBA")
            background = Image.new("RGBA", img.size, (255, 255, 255, 255))
            flat = Image.alpha_composite(background, img).convert("RGB")
        out = io.BytesIO()
        flat.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось декодировать изображение: {type(exc).__name__}")


# --------------------------------------------------------------------------- распознавание

async def _recognize_dddd_alnum(png_bytes: bytes) -> str:
    """Запасной вариант. recognize_dddd() оставляет только цифры (под 3-значные капчи),
    поэтому берём ddddocr напрямую и фильтруем по [0-9a-z]."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(EXECUTOR, ocr_dddd.classification, png_bytes)
    except Exception as exc:
        print(f"[captcha6][dddd] ошибка: {type(exc).__name__}")
        return ""


async def solve_captcha6(image_bytes: bytes, prompt: str, expected_length: int) -> Tuple[str, str, str]:
    """Возвращает (text, raw, engine). Бросает HTTPException(503), если не прочитано ни одного символа."""
    png = prepare_png(image_bytes)

    gemini_raw = await recognize_single_image(png, prompt) or ""
    gemini_text = normalize_captcha6(gemini_raw, expected_length)
    if len(_clean_captcha6(gemini_raw)) == expected_length:
        return gemini_text, gemini_raw, "gemini"

    dddd_raw = await _recognize_dddd_alnum(png)
    dddd_text = normalize_captcha6(dddd_raw, expected_length)
    if len(_clean_captcha6(dddd_raw)) == expected_length:
        return dddd_text, dddd_raw, ("gemini+ddddocr" if gemini_raw else "ddddocr")

    # Ни один движок не дал ровно expected_length символов: берём тот, что прочитал больше.
    if len(_clean_captcha6(gemini_raw)) >= len(_clean_captcha6(dddd_raw)):
        best = (gemini_text, gemini_raw, "gemini")
    else:
        best = (dddd_text, dddd_raw, "gemini+ddddocr" if gemini_raw else "ddddocr")
    if not _clean_captcha6(best[1]):
        raise HTTPException(status_code=503, detail="Капча не распознана: нет ответа ни от Gemini, ни от ddddocr")
    return best
