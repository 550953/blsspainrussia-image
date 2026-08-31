import asyncio
import base64
import gc
import itertools
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Union, Optional
from collections import Counter
from urllib.parse import urlsplit

import ddddocr
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import numpy as np
import cv2
import uvicorn

PROCESS_START_TIME = time.time()
STARTUP_TIMING_FILE = Path("/tmp/startup_timing.json")

app = FastAPI(title="Digit OCR Service", version="4.4")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)

OCR_MAX_WORKERS = int(os.getenv("OCR_MAX_WORKERS", "8"))
EXECUTOR = ThreadPoolExecutor(max_workers=OCR_MAX_WORKERS, thread_name_prefix="ocr-worker")


_RETRY_DELAY_RE = re.compile(r'retry[_\s\-]?delay[^0-9]*(\d+(?:\.\d+)?)', re.IGNORECASE)


def _parse_retry_delay(err_text: str) -> float:
    m = _RETRY_DELAY_RE.search(err_text)
    return float(m.group(1)) if m else 0.0


class GeminiKeyPool:
    """Пул ключей: round-robin + per-key cooldown + глобальный blackout.

    v4.2: убран GEMINI_MIN_INTERVAL_SEC как условие в acquire(). Диагностика
    (54 картинки, 15 ключей, round-robin, БЕЗ искусственного интервала)
    прошла 54/54 без единого 429 за 14.5 сек. Реальная защита от коллизий —
    это `reserved[idx]`: пока ключ занят, его никто больше не возьмёт.
    Интервал в 8 сек поверх этого просто душил throughput (15 ключей / 8 сек
    ≈ 1.9 запроса/сек теоретический потолок), не давая никакой
    дополнительной защиты от рейт-лимитов, которой уже не было бы от
    reserved-флага и cooldown после реального 429."""

    _BLACKOUT_MULT = 1.0
    _BLACKOUT_MIN = 30.0
    _JITTER_MAX = 3.0

    def __init__(self):
        self.keys: List[str] = []
        self.key_names: List[str] = []
        self.blocked: List[float] = []
        self.reserved: List[bool] = []
        self.dead: List[bool] = []
        self.last_used: List[float] = []
        self.blackout_until: float = 0.0
        self.rr_index: int = 0

    def init_from_env(self, prefix: str = "GEMINI_API_KEY") -> None:
        seen = set()
        keys, names = [], []
        for name, value in sorted(os.environ.items()):
            if name.startswith(prefix) and value:
                v = value.strip()
                if v and v not in seen:
                    seen.add(v)
                    keys.append(v)
                    names.append(name)

        if not keys and os.environ.get("INFISICAL_CLIENT_ID") and os.environ.get("INFISICAL_CLIENT_SECRET"):
            try:
                keys, names = self._fetch_keys_from_infisical(prefix)
                if keys:
                    print(f"[pid={os.getpid()}] Gemini pool: ключи не найдены в os.environ, "
                          f"загружено {len(keys)} из Infisical (режим локальной разработки)")
            except Exception as e:
                print(f"[pid={os.getpid()}] Gemini pool: не удалось загрузить ключи из Infisical: {e}")

        self.keys = keys
        self.key_names = names
        self.blocked = [0.0] * len(keys)
        self.reserved = [False] * len(keys)
        self.dead = [False] * len(keys)
        self.last_used = [0.0] * len(keys)
        self.blackout_until = 0.0
        self.rr_index = 0
        print(f"[pid={os.getpid()}] Gemini pool: инициализирован, {len(keys)} ключ(ей)")

    @staticmethod
    def _fetch_keys_from_infisical(prefix: str) -> tuple[List[str], List[str]]:
        import requests as _requests

        client_id = os.environ["INFISICAL_CLIENT_ID"]
        client_secret = os.environ["INFISICAL_CLIENT_SECRET"]
        # ИЗМЕНЕНИЕ v4.3: убран дефолт (был захардкожен реальный project_id).
        # Без переменной окружения — явный KeyError, а не тихий фоллбэк
        # на чужой/старый проект. Обязательно задать INFISICAL_PROJECT_ID
        # в .env перед публикацией репозитория.
        project_id = os.environ["INFISICAL_PROJECT_ID"]
        environment = os.environ.get("INFISICAL_ENVIRONMENT", "dev")

        token_resp = _requests.post(
            "https://app.infisical.com/api/v1/auth/universal-auth/login",
            json={"clientId": client_id, "clientSecret": client_secret},
            timeout=20,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["accessToken"]

        secrets_resp = _requests.get(
            "https://app.infisical.com/api/v3/secrets/raw",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "workspaceId": project_id,
                "environment": environment,
                "include_imports": "true",
                "secretPath": "/",
            },
            timeout=30,
        )
        secrets_resp.raise_for_status()
        all_secrets = {s["secretKey"]: s["secretValue"] for s in secrets_resp.json().get("secrets", [])}

        seen = set()
        keys, names = [], []
        for name, value in sorted(all_secrets.items()):
            if name.startswith(prefix) and value:
                v = value.strip()
                if v and v not in seen:
                    seen.add(v)
                    keys.append(v)
                    names.append(name)
        return keys, names

    def is_overloaded(self) -> tuple[bool, float]:
        now = time.monotonic()
        if now < self.blackout_until:
            return True, self.blackout_until - now
        return False, 0.0

    def acquire(self) -> tuple[Optional[int], Optional[str]]:
        """Резервирует и возвращает следующий свободный ключ по кругу.
        (None, None), если свободных сейчас нет."""
        if not self.keys:
            return None, None
        n = len(self.keys)
        now = time.monotonic()
        for _ in range(n):
            idx = self.rr_index % n
            self.rr_index += 1
            if not self.reserved[idx] and now >= self.blocked[idx]:
                self.reserved[idx] = True
                self.last_used[idx] = now
                return idx, self.key_names[idx]
        return None, None

    def release(self, idx: int, error_text: Optional[str] = None) -> None:
        self.reserved[idx] = False
        if error_text is None:
            return

        key_name = self.key_names[idx] if idx < len(self.key_names) else f"key_{idx}"
        err_up = error_text.upper()

        if "429" in err_up or "RESOURCE_EXHAUSTED" in err_up or "QUOTA" in err_up:
            base = _parse_retry_delay(error_text)
            cooldown = max(self._BLACKOUT_MIN, base * self._BLACKOUT_MULT) + random.uniform(0, self._JITTER_MAX)
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → 429, cooldown {cooldown:.0f} сек")
        elif "401" in err_up or "403" in err_up or "UNAUTHENTICATED" in err_up or "API_KEY_INVALID" in err_up or "PERMISSION_DENIED" in err_up:
            cooldown = 3600.0
            self.dead[idx] = True
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → ошибка авторизации (401/403), похоже ключ мёртв, cooldown 1ч")
        elif "503" in err_up or "UNAVAILABLE" in err_up:
            cooldown = 10.0 + random.uniform(0, self._JITTER_MAX)
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → 503 (модель перегружена), cooldown {cooldown:.0f} сек")
        else:
            cooldown = 15.0
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → прочая ошибка, cooldown 15 сек: {error_text[:200]}")

        self.blocked[idx] = time.monotonic() + cooldown
        now = time.monotonic()

        live_idx = [i for i in range(len(self.keys)) if not self.dead[i]]
        check_idx = live_idx if live_idx else list(range(len(self.keys)))

        if all(now < self.blocked[i] for i in check_idx):
            soonest = min(self.blocked[i] for i in check_idx)
            if soonest > self.blackout_until:
                self.blackout_until = soonest
                wait = soonest - now
                label = "живые " if live_idx else ""
                print(f"[pid={os.getpid()}][gemini_key] ВСЕ {label}ключи заняты → blackout {wait:.0f} сек (ближайшее освобождение)")


