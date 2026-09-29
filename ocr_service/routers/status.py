"""/status (страница) и /status.json (данные) под HTTP Basic.

Логин и пароль задаются в Render (STATUS_USER, STATUS_PASSWORD).
Без них оба адреса закрыты (503).
"""
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..config import STATUS_PASSWORD, STATUS_USER
from ..status_data import build_status

router = APIRouter()

# Вёрстка лежит отдельным файлом рядом с пакетом: ocr_service/status.html
STATUS_HTML = (Path(__file__).resolve().parent.parent / "status.html").read_text(encoding="utf-8")

_basic = HTTPBasic(auto_error=False)


def require_status_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_basic)) -> None:
    if not (STATUS_USER and STATUS_PASSWORD):
        raise HTTPException(status_code=503, detail="STATUS_USER и STATUS_PASSWORD не заданы в окружении")
    ok = (
        credentials is not None
        and secrets.compare_digest(credentials.username.encode("utf-8"), STATUS_USER.encode("utf-8"))
        and secrets.compare_digest(credentials.password.encode("utf-8"), STATUS_PASSWORD.encode("utf-8"))
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="OCR status", charset="UTF-8"'},
        )


_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/status", include_in_schema=False, dependencies=[Depends(require_status_auth)])
async def status_page():
    return HTMLResponse(STATUS_HTML, headers=_NO_STORE)


@router.get("/status.json", include_in_schema=False, dependencies=[Depends(require_status_auth)])
async def status_json():
    return JSONResponse(build_status(), headers=_NO_STORE)
