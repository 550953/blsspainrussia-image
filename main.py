# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Union
import base64
import io
from PIL import Image
import ddddocr
import uvicorn

app = FastAPI(title="Digit OCR Service", version="1.0")

# Инициализируем один раз (очень важно для скорости)
ocr = ddddocr.DdddOcr(show_ad=False)

class OCRRequest(BaseModel):
    images: Union[str, List[str]] = Field(
        ...,
        description="Один base64 или список base64 (можно с data:image/...;base64,)"
    )

class OCRResponse(BaseModel):
    results: List[str]

def clean_base64(b64: str) -> bytes:
    """Убираем data:image префикс если есть"""
    if b64.startswith("data:image"):
        b64 = b64.split(",", 1)[1]
    try:
        return base64.b64decode(b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")

def recognize_one(image_bytes: bytes) -> str:
    try:
        # ddddocr любит чистый PNG/JPEG
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        buff = io.BytesIO()
        img.save(buff, format="PNG")
        result = ocr.classification(buff.getvalue())
        # Оставляем только цифры (на всякий случай)
        return "".join(c for c in result if c.isdigit())
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
