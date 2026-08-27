import base64
import io
from pathlib import Path
from typing import List, Union
import ddddocr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
import uvicorn

# Импортируем вторую либку для подстраховки
import easyocr

app = FastAPI(title="Digit OCR Service with Fallback", version="1.0")

# Инициализируем движки один раз
ocr_dddd = ddddocr.DdddOcr(show_ad=False)
# Инициализируем easyocr только для цифр (ускоряет работу и повышает точность)
reader_easy = easyocr.Reader(["en"], gpu=False, verbose=False)


class OCRRequest(BaseModel):
  images: Union[str, List[str]] = Field(
      ..., description="Один base64 или список base64"
  )


class OCRResponse(BaseModel):
  results: List[str]


def clean_base64(b64: str) -> bytes:
  if b64.startswith("data:image"):
    b64 = b64.split(",", 1)[1]
  try:
    return base64.b64decode(b64)
  except Exception as e:
    raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")


def recognize_with_easyocr(image_bytes: bytes) -> str:
  try:
    # EasyOCR умеет принимать байты напрямую или через numpy, но проще через путь/байты
    results = reader_easy.readtext(
        image_bytes, allowlist="0123456789", detail=0
    )
    if results:
      # Объединяем все найденные куски текста в одну строку
      joined = "".join(results)
      return "".join(c for c in joined if c.isdigit())
  except Exception:
    pass
  return ""


def recognize_one(image_bytes: bytes) -> str:
  try:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buff = io.BytesIO()
    img.save(buff, format="PNG")
    img_bytes_png = buff.getvalue()

    # --- МЕТОД 1: ddddocr ---
    result_1 = ocr_dddd.classification(img_bytes_png)
    digits_1 = "".join(c for c in result_1 if c.isdigit())

    # Если метод 1 нашел 3 или более цифр — считаем успех
    if len(digits_1) >= 3:
      return digits_1

    # --- МЕТОД 2: EasyOCR (если цифр меньше 3) ---
    digits_2 = recognize_with_easyocr(img_bytes_png)
    if len(digits_2) >= 3:
      return digits_2

    # Если оба метода дали мало цифр, возвращаем то, что получилось у первого (или второго, где больше)
    return digits_1 if len(digits_1) >= len(digits_2) else digits_2

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
