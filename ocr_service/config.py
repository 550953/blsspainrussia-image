"""Настройки процесса и секреты.

Секреты: Infisical (основной источник) -> переменные Render (запасной).
BETTERSTACK_FORCE_RENDER_kj123664=true: значение Render главнее Infisical.

Этот модуль ни от чего в пакете не зависит, его можно импортировать откуда угодно.
"""
import os
import time
import math
from pathlib import Path
from typing import Optional

PROCESS_START_TIME = time.time()
BASE_DIR = Path(__file__).resolve().parent.parent  # папка, где лежит main.py
STARTUP_TIMING_FILE = Path("/tmp/startup_timing.json")

OCR_MAX_WORKERS = int(os.getenv("OCR_MAX_WORKERS", "8"))

# ---------------------------------------------------------------------------
# Infisical
# ---------------------------------------------------------------------------
_INFISICAL_SECRETS_CACHE: Optional[dict] = None


def _read_infisical_secrets_once() -> dict:
    global _INFISICAL_SECRETS_CACHE
    if _INFISICAL_SECRETS_CACHE is not None:
        return _INFISICAL_SECRETS_CACHE

    _INFISICAL_SECRETS_CACHE = {}
    required = (
        os.environ.get("INFISICAL_CLIENT_ID"),
        os.environ.get("INFISICAL_CLIENT_SECRET"),
        os.environ.get("INFISICAL_PROJECT_ID"),
    )
    if not all(required):
        return _INFISICAL_SECRETS_CACHE

    try:
        import requests as _requests

        client_id, client_secret, project_id = required
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
                "environment": os.environ.get("INFISICAL_ENVIRONMENT", "dev"),
                "include_imports": "true",
                "secretPath": "/",
            },
            timeout=30,
        )
        secrets_resp.raise_for_status()
        _INFISICAL_SECRETS_CACHE = {
            str(item["secretKey"]): str(item.get("secretValue") or "")
            for item in secrets_resp.json().get("secrets", [])
            if item.get("secretKey")
        }
        print(f"[pid={os.getpid()}] Infisical: загружено {len(_INFISICAL_SECRETS_CACHE)} секретов")
    except Exception as exc:
        print(f"[pid={os.getpid()}] Infisical: не удалось загрузить секреты: {type(exc).__name__}: {exc}")
    return _INFISICAL_SECRETS_CACHE


def _config_secret(name: str, default: str = "") -> tuple:
    render_value = os.getenv(name, "").strip()
    force_render = os.getenv("BETTERSTACK_FORCE_RENDER_kj123664", "").strip().lower() in {"1", "true", "yes", "on"}
    if force_render and render_value:
        return render_value, "render_override"
    infisical_value = _read_infisical_secrets_once().get(name, "").strip()
    if infisical_value:
        return infisical_value, "infisical"
    if render_value:
        return render_value, "render_fallback"
    return default, "default"


# ---------------------------------------------------------------------------
# Better Stack
# ---------------------------------------------------------------------------
BETTERSTACK_URL, _BETTERSTACK_URL_SOURCE = _config_secret("BETTERSTACK_URL_kj123664")
BETTERSTACK_URL = BETTERSTACK_URL.rstrip("/")
BETTERSTACK_BEARER, _BETTERSTACK_BEARER_SOURCE = _config_secret("BETTERSTACK_BEARER_kj123664")
BETTERSTACK_SERVICE, _BETTERSTACK_SERVICE_SOURCE = _config_secret("BETTERSTACK_SERVICE_kj123664", "bls-ocr")
BETTERSTACK_SERVICE = BETTERSTACK_SERVICE.strip() or "bls-ocr"
BETTERSTACK_CONFIG_SOURCE = (
    "infisical"
    if "infisical" in {_BETTERSTACK_URL_SOURCE, _BETTERSTACK_BEARER_SOURCE, _BETTERSTACK_SERVICE_SOURCE}
    else _BETTERSTACK_URL_SOURCE
)
BETTERSTACK_ENABLED = bool(BETTERSTACK_URL and BETTERSTACK_BEARER)