gemini_pool = GeminiKeyPool()
gemini_pool.init_from_env()

GEMINI_KEYS = gemini_pool.keys

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
gemini_model_name = GEMINI_MODEL if GEMINI_KEYS else None
print(f"[pid={os.getpid()}] Gemini ключей найдено: {len(GEMINI_KEYS)}, модель: {gemini_model_name}")

GEMINI_ACQUIRE_POLL = 0.15

# ---------------------------------------------------------------------------
# Прокси-пул для исходящих запросов к Gemini.
#
# Причина: сервис хостится на Render. Диагностика показала, что 429 сыпется
# НЕ из-за реальной нехватки квоты у ключей — на shared outbound IP Render
# буквально ПЕРВЫЙ запрос почти каждого ключа (включая ни разу не
# использованные) сразу получает 429. Тот же набор ключей и та же модель
# на локальном мультипоточном тесте прошли 54/54 картинки без единой
# ошибки, потому что запросы шли либо с домашнего IP, либо через сторонние
# прокси — то есть НЕ с адреса из известного датацентр-диапазона Render.
#
# GEMINI_PROXIES — список прокси через запятую в переменной окружения:
#   GEMINI_PROXIES=socks5://user:pass@ip1:port1,http://user:pass@ip2:port2
# Если не задана — сервис работает как раньше (DIRECT), это осознанный
# fallback, а не тихая деградация: в /health сразу видно текущий режим.
# ---------------------------------------------------------------------------
def _fetch_proxies_from_infisical(prefix: str = "PROXY_URL") -> List[str]:
    import requests as _requests

    client_id = os.environ["INFISICAL_CLIENT_ID"]
    client_secret = os.environ["INFISICAL_CLIENT_SECRET"]
    # ИЗМЕНЕНИЕ v4.3: убран дефолт (см. комментарий в _fetch_keys_from_infisical).
    project_id = os.environ["INFISICAL_PROJECT_ID"]
    environment = os.environ.get("INFISICAL_ENVIRONMENT", "dev")

    token_resp = _requests.post(
        "https://app.infisical.com/api/v1/auth/universal-auth/login",
        json={"clientId": client_id, "clientSecret": client_secret},
        timeout=20,
    )
    token_resp.raise_for_status()
    token = token_resp.json()["accessToken"]

    secrets_resp = _requests.get(
        "https://app.infisical.com/api/v3/secrets/raw",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "workspaceId": project_id,
            "environment": environment,
            "include_imports": "true",
            "secretPath": "/",
        },
        timeout=30,
    )
    secrets_resp.raise_for_status()
    all_secrets = {s["secretKey"]: s["secretValue"] for s in secrets_resp.json().get("secrets", [])}

    proxy_items = sorted((k, v) for k, v in all_secrets.items() if k.startswith(prefix) and v)
    return [v.strip() for _, v in proxy_items if v.strip()]


