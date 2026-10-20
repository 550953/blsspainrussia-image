"""Индекс давления на Gemini (0..100) и решение «пора запускать ddddocr параллельно».

Индекс считается по двум живым сигналам:
  * доля неуспешных попыток Gemini за последние OCR_HEDGE_WINDOW_S секунд (ATTEMPTS);
  * медианный возраст чанков, которые прямо сейчас ждут ответа (относительно дедлайна).
Формула: P = 100 * (W_FAIL * fail_rate + W_AGE * age_ratio).

Включение с гистерезисом: ON при P >= OCR_HEDGE_ON, OFF при P < OCR_HEDGE_OFF,
чтобы режим не дёргался туда-сюда на границе.
"""
import os
import time
from contextlib import contextmanager
from typing import Optional

from .telemetry import ATTEMPTS

HEDGE_ENABLED = os.getenv("OCR_HEDGE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
HEDGE_ON = float(os.getenv("OCR_HEDGE_ON", "50"))
HEDGE_OFF = float(os.getenv("OCR_HEDGE_OFF", "30"))
HEDGE_WINDOW_S = float(os.getenv("OCR_HEDGE_WINDOW_S", "120"))
HEDGE_MIN_SAMPLES = int(os.getenv("OCR_HEDGE_MIN_SAMPLES", "8"))
# Чанк, который ждёт Gemini дольше этого, получает ddddocr-подстраховку при любом индексе.
HEDGE_AFTER_S = float(os.getenv("OCR_HEDGE_AFTER_S", "20"))
# При включённом режиме ddddocr стартует не сразу, а когда чанк ждёт Gemini дольше этого:
# быстрые ответы Gemini (2-4 с) не должны нагружать CPU впустую.
HEDGE_MIN_WAIT_S = float(os.getenv("OCR_HEDGE_MIN_WAIT_S", "5"))
# Мягкий дедлайн чанка: к этому времени берём то, что есть (Gemini + ddddocr), остальное отменяем.
HEDGE_DEADLINE_S = float(os.getenv("OCR_HEDGE_DEADLINE_S", "40"))
# После того как ddddocr закончил чанк, ждём Gemini ещё столько секунд, затем переключаемся на ddddocr.
HEDGE_GRACE_S = float(os.getenv("OCR_HEDGE_GRACE_S", "6"))
# Сколько максимум ждём ddddocr после переключения (укладываемся в OCR_JOB_MAX_SECONDS=60).
HEDGE_FINISH_S = float(os.getenv("OCR_HEDGE_FINISH_S", "10"))
# Сколько чанков одновременно могут считаться в ddddocr (он грузит CPU, пул потоков общий).
HEDGE_MAX_CHUNKS = max(1, int(os.getenv("OCR_HEDGE_MAX_CHUNKS", "2")))
W_FAIL = float(os.getenv("OCR_HEDGE_W_FAIL", "0.6"))
W_AGE = float(os.getenv("OCR_HEDGE_W_AGE", "0.4"))

# Предохранитель: если event loop опаздывает больше этого (сек), ddddocr не запускаем и недосчитанное бросаем.
HEDGE_MAX_LAG_S = float(os.getenv("OCR_HEDGE_MAX_LAG_S", "0.4"))
_lags: list = []            # (monotonic, lag_s) за последние 10 с

_INFLIGHT: dict = {}        # token -> monotonic старт чанка
_next_token = 0
_hedge_on = False
_cache = {"at": 0.0, "value": None}


@contextmanager
def track_chunk():
    """Регистрирует чанк как «ждёт Gemini», чтобы считать его возраст в индексе."""
    global _next_token
    _next_token += 1
    token = _next_token
    _INFLIGHT[token] = time.monotonic()
    try:
        yield token
    finally:
        _INFLIGHT.pop(token, None)


def note_lag(lag_s: float) -> None:
    """Опоздание event loop: фактическое время сна минус заказанное. Зовёт опрос чанков."""
    now = time.monotonic()
    _lags.append((now, max(0.0, lag_s)))
    while _lags and now - _lags[0][0] > 10.0:
        _lags.pop(0)


def loop_lag() -> float:
    now = time.monotonic()
    return max((lag for ts, lag in _lags if now - ts <= 10.0), default=0.0)


def overloaded() -> bool:
    return loop_lag() > HEDGE_MAX_LAG_S


def _median(values: list) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def compute(now_wall: Optional[float] = None, now_mono: Optional[float] = None) -> dict:
    """Чистый расчёт индекса без побочных эффектов (гистерезис применяет snapshot())."""
    wall = time.time() if now_wall is None else now_wall
    mono = time.monotonic() if now_mono is None else now_mono
    cutoff = wall - HEDGE_WINDOW_S
    total = ok = 0
    for row in reversed(ATTEMPTS):          # новые записи в конце: идём с хвоста и выходим рано
        if row[0] < cutoff:
            break
        total += 1
        ok += row[3] == "SUCCESS"
    fail_rate = (1.0 - ok / total) if total >= HEDGE_MIN_SAMPLES else 0.0
    ages = [mono - started for started in _INFLIGHT.values()]
    age_ratio = min(1.0, _median(ages) / HEDGE_DEADLINE_S) if ages else 0.0
    index = round(100.0 * (W_FAIL * fail_rate + W_AGE * age_ratio))
    return {
        "index": max(0, min(100, index)),
        "fail_rate": round(fail_rate, 3), "samples": total,
        "median_age_s": round(_median(ages), 1), "inflight": len(ages), "loop_lag_s": round(loop_lag(), 2),
    }


def snapshot() -> dict:
    """Индекс + состояние гистерезиса (кэш 1 с: вызывается из каждого чанка и из /status)."""
    global _hedge_on
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["at"] < 1.0:
        return _cache["value"]
    snap = compute()
    if not HEDGE_ENABLED:
        _hedge_on = False
    elif snap["index"] >= HEDGE_ON:
        _hedge_on = True
    elif snap["index"] < HEDGE_OFF:
        _hedge_on = False
    snap.update(hedge_on=_hedge_on, enabled=HEDGE_ENABLED, on_at=HEDGE_ON, off_at=HEDGE_OFF)
    _cache.update(at=now, value=snap)
    return snap


def should_hedge(chunk_elapsed_s: float) -> bool:
    """Запускать ли ddddocr рядом с Gemini для чанка, который ждёт chunk_elapsed_s секунд."""
    if not HEDGE_ENABLED or overloaded():
        return False
    if chunk_elapsed_s >= HEDGE_AFTER_S:
        return True
    return chunk_elapsed_s >= HEDGE_MIN_WAIT_S and snapshot()["hedge_on"]
