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
        self.failures[route] = 0
        self.cooldown_until[route] = 0.0

    def mark_failed(self, route: Optional[str], error_text: object) -> None:
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