def _load_gemini_proxies() -> tuple[List[str], str]:
    raw = os.getenv("GEMINI_PROXIES", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()], "env:GEMINI_PROXIES"

    if os.environ.get("INFISICAL_CLIENT_ID") and os.environ.get("INFISICAL_CLIENT_SECRET"):
        try:
            proxies = _fetch_proxies_from_infisical("PROXY_URL")
            if proxies:
                return proxies, "infisical:PROXY_URL_*"
        except Exception as e:
            print(f"[pid={os.getpid()}] Не удалось загрузить прокси из Infisical: {e}")

    return [], "none"


_configured_proxies, _proxy_source = _load_gemini_proxies()

# DIRECT добавляем ПОСЛЕДНИМ резервным вариантом, а не первым — именно
# DIRECT с Render почти гарантированно ловит 429 по диагностике выше.
GEMINI_PROXIES: List[Optional[str]] = (_configured_proxies + [None]) if _configured_proxies else [None]
_proxy_cycle = itertools.cycle(range(len(GEMINI_PROXIES)))

print(
    f"[pid={os.getpid()}] Gemini proxy pool: {len(GEMINI_PROXIES)} канал(ов), источник: {_proxy_source} "
    f"({'прокси + DIRECT как fallback' if _configured_proxies else 'ТОЛЬКО DIRECT!'})"
)


def _next_proxy() -> Optional[str]:
    return GEMINI_PROXIES[next(_proxy_cycle)]


def _proxy_label(proxy_url: Optional[str]) -> str:
    """Безопасное имя канала для логов: никогда не печатаем user/password."""
    if not proxy_url:
        return "DIRECT"
    try:
        parsed = urlsplit(proxy_url)
        host = parsed.hostname or "unknown-host"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme or 'proxy'}://{host}{port}"
    except Exception:
        return "PROXY"


class GeminiSmartProxyPool:
    """Direct-first канал с автоматическим cooldown нерабочих маршрутов.

    DIRECT используется первым. Если Gemini начинает отдавать ошибки на
    прямом IP или сам маршрут рвётся, канал временно уходит в cooldown и
    выбирается следующий proxy из Infisical. После успешного запроса канал
    снова становится рабочим.
    """

    def __init__(self, configured: List[str]):
        unique: List[Optional[str]] = []
        seen = set()
        for route in [None, *configured]:
            if route not in seen:
                seen.add(route)
                unique.append(route)
        self.routes = unique
        self.cooldown_until = {route: 0.0 for route in self.routes}
        self.failures = {route: 0 for route in self.routes}
        self.cursor = 0

    def next(self) -> Optional[str]:
        now = time.monotonic()
        count = len(self.routes)
        for offset in range(count):
            route = self.routes[(self.cursor + offset) % count]
            if now >= self.cooldown_until[route]:
                self.cursor = (self.cursor + offset + 1) % count
                return route

        # Все каналы временно охлаждаются: выбираем тот, который освободится
        # раньше, не создавая дополнительную паузу внутри запроса.
        route = min(self.routes, key=lambda item: self.cooldown_until[item])
        self.cursor = (self.routes.index(route) + 1) % count
        return route

    def mark_ok(self, route: Optional[str]) -> None:
        self.failures[route] = 0
        self.cooldown_until[route] = 0.0

    def mark_failed(self, route: Optional[str], error_text: object) -> None:
        self.failures[route] += 1
        error_string = str(error_text)
        upper = error_string.upper()
        if isinstance(route, type(None)):
            base = 30.0 if "429" in upper or "QUOTA" in upper else 8.0
        elif "429" in upper or "QUOTA" in upper:
            base = 20.0
        elif isinstance(error_text, ProxyUnavailable):
            base = 8.0
        else:
            base = 5.0

        cooldown = min(120.0, base * (2 ** min(self.failures[route] - 1, 3)))
        cooldown += random.uniform(0.0, 2.0)
        self.cooldown_until[route] = time.monotonic() + cooldown
        print(
            f"[pid={os.getpid()}][gemini_proxy] {_proxy_label(route)} "
            f"cooldown {cooldown:.1f} сек"
        )


smart_proxy_pool = GeminiSmartProxyPool(_configured_proxies)


GEMINI_REST_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


class ProxyUnavailable(Exception):
    """Прокси не смог быть использован (не установлен socksio, обрыв
    соединения, таймаут подключения и т.п.) — это ошибка КАНАЛА, а не
    конкретного ключа. Ключ в этом не виноват и не должен уходить в
    cooldown — иначе один нерабочий прокси-канал может по цепочке
    остановить весь пул ключей."""
    pass


# ---------------------------------------------------------------------------
# Пул httpx.AsyncClient — один клиент на прокси-канал, переиспользуемый
# между запросами (keep-alive), вместо создания нового AsyncClient (а
# значит и нового TCP+TLS хендшейка) на КАЖДЫЙ вызов Gemini.
# ---------------------------------------------------------------------------
_HTTPX_CLIENTS: dict[Optional[str], httpx.AsyncClient] = {}


def _get_http_client(proxy_url: Optional[str]) -> httpx.AsyncClient:
    client = _HTTPX_CLIENTS.get(proxy_url)
    if client is None:
        client = httpx.AsyncClient(proxy=proxy_url, timeout=20.0)
        _HTTPX_CLIENTS[proxy_url] = client
    return client


