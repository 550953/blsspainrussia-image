import base64
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

app = FastAPI(title="Digit OCR Service with Preprocessing", version="1.7")

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
    h, w = img_bgr.shape[:2]
    variants = []

    # Два масштаба: 4x и 6x
    for scale in (4, 6):
        img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 1. Otsu
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th)
        variants.append(255 - th)

        # 2. Adaptive
        th_a = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 7
        )
        variants.append(th_a)
        variants.append(255 - th_a)

        # 3. CLAHE
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        _, th_c = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_c)
        variants.append(255 - th_c)

        # 4. Unsharp (важно для тонких нулей и 4/6)
        blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
        sharp = cv2.addWeighted(gray, 1.9, blur, -0.9, 0)
        _, th_s = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(th_s)
        variants.append(255 - th_s)

        # 5. Утолщение только после unsharp
        kernel = np.ones((2, 2), np.uint8)
        thick = cv2.dilate(th_s, kernel, iterations=1)
        variants.append(thick)
        variants.append(255 - thick)

    # Цветовой вариант (только 4x)
    img4 = cv2.resize(img_bgr, (w * 4, h * 4), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(img4, cv2.COLOR_BGR2LAB)
    l, a, _ = cv2.split(lab)
    color_enh = cv2.addWeighted(l, 0.55, a, 0.45, 0)
    _, th_col = cv2.threshold(color_enh, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_col)
    variants.append(255 - th_col)

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

        if not candidates:
            return "0"

        cnt = Counter(candidates)

        # Предпочитаем 3-значные
        def score(item):
            text, freq = item
            bonus = 70 if len(text) == 3 else (15 if len(text) == 2 else 0)
            return (freq + bonus, len(text))

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
