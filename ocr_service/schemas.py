"""Pydantic-модели запросов и ответов."""
from typing import List, Optional, Union

from pydantic import BaseModel, Field


class OCRRequest(BaseModel):
    images: Union[str, List[str]] = Field(..., description="Один base64 или список")
    # Небольшие метаданные без картинок: позволяют связать задание с аккаунтом.
    account_id: str = Field("", max_length=128)
    client_request_id: str = Field("", max_length=128)


class OCRResultItem(BaseModel):
    text: str
    source: str


class OCRResponse(BaseModel):
    results: List[OCRResultItem]


class JobSubmitResponse(BaseModel):
    job_id: str = Field(..., description="Уникальный ID задания, использовать для polling'а")
    status: str = Field(..., description="Всегда 'pending' сразу после сабмита")


class JobResultResponse(BaseModel):
    status: str = Field(..., description="'pending' | 'done' | 'error'")
    stage: Optional[str] = Field(None, description="'queued' | 'processing' | 'finished'")
    queue_position: Optional[int] = Field(None, description="Приблизительное место среди ожидающих заданий")
    queued_jobs: Optional[int] = Field(None, description="Сколько заданий ожидает запуска")
    running_jobs: Optional[int] = Field(None, description="Сколько заданий обрабатывается")
    max_concurrency: Optional[int] = Field(None, description="Лимит параллельной обработки")
    job_age_ms: Optional[int] = Field(None, description="Возраст задания в миллисекундах")
    queue_wait_ms: Optional[int] = Field(None, description="Ожидание запуска в миллисекундах")
    processing_ms: Optional[int] = Field(None, description="Время обработки в миллисекундах")
    results: Optional[List[OCRResultItem]] = Field(None, description="Заполнено только когда status == 'done'")
    error: Optional[str] = Field(None, description="Текст ошибки, только когда status == 'error'")
