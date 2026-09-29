"""Пул ключей Gemini: round-robin + per-key cooldown + глобальный blackout."""
import os
import time
from typing import List, Optional

from .config import GEMINI_MODEL, _read_infisical_secrets_once
from .gemini_errors import GeminiAPIError, set_known_keys


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
        self.last_error: List[Optional[dict]] = []
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
                self.last_used[idx] = now
                return idx, self.key_names[idx]
        return None, None

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

gemini_model_name = GEMINI_MODEL if GEMINI_KEYS else None
print(f"[pid={os.getpid()}] Gemini ключей найдено: {len(GEMINI_KEYS)}, модель: {gemini_model_name}")
