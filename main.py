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

# --- ИЗМЕНЕНИЕ 1: google-genai вместо google.generativeai --------------------
# Старый google.generativeai держит текущий API-ключ в ГЛОБАЛЬНОМ состоянии
# модуля (genai.configure()). Это и есть причина проблем 2 и 3:
#   - параллельные запросы физически невозможны без гонки ключей, поэтому
#     вызов был обёрнут в один общий Lock -> вся Gemini-обработка де-факто
#     последовательна, сколько бы ключей ни было в пуле;
#   - т.к. запросы шли быстро друг за другом без паузы, весь пул из N ключей
#     упирался в rate limit (RPM) за первые секунды, и все уходили в cooldown
#     почти синхронно.
# google-genai (пакет "google-genai", импорт "from google import genai")
# хранит ключ в экземпляре genai.Client(api_key=...) — ключ живёт в объекте,
# не в модуле. Это даёт: (а) настоящую параллельность без блокировок,
# (б) нативный async через client.aio.models.generate_content — не нужен
# ThreadPoolExecutor для самого сетевого вызова.
# pip install google-genai  (google-generativeai можно оставить закоммитированным
# в requirements, но лучше убрать во избежание путаницы двух SDK).
from google import genai
from google.genai import types

PROCESS_START_TIME = time.time()
STARTUP_TIMING_FILE = Path("/tmp/startup_timing.json")

app = FastAPI(title="Digit OCR Service", version="4.0")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)

# --- ИЗМЕНЕНИЕ 2: пул потоков для CPU-bound работы ---------------------------
# ddddocr.classification() и cv2-препроцессинг — синхронные блокирующие вызовы.
# Чтобы не блокировать event loop и не сериализовать обработку картинок,
# выполняем их в отдельных потоках через run_in_executor. Размер пула — не
# про Gemini-ключи (там своя параллельность через клиенты), а про то, сколько
# картинок одновременно можно грузить на CPU (ocr_dddd основан на onnxruntime,
# сессии потокобезопасны для одновременных Run()).
OCR_MAX_WORKERS = int(os.getenv("OCR_MAX_WORKERS", "8"))
EXECUTOR = ThreadPoolExecutor(max_workers=OCR_MAX_WORKERS, thread_name_prefix="ocr-worker")


# ---------------------------------------------------------------------------
# Пул Gemini-ключей: round-robin + per-key cooldown + глобальный blackout —
# логика 1:1 перенесена из vk-pet-care-assistant-zoo-mentor (key_pool.py /
# llm.py). Отличие от той реализации: там честно стоит asyncio.Semaphore(1)
# ("ключи никогда не бьются параллельно" — сознательный выбор для чат-бота
# с одним диалогом за раз). Здесь вместо глобального семафора — флаг
# `reserved` НА КАЖДЫЙ КЛЮЧ: пока ключ используется одной задачей, другая
# параллельная задача его не возьмёт (round-robin пропустит), но КЛЮЧИ
# работают параллельно друг с другом. Это и даёт настоящее ускорение на
# 54 картинках при сохранении корректности (один ключ = один запрос
# одновременно, гонок нет).
# ---------------------------------------------------------------------------
_RETRY_DELAY_RE = re.compile(r'retry[_\s\-]?delay[^0-9]*(\d+(?:\.\d+)?)', re.IGNORECASE)


def _parse_retry_delay(err_text: str) -> float:
    m = _RETRY_DELAY_RE.search(err_text)
    return float(m.group(1)) if m else 0.0


