import time

from ocr_service.gemini_errors import GeminiAPIError, _classify_gemini_http_error
from ocr_service.key_pool import GeminiKeyPool


def make_pool(keys=("GEMINI_API_KEY_1", "GEMINI_API_KEY_2")):
    pool = GeminiKeyPool()
    pool.keys = [f"secret-{index}" for index in range(len(keys))]
    pool.key_names = list(keys)
    pool.blocked = [0.0] * len(keys)
    pool.reserved = [False] * len(keys)
    pool.dead = [False] * len(keys)
    pool.last_used = [0.0] * len(keys)
    pool.reserved_at = [0.0] * len(keys)
    pool.last_error = [None] * len(keys)
    pool.lease_ttl = [60.0] * len(keys)
    return pool


def test_email_reserve_leaves_a_free_key_for_ocr():
    pool = make_pool()
    first = pool.acquire(reserve=1, model_chain=["model-a"])
    assert first == (0, "GEMINI_API_KEY_1")
    assert pool.acquire(reserve=1, model_chain=["model-a"]) == (None, None)
    assert pool.acquire(reserve=0) == (1, "GEMINI_API_KEY_2")


def test_reap_stale_uses_per_key_lease_ttl(monkeypatch):
    pool = make_pool(("GEMINI_API_KEY_1", "GEMINI_API_KEY_2"))
    now = 1000.0
    monkeypatch.setattr("ocr_service.key_pool.time.monotonic", lambda: now)
    pool.reserved = [True, True]
    pool.reserved_at = [now - 100, now - 201]
    pool.lease_ttl = [200.0, 200.0]
    assert pool.reap_stale() == ["GEMINI_API_KEY_2"]
    assert pool.reserved == [True, False]

    pool.reserved[1] = True
    pool.reserved_at[1] = now - 61
    pool.lease_ttl[1] = 60.0
    assert pool.reap_stale() == ["GEMINI_API_KEY_2"]


def test_template_429_cools_only_the_key_model_pair():
    pool = make_pool(("GEMINI_API_KEY_1",))
    index, _ = pool.acquire(model_chain=["model-a"])
    policy = _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", "per-minute quota")
    error = GeminiAPIError(429, "RESOURCE_EXHAUSTED", "per-minute quota", policy)
    outcome = pool.release(index, error=error, model="model-a")

    assert outcome["state"] == "MODEL_COOLDOWN"
    assert pool.blocked[index] == 0.0
    assert pool.blackout_until == 0.0
    assert not pool.dead[index]
    assert not pool.model_allowed(index, "model-a", respect_key_cooldown=True)
    assert pool.model_allowed(index, "model-b", respect_key_cooldown=True)
    assert pool.pick_model(index, ["model-a", "model-b"], respect_key_cooldown=True) == "model-b"
    assert pool.acquire(model_chain=["model-a"], respect_key_cooldown=True) == (None, None)


def test_release_without_model_keeps_ocr_key_cooldown_behavior():
    pool = make_pool(("GEMINI_API_KEY_1",))
    index, _ = pool.acquire()
    policy = _classify_gemini_http_error(429, "RESOURCE_EXHAUSTED", "per-minute quota")
    error = GeminiAPIError(429, "RESOURCE_EXHAUSTED", "per-minute quota", policy)
    pool.release(index, error=error)

    assert pool.blocked[index] > time.monotonic()
    assert pool.blackout_until == pool.blocked[index]
    assert not pool.dead[index]
