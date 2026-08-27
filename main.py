import base64
import io
from pathlib import Path
from typing import List, Union
import ddddocr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image
import numpy as np
import cv2
import uvicorn

app = FastAPI(title="Digit OCR Service with Preprocessing", version="1.3")

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
    # Сильный апскейл — критично для тонких цифр
    scale = 4
    img = cv2.resize(img_bgr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

    variants = []

    # 1. Классический grayscale + Otsu
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, th1 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th1)

    # 2. Инверсия Otsu (на случай светлых цифр)
    variants.append(255 - th1)

    # 3. Adaptive threshold (лучше держит тонкие линии)
    th_adapt = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )
    variants.append(th_adapt)
    variants.append(255 - th_adapt)

    # 4. Работа с цветом: выделяем "насыщенный" канал
    # Фиолетовый/цветной текст часто лучше видно в LAB или после max-канала
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    # Берём канал a (красно-зелёный) + L
    color_enh = cv2.addWeighted(l, 0.6, a, 0.4, 0)
    _, th_color = cv2.threshold(color_enh, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_color)
    variants.append(255 - th_color)

    # 5. Утолщение линий (morphology) — спасает тонкий "0"
    kernel = np.ones((2, 2), np.uint8)
    for v in list(variants):
        thick = cv2.dilate(v, kernel, iterations=1)
        variants.append(thick)

    # 6. Ещё один вариант: просто сильный контраст + CLAHE
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, th_clahe = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(th_clahe)
    variants.append(255 - th_clahe)

    return variants

def recognize_one(image_bytes: bytes) -> str:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            # fallback на оригинал
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

        # Выбираем самый частый результат.
        # Если частоты равны — берём самый длинный (чаще всего правильный "320" длиннее "32")
        from collections import Counter
        cnt = Counter(candidates)
        # Сортируем: сначала по частоте ↓, потом по длине ↓
        best = sorted(cnt.items(), key=lambda x: (x[1], len(x[0])), reverse=True)[0][0]

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
