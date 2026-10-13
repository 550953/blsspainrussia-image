"""Сборка системного и пользовательского запросов для генерации письма."""
import hashlib
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Sequence

PROMPT_VERSION = "1.0"
PROMPT_PATH = Path(__file__).with_name("prompt_base.txt")


def base_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


_BASE_PROMPT = None


def system_instruction() -> str:
    global _BASE_PROMPT
    if _BASE_PROMPT is None:
        _BASE_PROMPT = base_prompt()
    return _BASE_PROMPT


def prompt_sha1() -> str:
    return hashlib.sha1(system_instruction().encode("utf-8")).hexdigest()[:12]


def _brief_mapping(brief) -> dict:
    if isinstance(brief, dict):
        return brief
    if is_dataclass(brief):
        return asdict(brief)
    return dict(vars(brief))


def build_user_message(brief: dict) -> str:
    brief = _brief_mapping(brief)
    return (
        "ПАРАМЕТРЫ ЗАПУСКА (обязательны к исполнению, выбирать самому не нужно):\n"
        "Способ оформления знака: №{currency_method}\n"
        "Семейство палитры: {palette_family}\n"
        "Пара шрифтов: {fonts}\n"
        "Композиция шапки: {header}\n"
        "Тип статус-индикатора: {indicator}\n"
        "Не повторяй палитры недавних писем: {avoid_palettes}\n"
        "Номер запуска (для разнообразия формулировок): {seed}\n"
        "Выполни задание из системной инструкции. Верни ответ в формате из раздела «ФОРМАТ ОТВЕТА»; "
        "файл template.html допустимо вернуть одним блоком ```html."
    ).format(
        currency_method=brief["currency_method"], palette_family=brief["palette_family"], fonts=brief["fonts"],
        header=brief["header"], indicator=brief["indicator"],
        avoid_palettes=", ".join(brief["avoid_palettes"]) or "нет", seed=brief["seed"],
    )


def build_repair_message(issues: list, cut_off=False) -> str:
    order = {"T": 0, "S": 0, "V": 0, "X": 0, "L": 1, "A": 2}
    issues = [
        {"rule": item[0], "message": item[1], "where": item[2] if len(item) > 2 else ""}
        if isinstance(item, (tuple, list)) else item
        for item in issues
    ]
    issues = sorted(issues, key=lambda item: order.get(str(item.get("rule", "?"))[:1], 3))
    entries = []
    for issue in issues[:12]:
        item = "- [{rule}] {message}".format(rule=issue.get("rule", "?"), message=issue.get("message", ""))
        if issue.get("where"):
            item += " — фрагмент: " + str(issue["where"])[:120]
        entries.append(item)
    return (
        "Проверка нашла нарушения. Исправь ТОЛЬКО их; дизайн, палитру, тексты и выбранные параметры не меняй.\n"
        "Верни ответ в том же формате: концепция, файл template.html целиком, строка с переменными.\n"
        + ("HTML был обрезан: сократи формулировки и заверши полный документ.\n" if cut_off else "")
        + "Нарушения:\n" + "\n".join(entries)
    )


def first_user_message(brief) -> str:
    return build_user_message(brief)


def repair_message(issues: Sequence, truncated=False) -> str:
    return build_repair_message(list(issues), cut_off=truncated)


def build_contents(brief, prev_answer="", issues=(), truncated=False) -> list:
    first = {"role": "user", "parts": [{"text": first_user_message(brief)}]}
    if not prev_answer:
        return [first]
    return [
        first,
        {"role": "model", "parts": [{"text": prev_answer}]},
        {"role": "user", "parts": [{"text": repair_message(issues, truncated)}]},
    ]
