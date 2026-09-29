"""Мелкие общие помощники."""
import base64
import re

from fastapi import HTTPException


def clean_base64(b64: str) -> bytes:
    b64 = b64.strip().strip("\"'")
    if b64.startswith("data:image"):
        b64 = b64.split(",", 1)[1]
    b64 = re.sub(r"\s+", "", b64)
    try:
        return base64.b64decode(b64, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")


def _score(item):
    """Ключ сортировки для голосования: (текст, число голосов)."""
    text, freq = item
    bonus = 120 if len(text) == 3 else (25 if len(text) == 2 else 0)
    return (freq + bonus, len(text))
