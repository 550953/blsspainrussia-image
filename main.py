import asyncio
import base64
import gc
import json
import math
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Union, Optional
from collections import Counter

import ddddocr
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import numpy as np
import cv2
import uvicorn

from google import genai
from google.genai import types

PROCESS_START_TIME = time.time()
STARTUP_TIMING_FILE = Path("/tmp/startup_timing.json")

app = FastAPI(title="Digit OCR Service", version="4.0")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)

OCR_MAX_WORKERS = int(os.getenv("OCR_MAX_WORKERS", "8"))
EXECUTOR = ThreadPoolExecutor(max_workers=OCR_MAX_WORKERS, thread_name_prefix="ocr-worker")


_RETRY_DELAY_RE = re.compile(r'retry[_\s\-]?delay[^0-9]*(\d+(?:\.\d+)?)', re.IGNORECASE)


def _parse_retry_delay(err_text: str) -> float:
    m = _RETRY_DELAY_RE.search(err_text)
    return float(m.group(1)) if m else 0.0


class GeminiKeyPool:
    _BLACKOUT_MULT = 1.0
    _BLACKOUT_MIN = 30.0
    _JITTER_MAX = 3.0

    def __init__(self):
        self.keys: List[str] = []
        self.key_names: List[str] = []
        self.clients: List[Optional[genai.Client]] = []
        self.blocked: List[float] = []
        self.reserved: List[bool] = []
        self.dead: List[bool] = []
        self.last_used: List[float] = []
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

        if not keys and os.environ.get("INFISICAL_CLIENT_ID") and os.environ.get("INFISICAL_CLIENT_SECRET"):
            try:
                keys, names = self._fetch_keys_from_infisical(prefix)
                if keys:
                    print(f"[pid={os.getpid()}] Gemini pool: ключи не найдены в os.environ, "
                          f"загружено {len(keys)} из Infisical (режим локальной разработки)")
            except Exception as e:
                print(f"[pid={os.getpid()}] Gemini pool: не удалось загрузить ключи из Infisical: {e}")

        self.keys = keys
        self.key_names = names
        self.clients = [None] * len(keys)
        self.blocked = [0.0] * len(keys)
        self.reserved = [False] * len(keys)
        self.dead = [False] * len(keys)
        self.last_used = [0.0] * len(keys)
        self.blackout_until = 0.0
        self.rr_index = 0
        print(f"[pid={os.getpid()}] Gemini pool: инициализирован, {len(keys)} ключ(ей)")

    @staticmethod
    def _fetch_keys_from_infisical(prefix: str) -> tuple[List[str], List[str]]:
        import requests as _requests

        client_id = os.environ["INFISICAL_CLIENT_ID"]
        client_secret = os.environ["INFISICAL_CLIENT_SECRET"]
        project_id = os.environ["INFISICAL_PROJECT_ID"]
        environment = os.environ.get("INFISICAL_ENVIRONMENT", "dev")

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
                "environment": environment,
                "include_imports": "true",
                "secretPath": "/",
            },
            timeout=30,
        )
        secrets_resp.raise_for_status()
        all_secrets = {s["secretKey"]: s["secretValue"] for s in secrets_resp.json().get("secrets", [])}

        seen = set()
        keys, names = [], []
        for name, value in sorted(all_secrets.items()):
            if name.startswith(prefix) and value:
                v = value.strip()
                if v and v not in seen:
                    seen.add(v)
                    keys.append(v)
                    names.append(name)
        return keys, names

    def _client(self, idx: int) -> genai.Client:
        if self.clients[idx] is None:
            self.clients[idx] = genai.Client(api_key=self.keys[idx])
        return self.clients[idx]

    def is_overloaded(self) -> tuple[bool, float]:
        now = time.monotonic()
        if now < self.blackout_until:
            return True, self.blackout_until - now
        return False, 0.0

    def acquire(self) -> tuple[Optional[int], Optional[str], Optional[genai.Client]]:
        if not self.keys:
            return None, None, None
        n = len(self.keys)
        now = time.monotonic()
        for _ in range(n):
            idx = self.rr_index % n
            self.rr_index += 1
            if (
                not self.reserved[idx]
                and now >= self.blocked[idx]
                and now - self.last_used[idx] >= GEMINI_MIN_INTERVAL_SEC
            ):
                self.reserved[idx] = True
                self.last_used[idx] = now
                return idx, self.key_names[idx], self._client(idx)
        return None, None, None

    def release(self, idx: int, error_text: Optional[str] = None) -> None:
        self.reserved[idx] = False
        if error_text is None:
            return

        key_name = self.key_names[idx] if idx < len(self.key_names) else f"key_{idx}"
        err_up = error_text.upper()

        if "429" in err_up or "RESOURCE_EXHAUSTED" in err_up or "QUOTA" in err_up:
            base = _parse_retry_delay(error_text)
            cooldown = max(self._BLACKOUT_MIN, base * self._BLACKOUT_MULT) + random.uniform(0, self._JITTER_MAX)
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → 429, cooldown {cooldown:.0f} сек")
        elif "401" in err_up or "403" in err_up or "UNAUTHENTICATED" in err_up or "API_KEY_INVALID" in err_up or "PERMISSION_DENIED" in err_up:
            cooldown = 3600.0
            self.dead[idx] = True
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → ошибка авторизации (401/403), похоже ключ мёртв, cooldown 1ч")
        elif "503" in err_up or "UNAVAILABLE" in err_up:
            cooldown = 10.0 + random.uniform(0, self._JITTER_MAX)
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → 503 (модель перегружена), cooldown {cooldown:.0f} сек")
        else:
            cooldown = 15.0
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → прочая ошибка, cooldown 15 сек: {error_text[:200]}")

        self.blocked[idx] = time.monotonic() + cooldown
        now = time.monotonic()

        live_idx = [i for i in range(len(self.keys)) if not self.dead[i]]
        check_idx = live_idx if live_idx else list(range(len(self.keys)))

        if all(now < self.blocked[i] for i in check_idx):
            soonest = min(self.blocked[i] for i in check_idx)
            if soonest > self.blackout_until:
                self.blackout_until = soonest
                wait = soonest - now
                label = "живые " if live_idx else ""
                print(f"[pid={os.getpid()}][gemini_key] ВСЕ {label}ключи заняты → blackout {wait:.0f} сек (ближайшее освобождение)")