async def _call_gemini_rest(
    png_bytes: bytes,
    prompt: str,
    model: str,
    api_key: str,
    proxy_url: Optional[str],
    generation_config: Optional[dict] = None,
) -> str:
    """Прямой REST-вызов к Gemini вместо genai SDK — прокси задаётся на
    каждую попытку отдельно через переиспользуемый клиент (см. выше)."""
    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(png_bytes).decode("utf-8")}},
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 10,
            **(generation_config or {}),
        },
    }
    url = GEMINI_REST_URL.format(model=model, key=api_key)
    http_client = _get_http_client(proxy_url)

    try:
        response = await http_client.post(url, json=payload)
    except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise ProxyUnavailable(f"{type(e).__name__}: {e}") from e
    except Exception as e:
        msg = str(e).lower()
        if "socksio" in msg or "unsupported proxy" in msg or "sockshandler" in msg:
            raise ProxyUnavailable(f"{type(e).__name__}: {e}") from e
        raise

    if response.status_code != 200:
        raise RuntimeError(f"{response.status_code} {response.text}")

    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return ""


class OCRRequest(BaseModel):
    images: Union[str, List[str]] = Field(..., description="Один base64 или список")


class OCRResultItem(BaseModel):
    text: str
    source: str


class OCRResponse(BaseModel):
    results: List[OCRResultItem]


def clean_base64(b64: str) -> bytes:
    b64 = b64.strip().strip("\"'")
    if b64.startswith("data:image"):
        b64 = b64.split(",", 1)[1]
    b64 = re.sub(r"\s+", "", b64)
    try:
        return base64.b64decode(b64, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")


def make_variants_fast(img_bgr: np.ndarray, scale: int = 4) -> list[np.ndarray]:
    h, w = img_bgr.shape[:2]
    img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_LINEAR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    variants = []

    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th)
    variants.append(255 - th)

    th_a = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
    )
    variants.append(th_a)
    variants.append(255 - th_a)

    clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, th_c = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_c)
    variants.append(255 - th_c)

    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    _, a, _ = cv2.split(lab)
    _, th_a_ch = cv2.threshold(a, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_a_ch)
    variants.append(255 - th_a_ch)

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    _, s_ch, v_ch = cv2.split(hsv)
    _, th_s_ch = cv2.threshold(s_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_s_ch)
    variants.append(255 - th_s_ch)

    _, th_v = cv2.threshold(v_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_v)
    variants.append(255 - th_v)

    return variants


def make_variants_full(img_bgr: np.ndarray) -> list[np.ndarray]:
    h, w = img_bgr.shape[:2]
    variants = []

    for scale in (5, 7):
        img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th)
        variants.append(255 - th)

        th_a = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
        )
        variants.append(th_a)
        variants.append(255 - th_a)

        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        _, th_c = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_c)
        variants.append(255 - th_c)

        blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
        sharp = cv2.addWeighted(gray, 2.2, blur, -1.2, 0)
        _, th_s = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_s)
        variants.append(255 - th_s)

        kernel = np.ones((2, 2), np.uint8)
        variants.append(cv2.dilate(th_s, kernel, iterations=1))
        variants.append(cv2.dilate(th_s, kernel, iterations=2))

        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)

        _, th_a_ch = cv2.threshold(a, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_a_ch)
        variants.append(255 - th_a_ch)

        color1 = cv2.addWeighted(l, 0.4, a, 0.6, 0)
        _, th_col1 = cv2.threshold(color1, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_col1)
        variants.append(255 - th_col1)

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        _, s_ch, v_ch = cv2.split(hsv)

        _, th_s_ch = cv2.threshold(s_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_s_ch)
        variants.append(255 - th_s_ch)

        _, th_v = cv2.threshold(v_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_v)
        variants.append(255 - th_v)

    return variants


