import base64
import gc
import os
from pathlib import Path
from typing import List, Union
from collections import Counter

import ddddocr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import numpy as np
import cv2
import uvicorn
import google.generativeai as genai

app = FastAPI(title="Digit OCR Service", version="2.3")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_model = None

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-flash")
        print("Gemini подключен")
    except Exception as e:
        print(f"Gemini не удалось инициализировать: {e}")


class OCRRequest(BaseModel):
    images: Union[str, List[str]] = Field(..., description="Один base64 или список (рекомендуется 1)")


class OCRResponse(BaseModel):
    results: List[str]


def clean_base64(b64: str) -> bytes:
    if b64.startswith("data:image"):
        b64 = b64.split(",", 1)[1]
    try:
        return base64.b64decode(b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")


def make_variants(img_bgr: np.ndarray) -> list[np.ndarray]:
    h, w = img_bgr.shape[:2]
    variants = []

    for scale in (5, 7):
        img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Otsu
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th)
        variants.append(255 - th)

        # Adaptive
        th_a = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
        )
        variants.append(th_a)
        variants.append(255 - th_a)

        # CLAHE
        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        _, th_c = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_c)
        variants.append(255 - th_c)

        # Unsharp
        blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
        sharp = cv2.addWeighted(gray, 2.2, blur, -1.2, 0)
        _, th_s = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_s)
        variants.append(255 - th_s)

        # Утолщение
        kernel = np.ones((2, 2), np.uint8)
        variants.append(cv2.dilate(th_s, kernel, iterations=1))
        variants.append(cv2.dilate(th_s, kernel, iterations=2))

        # Цветовые каналы
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
    """Запасной вариант через Gemini Vision"""
    if gemini_model is None:
        return "0"

    try:
        # Всегда отправляем как PNG
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


def recognize_one(image_bytes: bytes) -> str:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            res = ocr_dddd.classification(image_bytes)
            return "".join(c for c in res if c.isdigit()) or "0"

        candidates = []

        # Оригинал
        res0 = ocr_dddd.classification(image_bytes)
        dig0 = "".join(c for c in res0 if c.isdigit())
        if dig0:
            candidates.append(dig0)

        for var in make_variants(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        del img, nparr
        gc.collect()

        if not candidates:
            best = "0"
        else:
            cnt = Counter(candidates)

            def score(item):
                text, freq = item
                bonus = 120 if len(text) == 3 else (25 if len(text) == 2 else 0)
                return (freq + bonus, len(text))

            best = sorted(cnt.items(), key=score, reverse=True)[0][0]

        # === Умный fallback на Gemini ===
        # Если результат короче 3 цифр — пробуем Gemini
        if len(best) < 3 and gemini_model is not None:
            gemini_result = recognize_with_gemini(image_bytes)
            if len(gemini_result) >= 3:
                return gemini_result
            # Если Gemini тоже короткий, но лучше чем best — берём его
            if len(gemini_result) > len(best):
                return gemini_result

        return best

    except Exception as e:
        return f"ERROR:{str(e)}"


@app.post("/ocr", response_model=OCRResponse)
async def ocr_endpoint(req: OCRRequest):
    images = req.images if isinstance(req.images, list) else [req.images]

    if len(images) > 3:
        raise HTTPException(status_code=400, detail="Максимум 3 изображения за один запрос")

    results = []
    for b64 in images:
        result = recognize_one(clean_base64(b64))
        results.append(result)
        gc.collect()

    return OCRResponse(results=results)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "gemini": gemini_model is not None
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = Path(__file__).parent / "favicon.ico"
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(favicon_path)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