gemini_pool = GeminiKeyPool()
gemini_pool.init_from_env()

GEMINI_KEYS = gemini_pool.keys

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
gemini_model_name = GEMINI_MODEL if GEMINI_KEYS else None
print(f"[pid={os.getpid()}] Gemini ключей найдено: {len(GEMINI_KEYS)}, модель: {gemini_model_name}")

GEMINI_MIN_INTERVAL_SEC = float(os.getenv("GEMINI_MIN_INTERVAL_SEC", "8.0"))
GEMINI_ACQUIRE_TIMEOUT = float(os.getenv("GEMINI_ACQUIRE_TIMEOUT", "40.0"))
GEMINI_ACQUIRE_POLL = 0.15

GEMINI_CONCURRENT_LIMIT = int(os.getenv("GEMINI_CONCURRENT_LIMIT", "5"))
_gemini_network_semaphore = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)


class OCRRequest(BaseModel):
    images: Union[str, List[str]] = Field(..., description="Один base64 или список")


class OCRResultItem(BaseModel):
    text: str
    source: str


class OCRResponse(BaseModel):
    results: List[OCRResultItem]


def clean_base64(b64: str) -> bytes:
    if b64.startswith("data:image"):
        b64 = b64.split(",", 1)[1]
    try:
        return base64.b64decode(b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")


def make_variants_fast(img_bgr: np.ndarray, scale: int = 4) -> list[np.ndarray]:
    h, w = img_bgr.shape[:2]
    img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_LINEAR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    variants = []

    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th)
    variants.append(255 - th)

    th_a = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
    )
    variants.append(th_a)
    variants.append(255 - th_a)

    clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, th_c = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_c)
    variants.append(255 - th_c)

    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    _, a, _ = cv2.split(lab)
    _, th_a_ch = cv2.threshold(a, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_a_ch)
    variants.append(255 - th_a_ch)

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    _, s_ch, v_ch = cv2.split(hsv)
    _, th_s_ch = cv2.threshold(s_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_s_ch)
    variants.append(255 - th_s_ch)

    _, th_v = cv2.threshold(v_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_v)
    variants.append(255 - th_v)

    return variants


