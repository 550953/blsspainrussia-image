"""Прокси для OCR из проксимена (bls-proxy): снапшот пула + пачки итогов ok/fail/geo.

Включается ТОЛЬКО если заданы PROXY_MANAGER_URL и PROXY_MANAGER_TOKEN (Infisical или переменные Render).
Иначе proxies.py работает ровно как раньше: GEMINI_PROXIES / PROXY_URL_* + DIRECT.

Как это работает
  * раз в PROXY_MANAGER_REFRESH_S (60 с) тянем GET {URL}/ocr/pool: список живых прокси (без аренд);
  * next()/mark_ok()/mark_failed()/quarantine_geo() остаются локальными и мгновенными: никакой сети
    на запрос Gemini; счётчики копятся в памяти;
  * раз в PROXY_MANAGER_REPORT_S (15 с) счётчики уходят одной пачкой POST {URL}/ocr/report;
  * менеджер недоступен -> живём на последнем снапшоте до PROXY_MANAGER_STALE_S (900 с), дальше
    возвращаемся к статическому списку (GEMINI_PROXIES / PROXY_URL_*) + DIRECT;
  * менеджер вернул пустой список -> тот же статический список + DIRECT.
DIRECT (None) всегда остаётся в ротации, как в исходном GeminiSmartProxyPool.

Этот модуль НЕ импортирует остальной пакет (нет циклов): класс-наследник собирается в proxies.py.
Токен и логины прокси в логи не попадают.
"""
import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import httpx


@dataclass(frozen=True)
class ManagerConfig:
    url: str
    token: str
    refresh_s: float = 60.0
    report_s: float = 15.0
    stale_s: float = 900.0
    timeout_s: float = 5.0


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        value = default
    return min(high, max(low, value))


def load_manager_config(get_secret: Callable[[str], tuple]) -> Optional[ManagerConfig]:
    """get_secret(name) -> (value, source), как config._config_secret. Нет URL или токена -> None (выключено)."""
    url = (get_secret("PROXY_MANAGER_URL")[0] or "").strip().rstrip("/")
    token = (get_secret("PROXY_MANAGER_TOKEN")[0] or "").strip()
    if not url or not token:
        return None
    if not url.startswith(("http://", "https://")):
        print(f"[pid={os.getpid()}][proxy_manager] PROXY_MANAGER_URL без http(s):// — менеджер ВЫКЛЮЧЕН")
        return None
    if url.startswith("http://"):
        print(f"[pid={os.getpid()}][proxy_manager] ВНИМАНИЕ: PROXY_MANAGER_URL по http, токен пойдёт открытым текстом")
    return ManagerConfig(
        url=url,
        token=token,
        refresh_s=_env_float("PROXY_MANAGER_REFRESH_S", 60, 10, 3600),
        report_s=_env_float("PROXY_MANAGER_REPORT_S", 15, 5, 600),
        stale_s=_env_float("PROXY_MANAGER_STALE_S", 900, 60, 86400),
        timeout_s=_env_float("PROXY_MANAGER_TIMEOUT_S", 5, 1, 30),
    )


def parse_pool_response(data: object) -> List[Tuple[str, str]]:
    """{"proxies":[{"id":..,"url":..}]} -> [(id, url)]. Структура не та -> ValueError (снапшот не применяем)."""
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise ValueError("unexpected /ocr/pool payload")
    out: List[Tuple[str, str]] = []
    for item in data["proxies"]:
        if not isinstance(item, dict):
            continue
        pid, url = item.get("id"), item.get("url")
        if isinstance(pid, str) and pid and isinstance(url, str) and url:
            out.append((pid, url))
    return out