# ---------------------------------------------------------------------------
# НОВОЕ (v4.4): точечные варианты для ПОВТОРНОГО Gemini-запроса по ОДНОЙ
# нераспознанной ячейке — перед тем как окончательно упасть в dddd.
#
# Проверено вручную на реальном сложном сэмпле этого генератора капчи:
# одиночные цветовые каналы (просто GREEN, просто BLUE и т.п.) почти не
# дают контраста, потому что цифра здесь отличается от фона ОТТЕНКОМ
# МЕЖДУ каналами, а не яркостью одного канала. Разность каналов (G-B,
# R-G, R-B) + гауссово размытие ПОСЛЕ разности — рабочий метод: без
# размытия halftone-паттерн фона того же порядка яркости, что и сама
# цифра, и топит сигнал в шуме. Если на вашем реальном датасете фон
# генерится иначе — параметры (какие каналы, blur_ksize) почти наверняка
# нужно будет подстроить под конкретный паттерн.
# ---------------------------------------------------------------------------
def make_variants_for_retry(image_bytes: bytes, scale: int = 6, blur_ksize: int = 9) -> list[bytes]:
    """Точечные варианты для ОДНОЙ проблемной ячейки, только в памяти,
    без файлов на диск. Возвращает список PNG-байтов."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return []

    h, w = img.shape[:2]
    img = cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC).astype(np.int16)
    b, g, r = cv2.split(img)

    def diff_variant(a: np.ndarray, bb: np.ndarray, invert: bool = False) -> np.ndarray:
        d = cv2.GaussianBlur((a - bb).astype(np.float32), (blur_ksize, blur_ksize), 0)
        d = cv2.normalize(d, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        return 255 - d if invert else d

    grays = [
        diff_variant(g, b),
        diff_variant(r, g),
        diff_variant(r, b),
        diff_variant(g, b, invert=True),
        diff_variant(r, g, invert=True),
        diff_variant(r, b, invert=True),
    ]

    out = []
    for v in grays:
        ok, buf = cv2.imencode(".png", cv2.cvtColor(v, cv2.COLOR_GRAY2BGR))
        if ok:
            out.append(buf.tobytes())
    return out


def _score(item):
    text, freq = item
    bonus = 120 if len(text) == 3 else (25 if len(text) == 2 else 0)
    return (freq + bonus, len(text))


def recognize_dddd(image_bytes: bytes) -> str:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            res = ocr_dddd.classification(image_bytes)
            return "".join(c for c in res if c.isdigit()) or "0"

        candidates = []

        res0 = ocr_dddd.classification(image_bytes)
        dig0 = "".join(c for c in res0 if c.isdigit())
        if dig0:
            candidates.append(dig0)

        for var in make_variants_fast(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        cnt = Counter(c for c in candidates if len(c) == 3)
        if cnt:
            top_text, top_freq = cnt.most_common(1)[0]
            if top_freq >= 3:
                del img, nparr
                return top_text

        for var in make_variants_full(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        del img, nparr

        if not candidates:
            return "0"

        cnt_full = Counter(candidates)
        return sorted(cnt_full.items(), key=_score, reverse=True)[0][0]

    except Exception as e:
        print(f"[pid={os.getpid()}][dddd] Ошибка распознавания: {e}")
        return "0"


def recognize_one_sync_part(image_bytes: bytes) -> str:
    return recognize_dddd(image_bytes)


GEMINI_SHEET_MAX_IMAGES = int(os.getenv("GEMINI_SHEET_MAX_IMAGES", "100"))
GEMINI_SHEET_COLS = int(os.getenv("GEMINI_SHEET_COLS", "9"))
GEMINI_SHEET_CELL_WIDTH = int(os.getenv("GEMINI_SHEET_CELL_WIDTH", "170"))
GEMINI_SHEET_CELL_HEIGHT = int(os.getenv("GEMINI_SHEET_CELL_HEIGHT", "112"))

# ИЗМЕНЕНИЕ v4.3: раньше /ocr/batch пускал до 500 картинок, а
# make_gemini_contact_sheet кидал ValueError при >GEMINI_SHEET_MAX_IMAGES
# (100). recognize_gemini_sheet_async ловил это исключение и молча
# возвращал "0" на ВСЮ пачку — ни разу не сходив в сеть. Теперь большие
# пачки бьются на чанки размером GEMINI_SHEET_CHUNK_SIZE (54 — размер,
# подтверждённый локальными тестами: 54/54 без единой ошибки) и гонятся
# параллельно.
GEMINI_SHEET_CHUNK_SIZE = int(os.getenv("GEMINI_SHEET_CHUNK_SIZE", "54"))
GEMINI_SHEET_MAX_CONCURRENT_CHUNKS = int(
    os.getenv("GEMINI_SHEET_MAX_CONCURRENT_CHUNKS", str(max(len(GEMINI_KEYS), 1)))
)

# НОВОЕ (v4.4): настройки точечного повтора по одной нераспознанной ячейке.
GEMINI_VARIANT_RETRY_ENABLED = os.getenv("GEMINI_VARIANT_RETRY_ENABLED", "1") not in ("0", "false", "False")
GEMINI_VARIANT_RETRY_COLS = int(os.getenv("GEMINI_VARIANT_RETRY_COLS", "3"))
GEMINI_VARIANT_RETRY_MIN_VOTES = int(os.getenv("GEMINI_VARIANT_RETRY_MIN_VOTES", "2"))
GEMINI_VARIANT_RETRY_SCALE = int(os.getenv("GEMINI_VARIANT_RETRY_SCALE", "6"))
GEMINI_VARIANT_RETRY_BLUR_KSIZE = int(os.getenv("GEMINI_VARIANT_RETRY_BLUR_KSIZE", "9"))


def _chunk(items: List[bytes], size: int) -> List[List[bytes]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def make_gemini_contact_sheet(images: List[bytes], cols: Optional[int] = None) -> bytes:
    """Собирает список изображений (<=GEMINI_SHEET_MAX_IMAGES) в одну
    PNG-картинку с ID ячеек. Чанкинг на верхнем уровне (process_images_gemini_sheet)
    гарантирует, что сюда никогда не придёт больше лимита.

    cols: опционально переопределить число колонок сетки (используется
    точечным повтором — там всегда маленький лист на несколько вариантов
    одной картинки, а не GEMINI_SHEET_COLS=9 как для полного батча)."""
    if not images:
        raise ValueError("Пустой список изображений")
    if len(images) > GEMINI_SHEET_MAX_IMAGES:
        raise ValueError(
            f"gemini_sheet поддерживает максимум {GEMINI_SHEET_MAX_IMAGES} "
            f"изображений, получено {len(images)}"
        )

    cols = max(1, min(cols or GEMINI_SHEET_COLS, len(images)))
    rows = (len(images) + cols - 1) // cols
    sheet = np.full(
        (
            rows * GEMINI_SHEET_CELL_HEIGHT,
            cols * GEMINI_SHEET_CELL_WIDTH,
            3,
        ),
        255,
        dtype=np.uint8,
    )

    for index, image_bytes in enumerate(images, start=1):
        encoded = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Не удалось декодировать изображение №{index}")

        max_w = GEMINI_SHEET_CELL_WIDTH - 20
        max_h = GEMINI_SHEET_CELL_HEIGHT - 30
        h, w = image.shape[:2]
        scale = min(max_w / max(w, 1), max_h / max(h, 1), 1.0)
        if scale != 1.0:
            image = cv2.resize(
                image,
                (max(1, int(w * scale)), max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )

        col = (index - 1) % cols
        row = (index - 1) // cols
        left = col * GEMINI_SHEET_CELL_WIDTH
        top = row * GEMINI_SHEET_CELL_HEIGHT
        ih, iw = image.shape[:2]
        x = left + (GEMINI_SHEET_CELL_WIDTH - iw) // 2
        y = top + 25 + (max_h - ih) // 2
        sheet[y:y + ih, x:x + iw] = image

        cv2.rectangle(
            sheet,
            (left, top),
            (left + GEMINI_SHEET_CELL_WIDTH - 1, top + GEMINI_SHEET_CELL_HEIGHT - 1),
            (150, 150, 150),
            1,
        )
        cv2.putText(
            sheet,
            f"#{index}",
            (left + 5, top + 17),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (20, 20, 20),
            1,
            cv2.LINE_AA,
        )

    success, buffer = cv2.imencode(
        ".png",
        sheet,
        [cv2.IMWRITE_PNG_COMPRESSION, 3],
    )
    if not success:
        raise ValueError("Не удалось собрать PNG contact sheet")
    return buffer.tobytes()


GEMINI_SHEET_PROMPT = f"""
На изображении contact sheet из пронумерованных ячеек.
В каждой ячейке находится одна картинка с трёхзначным числом.
Распознай число в каждой ячейке и сопоставь его с номером ячейки.

