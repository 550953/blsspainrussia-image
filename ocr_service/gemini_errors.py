"""Классификация ошибок Gemini и безопасные сообщения (без ключей в тексте)."""
import random
import re
from typing import List

_RETRY_DELAY_RE = re.compile(r'retry[_\s\-]?delay[^0-9]*(\d+(?:\.\d+)?)', re.IGNORECASE)

# Список ключей для вычёркивания из сообщений. Заполняется из key_pool.py
# (раньше здесь использовался globals().get("GEMINI_KEYS"), теперь явная регистрация).
_KNOWN_KEYS: List[str] = []


def set_known_keys(keys: List[str]) -> None:
    global _KNOWN_KEYS
    _KNOWN_KEYS = keys


# Google отвечает 400 FAILED_PRECONDITION, если ВЫХОДНОЙ IP запроса стоит в неподдерживаемой стране.
# Это вина канала (прокси/DIRECT), а не ключа и не модели.
_GEO_BLOCK_MARKERS = (
    "user location is not supported",
    "location is not supported for the api use",
    "not available in your country",
    "not available in your region",
)


def is_geo_blocked(http_status: int, message: str) -> bool:
    if int(http_status or 0) != 400:
        return False
    text = str(message or "").casefold()
    return any(marker in text for marker in _GEO_BLOCK_MARKERS)


def _parse_retry_delay(err_text: str) -> float:
    m = _RETRY_DELAY_RE.search(err_text)
    return float(m.group(1)) if m else 0.0


def _classify_gemini_http_error(http_status: int, provider_status: str, message: str) -> dict:
    status = int(http_status or 0)
    provider = str(provider_status or "").upper()
    text = str(message or "").casefold()

    def hold(category: str, action: str) -> dict:
        return {
            "category": category,
            "cooldown_seconds": 0.0,
            "permanent_key_failure": False,
            "hold_until_restart": True,
            "action": action,
        }

    if "prepayment credits are depleted" in text:
        return hold("BILLING_CREDITS_DEPLETED", "billing_hold_until_restart; no proxy cooldown")
    if status == 402:
        return hold("BILLING_PAYMENT_REQUIRED", "billing_hold_until_restart; no proxy cooldown")
    if status == 403 and "spend cap breached" in text:
        return hold("BILLING_SPEND_CAP_BREACHED", "billing_hold_until_restart; no proxy cooldown")
    if status == 403 and "project has been denied access" in text:
        return hold("PROJECT_ACCESS_DENIED", "project_hold_until_restart; no proxy cooldown")
    if (
        status == 401
        or "api_key_invalid" in provider.casefold()
        or "api key not valid" in text
        or "api key is invalid" in text
    ):
        return {
            "category": "INVALID_API_KEY",
            "cooldown_seconds": 0.0,
            "permanent_key_failure": True,
            "hold_until_restart": False,
            "action": "key_disabled_until_replaced_and_restart",
        }
    if status == 429 or provider == "RESOURCE_EXHAUSTED":
        retry_after = _parse_retry_delay(message)
        return {
            "category": "RATE_LIMIT_OR_QUOTA",
            "cooldown_seconds": max(30.0, retry_after) + random.uniform(0.0, 3.0),
            "permanent_key_failure": False,
            "hold_until_restart": False,
            "action": "temporary_key_pause; no proxy cooldown",
        }
    if status == 403:
        return hold("PROJECT_PERMISSION_DENIED", "project_hold_until_restart; no proxy cooldown")
    if status == 503 or provider == "UNAVAILABLE":
        return {
            "category": "UPSTREAM_UNAVAILABLE",
            "cooldown_seconds": 10.0 + random.uniform(0.0, 3.0),
            "permanent_key_failure": False,
            "hold_until_restart": False,
            "action": "temporary_key_pause; no proxy cooldown",
        }
    return {
        "category": f"HTTP_{status}" if status else "API_RESPONSE_ERROR",
        "cooldown_seconds": 15.0,
        "permanent_key_failure": False,
        "hold_until_restart": False,
        "action": "temporary_key_pause; no proxy cooldown",
    }


def _safe_gemini_message(message: str) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(message or "")).strip()
    for secret in _KNOWN_KEYS:
        if secret:
            text = text.replace(secret, "[REDACTED_KEY]")
    text = re.sub(r"projects/\d+", "projects/[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"Correlation id:\s*[A-Za-z0-9-]+", "Correlation id: [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED_KEY]", text)
    text = re.sub(r"(?i)((?:x-goog-)?api[_ -]?key\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED_KEY]", text)
    return text[:500]


class GeminiAPIError(RuntimeError):
    def __init__(self, http_status: int, provider_status: str, message: str, policy: dict):
        self.http_status = int(http_status or 0)
        self.provider_status = str(provider_status or "")
        self.message = _safe_gemini_message(message)
        self.category = str(policy.get("category") or "API_RESPONSE_ERROR")
        self.cooldown_seconds = float(policy.get("cooldown_seconds") or 0.0)
        self.permanent_key_failure = bool(policy.get("permanent_key_failure"))
        self.hold_until_restart = bool(policy.get("hold_until_restart"))
        self.action = str(policy.get("action") or "")
        super().__init__(f"HTTP {self.http_status} {self.provider_status or 'API_ERROR'}: {self.message}")
