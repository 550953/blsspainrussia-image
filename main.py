import base64
import gc
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

app = FastAPI(title="Digit OCR Service", version="1.9")

ocr_dddd = ddddocr.DdddOcr(show_ad=False)


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
    """Очень лёгкий набор вариантов"""
    h, w = img_bgr.shape[:2]
    variants = []

    for scale in (4, 5):
        img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Otsu
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th)
        variants.append(255 - th)

        # Adaptive
        th_a = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 5
        )
        variants.append(th_a)
        variants.append(255 - th_a)

        # Unsharp + утолщение (для нулей)
        blur = cv2.GaussianBlur(gray, (0, 0), 1.0)
        sharp = cv2.addWeighted(gray, 1.7, blur, -0.7, 0)
        _, th_s = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_s)

        kernel = np.ones((2, 2), np.uint8)
        variants.append(cv2.dilate(th_s, kernel, iterations=1))

    return variants


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

        # Чистим память сразу
        del img, nparr
        gc.collect()

        if not candidates:
            return "0"

        cnt = Counter(candidates)

        def score(item):
            text, freq = item
            bonus = 50 if len(text) == 3 else 0
            return (freq + bonus, len(text))

        best = sorted(cnt.items(), key=score, reverse=True)[0][0]
        return best

    except Exception as e:
        return f"ERROR:{str(e)}"


@app.post("/ocr", response_model=OCRResponse)
async def ocr_endpoint(req: OCRRequest):
    images = req.images if isinstance(req.images, list) else [req.images]

    # Жёстко ограничиваем — максимум 3 картинки за раз
    if len(images) > 3:
        raise HTTPException(status_code=400, detail="Максимум 3 изображения за один запрос")

    results = []
    for b64 in images:
        img_bytes = clean_base64(b64)
        result = recognize_one(img_bytes)
        results.append(result)
        # Принудительно чистим после каждой картинки
        gc.collect()

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
