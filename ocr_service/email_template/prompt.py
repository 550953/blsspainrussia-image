"""Сборка системного и пользовательского запросов для генерации письма."""
import hashlib
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Sequence

PROMPT_VERSION = "1.2"
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


_REPAIR_INSTRUCTIONS = {
    "T18": (
        "Оставь ровно один <!-- Здесь вставляется тело письма --> и поставь его вплотную перед первым "
        "<h2> после {{transactionalId}}, внутри той же ячейки <td>; между маркером и <h2> не оставляй "
        "пробелов, тегов или декора. Полоску и отступ поставь до маркера."
    ),
    "V07": (
        "Оставь ровно одну ссылку href=\"mailto:{{supportEmail}}\"; {{supportEmail}} должен встретиться "
        "только в этом href. Подпись ссылки — «Напишите нам», не адрес и не переменная; другие ссылки "
        "замени обычным текстом. Поставь {{email}} вне <a> в отдельный <p> или <span> после ссылки."
    ),
    "V12": (
        "Выведи {{brandName}} ровно один раз в шапке как обычный переносимый текст; убери у окружающих "
        "элементов фиксированные width/height, круги и line-height меньше font-size. Акцент поставь рядом."
    ),
    "S04": (
        "Удали text-transform:uppercase и перепиши заглавные подписи/заголовки обычным регистром "
        "(кроме стандартных аббревиатур)."
    ),
    "S13": "Удали text-transform:uppercase; используй обычный регистр.",
    "S07": (
        "Удали обещание срока и замени его нейтральным сообщением без даты или длительности, "
        "например: «Мы сообщим вам, когда появится обновление»."
    ),
}


def build_repair_message(issues: list, cut_off=False, compact=False) -> str:
    order = {"T": 0, "S": 0, "V": 0, "X": 0, "L": 1, "A": 2}
    issues = [
        {"rule": item[0], "message": item[1], "where": item[2] if len(item) > 2 else ""}
        if isinstance(item, (tuple, list)) else item
        for item in issues
    ]
    issues = sorted(issues, key=lambda item: order.get(str(item.get("rule", "?"))[:1], 3))
    entries = []
    for issue in issues[:6 if compact else 12]:
        rule = str(issue.get("rule", "?"))
        instruction = _REPAIR_INSTRUCTIONS.get(rule) or str(issue.get("message", ""))
        item = "- [{rule}] Исправление: {instruction}".format(rule=rule, instruction=instruction)
        excerpt = str(issue.get("where") or "").strip()
        item += " — проблемный фрагмент: " + (excerpt[:200] if excerpt else "не локализован валидатором")
        entries.append(item)
    if compact:
        opening = (
            "Перезапусти исправление с исходных требований. Не продолжай предыдущую редакцию по памяти; "
            "исправь только обязательные пункты ниже.\n"
        )
    else:
        opening = "Проверка нашла нарушения. Исправь ТОЛЬКО их; дизайн, палитру, тексты и выбранные параметры не меняй.\n"
    return (
        opening
        + "Верни весь HTML целиком: полный документ от <!DOCTYPE html> до </html>, не фрагмент и не diff. "
        "Сохрани формат ответа: концепция, файл template.html целиком, строка с переменными.\n"
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
