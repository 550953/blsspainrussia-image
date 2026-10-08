"""Настройки процесса и секреты.

Секреты: Infisical (основной источник) -> переменные Render (запасной).
BETTERSTACK_FORCE_RENDER_kj123664=true: значение Render главнее Infisical.

Этот модуль ни от чего в пакете не зависит, его можно импортировать откуда угодно.
"""
import os
import time
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
# На сколько секунд модель считается перегруженной после 503/500/504.
GEMINI_MODEL_COOLDOWN = float(os.getenv("GEMINI_MODEL_COOLDOWN", "30"))
GEMINI_ACQUIRE_POLL = 0.15

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