Верни строго JSON-объект без markdown и пояснений:
{{"1":"353","2":"872", ... "{GEMINI_SHEET_MAX_IMAGES}":null}}

Правила:
- верни ключи только для фактически присутствующих ячеек;
- ключи — строки с номерами ячеек от "1" до последней;
- значение — строка ровно из трёх цифр;
- если ячейка не читается, значение null;
- ничего не переставляй и не придумывай.
""".strip()

# НОВОЕ (v4.4): промпт для точечного повтора. Все ячейки на этом листе —
# РАЗНЫЕ версии обработки ОДНОЙ И ТОЙ ЖЕ картинки, а не разные капчи, так
# что задача — распознать (возможно, по-разному читаемое на разных
# вариантах) одно и то же число и вернуть его для каждой ячейки, где
# получилось.
GEMINI_VARIANT_RETRY_PROMPT_TEMPLATE = """
На изображении contact sheet из {n} пронумерованных ячеек.
Это РАЗНЫЕ варианты обработки контраста ОДНОЙ И ТОЙ ЖЕ исходной картинки
с трёхзначным числом — само число везде одно и то же, отличается только
то, насколько хорошо оно видно на конкретном варианте.

Верни строго JSON-объект без markdown и пояснений:
{{"1":"353","2":"353", ... "{n}":null}}

Правила:
- верни ключи для всех ячеек от "1" до "{n}";
- значение — строка ровно из трёх цифр, если смог прочитать на этом варианте;
- если конкретный вариант нечитаем, значение null для него (другие ячейки
  на это не влияют);
- ничего не придумывай — если не уверен, лучше null, чем случайное число.
""".strip()


def parse_gemini_sheet_result(text: str, count: int) -> List[str]:
    """Принимает JSON object/list и приводит его к списку результатов."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = min(
            [pos for pos in (cleaned.find("{"), cleaned.find("[")) if pos >= 0],
            default=-1,
        )
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if start < 0 or end <= start:
            raise ValueError("Gemini вернул не JSON")
        data = json.loads(cleaned[start:end + 1])

    if isinstance(data, dict) and isinstance(data.get("results"), list):
        values = data["results"]
    elif isinstance(data, dict):
        values = [data.get(str(index)) for index in range(1, count + 1)]
    elif isinstance(data, list):
        values = data
    else:
        raise ValueError("Ожидался JSON-объект или JSON-массив")

    result: List[str] = []
    for value in values[:count]:
        if value is None:
            result.append("0")
            continue
        digits = "".join(char for char in str(value) if char.isdigit())
        result.append(digits if len(digits) == 3 else "0")
    result.extend(["0"] * (count - len(result)))
    return result