class GeminiKeyPool:
    _BLACKOUT_MULT = 1.0
    _BLACKOUT_MIN = 30.0  # минимум cooldown при 429, если retry_delay не найден
    _JITTER_MAX = 3.0     # небольшой разброс, чтобы ключи не освобождались все разом

    def __init__(self):
        self.keys: List[str] = []
        self.key_names: List[str] = []
        self.clients: List[Optional[genai.Client]] = []  # lazy-init, см. _client()
        self.blocked: List[float] = []   # monotonic-время, до которого ключ на cooldown
        self.reserved: List[bool] = []   # НОВОЕ: ключ сейчас используется одной задачей
        self.dead: List[bool] = []       # ключ получил 401/403 — считаем нерабочим навсегда
        self.last_used: List[float] = []  # НОВОЕ: monotonic-время последней ВЫДАЧИ ключа —
        # используется для упреждающего пейсинга в acquire(), а не только реактивного
        # cooldown после ошибки. Наблюдение из продакшн-логов: у бесплатного тира
        # квота настолько мала, что ключ ловит 429 уже на ВТОРОМ подряд запросе —
        # ждать реальной ошибки слишком поздно, разумнее не давать ключ повторно
        # раньше GEMINI_MIN_INTERVAL_SEC после предыдущей выдачи.
        self.blackout_until: float = 0.0
        self.rr_index: int = 0
        # Важно: acquire()/release() вызываются только из корутин, которые
        # выполняются в event loop (не из ThreadPoolExecutor-потоков), а
        # значит между проверкой и установкой reserved нет await —
        # asyncio кооперативен, поэтому доп. блокировка (Lock) не нужна.

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
        """Резервирует и возвращает следующий свободный ключ по кругу.
        Свободный = не в cooldown, не занят другой задачей И не использовался
        последние GEMINI_MIN_INTERVAL_SEC секунд (упреждающий пейсинг —
        не дожидаемся 429, чтобы понять, что квота ключа мала).
        (None, None, None), если свободных сейчас нет — вызывающий код должен
        подождать и повторить."""
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
        """Освобождает ключ. Если была ошибка — выставляет cooldown по типу
        ошибки (та же классификация, что в llm.py: 429 / 401-403 / прочее)."""
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
            self.dead[idx] = True  # ВАЖНО: помечаем отдельно, см. ниже — не даём этому
            # ключу влиять на расчёт общего blackout наравне с временно занятыми.
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → ошибка авторизации (401/403), похоже ключ мёртв, cooldown 1ч")
        elif "503" in err_up or "UNAVAILABLE" in err_up:
            # Модель перегружена НА СТОРОНЕ GOOGLE — это не проблема конкретного
            # ключа, поэтому cooldown короче, чем для прочих ошибок: ключ скорее
            # всего рабочий, просто сейчас не повезло с моделью/таймингом.
            cooldown = 10.0 + random.uniform(0, self._JITTER_MAX)
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → 503 (модель перегружена), cooldown {cooldown:.0f} сек")
        else:
            cooldown = 15.0
            print(f"[pid={os.getpid()}][gemini_key] {key_name} → прочая ошибка, cooldown 15 сек: {error_text[:200]}")

        self.blocked[idx] = time.monotonic() + cooldown
        now = time.monotonic()

        # --- ИСПРАВЛЕНО: раньше здесь стоял max(self.blocked) — это брало
        # САМЫЙ ДОЛГИЙ cooldown среди ВСЕХ ключей (включая мёртвые с 1-часовым
        # cooldown) и делало blackout на час, хотя живые ключи освобождались
        # уже через 15-30 сек. Правильно — min(): момент, когда освободится
        # БЛИЖАЙШИЙ ключ. Дополнительно исключаем заведомо мёртвые (401/403)
        # ключи из этого расчёта — они не должны участвовать ни в "все ли
        # заняты", ни тем более задавать длительность blackout.
        live_idx = [i for i in range(len(self.keys)) if not self.dead[i]]
        check_idx = live_idx if live_idx else list(range(len(self.keys)))  # если все мертвы — уже без разницы

        if all(now < self.blocked[i] for i in check_idx):
            soonest = min(self.blocked[i] for i in check_idx)
            if soonest > self.blackout_until:
                self.blackout_until = soonest
                wait = soonest - now
                label = "живые " if live_idx else ""
                print(f"[pid={os.getpid()}][gemini_key] ВСЕ {label}ключи заняты → blackout {wait:.0f} сек (ближайшее освобождение)")


gemini_pool = GeminiKeyPool()
gemini_pool.init_from_env()

GEMINI_KEYS = gemini_pool.keys  # для обратной совместимости (проверки "if GEMINI_KEYS")

# Модель — фиксированная константа, как в проверенной реализации (llm.py),
# а не "проверка какая модель доступна" при старте: в старом коде эта
# проверка всё равно ничего не стоила (GenerativeModel() — локальный
# конструктор без сетевого вызова), а в новом SDK бесплатной проверки без
# реального запроса нет. Если понадобится сменить модель — через env.
#
# ВАЖНО: "gemini-flash-latest" по вашим логам массово отдаёт 503 UNAVAILABLE
# ("high demand") — это перегрузка модели на стороне Google, не проблема
# ключей. Ваш же диагностический скрипт (key_diag.py) подтвердил, что
# "gemini-3.1-flash-lite" отвечает без единой ошибки на 15 из 18 ключей —
# переключаю дефолт на неё. Смените через переменную окружения GEMINI_MODEL,
# если понадобится другая.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
gemini_model_name = GEMINI_MODEL if GEMINI_KEYS else None
print(f"[pid={os.getpid()}] Gemini ключей найдено: {len(GEMINI_KEYS)}, модель: {gemini_model_name}")

