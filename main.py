import base64
import io
from pathlib import Path
from typing import List, Union
import ddddocr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
import numpy as np
import cv2
import uvicorn

app = FastAPI(title="Digit OCR Service with Preprocessing", version="1.1")

# Инициализируем только ddddocr — он очень легкий и отлично помещается в лимит 512 МБ
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

def preprocess_image(image_bytes: bytes) -> bytes:
    """Улучшаем качество изображения: увеличиваем размер, делаем контрастным черно-белым"""
    try:
        # Читаем картинку через OpenCV из байтов
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            return image_bytes

        # 1. Увеличиваем в 2 раза (интерполяция кубическая для сглаживания)
        height, width = img.shape[:2]
        img_resized = cv2.resize(img, (width * 2, height * 2), interpolation=cv2.INTER_CUBIC)

        # 2. Переводим в оттенки серого
        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)

        # 3. Бинаризация (порог Оцу) — убирает фоновый шум и делает цифры черными на белом фоне
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # Кодируем обратно в PNG байты
        success, encoded_img = cv2.imencode('.png', thresh)
        if success:
            return encoded_img.tobytes()
    except Exception:
        pass
    
    return image_bytes

def recognize_one(image_bytes: bytes) -> str:
    try:
        # Попытка 1: Распознаем оригинал
        result_1 = ocr_dddd.classification(image_bytes)
        digits_1 = "".join(c for c in result_1 if c.isdigit())

        # Если нашли 3 или более цифр — сразу отдаем результат
        if len(digits_1) >= 3:
            return digits_1

        # Попытка 2: Если цифр мало, применяем умную предобработку и гоним снова
        processed_bytes = preprocess_image(image_bytes)
        result_2 = ocr_dddd.classification(processed_bytes)
        digits_2 = "".join(c for c in result_2 if c.isdigit())

        # Возвращаем тот вариант, где нашлось больше цифр
        return digits_2 if len(digits_2) > len(digits_1) else digits_1

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