def make_variants_full(img_bgr: np.ndarray) -> list[np.ndarray]:
    h, w = img_bgr.shape[:2]
    variants = []

    for scale in (5, 7):
        img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th)
        variants.append(255 - th)

        th_a = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
        )
        variants.append(th_a)
        variants.append(255 - th_a)

        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        _, th_c = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_c)
        variants.append(255 - th_c)

        blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
        sharp = cv2.addWeighted(gray, 2.2, blur, -1.2, 0)
        _, th_s = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_s)
        variants.append(255 - th_s)

        kernel = np.ones((2, 2), np.uint8)
        variants.append(cv2.dilate(th_s, kernel, iterations=1))
        variants.append(cv2.dilate(th_s, kernel, iterations=2))

        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)

        _, th_a_ch = cv2.threshold(a, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_a_ch)
        variants.append(255 - th_a_ch)

        color1 = cv2.addWeighted(l, 0.4, a, 0.6, 0)
        _, th_col1 = cv2.threshold(color1, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_col1)
        variants.append(255 - th_col1)

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        _, s_ch, v_ch = cv2.split(hsv)

        _, th_s_ch = cv2.threshold(s_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_s_ch)
        variants.append(255 - th_s_ch)

        _, th_v = cv2.threshold(v_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_v)
        variants.append(255 - th_v)

    return variants


def _score(item):
    text, freq = item
    bonus = 120 if len(text) == 3 else (25 if len(text) == 2 else 0)
    return (freq + bonus, len(text))


