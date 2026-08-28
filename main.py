import base64
import gc
import itertools
import json
import os
import threading
import time
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
import google.generativeai as genai

# ---------------------------------------------------------------------------
# Замер времени старта: фиксируем момент, когда процесс начал загружаться
# (импорт модуля — самое раннее, что можно поймать изнутри Python).
# После того как FastAPI полностью поднимется (startup event), посчитаем
# сколько прошло секунд и запишем в лог + локальный файл.
# ---------------------------------------------------------------------------
PROCESS_START_TIME = time.time()
STARTUP_TIMING_FILE = Path("/tmp/startup_timing.json")

app = FastAPI(title="Digit OCR Service", version="3.0")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)


# ---------------------------------------------------------------------------
# Ротация Gemini API ключей.
# Ключи задаются переменными окружения с общим префиксом GEMINI_API_KEY,
# например: GEMINI_API_KEY_skukolka0, GEMINI_API_KEY_skukolka01, ...,
# GEMINI_API_KEY_ittlefairybox — сколько угодно штук, любые суффиксы.
# Каждый вызов Gemini берёт следующий ключ по кругу (round-robin).
# Если запрос с текущим ключом падает (квота/ошибка) — автоматически
# пробуем следующий ключ, пока не переберём все или не получим успех.
# ---------------------------------------------------------------------------
def _collect_gemini_keys() -> List[str]:
    keys = []
    for name, value in sorted(os.environ.items()):
        if name.startswith("GEMINI_API_KEY") and value:
            keys.append(value)
    return keys


GEMINI_KEYS = _collect_gemini_keys()
_gemini_key_cycle = itertools.cycle(GEMINI_KEYS) if GEMINI_KEYS else None
_gemini_lock = threading.Lock()  # genai.configure — глобальное состояние, сериализуем доступ

GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]

gemini_model_name = None


def _pick_working_model_name() -> Optional[str]:
    """Проверяем один раз при старте, какая модель из списка вообще доступна."""
    if not GEMINI_KEYS:
        return None
    try:
        genai.configure(api_key=GEMINI_KEYS[0])
        for model_name in GEMINI_MODELS:
            try:
                genai.GenerativeModel(model_name)
                print(f"Gemini модель выбрана: {model_name}")
                return model_name
            except Exception as e:
                print(f"Модель {model_name} недоступна: {e}")
                continue
    except Exception as e:
        print(f"Gemini не удалось инициализировать: {e}")
    return None


if GEMINI_KEYS:
    gemini_model_name = _pick_working_model_name()
    print(f"Gemini ключей найдено: {len(GEMINI_KEYS)}")
else:
    print("Gemini ключи не найдены (переменные GEMINI_API_KEY* отсутствуют)")


class OCRRequest(BaseModel):
    images: Union[str, List[str]] = Field(..., description="Один base64 или список (рекомендуется 1)")


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
# TIER 1: быстрый набор вариантов (один масштаб, LINEAR-ресайз).
# Покрывает все ключевые цветовые каналы, обычно этого достаточно, чтобы
# уверенно распознать 3-значное число на цветном фоне.
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


# ---------------------------------------------------------------------------
# TIER 2: полный (тяжёлый) набор вариантов — фоллбэк, если Tier 1 не дал
# уверенного консенсуса. Исходная логика без изменений.
# ---------------------------------------------------------------------------
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


def _next_gemini_key() -> Optional[str]:
    if _gemini_key_cycle is None:
        return None
    with _gemini_lock:
        return next(_gemini_key_cycle)


