import base64
import io
from pathlib import Path
from typing import List, Union
from collections import Counter

import ddddocr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image
import numpy as np
import cv2
import uvicorn

app = FastAPI(title="Digit OCR Service with Preprocessing", version="1.4")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)


class OCRRequest(BaseModel):
    images: Union[str, List[str]] = Field(..., description="Один base64 или список base64")


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
    """Генерируем несколько вариантов изображения для OCR."""
    h, w = img_bgr.shape[:2]
    variants = []

    # Два масштаба: 4x (быстрый) и 6x (для тонких цифр)
    for scale in (4, 6):
        img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 1. Классический Otsu
        _, th1 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th1)
        variants.append(255 - th1)

        # 2. Adaptive threshold
        th_adapt = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 8
        )
        variants.append(th_adapt)
        variants.append(255 - th_adapt)

        # 3. CLAHE + Otsu
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        _, th_clahe = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_clahe)
        variants.append(255 - th_clahe)

        # 4. Unsharp mask (хорошо поднимает тонкие линии 1/7/0)
        blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
        sharp = cv2.addWeighted(gray, 1.8, blur, -0.8, 0)
        _, th_sharp = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_sharp)
        variants.append(255 - th_sharp)

    # 5. Цветовой вариант (LAB) — только на 4x, чтобы не раздувать
    img4 = cv2.resize(img_bgr, (w * 4, h * 4), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(img4, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    color_enh = cv2.addWeighted(l, 0.55, a, 0.45, 0)
    _, th_color = cv2.threshold(color_enh, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_color)
    variants.append(255 - th_color)

    # 6. Утолщение линий (только на первых вариантах)
    kernel = np.ones((2, 2), np.uint8)
    for v in variants[:10]:
        variants.append(cv2.dilate(v, kernel, iterations=1))

    return variants


def recognize_one(image_bytes: bytes) -> str:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            res = ocr_dddd.classification(image_bytes)
            return "".join(c for c in res if c.isdigit()) or "0"

        candidates = []

        # Оригинал без обработки
        res0 = ocr_dddd.classification(image_bytes)
        dig0 = "".join(c for c in res0 if c.isdigit())
        if dig0:
            candidates.append(dig0)

        # Все варианты препроцессинга
        for var in make_variants(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        if not candidates:
            return "0"

        # Выбор лучшего: частота → длина → бонус за 3 цифры
        cnt = Counter(candidates)

        def score(item):
            text, freq = item
            length_score = len(text) * 10
            three_digit_bonus = 40 if len(text) == 3 else 0
            return (freq, length_score + three_digit_bonus)

        best = sorted(cnt.items(), key=score, reverse=True)[0][0]
        return best

    except Exception as e:
        return f"ERROR:{str(e)}"


@app.post("/ocr", response_model=OCRResponse)
async def ocr_endpoint(req: OCRRequest):
    images = req.images if isinstance(req.images, list) else [req.images]
    if not images:
        raise HTTPException(status_code=400, detail="Empty images list")

    results = []
    for b64 in images:
        img_bytes = clean_base64(b64)
        results.append(recognize_one(img_bytes))

    return OCRResponse(results=results)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = Path(__file__).parent / "favicon.ico"
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(favicon_path)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
