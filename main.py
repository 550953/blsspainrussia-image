import base64
import gc
import os
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

app = FastAPI(title="Digit OCR Service", version="2.7")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_model = None
gemini_model_name = None

GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)

        for model_name in GEMINI_MODELS:
            try:
                test_model = genai.GenerativeModel(model_name)
                gemini_model = test_model
                gemini_model_name = model_name
                print(f"Gemini подключен: {model_name}")
                break
            except Exception as e:
                print(f"Модель {model_name} недоступна: {e}")
                continue

        if gemini_model is None:
            print("Ни одна из указанных моделей Gemini не доступна")

    except Exception as e:
        print(f"Gemini не удалось инициализировать: {e}")


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
# TIER 1: быстрый набор вариантов (покрывает все ключевые цветовые каналы,
# но без дублирования нескольких масштабов — этого обычно достаточно, чтобы
# уверенно распознать 3-значное число на цветном фоне).
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
# TIER 2: полный (тяжёлый) набор вариантов — используется только когда Tier 1
# не дал уверенного консенсуса. Это твоя исходная функция, без изменений в
# логике, только используется как фоллбэк, а не всегда.
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


def recognize_with_gemini(image_bytes: bytes) -> str:
    if gemini_model is None:
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

        response = gemini_model.generate_content(
            [
                prompt,
                {
                    "mime_type": "image/png",
                    "data": png_bytes
                }
            ],
            generation_config={
                "temperature": 0.0,
                "max_output_tokens": 10
            }
        )

        text = response.text.strip()
        digits = "".join(c for c in text if c.isdigit())
        return digits if digits else "0"

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
      Tier 2 — фоллбэк на полный (тяжёлый) перебор, как в исходной версии,
               если Tier 1 не дал уверенного результата. Срабатывает редко
               (сложные / нестандартные случаи), поэтому не бьёт по средней
               скорости, но сохраняет прежнее качество на трудных картинках.
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
        if gemini_model is None:
            return {"text": "0", "source": "gemini_unavailable"}
        result = recognize_with_gemini(image_bytes)
        return {"text": result, "source": "gemini"}

    # Только ddddocr
    if mode == "dddd":
        result = recognize_dddd(image_bytes)
        return {"text": result, "source": "ddddocr"}

    # Комбо (по умолчанию)
    dddd_result = recognize_dddd(image_bytes)

    if len(dddd_result) < 3 and gemini_model is not None:
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

    # gc.collect() убран из цикла — вызывается один раз на весь запрос,
    # этого достаточно и не тормозит обработку каждой картинки
    gc.collect()

    return OCRResponse(results=results)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "gemini": gemini_model is not None,
        "gemini_model": gemini_model_name
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = Path(__file__).parent / "favicon.ico"
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(favicon_path)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
