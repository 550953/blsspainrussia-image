import json

import httpx
import pytest

from ocr_service import proxy_manager as pm
from ocr_service.proxies import (
    GeminiSmartProxyPool, ManagedGeminiProxyPool, ProxyGeoBlocked, ProxyUnavailable,
)

A = "socks5://u:p@1.1.1.1:1080"
B = "socks5://u:p@2.2.2.2:1080"


def make_pool(handler=None, **cfg_kw):
    cfg = pm.ManagerConfig(url="https://pm.example", token="tok", **cfg_kw)
    seen = []
    pool = ManagedGeminiProxyPool([])
    pool._managed_init([], cfg, on_change=seen.append)
    if handler is not None:
        pool._transport = httpx.MockTransport(handler)
    pool.seen = seen
    return pool


def pool_reply(*items):
    return httpx.Response(200, json={"proxies": [{"id": i, "url": u} for i, u in items]})


def test_config_disabled_without_url_or_token():
    assert pm.load_manager_config(lambda n: ("", "x")) is None
    assert pm.load_manager_config(lambda n: ("" if n.endswith("TOKEN") else "https://x", "x")) is None
    assert pm.load_manager_config(lambda n: ("ftp://x" if n.endswith("URL") else "t", "x")) is None


def test_config_enabled_and_clamped(monkeypatch):
    monkeypatch.setenv("PROXY_MANAGER_REFRESH_S", "1")
    cfg = pm.load_manager_config(lambda n: ("https://pm.example/" if n.endswith("URL") else "tok", "x"))
    assert cfg.url == "https://pm.example" and cfg.token == "tok" and cfg.refresh_s == 10


def test_parse_pool_response_rejects_garbage():
    with pytest.raises(ValueError):
        pm.parse_pool_response({"nope": 1})
    assert pm.parse_pool_response({"proxies": [{"id": "a", "url": "x"}, {"id": 1}, "z"]}) == [("a", "x")]


def test_snapshot_replaces_routes_and_keeps_direct():
    pool = make_pool()
    pool.apply_snapshot([("a", A), ("b", B)])
    assert pool.routes[0] is None and set(pool.routes[1:]) == {A, B}
    assert pool.seen[-1] == sorted([A, B])
    for _ in range(6):
        assert pool.next() in pool.routes


def test_empty_snapshot_falls_back_to_static():
    pool = ManagedGeminiProxyPool(["socks5://static:1"])
    pool._managed_init(["socks5://static:1"], pm.ManagerConfig("https://pm", "t"))
    pool.apply_snapshot([("a", A)])
    assert A in pool.routes
    pool.apply_snapshot([])
    assert pool.routes == [None, "socks5://static:1"]


def test_stale_snapshot_returns_to_static(monkeypatch):
    pool = make_pool(stale_s=100)
    pool.apply_snapshot([("a", A)])
    pool._last_good -= 1000
    pool._refresh_failed(RuntimeError("boom"))
    assert pool.routes == [None] and not pool._managed_active


def test_fresh_snapshot_survives_manager_outage():
    pool = make_pool(stale_s=900)
    pool.apply_snapshot([("a", A)])
    pool._refresh_failed(RuntimeError("boom"))
    assert A in pool.routes and pool._managed_active


def test_counters_ok_fail_geo_and_ignored_errors():
    pool = make_pool()
    pool.apply_snapshot([("a", A), ("b", B)])
    pool.mark_ok(A)
    pool.mark_ok(A)
    pool.mark_failed(A, ProxyUnavailable("socks"))            # вина канала
    pool.mark_failed(A, "429 quota")                          # ключ, не прокси
    pool.mark_failed(A, RuntimeError("model"))               # модель
    pool.mark_failed(B, ProxyGeoBlocked("geo"))               # гео
    pool.mark_ok(None)                                        # DIRECT не считаем
    pool.mark_failed(None, ProxyUnavailable("x"))
    assert pool._counts == {"a": [2, 1, 0], "b": [0, 0, 1]}
    assert pool.block_reason[B] == "geo" and pool.geo_hits[B] == 1


def test_route_removed_while_in_flight_does_not_crash():
    pool = make_pool()
    pool.apply_snapshot([("a", A)])
    pool.apply_snapshot([("b", B)])          # A исчез из снапшота
    pool.mark_ok(A)
    pool.mark_failed(A, ProxyUnavailable("late"))
    pool.quarantine_geo(A)
    assert pool._counts == {}                # неизвестный менеджеру маршрут не отчитываем


def test_cooling_route_keeps_penalty_if_it_returns():
    pool = make_pool()
    pool.apply_snapshot([("a", A)])
    pool.mark_failed(A, ProxyUnavailable("x"))
    pool.apply_snapshot([("b", B)])
    assert A in pool.cooldown_until           # отдыхающий помним
    pool.apply_snapshot([("a", A), ("b", B)])
    assert pool.cooldown_until[A] > 0


@pytest.mark.asyncio
async def test_refresh_and_flush_roundtrip():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers.get("x-bot-token"), req.content))
        if req.url.path == "/ocr/pool":
            return pool_reply(("a", A))
        return httpx.Response(200, json={"ok": True})

    pool = make_pool(handler)
    assert await pool.refresh_once()
    assert A in pool.routes
    pool.mark_ok(A)
    pool.mark_failed(A, ProxyUnavailable("x"))
    assert await pool.flush()
    method, path, token, body = calls[-1]
    assert (method, path, token) == ("POST", "/ocr/report", "tok")
    assert json.loads(body) == {"items": [{"id": "a", "ok": 1, "fail": 1, "geo": 0}]}
    assert pool._counts == {}
    n = len(calls)
    assert await pool.flush() and len(calls) == n        # пусто — сети нет


@pytest.mark.asyncio
async def test_flush_failure_requeues_counts():
    pool = make_pool(lambda req: httpx.Response(500))
    pool.apply_snapshot([("a", A)])
    pool.mark_ok(A)
    assert not await pool.flush()
    pool.mark_ok(A)
    assert pool._counts == {"a": [2, 0, 0]}


@pytest.mark.asyncio
async def test_bad_payload_keeps_old_snapshot():
    pool = make_pool(lambda req: httpx.Response(200, json={"oops": 1}))
    pool.apply_snapshot([("a", A)])
    assert not await pool.refresh_once()
    assert A in pool.routes


@pytest.mark.asyncio
async def test_manager_down_keeps_old_snapshot():
    def handler(req):
        raise httpx.ConnectError("down")
    pool = make_pool(handler)
    pool.apply_snapshot([("a", A)])
    assert not await pool.refresh_once()
    assert A in pool.routes


@pytest.mark.asyncio
async def test_start_stop_flushes_and_never_raises():
    sent = []

    def handler(req: httpx.Request):
        if req.url.path == "/ocr/pool":
            return pool_reply(("a", A))
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    pool = make_pool(handler)
    await pool.start()
    assert A in pool.routes and len(pool._tasks) == 2
    pool.mark_ok(A)
    await pool.stop()
    assert sent == [{"items": [{"id": "a", "ok": 1, "fail": 0, "geo": 0}]}]
    assert pool._tasks == []


@pytest.mark.asyncio
async def test_start_with_manager_down_still_returns():
    def handler(req):
        raise httpx.ConnectError("down")
    pool = make_pool(handler)
    await pool.start()
    assert pool.routes == [None]
    await pool.stop()


def test_default_pool_untouched_without_manager():
    from ocr_service import proxies
    if proxies._manager_cfg is None:
        assert type(proxies.smart_proxy_pool) is GeminiSmartProxyPool
        assert not hasattr(proxies.smart_proxy_pool, "start")
