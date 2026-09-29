"""Digit OCR Service v4.6 (Gemini contact sheet + очередь заданий + защищённый /status).

Точка входа: uvicorn main:app. Вся логика лежит в пакете ocr_service/:

  config.py        env, Infisical, Better Stack, константы
  gemini_errors.py классификация ошибок Gemini, GeminiAPIError
  key_pool.py      пул ключей (round-robin, cooldown, hold)
  proxies.py       пул прокси/каналов
  gemini_client.py REST-вызов Gemini (httpx)
  telemetry.py     JSON-логи, Better Stack, статистика в памяти
  variants.py      варианты предобработки картинки
  dddd_ocr.py      запасное распознавание ddddocr
  contact_sheet.py сборка листа, промпты, разбор ответа
  gemini_sheet.py  вызов Gemini на чанк + точечный повтор
  pipeline.py      чанки -> Gemini -> повтор -> ddddocr
  jobs.py          очередь заданий (submit -> poll)
  status_data.py   build_status() для /status.json
  status.html      страница /status
  schemas.py       pydantic-модели
  routers/         status.py, ocr.py, health.py
"""
import json
import time

import uvicorn
from fastapi import FastAPI

from ocr_service.config import (  # config первым: тут PROCESS_START_TIME
    BETTERSTACK_CONFIG_SOURCE,
    BETTERSTACK_ENABLED,
    BETTERSTACK_SERVICE,
    PROCESS_START_TIME,
    STARTUP_TIMING_FILE,
    STATUS_PASSWORD,
    STATUS_USER,
)
from ocr_service import jobs, telemetry
from ocr_service.dddd_ocr import EXECUTOR
from ocr_service.gemini_client import close_http_clients
from ocr_service.key_pool import GEMINI_KEYS
from ocr_service.routers import health, ocr, status

app = FastAPI(title="Digit OCR Service", version="4.6")

# Порядок как в исходнике: /status, /ocr/*, /health, /favicon.ico
app.include_router(status.router)
app.include_router(ocr.router)
app.include_router(health.router)


@app.on_event("startup")
async def on_startup():
    elapsed = time.time() - PROCESS_START_TIME
    payload = {
        "process_start_time": PROCESS_START_TIME,
        "ready_time": time.time(),
        "elapsed_seconds": round(elapsed, 3),
    }
    print(f"[startup_timing] Приложение готово через {elapsed:.3f} сек после старта процесса")
    print(f"[status] /status {'включён (логин из STATUS_USER)' if STATUS_USER and STATUS_PASSWORD else 'ЗАКРЫТ: задай STATUS_USER и STATUS_PASSWORD в Render'}")

    try:
        STARTUP_TIMING_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[startup_timing] Не удалось записать файл замера: {e}")

    jobs.start_cleanup_loop()
    telemetry.start_betterstack()
    telemetry._job_log(
        "service_ready",
        result_ttl_seconds=jobs.JOB_RESULT_TTL_SECONDS,
        cleanup_interval_seconds=jobs.JOB_CLEANUP_INTERVAL_SECONDS,
        gemini_keys=len(GEMINI_KEYS),
        betterstack_enabled=BETTERSTACK_ENABLED,
        betterstack_service=BETTERSTACK_SERVICE,
        betterstack_config_source=BETTERSTACK_CONFIG_SOURCE,
    )


@app.on_event("shutdown")
async def on_shutdown():
    jobs.stop_cleanup_loop()
    await telemetry.stop_betterstack()
    EXECUTOR.shutdown(wait=False)
    await close_http_clients()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