def recognize_dddd(image_bytes: bytes) -> str:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            res = ocr_dddd.classification(image_bytes)
            return "".join(c for c in res if c.isdigit()) or "0"

        candidates = []

        res0 = ocr_dddd.classification(image_bytes)
        dig0 = "".join(c for c in res0 if c.isdigit())
        if dig0:
            candidates.append(dig0)

        for var in make_variants_fast(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        cnt = Counter(c for c in candidates if len(c) == 3)
        if cnt:
            top_text, top_freq = cnt.most_common(1)[0]
            if top_freq >= 3:
                del img, nparr
                return top_text

        for var in make_variants_full(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        del img, nparr

        if not candidates:
            return "0"

        cnt_full = Counter(candidates)
        return sorted(cnt_full.items(), key=_score, reverse=True)[0][0]

    except Exception as e:
        print(f"[pid={os.getpid()}][dddd] Ошибка распознавания: {e}")
        return "0"


async def recognize_with_gemini_async(image_bytes: bytes) -> str:
    if not GEMINI_KEYS or gemini_model_name is None:
        return "0"

    overloaded, _ = gemini_pool.is_overloaded()
    if overloaded:
        return "0"

    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return "0"
    success, buf = cv2.imencode(".png", img)
    if not success:
        return "0"
    png_bytes = buf.tobytes()

    prompt = (
        "На изображении написано трёхзначное число. "
        "Распознай только это число. "
        "Ответь строго только цифрами, ничего больше. "
        "Пример правильного ответа: 678 или 700."
    )
    config = types.GenerateContentConfig(temperature=0.0, max_output_tokens=10)

    n = len(GEMINI_KEYS)
    tried_idx: set[int] = set()
    last_error: Optional[str] = None
    deadline = time.monotonic() + GEMINI_ACQUIRE_TIMEOUT

    while len(tried_idx) < n:
        idx, key_name, client = gemini_pool.acquire()

        if idx is None:
            overloaded, _ = gemini_pool.is_overloaded()
            if overloaded or time.monotonic() > deadline:
                break
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue

        if idx in tried_idx:
            gemini_pool.release(idx)
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue

        tried_idx.add(idx)
        error_text: Optional[str] = None
        try:
            async with _gemini_network_semaphore:
                response = await client.aio.models.generate_content(
                    model=gemini_model_name,
                    contents=[prompt, types.Part.from_bytes(data=png_bytes, mime_type="image/png")],
                    config=config,
                )
            text = (response.text or "").strip()
            digits = "".join(c for c in text if c.isdigit())
            if digits:
                print(f"[pid={os.getpid()}][gemini_key] OK  key={key_name}")
                return digits
        except Exception as e:
            error_text = str(e)
            last_error = error_text
        finally:
            gemini_pool.release(idx, error_text)

    if last_error:
        print(f"Gemini error (перебор завершён, попыток={len(tried_idx)}): {last_error}")
    return "0"


def recognize_one_sync_part(image_bytes: bytes) -> str:
    return recognize_dddd(image_bytes)


async def recognize_one_async(image_bytes: bytes, mode: str = "combo") -> dict:
    mode = mode.lower().strip()
    loop = asyncio.get_event_loop()

    if mode == "gemini":
        if not GEMINI_KEYS:
            return {"text": "0", "source": "gemini_unavailable"}
        result = await recognize_with_gemini_async(image_bytes)
        if result == "0":
            fallback = await loop.run_in_executor(EXECUTOR, recognize_one_sync_part, image_bytes)
            if fallback and fallback != "0":
                return {"text": fallback, "source": "ddddocr_fallback"}
        return {"text": result, "source": "gemini"}

    if mode == "dddd":
        result = await loop.run_in_executor(EXECUTOR, recognize_one_sync_part, image_bytes)
        return {"text": result, "source": "ddddocr"}

    dddd_result = await loop.run_in_executor(EXECUTOR, recognize_one_sync_part, image_bytes)

    if len(dddd_result) < 3 and GEMINI_KEYS:
        gemini_result = await recognize_with_gemini_async(image_bytes)
        if len(gemini_result) >= 3:
            return {"text": gemini_result, "source": "gemini"}
        if len(gemini_result) > len(dddd_result):
            return {"text": gemini_result, "source": "gemini"}

    return {"text": dddd_result, "source": "ddddocr"}


OCR_CONCURRENCY = int(os.getenv("OCR_CONCURRENCY", str(max(6, len(GEMINI_KEYS), OCR_MAX_WORKERS))))


async def process_images_concurrently(images_b64: List[str], mode: str) -> List[dict]:
    semaphore = asyncio.Semaphore(OCR_CONCURRENCY)

    async def _one(b64: str) -> dict:
        async with semaphore:
            return await recognize_one_async(clean_base64(b64), mode=mode)

    results = await asyncio.gather(*[_one(b64) for b64 in images_b64])
    gc.collect()
    return results


@app.post("/ocr", response_model=OCRResponse)
async def ocr_endpoint(
    req: OCRRequest,
    mode: Optional[str] = Query("combo", description="combo | gemini | dddd")
):
    images = req.images if isinstance(req.images, list) else [req.images]

    if len(images) > 200:
        raise HTTPException(status_code=400, detail="Слишком много изображений за один запрос (лимит 200)")

    results = await process_images_concurrently(images, mode)
    return OCRResponse(results=results)


@app.post("/ocr/batch", response_model=OCRResponse)
async def ocr_batch_endpoint(
    req: OCRRequest,
    mode: Optional[str] = Query("combo", description="combo | gemini | dddd")
):
    images = req.images if isinstance(req.images, list) else [req.images]

    if not images:
        raise HTTPException(status_code=400, detail="Пустой список изображений")
    if len(images) > 500:
        raise HTTPException(status_code=400, detail="Слишком много изображений за один запрос (лимит 500)")

    results = await process_images_concurrently(images, mode)
    return OCRResponse(results=results)


@app.get("/health")
async def health():
    startup_info = None
    if STARTUP_TIMING_FILE.exists():
        try:
            startup_info = json.loads(STARTUP_TIMING_FILE.read_text())
        except Exception:
            startup_info = None

    overloaded, remaining = gemini_pool.is_overloaded()
    now = time.monotonic()

    return {
        "status": "ok",
        "pid": os.getpid(),
        "gemini_keys_count": len(GEMINI_KEYS),
        "gemini_key_names": gemini_pool.key_names,
        "gemini_model": gemini_model_name,
        "gemini_pool_overloaded": overloaded,
        "gemini_pool_overloaded_seconds_left": round(remaining, 1) if overloaded else 0,
        "gemini_concurrent_limit": GEMINI_CONCURRENT_LIMIT,
        "gemini_keys_on_cooldown": [
            gemini_pool.key_names[i]
            for i, t in enumerate(gemini_pool.blocked)
            if now < t
        ],
        "gemini_keys_in_use": [
            gemini_pool.key_names[i]
            for i, busy in enumerate(gemini_pool.reserved)
            if busy
        ],
        "gemini_keys_dead": [
            gemini_pool.key_names[i]
            for i, dead in enumerate(gemini_pool.dead)
            if dead
        ],
        "ocr_concurrency": OCR_CONCURRENCY,
        "ocr_max_workers": OCR_MAX_WORKERS,
        "startup_timing": startup_info,
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = Path(__file__).parent / "favicon.ico"
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(favicon_path)


@app.on_event("startup")
async def on_startup():
    elapsed = time.time() - PROCESS_START_TIME
    payload = {
        "process_start_time": PROCESS_START_TIME,
        "ready_time": time.time(),
        "elapsed_seconds": round(elapsed, 3),
    }
    print(f"[startup_timing] Приложение готово через {elapsed:.3f} сек после старта процесса")

    try:
        STARTUP_TIMING_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[startup_timing] Не удалось записать файл замера: {e}")


@app.on_event("shutdown")
async def on_shutdown():
    EXECUTOR.shutdown(wait=False)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
