"""Пул ключей Gemini: round-robin + per-key cooldown + глобальный blackout."""
import os
import time
from typing import List, Optional

from .config import GEMINI_MODEL, GEMINI_MODELS, GEMINI_MODEL_COOLDOWN, GEMINI_MODEL_KEYS, _read_infisical_secrets_once
from .gemini_errors import GeminiAPIError, set_known_keys


# Аренда ключа на ОДНУ попытку. Должна быть больше GEMINI_HARD_TIMEOUT (45с).
KEY_LEASE_TTL = float(os.getenv("GEMINI_KEY_LEASE_TTL", "60"))


class GeminiKeyPool:
    """Пул ключей: round-robin + per-key cooldown + глобальный blackout.

    Защита от коллизий: пока ключ занят (reserved), его никто больше не берёт.
    Искусственного интервала между запросами нет (убран в v4.2)."""

    _BILLING_HOLD_CATEGORIES = {
        "BILLING_CREDITS_DEPLETED",
        "BILLING_PAYMENT_REQUIRED",
        "BILLING_SPEND_CAP_BREACHED",
    }
    _PROJECT_HOLD_CATEGORIES = {"PROJECT_ACCESS_DENIED", "PROJECT_PERMISSION_DENIED"}

    def __init__(self):
        self.keys: List[str] = []
        self.key_names: List[str] = []
        self.blocked: List[float] = []
        self.reserved: List[bool] = []
        self.dead: List[bool] = []
        self.last_used: List[float] = []
        self.reserved_at: List[float] = []
        self.last_error: List[Optional[dict]] = []
        self.blackout_until: float = 0.0
        self.rr_index: int = 0
        self.model_blocked_until: dict = {}
        self.model_unavailable: set = set()   # (idx, model): модель закрыта для этого ключа (404)

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

        if not keys:
            for name, value in sorted(_read_infisical_secrets_once().items()):
                v = value.strip()
                if name.startswith(prefix) and v and v not in seen:
                    seen.add(v)
                    keys.append(v)
                    names.append(name)
            if keys:
                print(f"[pid={os.getpid()}] Gemini pool: ключи не найдены в os.environ, "
                      f"загружено {len(keys)} из Infisical (режим локальной разработки)")

        self.keys = keys
        self.key_names = names
        self.blocked = [0.0] * len(keys)
        self.reserved = [False] * len(keys)
        self.dead = [False] * len(keys)
        self.last_used = [0.0] * len(keys)
        self.reserved_at = [0.0] * len(keys)
        self.last_error = [None] * len(keys)
        self.blackout_until = 0.0
        self.rr_index = 0
        print(f"[pid={os.getpid()}] Gemini pool: инициализирован, {len(keys)} ключ(ей)")

    def is_overloaded(self) -> tuple:
        now = time.monotonic()
        if now < self.blackout_until:
            return True, self.blackout_until - now
        return False, 0.0

    def _is_operator_hold(self, idx: int) -> bool:
        category = (self.last_error[idx] or {}).get("category")
        return category in self._BILLING_HOLD_CATEGORIES or category in self._PROJECT_HOLD_CATEGORIES

    def acquire(self) -> tuple:
        """Резервирует следующий свободный ключ по кругу. (None, None), если свободных нет."""
        if not self.keys:
            return None, None
        n = len(self.keys)
        now = time.monotonic()
        for _ in range(n):
            idx = self.rr_index % n
            self.rr_index += 1
            if not self.dead[idx] and not self.reserved[idx] and now >= self.blocked[idx]:
                self.reserved[idx] = True
                self.reserved_at[idx] = now
                self.last_used[idx] = now
                return idx, self.key_names[idx]
        return None, None

    def model_allowed(self, idx: Optional[int], model: str) -> bool:
        """Можно ли этому ключу работать с моделью (ограничение GEMINI_MODEL_KEYS и 404-память)."""
        if idx is None:
            return True
        if (idx, model) in self.model_unavailable:
            return False
        prefixes = GEMINI_MODEL_KEYS.get(model)
        return prefixes is None or any(self.key_names[idx].startswith(p) for p in prefixes)

    def mark_model_unavailable(self, idx: int, model: str) -> None:
        if (idx, model) not in self.model_unavailable:
            self.model_unavailable.add((idx, model))
            print(f"[pid={os.getpid()}][gemini_model] {model} недоступна ключу {self.key_names[idx]} (404), больше не пробую", flush=True)

    def pick_model(self, idx: Optional[int] = None) -> str:
        """Первая допустимая для ключа модель цепочки, не помеченная перегруженной.
        Если перегружены все, берётся та, что освободится раньше (запросы не блокируются)."""
        candidates = [m for m in GEMINI_MODELS if self.model_allowed(idx, m)] or list(GEMINI_MODELS)
        now = time.monotonic()
        for model in candidates:
            if now >= self.model_blocked_until.get(model, 0.0):
                return model
        return min(candidates, key=lambda m: self.model_blocked_until.get(m, 0.0))

    def mark_model_overloaded(self, model: str, seconds: Optional[float] = None) -> None:
        seconds = GEMINI_MODEL_COOLDOWN if seconds is None else seconds
        until = time.monotonic() + seconds
        if until > self.model_blocked_until.get(model, 0.0):
            self.model_blocked_until[model] = until
            print(f"[pid={os.getpid()}][gemini_model] {model} перегружена, обхожу {seconds:.0f}s", flush=True)

    @staticmethod
    def is_model_side_error(error: GeminiAPIError) -> bool:
        """Сбой на стороне модели (не ключа): ключ штрафовать не нужно."""
        return error.category == "UPSTREAM_UNAVAILABLE" or error.http_status in (500, 502, 504)

    def ensure_released(self, idx: Optional[int]) -> None:
        """Идемпотентно снять аренду (для finally): без cooldown и побочных эффектов."""
        if idx is not None and 0 <= idx < len(self.reserved) and self.reserved[idx]:
            self.reserved[idx] = False

    def reap_stale(self, max_age: Optional[float] = None) -> List[str]:
        """Снять аренды старше max_age (потерянные). Возвращает имена снятых ключей."""
        max_age = KEY_LEASE_TTL if max_age is None else max_age
        now = time.monotonic()
        reaped = []
        for idx, held in enumerate(self.reserved):
            if held and now - self.reserved_at[idx] > max_age:
                self.reserved[idx] = False
                reaped.append(self.key_names[idx])
        return reaped

    def release(self, idx: int, error: Optional[GeminiAPIError] = None) -> dict:
        self.reserved[idx] = False
        key_name = self.key_names[idx] if idx < len(self.key_names) else f"key_{idx}"
        if error is None:
            self.blocked[idx] = 0.0
            self.dead[idx] = False
            self.last_error[idx] = None
            return {"state": "READY", "cooldown_seconds": 0.0}

        cooldown = max(0.0, float(error.cooldown_seconds))
        self.dead[idx] = bool(error.permanent_key_failure)
        operator_hold = bool(error.hold_until_restart)
        self.blocked[idx] = (
            float("inf") if self.dead[idx] or operator_hold else time.monotonic() + cooldown
        )
        self.last_error[idx] = {
            "key_name": key_name,
            "http_status": error.http_status,
            "provider_status": error.provider_status,
            "category": error.category,
            "message": error.message,
        }
        state = (
            "INVALID_KEY" if self.dead[idx]
            else "BILLING_HOLD" if error.category in self._BILLING_HOLD_CATEGORIES
            else "PROJECT_HOLD" if error.category in self._PROJECT_HOLD_CATEGORIES
            else "COOLDOWN"
        )
        state_detail = (
            "requires_key_replacement=true" if self.dead[idx]
            else "retry=disabled_until_restart" if operator_hold
            else f"cooldown={cooldown:.0f}s"
        )
        print(
            f"[pid={os.getpid()}][gemini_key] {key_name} HTTP {error.http_status} {error.category}; "
            f"state={state}; {state_detail}; proxy is not quarantined for API responses",
            flush=True,
        )

        now = time.monotonic()
        live_idx = [i for i in range(len(self.keys)) if not self.dead[i] and not self._is_operator_hold(i)]
        if live_idx and all(now < self.blocked[i] for i in live_idx):
            soonest = min(self.blocked[i] for i in live_idx)
            if soonest > self.blackout_until:
                self.blackout_until = soonest
                print(
                    f"[pid={os.getpid()}][gemini_key] все доступные ключи временно заблокированы; "
                    f"ближайшая проверка через {soonest - now:.0f}s",
                    flush=True,
                )
        return {
            "state": state,
            "cooldown_seconds": cooldown,
            "permanent_key_failure": self.dead[idx],
            "hold_until_restart": operator_hold,
            "category": error.category,
        }

    def key_state(self, idx: int, now: float) -> tuple:
        """(state, category, cooldown_left_s) для одного ключа; now = time.monotonic()."""
        category = str((self.last_error[idx] or {}).get("category") or "")
        if self.reserved[idx]:
            return "in_use", category, 0.0
        if self.dead[idx]:
            return "invalid", category, 0.0
        if category in self._BILLING_HOLD_CATEGORIES:
            return "billing", category, 0.0
        if category in self._PROJECT_HOLD_CATEGORIES:
            return "project", category, 0.0
        if now < self.blocked[idx]:
            return "cooldown", category, round(self.blocked[idx] - now, 1)
        return "ready", category, 0.0

    def grouped_keys(self, now: Optional[float] = None) -> dict:
        """Компактные группы ключей для /health."""
        now = time.monotonic() if now is None else now
        groups = {
            "ready": [], "in_use": [], "cooldown": {},
            "billing_hold": {}, "project_hold": {}, "invalid_key": [],
        }
        for idx, key_name in enumerate(self.key_names):
            state, category, _ = self.key_state(idx, now)
            if state == "in_use":
                groups["in_use"].append(key_name)
            elif state == "invalid":
                groups["invalid_key"].append(key_name)
            elif state == "billing":
                groups["billing_hold"].setdefault(category, []).append(key_name)
            elif state == "project":
                groups["project_hold"].setdefault(category, []).append(key_name)
            elif state == "cooldown":
                groups["cooldown"].setdefault(category or "TEMPORARY_BLOCK", []).append(key_name)
            else:
                groups["ready"].append(key_name)
        return groups


# Инициализация при импорте (как и раньше).
gemini_pool = GeminiKeyPool()
gemini_pool.init_from_env()

GEMINI_KEYS = gemini_pool.keys
set_known_keys(GEMINI_KEYS)  # чтобы _safe_gemini_message вычёркивал ключи из текста ошибок

gemini_model_name = GEMINI_MODELS[0] if GEMINI_KEYS else None  # основная модель (для /status)
print(f"[pid={os.getpid()}] Gemini ключей найдено: {len(GEMINI_KEYS)}, модели: {GEMINI_MODELS}")