async def recognize_gemini_sheet_async(
    images: List[bytes],
    cols: Optional[int] = None,
    prompt: Optional[str] = None,
) -> List[dict]:
    """Один Gemini-вызов на ОДИН чанк (<=GEMINI_SHEET_MAX_IMAGES) с
    direct-first failover. Чанкинг делается выше, в process_images_gemini_sheet.

    prompt: опционально переопределить промпт (используется точечным
    повтором — там свой промпт про "варианты одной картинки", а не про
    "разные капчи")."""
    if not GEMINI_KEYS or gemini_model_name is None:
        return [{"text": "0", "source": "gemini_sheet_unavailable"} for _ in images]

    try:
        png_bytes = make_gemini_contact_sheet(images, cols=cols)
    except ValueError as exc:
        print(f"[pid={os.getpid()}][gemini_sheet] {exc}")
        return [{"text": "0", "source": "gemini_sheet_error"} for _ in images]

    effective_prompt = prompt if prompt is not None else GEMINI_SHEET_PROMPT.replace(
        f'"{GEMINI_SHEET_MAX_IMAGES}"',
        f'"{len(images)}"',
    )

    max_attempts = max(4, min(24, len(GEMINI_KEYS) * 2 + len(smart_proxy_pool.routes)))
    attempted_routes: set[tuple[int, Optional[str]]] = set()
    last_error: Optional[str] = None

    for _ in range(max_attempts):
        idx, key_name = gemini_pool.acquire()
        if idx is None:
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue

        proxy_url = smart_proxy_pool.next()
        route_key = (idx, proxy_url)
        if route_key in attempted_routes:
            gemini_pool.release(idx)
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue
        attempted_routes.add(route_key)

        try:
            text = await _call_gemini_rest(
                png_bytes,
                effective_prompt,
                gemini_model_name,
                gemini_pool.keys[idx],
                proxy_url,
                generation_config={
                    "temperature": 0.0,
                    "responseMimeType": "application/json",
                    "maxOutputTokens": max(256, len(images) * 12),
                },
            )
            values = parse_gemini_sheet_result(text, len(images))
            smart_proxy_pool.mark_ok(proxy_url)
            gemini_pool.release(idx, None)
            print(
                f"[pid={os.getpid()}][gemini_sheet] OK key={key_name} "
                f"proxy={_proxy_label(proxy_url)} images={len(images)}"
            )
            return [
                {"text": value, "source": "gemini_sheet"}
                for value in values
            ]
        except ProxyUnavailable as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            smart_proxy_pool.mark_failed(proxy_url, exc)
            gemini_pool.release(idx, None)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            smart_proxy_pool.mark_failed(proxy_url, last_error)
            gemini_pool.release(idx, last_error)

    print(
        f"[pid={os.getpid()}][gemini_sheet] не удалось выполнить запрос: "
        f"{last_error or 'нет доступного ключа/канала'}"
    )
    return [{"text": "0", "source": "gemini_sheet_error"} for _ in images]


async def recognize_gemini_variant_retry(image_bytes: bytes) -> Optional[tuple[str, int]]:
    """НОВОЕ (v4.4): повторный запрос в Gemini ТОЛЬКО по этой картинке —
    не весь батч, не 100 чужих ячеек, а несколько вариантов контраста
    именно её, одним небольшим API-вызовом. Голосование по большинству
    среди вариантов, которые Gemini смог прочитать.

    Возвращает (текст, число_голосов) либо None, если Gemini недоступен
    или ни один вариант не набрал GEMINI_VARIANT_RETRY_MIN_VOTES —
    тогда вызывающий код идёт в dddd как раньше."""
    if not GEMINI_VARIANT_RETRY_ENABLED or not GEMINI_KEYS:
        return None

    variants = make_variants_for_retry(
        image_bytes,
        scale=GEMINI_VARIANT_RETRY_SCALE,
        blur_ksize=GEMINI_VARIANT_RETRY_BLUR_KSIZE,
    )
    if not variants:
        return None

    prompt = GEMINI_VARIANT_RETRY_PROMPT_TEMPLATE.format(n=len(variants))
    sheet_results = await recognize_gemini_sheet_async(
        variants,
        cols=GEMINI_VARIANT_RETRY_COLS,
        prompt=prompt,
    )
    votes = Counter(
        r["text"] for r in sheet_results
        if r["text"] != "0" and r["source"] == "gemini_sheet"
    )
    if not votes:
        return None

    top_text, top_freq = sorted(votes.items(), key=_score, reverse=True)[0]
    if top_freq < GEMINI_VARIANT_RETRY_MIN_VOTES:
        return None
    return top_text, top_freq


async def _fill_unrecognized(images: List[bytes], results: List[dict]) -> List[dict]:
    """ИЗМЕНЕНИЕ v4.4 (было _fill_unrecognized_with_dddd): теперь три
    уровня для каждой нераспознанной ячейки —
      1) точечный Gemini-повтор по вариантам именно этой картинки;
      2) если и это не сошлось — dddd на оригинале, как раньше.
    Не трогает ячейки, которые уже успешно распознались общим листом."""
    missing_idx = [i for i, r in enumerate(results) if r["text"] == "0"]
    if not missing_idx:
        return results

    # Шаг 1: точечный повтор в Gemini по каждой проблемной ячейке отдельно.
    # Последовательно, а не gather — чтобы не толкаться за один и тот же
    # пул ключей параллельно ради одних и тех же нескольких картинок.
    for i in missing_idx:
        retry = await recognize_gemini_variant_retry(images[i])
        if retry is not None:
            text, votes = retry
            results[i] = {
                "text": text,
                "source": f"{results[i]['source']}+gemini_variant_retry(votes={votes})",
            }

    # Шаг 2: то, что и повтор не взял — как раньше, в dddd.
    still_missing = [i for i in missing_idx if results[i]["text"] == "0"]
    if not still_missing:
        return results

    loop = asyncio.get_event_loop()
    fallback_values = await asyncio.gather(*[
        loop.run_in_executor(EXECUTOR, recognize_one_sync_part, images[i])
        for i in still_missing
    ])
    for i, value in zip(still_missing, fallback_values):
        if value and value != "0":
            results[i] = {"text": value, "source": f"{results[i]['source']}+ddddocr_fallback"}
    return results


