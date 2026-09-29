"""Сборка contact sheet, промпты для Gemini и разбор его JSON-ответа."""
import json
import re
from typing import List, Optional

import cv2
import numpy as np

from .config import (
    GEMINI_SHEET_CELL_HEIGHT,
    GEMINI_SHEET_CELL_WIDTH,
    GEMINI_SHEET_COLS,
    GEMINI_SHEET_MAX_IMAGES,
)


def _chunk(items: List[bytes], size: int) -> List[List[bytes]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def make_gemini_contact_sheet(images: List[bytes], cols: Optional[int] = None) -> bytes:
    """Собирает <=GEMINI_SHEET_MAX_IMAGES изображений в один PNG с номерами ячеек.
    cols: переопределить число колонок (для точечного повтора нужен маленький лист)."""
    if not images:
        raise ValueError("Пустой список изображений")
    if len(images) > GEMINI_SHEET_MAX_IMAGES:
        raise ValueError(
            f"gemini_sheet поддерживает максимум {GEMINI_SHEET_MAX_IMAGES} изображений, получено {len(images)}"
        )

    cols = max(1, min(cols or GEMINI_SHEET_COLS, len(images)))
    rows = (len(images) + cols - 1) // cols
    sheet = np.full(
        (rows * GEMINI_SHEET_CELL_HEIGHT, cols * GEMINI_SHEET_CELL_WIDTH, 3),
        255,
        dtype=np.uint8,
    )

    for index, image_bytes in enumerate(images, start=1):
        encoded = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Не удалось декодировать изображение №{index}")

        max_w = GEMINI_SHEET_CELL_WIDTH - 20
        max_h = GEMINI_SHEET_CELL_HEIGHT - 30
        h, w = image.shape[:2]
        scale = min(max_w / max(w, 1), max_h / max(h, 1), 1.0)
        if scale != 1.0:
            image = cv2.resize(
                image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA
            )

        col = (index - 1) % cols
        row = (index - 1) // cols
        left = col * GEMINI_SHEET_CELL_WIDTH
        top = row * GEMINI_SHEET_CELL_HEIGHT
        ih, iw = image.shape[:2]
        x = left + (GEMINI_SHEET_CELL_WIDTH - iw) // 2
        y = top + 25 + (max_h - ih) // 2
        sheet[y:y + ih, x:x + iw] = image

        cv2.rectangle(
            sheet,
            (left, top),
            (left + GEMINI_SHEET_CELL_WIDTH - 1, top + GEMINI_SHEET_CELL_HEIGHT - 1),
            (150, 150, 150),
            1,
        )
        cv2.putText(
            sheet, f"#{index}", (left + 5, top + 17),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA,
        )

    success, buffer = cv2.imencode(".png", sheet, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not success:
        raise ValueError("Не удалось собрать PNG contact sheet")
    return buffer.tobytes()


GEMINI_SHEET_PROMPT = f"""
На изображении contact sheet из пронумерованных ячеек.
В каждой ячейке находится одна картинка с трёхзначным числом.
Распознай число в каждой ячейке и сопоставь его с номером ячейки.

Верни строго JSON-объект без markdown и пояснений:
{{"1":"353","2":"872", ... "{GEMINI_SHEET_MAX_IMAGES}":null}}

Правила:
- верни ключи только для фактически присутствующих ячеек;
- ключи — строки с номерами ячеек от "1" до последней;
- значение — строка ровно из трёх цифр;
- если ячейка не читается, значение null;
- ничего не переставляй и не придумывай.
""".strip()

# Промпт точечного повтора: ячейки листа это РАЗНЫЕ варианты обработки ОДНОЙ картинки.
GEMINI_VARIANT_RETRY_PROMPT_TEMPLATE = """
На изображении contact sheet из {n} пронумерованных ячеек.
Это РАЗНЫЕ варианты обработки контраста ОДНОЙ И ТОЙ ЖЕ исходной картинки
с трёхзначным числом — само число везде одно и то же, отличается только
то, насколько хорошо оно видно на конкретном варианте.

Верни строго JSON-объект без markdown и пояснений:
{{"1":"353","2":"353", ... "{n}":null}}

Правила:
- верни ключи для всех ячеек от "1" до "{n}";
- значение — строка ровно из трёх цифр, если смог прочитать на этом варианте;
- если конкретный вариант нечитаем, значение null для него (другие ячейки
  на это не влияют);
- ничего не придумывай — если не уверен, лучше null, чем случайное число.
""".strip()


def parse_gemini_sheet_result(text: str, count: int) -> List[str]:
    """Принимает JSON object/list и приводит его к списку результатов."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = min([pos for pos in (cleaned.find("{"), cleaned.find("[")) if pos >= 0], default=-1)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if start < 0 or end <= start:
            raise ValueError("Gemini вернул не JSON")
        data = json.loads(cleaned[start:end + 1])

    if isinstance(data, dict) and isinstance(data.get("results"), list):
        values = data["results"]
    elif isinstance(data, dict):
        values = [data.get(str(index)) for index in range(1, count + 1)]
    elif isinstance(data, list):
        values = data
    else:
        raise ValueError("Ожидался JSON-объект или JSON-массив")

    result: List[str] = []
    for value in values[:count]:
        if value is None:
            result.append("0")
            continue
        digits = "".join(char for char in str(value) if char.isdigit())
        result.append(digits if len(digits) == 3 else "0")
    result.extend(["0"] * (count - len(result)))
    return result
