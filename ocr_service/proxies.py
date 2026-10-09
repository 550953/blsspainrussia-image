"""Прокси для исходящих запросов к Gemini.

Причина: на общем IP Render почти первый запрос каждого ключа получает 429.
GEMINI_PROXIES (через запятую) или PROXY_URL_* в Infisical. DIRECT всегда остаётся в ротации.
"""
import os
import random
import time
from typing import List, Optional
from urllib.parse import urlsplit

from .config import _read_infisical_secrets_once


def _load_gemini_proxies() -> tuple:
    raw = os.getenv("GEMINI_PROXIES", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()], "env:GEMINI_PROXIES"
    items = sorted((k, v.strip()) for k, v in _read_infisical_secrets_once().items() if k.startswith("PROXY_URL") and v.strip())
    if items:
        return [v for _, v in items], "infisical:PROXY_URL_*"
    return [], "none"


_configured_proxies, _proxy_source = _load_gemini_proxies()
GEMINI_PROXIES: List[Optional[str]] = (_configured_proxies + [None]) if _configured_proxies else [None]

print(
    f"[pid={os.getpid()}] Gemini proxy pool: {len(GEMINI_PROXIES)} канал(ов), источник: {_proxy_source} "
    f"({'прокси + DIRECT как fallback' if _configured_proxies else 'ТОЛЬКО DIRECT!'})"
)


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


class ProxyUnavailable(Exception):
    """Ошибка КАНАЛА (обрыв, таймаут, нет socksio), а не ключа: ключ в cooldown не уходит."""


class ProxyGeoBlocked(ProxyUnavailable):
    """Google отверг запрос по гео (400 User location is not supported): выходной IP канала в плохой стране.
    Канал уходит в карантин надолго, ключ не штрафуется, запрос повторяется через другой канал."""


# Сколько держим канал в карантине после гео-отказа (сек). По умолчанию 1 час.
PROXY_GEO_QUARANTINE_SECONDS = float(os.getenv("PROXY_GEO_QUARANTINE_SECONDS", "3600"))


class GeminiSmartProxyPool:
    """DIRECT первым, остальные по кругу; при обрыве канал уходит в cooldown."""

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
        self.block_reason = {route: "" for route in self.routes}  # "geo" | "" (для /status)
        self.geo_hits = {route: 0 for route in self.routes}       # сколько раз ловили гео-отказ с запуска
        self.cursor = 0

    def next(self) -> Optional[str]:
        now = time.monotonic()
        count = len(self.routes)
        for offset in range(count):
            route = self.routes[(self.cursor + offset) % count]
            if now >= self.cooldown_until[route]:
                self.cursor = (self.cursor + offset + 1) % count
                return route
        route = min(self.routes, key=lambda item: self.cooldown_until[item])
        self.cursor = (self.routes.index(route) + 1) % count
        return route

    def mark_ok(self, route: Optional[str]) -> None:
        """Успех (или ответ Gemini с ошибкой ключа/модели) снимает обычный cooldown,
        но НЕ трогает гео-карантин: параллельный запрос не должен его стереть."""
        self.failures[route] = 0
        if self.block_reason.get(route) == "geo" and time.monotonic() < self.cooldown_until[route]:
            return
        self.block_reason[route] = ""
        self.cooldown_until[route] = 0.0

    def quarantine_geo(self, route: Optional[str]) -> None:
        """Канал отдаёт гео-отказ: в карантин на PROXY_GEO_QUARANTINE_SECONDS (1 час)."""
        self.geo_hits[route] += 1
        self.failures[route] += 1
        self.block_reason[route] = "geo"
        self.cooldown_until[route] = max(self.cooldown_until[route], time.monotonic() + PROXY_GEO_QUARANTINE_SECONDS)
        print(
            f"[pid={os.getpid()}][gemini_proxy] {_proxy_label(route)} GEO-блок (User location is not supported): "
            f"карантин {PROXY_GEO_QUARANTINE_SECONDS / 60:.0f} мин"
        )

    def mark_failed(self, route: Optional[str], error_text: object) -> None:
        if isinstance(error_text, ProxyGeoBlocked):
            self.quarantine_geo(route)
            return
        self.failures[route] += 1
        upper = str(error_text).upper()
        if route is None:
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
        print(f"[pid={os.getpid()}][gemini_proxy] {_proxy_label(route)} cooldown {cooldown:.1f} сек")


smart_proxy_pool = GeminiSmartProxyPool(_configured_proxies)