class ManagedPoolMixin:
    """Подмешивается ПЕРЕД GeminiSmartProxyPool. Наследник задаёт GEO_EXC и FAULT_EXC."""

    GEO_EXC: tuple = ()      # гео-отказ Gemini (ProxyGeoBlocked)
    FAULT_EXC: tuple = ()    # вина канала: обрыв/таймаут/SOCKS (ProxyUnavailable)

    def _managed_init(self, static_routes: Iterable[str], cfg: ManagerConfig,
                      on_change: Optional[Callable[[List[str]], None]] = None) -> None:
        self._static_routes: List[str] = list(static_routes)
        self._cfg = cfg
        self._on_change = on_change
        self._id_by_route: Dict[str, str] = {}
        self._counts: Dict[str, List[int]] = {}      # proxy_id -> [ok, fail, geo]
        self._last_good = 0.0
        self._managed_active = False
        self._tasks: List[asyncio.Task] = []
        self._transport: Optional[httpx.AsyncBaseTransport] = None   # для тестов
        self._last_log: Dict[str, float] = {}

    # ------------------------------------------------------------------ состояние
    def _ensure_state(self, route: Optional[str]) -> None:
        """Маршрут мог уйти из снапшота, пока запрос был в полёте: не даём base-классу упасть с KeyError."""
        self.cooldown_until.setdefault(route, 0.0)
        self.failures.setdefault(route, 0)
        self.block_reason.setdefault(route, "")
        self.geo_hits.setdefault(route, 0)

    def _log(self, key: str, text: str, every: float = 60.0) -> None:
        now = time.monotonic()
        if now - self._last_log.get(key, -1e9) >= every:
            self._last_log[key] = now
            print(f"[pid={os.getpid()}][proxy_manager] {text}")

    def _set_routes(self, urls: List[str], id_map: Dict[str, str], managed: bool) -> None:
        routes: List[Optional[str]] = [None] + list(urls)
        for route in routes:
            self._ensure_state(route)
        now = time.monotonic()
        keep = set(routes)
        for route in [r for r in list(self.cooldown_until) if r not in keep]:
            if self.cooldown_until[route] <= now:      # ещё отдыхающие помним: вернётся — останется штраф
                self.cooldown_until.pop(route, None)
                self.failures.pop(route, None)
                self.block_reason.pop(route, None)
                self.geo_hits.pop(route, None)
        self._id_by_route = dict(id_map)
        self.routes = routes
        self.cursor = self.cursor % len(routes)
        self._managed_active = managed
        if self._on_change is not None:
            try:
                self._on_change(list(urls))
            except Exception:
                pass

    def apply_snapshot(self, items: Iterable[Tuple[str, str]]) -> None:
        """Синхронно и целиком (без await внутри): параллельный next() видит либо старый, либо новый список."""
        id_map: Dict[str, str] = {}
        for pid, url in items:
            id_map.setdefault(url, pid)
        self._last_good = time.monotonic()
        if id_map:
            self._set_routes(sorted(id_map), id_map, managed=True)
        else:
            self._set_routes(self._static_routes, {}, managed=False)
            self._log("empty", "менеджер вернул пустой пул — работаем на статическом списке + DIRECT")

    # ------------------------------------------------------------------ счётчики
    def _count(self, route: Optional[str], idx: int) -> None:
        pid = self._id_by_route.get(route) if route is not None else None
        if pid is None:
            return                                  # DIRECT и статические прокси менеджеру не интересны
        counts = self._counts.setdefault(pid, [0, 0, 0])
        counts[idx] = min(counts[idx] + 1, 1_000_000)

    def mark_ok(self, route: Optional[str]) -> None:
        self._ensure_state(route)
        super().mark_ok(route)
        self._count(route, 0)

    def mark_failed(self, route: Optional[str], error_text: object) -> None:
        self._ensure_state(route)
        super().mark_failed(route, error_text)      # гео-отказ внутри зовёт quarantine_geo (ниже)
        if isinstance(error_text, self.GEO_EXC):
            return
        if isinstance(error_text, self.FAULT_EXC):
            self._count(route, 1)
        # 429 / квота ключа / ошибки модели — не вина прокси, менеджеру не отправляем

    def quarantine_geo(self, route: Optional[str]) -> None:
        self._ensure_state(route)
        super().quarantine_geo(route)
        self._count(route, 2)

    # ------------------------------------------------------------------ сеть
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._cfg.timeout_s, trust_env=False, transport=self._transport)

    def _headers(self) -> Dict[str, str]:
        return {"X-Bot-Token": self._cfg.token}

    @staticmethod
    def _why(exc: BaseException) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP {exc.response.status_code}"
        return type(exc).__name__          # без текста исключения: там может быть URL

    async def refresh_once(self) -> bool:
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._cfg.url}/ocr/pool", headers=self._headers())
            resp.raise_for_status()
            items = parse_pool_response(resp.json())
        except Exception as exc:
            self._refresh_failed(exc)
            return False
        was_managed = self._managed_active
        self.apply_snapshot(items)
        if self._managed_active and not was_managed:
            self._log("up", f"пул из менеджера: каналов {len(items)}", every=0)
        return True

    def _refresh_failed(self, exc: BaseException) -> None:
        self._log("refresh", f"не удалось обновить пул ({self._why(exc)}); держу последний снапшот")
        if self._managed_active and time.monotonic() - self._last_good > self._cfg.stale_s:
            self._set_routes(self._static_routes, {}, managed=False)
            self._log("stale", "снапшот протух — возвращаюсь на статический список + DIRECT", every=0)

    async def flush(self) -> bool:
        if not self._counts:
            return True
        batch, self._counts = self._counts, {}
        items = [{"id": pid, "ok": c[0], "fail": c[1], "geo": c[2]} for pid, c in batch.items() if any(c)]
        if not items:
            return True
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._cfg.url}/ocr/report", headers=self._headers(),
                                         json={"items": items[:500]})
            resp.raise_for_status()
            return True
        except Exception as exc:
            for pid, c in batch.items():             # вернуть в очередь, отправим со следующей пачкой
                cur = self._counts.setdefault(pid, [0, 0, 0])
                for i in range(3):
                    cur[i] = min(cur[i] + c[i], 1_000_000)
            self._log("report", f"не удалось отправить итоги ({self._why(exc)}); повторю")
            return False

    # ------------------------------------------------------------------ фон
    async def _refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception:
                pass
            await asyncio.sleep(self._cfg.refresh_s * random.uniform(0.9, 1.1))

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.report_s * random.uniform(0.9, 1.1))
            try:
                await self.flush()
            except Exception:
                pass

    async def start(self) -> None:
        """Первый снапшот до приёма трафика (ограничен timeout_s), затем фоновые циклы. Не бросает."""
        if any(not t.done() for t in self._tasks):
            return
        try:
            await self.refresh_once()
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._refresh_loop(), name="proxy_manager_refresh"),
                       loop.create_task(self._report_loop(), name="proxy_manager_report")]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(self.flush(), timeout=3.0)
        except Exception:
            pass