# ---------------------------------------------------------------------------
# /status: логин и пароль берутся ТОЛЬКО из Render (не из Infisical).
# ---------------------------------------------------------------------------
STATUS_USER = os.getenv("STATUS_USER", "").strip()
STATUS_PASSWORD = os.getenv("STATUS_PASSWORD", "")

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
# Цепочка моделей по приоритету, через запятую. Если GEMINI_MODELS не задан, работает одна GEMINI_MODEL.
# Пример: GEMINI_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "").split(",") if m.strip()] or [GEMINI_MODEL]
# Ограничить модель определёнными ключами (по началу имени ключа), формат: "модель=префикс1|префикс2;модель2=префикс3".
# Нужно для "чёрного дня": gemini-2.5-flash закрыта для новых проектов и работает только на старом ключе.
# Пример: GEMINI_MODEL_KEYS=gemini-2.5-flash=550953
GEMINI_MODEL_KEYS: dict = {}
for _part in os.getenv("GEMINI_MODEL_KEYS", "").split(";"):
    if "=" in _part:
        _m, _prefixes = _part.split("=", 1)
        _plist = [x.strip() for x in _prefixes.split("|") if x.strip()]
        if _m.strip() and _plist:
            GEMINI_MODEL_KEYS[_m.strip()] = _plist
# На сколько секунд модель считается перегруженной после 503/500/504.
GEMINI_MODEL_COOLDOWN = float(os.getenv("GEMINI_MODEL_COOLDOWN", "30"))
GEMINI_ACQUIRE_POLL = 0.15

# ---------------------------------------------------------------------------
# Генерация HTML-писем (функция отключена по умолчанию)
# ---------------------------------------------------------------------------
def _template_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return max(minimum, value)
    except (TypeError, ValueError, OverflowError):
        return default


def _template_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return max(minimum, value) if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


