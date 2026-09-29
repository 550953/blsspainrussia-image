"""Запасное распознавание через ddddocr (когда Gemini вернул 0)."""
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import cv2
import ddddocr
import numpy as np

from .config import OCR_MAX_WORKERS
from .utils import _score
from .variants import make_variants_fast, make_variants_full

ocr_dddd = ddddocr.DdddOcr(show_ad=False)

EXECUTOR = ThreadPoolExecutor(max_workers=OCR_MAX_WORKERS, thread_name_prefix="ocr-worker")


def recognize_dddd(image_bytes: bytes) -> str:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            res = ocr_dddd.classification(image_bytes)
            return "".join(c for c in res if c.isdigit()) or "0"

        candidates = []

        res0 = ocr_dddd.classification(image_bytes)
        dig0 = "".join(c for c in res0 if c.isdigit())
        if dig0:
            candidates.append(dig0)

        for var in make_variants_fast(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        cnt = Counter(c for c in candidates if len(c) == 3)
        if cnt:
            top_text, top_freq = cnt.most_common(1)[0]
            if top_freq >= 3:
                del img, nparr
                return top_text

        for var in make_variants_full(img):
            success, buf = cv2.imencode(".png", var)
            if not success:
                continue
            res = ocr_dddd.classification(buf.tobytes())
            dig = "".join(c for c in res if c.isdigit())
            if dig:
                candidates.append(dig)

        del img, nparr

        if not candidates:
            return "0"

        cnt_full = Counter(candidates)
        return sorted(cnt_full.items(), key=_score, reverse=True)[0][0]

    except Exception as e:
        print(f"[pid={os.getpid()}][dddd] Ошибка распознавания: {e}")
        return "0"


def recognize_one_sync_part(image_bytes: bytes) -> str:
    return recognize_dddd(image_bytes)