def recognize_with_gemini(image_bytes: bytes) -> str:
    if not GEMINI_KEYS or gemini_model_name is None:
        return "0"

    try:
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

        # Пробуем ключи по кругу: если текущий упал (квота/ошибка) —
        # переходим к следующему, максимум по разу на каждый доступный ключ.
        attempts = len(GEMINI_KEYS)
        last_error = None

        for _ in range(attempts):
            key = _next_gemini_key()
            try:
                with _gemini_lock:
                    genai.configure(api_key=key)
                    model = genai.GenerativeModel(gemini_model_name)
                    response = model.generate_content(
                        [
                            prompt,
                            {"mime_type": "image/png", "data": png_bytes},
                        ],
                        generation_config={
                            "temperature": 0.0,
                            "max_output_tokens": 10,
                        },
                    )

                text = response.text.strip()
                digits = "".join(c for c in text if c.isdigit())
                if digits:
                    return digits
            except Exception as e:
                last_error = e
                print(f"Gemini key failed, trying next: {e}")
                continue

        if last_error:
            print(f"Gemini error (all keys exhausted): {last_error}")
        return "0"

    except Exception as e:
        print(f"Gemini error: {e}")
        return "0"


def _score(item):
    text, freq = item
    bonus = 120 if len(text) == 3 else (25 if len(text) == 2 else 0)
    return (freq + bonus, len(text))


def recognize_dddd(image_bytes: bytes) -> str:
    """
    Двухуровневое распознавание:
      Tier 1 — быстрый набор из ~14 вариантов (1 масштаб, LINEAR-ресайз).
               Если один и тот же 3-значный ответ набрал consensus >= 3 —
               сразу возвращаем его, не считая ничего больше.
      Tier 2 — фоллбэк на полный (тяжёлый) перебор, если Tier 1 не дал
               уверенного результата. Срабатывает редко, поэтому не бьёт
               по средней скорости, но сохраняет качество на трудных
               картинках.
    """
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

        # ---------- TIER 1 ----------
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

        # ---------- TIER 2 (фоллбэк, только если Tier 1 не уверен) ----------
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


def recognize_one(image_bytes: bytes, mode: str = "combo") -> dict:
    mode = mode.lower().strip()

    # Только Gemini
    if mode == "gemini":
        if not GEMINI_KEYS:
            return {"text": "0", "source": "gemini_unavailable"}
        result = recognize_with_gemini(image_bytes)
        return {"text": result, "source": "gemini"}

    # Только ddddocr
    if mode == "dddd":
        result = recognize_dddd(image_bytes)
        return {"text": result, "source": "ddddocr"}

    # Комбо (по умолчанию)
    dddd_result = recognize_dddd(image_bytes)

    if len(dddd_result) < 3 and GEMINI_KEYS:
        gemini_result = recognize_with_gemini(image_bytes)
        if len(gemini_result) >= 3:
            return {"text": gemini_result, "source": "gemini"}
        if len(gemini_result) > len(dddd_result):
            return {"text": gemini_result, "source": "gemini"}

    return {"text": dddd_result, "source": "ddddocr"}


@app.post("/ocr", response_model=OCRResponse)
async def ocr_endpoint(
    req: OCRRequest,
    mode: Optional[str] = Query("combo", description="combo | gemini | dddd")
):
    images = req.images if isinstance(req.images, list) else [req.images]

    if len(images) > 3:
        raise HTTPException(status_code=400, detail="Максимум 3 изображения за один запрос")

    results = []
    for b64 in images:
        result = recognize_one(clean_base64(b64), mode=mode)
        results.append(result)

    gc.collect()

    return OCRResponse(results=results)


@app.get("/health")
async def health():
    startup_info = None
    if STARTUP_TIMING_FILE.exists():
        try:
            startup_info = json.loads(STARTUP_TIMING_FILE.read_text())
        except Exception:
            startup_info = None

    return {
        "status": "ok",
        "gemini_keys_count": len(GEMINI_KEYS),
        "gemini_model": gemini_model_name,
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
    """
    Разовый замер: сколько секунд прошло с момента запуска процесса
    (импорта модуля) до момента, когда FastAPI полностью готов принимать
    запросы. Пишем и в лог (видно в Render Logs сразу после деплоя),
    и в локальный файл — можно потом прочитать через GET /health.
    """
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


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