async def process_images_gemini_sheet(images_b64: List[str]) -> List[dict]:
    """Единственный публичный режим обработки: бьёт вход на чанки по
    GEMINI_SHEET_CHUNK_SIZE, гонит чанки параллельно (ограничено
    семафором по числу живых ключей — не больше одного tile-запроса на
    ключ единовременно, без залповой нагрузки в момент пика), и точечно
    добивает нераспознанные ячейки каждого чанка (Gemini-повтор → dddd)."""
    images = [clean_base64(b64) for b64 in images_b64]
    chunks = _chunk(images, GEMINI_SHEET_CHUNK_SIZE)
    semaphore = asyncio.Semaphore(GEMINI_SHEET_MAX_CONCURRENT_CHUNKS)

    async def _run_chunk(chunk: List[bytes]) -> List[dict]:
        async with semaphore:
            chunk_results = await recognize_gemini_sheet_async(chunk)
            return await _fill_unrecognized(chunk, chunk_results)

    chunk_results = await asyncio.gather(*[_run_chunk(c) for c in chunks])
    flat: List[dict] = []
    for r in chunk_results:
        flat.extend(r)
    gc.collect()
    return flat


@app.post("/ocr", response_model=OCRResponse)
async def ocr_endpoint(req: OCRRequest):
    images = req.images if isinstance(req.images, list) else [req.images]
    if len(images) > 500:
        raise HTTPException(status_code=400, detail="Слишком много изображений за один запрос (лимит 500)")
    return OCRResponse(results=await process_images_gemini_sheet(images))


@app.post("/ocr/batch", response_model=OCRResponse)
async def ocr_batch_endpoint(req: OCRRequest):
    images = req.images if isinstance(req.images, list) else [req.images]
    if not images:
        raise HTTPException(status_code=400, detail="Пустой список изображений")
    if len(images) > 500:
        raise HTTPException(status_code=400, detail="Слишком много изображений за один запрос (лимит 500)")
    return OCRResponse(results=await process_images_gemini_sheet(images))


@app.get("/health")
async def health():
    startup_info = None
    if STARTUP_TIMING_FILE.exists():
        try:
            startup_info = json.loads(STARTUP_TIMING_FILE.read_text())
        except Exception:
            startup_info = None

    overloaded, remaining = gemini_pool.is_overloaded()
    now = time.monotonic()

    return {
        "status": "ok",
        "pid": os.getpid(),
        "gemini_keys_count": len(GEMINI_KEYS),
        "gemini_key_names": gemini_pool.key_names,
        "gemini_model": gemini_model_name,
        "gemini_pool_overloaded": overloaded,
        "gemini_pool_overloaded_seconds_left": round(remaining, 1) if overloaded else 0,
        "gemini_proxy_channels": len(GEMINI_PROXIES),
        "gemini_proxy_mode": "proxied+direct_fallback" if _configured_proxies else "direct_only",
        "gemini_proxy_source": _proxy_source,
        "gemini_keys_on_cooldown": [
            gemini_pool.key_names[i]
            for i, t in enumerate(gemini_pool.blocked)
            if now < t
        ],
        "gemini_keys_in_use": [
            gemini_pool.key_names[i]
            for i, busy in enumerate(gemini_pool.reserved)
            if busy
        ],
        "gemini_keys_dead": [
            gemini_pool.key_names[i]
            for i, dead in enumerate(gemini_pool.dead)
            if dead
        ],
        "gemini_sheet_chunk_size": GEMINI_SHEET_CHUNK_SIZE,
        "gemini_sheet_max_concurrent_chunks": GEMINI_SHEET_MAX_CONCURRENT_CHUNKS,
        "gemini_variant_retry_enabled": GEMINI_VARIANT_RETRY_ENABLED,
        "gemini_variant_retry_cols": GEMINI_VARIANT_RETRY_COLS,
        "gemini_variant_retry_min_votes": GEMINI_VARIANT_RETRY_MIN_VOTES,
        "ocr_max_workers": OCR_MAX_WORKERS,
        "startup_timing": startup_info,
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = Path(__file__).parent / "favicon.ico"
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(favicon_path)


@app.on_event("startup")
async def on_startup():
    elapsed = time.time() - PROCESS_START_TIME
    payload = {
        "process_start_time": PROCESS_START_TIME,
        "ready_time": time.time(),
        "elapsed_seconds": round(elapsed, 3),
    }
    print(f"[startup_timing] Приложение готово через {elapsed:.3f} сек после старта процесса")

    try:
        STARTUP_TIMING_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[startup_timing] Не удалось записать файл замера: {e}")


@app.on_event("shutdown")
async def on_shutdown():
    EXECUTOR.shutdown(wait=False)
    for client in _HTTPX_CLIENTS.values():
        await client.aclose()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