TEMPLATE_ENABLED = os.getenv("TEMPLATE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
TEMPLATE_API_KEYS, _TEMPLATE_API_KEYS_SOURCE = _config_secret("TEMPLATE_API_KEYS")
TEMPLATE_API_KEYS = [item.strip() for item in TEMPLATE_API_KEYS.split(",") if item.strip()]
TEMPLATE_API_KEY_MIN_LENGTH = _template_int("TEMPLATE_API_KEY_MIN_LENGTH", 24, 1)
TEMPLATE_API_KEY_WARNINGS = [
    f"TEMPLATE_API_KEYS[{index + 1}] is shorter than {TEMPLATE_API_KEY_MIN_LENGTH} characters"
    for index, value in enumerate(TEMPLATE_API_KEYS)
    if len(value) < TEMPLATE_API_KEY_MIN_LENGTH
]
if TEMPLATE_API_KEY_WARNINGS:
    print(f"[pid={os.getpid()}] Template auth warning: {len(TEMPLATE_API_KEY_WARNINGS)} short client API key(s)")
TEMPLATE_MODELS = [item.strip() for item in os.getenv("TEMPLATE_MODELS", "").split(",") if item.strip()]
TEMPLATE_REPAIR_MODEL = os.getenv("TEMPLATE_REPAIR_MODEL", "").strip()
TEMPLATE_MAX_CONCURRENCY = _template_int("TEMPLATE_MAX_CONCURRENCY", 3, 1)
TEMPLATE_RESERVE_KEYS_FOR_OCR = _template_int("TEMPLATE_RESERVE_KEYS_FOR_OCR", 5, 0)
TEMPLATE_YIELD_TO_OCR = os.getenv("TEMPLATE_YIELD_TO_OCR", "1").strip().lower() not in {"0", "false", "no"}
TEMPLATE_MAX_ATTEMPTS = min(5, _template_int("TEMPLATE_MAX_ATTEMPTS", 3, 1))
TEMPLATE_INFRA_RETRIES = _template_int("TEMPLATE_INFRA_RETRIES", 4, 1)
TEMPLATE_JOB_DEADLINE_SECONDS = _template_float("TEMPLATE_JOB_DEADLINE_SECONDS", 420, 45)
TEMPLATE_QUEUE_MAX_WAIT_SECONDS = _template_float("TEMPLATE_QUEUE_MAX_WAIT_SECONDS", 300, 1)
TEMPLATE_MIN_ATTEMPT_SECONDS = _template_float("TEMPLATE_MIN_ATTEMPT_SECONDS", 90, 1)
TEMPLATE_DEADLINE_MARGIN = _template_float("TEMPLATE_DEADLINE_MARGIN", 5, 0)
TEMPLATE_MIN_CALL_SECONDS = _template_float("TEMPLATE_MIN_CALL_SECONDS", 45, 1)
TEMPLATE_RESULT_TTL_SECONDS = _template_int("TEMPLATE_RESULT_TTL_SECONDS", 3600, 60)
TEMPLATE_MAX_PENDING = _template_int("TEMPLATE_MAX_PENDING", 6, 1)
TEMPLATE_DAILY_CAP = _template_int("TEMPLATE_DAILY_CAP", 100, 1)
TEMPLATE_DAILY_TOKEN_CAP = _template_int("TEMPLATE_DAILY_TOKEN_CAP", 20000000, 1000)
TEMPLATE_BREAKER_FAILS = _template_int("TEMPLATE_BREAKER_FAILS", 5, 1)
TEMPLATE_BREAKER_PAUSE_SECONDS = _template_int("TEMPLATE_BREAKER_PAUSE_SECONDS", 120, 1)
TEMPLATE_MAX_HTML_BYTES = _template_int("TEMPLATE_MAX_HTML_BYTES", 300000, 1024)
TEMPLATE_HTML_ERROR_BYTES = _template_int("TEMPLATE_HTML_ERROR_BYTES", 98000, 1024)
TEMPLATE_MAX_OUTPUT_TOKENS = _template_int("TEMPLATE_MAX_OUTPUT_TOKENS", 16384, 1024)
TEMPLATE_TEMPERATURE = _template_float("TEMPLATE_TEMPERATURE", 1.0)
TEMPLATE_REPAIR_TEMPERATURE = _template_float("TEMPLATE_REPAIR_TEMPERATURE", 0.4)
TEMPLATE_THINKING_BUDGET = _template_int("TEMPLATE_THINKING_BUDGET", 0, -1)
TEMPLATE_THINKING_LEVEL = os.getenv("TEMPLATE_THINKING_LEVEL", "low").strip().lower()
if TEMPLATE_THINKING_LEVEL not in {"minimal", "low", "medium", "high"}:
    TEMPLATE_THINKING_LEVEL = "low"
TEMPLATE_HTTP_READ_TIMEOUT = _template_float("TEMPLATE_HTTP_READ_TIMEOUT", 130, 15)
TEMPLATE_CONNECT_TIMEOUT = _template_float("TEMPLATE_CONNECT_TIMEOUT", 8, 1)
TEMPLATE_HARD_TIMEOUT = _template_float("TEMPLATE_HARD_TIMEOUT", 150, 15)
TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT = _template_float("TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT", 45, 1)
TEMPLATE_STREAM_STALL_TIMEOUT = _template_float("TEMPLATE_STREAM_STALL_TIMEOUT", 30, 1)
# Ремонтный запрос тяжелее первого (≈9 тыс. входных токенов против ≈4,7 тыс.): модель с thinking
# отдаёт первый байт позже. Для «больших» запросов ждём первый байт дольше базового значения.
TEMPLATE_REPAIR_FIRST_BYTE_TIMEOUT = _template_float("TEMPLATE_REPAIR_FIRST_BYTE_TIMEOUT", 90, 1)
TEMPLATE_LARGE_REQUEST_CHARS = _template_int("TEMPLATE_LARGE_REQUEST_CHARS", 22000, 1000)
TEMPLATE_PROXY_PREFLIGHT_TIMEOUT = _template_float("TEMPLATE_PROXY_PREFLIGHT_TIMEOUT", 7, 1)
TEMPLATE_LEASE_TTL = max(TEMPLATE_HARD_TIMEOUT + 30, _template_float("TEMPLATE_LEASE_TTL", 180, 1))
TEMPLATE_MODEL_COOLDOWN = _template_float("TEMPLATE_MODEL_COOLDOWN", 60, 1)
# 503 «высокий спрос»: это перегрузка модели, а не поломка маршрута. Такие ответы не тратят
# TEMPLATE_INFRA_RETRIES (до TEMPLATE_OVERLOAD_RETRIES за вызов), между ними пауза с нарастанием.
TEMPLATE_OVERLOAD_RETRIES = _template_int("TEMPLATE_OVERLOAD_RETRIES", 6, 0)
TEMPLATE_OVERLOAD_BACKOFF = _template_float("TEMPLATE_OVERLOAD_BACKOFF", 4, 0)
TEMPLATE_OVERLOAD_BACKOFF_MAX = _template_float("TEMPLATE_OVERLOAD_BACKOFF_MAX", 15, 0)
TEMPLATE_SLOW_ROUTE_COOLDOWN = _template_float("TEMPLATE_SLOW_ROUTE_COOLDOWN", 600, 1)
TEMPLATE_QUOTA_BLOCK_SECONDS = _template_int("TEMPLATE_QUOTA_BLOCK_SECONDS", 900, 60)
TEMPLATE_CONTRAST_SUGGEST = os.getenv("TEMPLATE_CONTRAST_SUGGEST", "1").strip().lower() not in {"0", "false", "no"}
TEMPLATE_AUTOFIX_CONTRAST = os.getenv("TEMPLATE_AUTOFIX_CONTRAST", "1").strip().lower() in {"1", "true", "yes", "on"}
# Постпроцессор писем (метатеги, text-size-adjust, mso-table-*) и text/plain в ответе.
TEMPLATE_POSTPROCESS = os.getenv("TEMPLATE_POSTPROCESS", "1").strip().lower() not in {"0", "false", "no"}
# Строгий режим Outlook: T26 (таблица внутри строки с текстом) и P03 (тёплая заливка индикатора)
# становятся ошибками и запускают ремонт. По умолчанию это предупреждения.
TEMPLATE_STRICT_OUTLOOK = os.getenv("TEMPLATE_STRICT_OUTLOOK", "0").strip().lower() in {"1", "true", "yes", "on"}
# Отладка: хранить в памяти HTML каждой попытки (до 3 штук по 40 КБ, TTL как у результата) и отдавать через ?attempts_html=1.
TEMPLATE_KEEP_ATTEMPT_HTML = os.getenv("TEMPLATE_KEEP_ATTEMPT_HTML", "0").strip().lower() in {"1", "true", "yes", "on"}
TEMPLATE_HISTORY = _template_int("TEMPLATE_HISTORY", 12, 1)
TEMPLATE_STORE_MAX = _template_int("TEMPLATE_STORE_MAX", 100, 1)
TEMPLATE_VALIDATE_RATE_PER_MIN = _template_int("TEMPLATE_VALIDATE_RATE_PER_MIN", 30, 1)
TEMPLATE_SUBMIT_RATE_PER_MIN = _template_int("TEMPLATE_SUBMIT_RATE_PER_MIN", 20, 1)
TEMPLATE_DISABLED_RULES = {
    item.strip().upper() for item in os.getenv("TEMPLATE_DISABLED_RULES", "").split(",") if item.strip()
}

# ---------------------------------------------------------------------------
# Contact sheet
# ---------------------------------------------------------------------------
GEMINI_SHEET_MAX_IMAGES = int(os.getenv("GEMINI_SHEET_MAX_IMAGES", "100"))
GEMINI_SHEET_COLS = int(os.getenv("GEMINI_SHEET_COLS", "9"))
GEMINI_SHEET_CELL_WIDTH = int(os.getenv("GEMINI_SHEET_CELL_WIDTH", "170"))
GEMINI_SHEET_CELL_HEIGHT = int(os.getenv("GEMINI_SHEET_CELL_HEIGHT", "112"))

# Большие пачки бьются на чанки (54 = размер, проверенный локально) и гонятся параллельно.
GEMINI_SHEET_CHUNK_SIZE = int(os.getenv("GEMINI_SHEET_CHUNK_SIZE", "54"))

# Точечный повтор по одной нераспознанной ячейке.
GEMINI_VARIANT_RETRY_ENABLED = os.getenv("GEMINI_VARIANT_RETRY_ENABLED", "1") not in ("0", "false", "False")
GEMINI_VARIANT_RETRY_COLS = int(os.getenv("GEMINI_VARIANT_RETRY_COLS", "3"))
GEMINI_VARIANT_RETRY_MIN_VOTES = int(os.getenv("GEMINI_VARIANT_RETRY_MIN_VOTES", "2"))
GEMINI_VARIANT_RETRY_SCALE = int(os.getenv("GEMINI_VARIANT_RETRY_SCALE", "6"))
GEMINI_VARIANT_RETRY_BLUR_KSIZE = int(os.getenv("GEMINI_VARIANT_RETRY_BLUR_KSIZE", "9"))
