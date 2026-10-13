"""Метаданные типов фоновых заданий; реестр остаётся пустым до загрузки сервиса."""
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict


@dataclass(frozen=True)
class TaskKind:
    name: str
    runner: Callable[[str, dict], Awaitable[None]]
    deadline_s: Callable[[], float]
    deadline_from: str
    queue_max_wait_s: float
    watchdog_grace_s: float
    result_ttl_s: Callable[[], int]
    max_concurrency: Callable[[], int]
    store_max: Callable[[], int]
    stats_prefix: str


KINDS: Dict[str, TaskKind] = {}


def register(kind: TaskKind) -> None:
    if kind.name in KINDS and KINDS[kind.name] != kind:
        raise ValueError("Task kind already registered: " + kind.name)
    if kind.deadline_from not in {"created", "started"}:
        raise ValueError("deadline_from must be created or started")
    KINDS[kind.name] = kind


def get(name: str) -> TaskKind:
    try:
        return KINDS[name]
    except KeyError as exc:
        raise ValueError("Unknown task kind: " + str(name)) from exc