# --- ИЗМЕНЕНИЕ: упреждающий пейсинг ключа + больше терпения при ожидании ---
# По логам: свободный тир текущей модели даёт квоту ~1 запрос за GEMINI_MIN_INTERVAL_SEC
# секунд НА КЛЮЧ — второй подряд запрос почти гарантированно ловит 429 с
# retry_delay ~30 сек. GEMINI_MIN_INTERVAL_SEC не даёт ключу уйти в реальный 429
# заранее. GEMINI_ACQUIRE_TIMEOUT увеличен с 8 до 40 сек — раньше задачи сдавались
# в ddddocr fallback быстрее, чем успевал освободиться хоть один ключ (30+ сек).
GEMINI_MIN_INTERVAL_SEC = float(os.getenv("GEMINI_MIN_INTERVAL_SEC", "21.0"))
GEMINI_ACQUIRE_TIMEOUT = float(os.getenv("GEMINI_ACQUIRE_TIMEOUT", "40.0"))
GEMINI_ACQUIRE_POLL = 0.15


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


# ---------------------------------------------------------------------------
# TIER 1 / TIER 2 препроцессинг и recognize_dddd — БЕЗ ИЗМЕНЕНИЙ (исходная
# логика сохранена полностью, только вызываются теперь через executor).
# ---------------------------------------------------------------------------
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
    """Синхронная, CPU-bound. Вызывается через EXECUTOR.submit / run_in_executor."""
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
        return f"ERROR:{str(e)}"


# ---------------------------------------------------------------------------
# ИЗМЕНЕНИЕ 4: recognize_with_gemini теперь async и использует
# client.aio.models.generate_content — нативный неблокирующий вызов SDK,
# без ThreadPoolExecutor. Параллельность обеспечивается тем, что несколько
# таких корутин могут одновременно ждать сетевой ответ на РАЗНЫХ ключах.
# ---------------------------------------------------------------------------
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
            # Все ключи либо заняты другой задачей, либо на cooldown.
            overloaded, _ = gemini_pool.is_overloaded()
            if overloaded or time.monotonic() > deadline:
                break
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue

        if idx in tried_idx:
            # Уже пробовали этот ключ в рамках текущей картинки — вернули его
            # в оборот раньше времени, отпускаем и просим следующий круг.
            gemini_pool.release(idx)
            await asyncio.sleep(GEMINI_ACQUIRE_POLL)
            continue

        tried_idx.add(idx)
        error_text: Optional[str] = None
        try:
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
    """Обёртка вокруг recognize_dddd для единообразного вызова через executor."""
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

    # combo (по умолчанию) — та же логика, что была в исходном recognize_one
    dddd_result = await loop.run_in_executor(EXECUTOR, recognize_one_sync_part, image_bytes)

    if len(dddd_result) < 3 and GEMINI_KEYS:
        gemini_result = await recognize_with_gemini_async(image_bytes)
        if len(gemini_result) >= 3:
            return {"text": gemini_result, "source": "gemini"}
        if len(gemini_result) > len(dddd_result):
            return {"text": gemini_result, "source": "gemini"}

    return {"text": dddd_result, "source": "ddddocr"}


# --- ИЗМЕНЕНИЕ 5: общий помощник параллельной batch-обработки ---------------
# OCR_CONCURRENCY — сколько картинок обрабатывается одновременно суммарно
# (и Gemini, и ddddocr-фоллбэки). Не привязан к числу ключей напрямую:
# задачи сверх числа свободных ключей просто дождутся своего в acquire().
# Разумный старт — среднее между "числом ключей" и "числом воркеров CPU".
OCR_CONCURRENCY = int(os.getenv("OCR_CONCURRENCY", str(max(6, len(GEMINI_KEYS) * 2, OCR_MAX_WORKERS))))


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
    """Старый лимит в 3 картинки снят: он был нужен, только пока обработка
    была строго последовательной с общим Gemini-локом. Теперь и одиночные,
    и множественные запросы идут через один и тот же параллельный пайплайн.
    Для действительно больших пачек используйте /ocr/batch — эндпоинты
    идентичны по сути, /ocr/batch просто явное имя без implicit-лимита."""
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
    """Массовая обработка: список base64-картинок, параллельно, с бережным
    использованием пула Gemini-ключей (round-robin + резервирование +
    per-key cooldown) и CPU-пула для ddddocr-фоллбэка."""
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
