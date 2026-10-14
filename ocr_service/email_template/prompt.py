"""Сборка системного и пользовательского запросов для генерации письма."""
import hashlib
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Sequence

PROMPT_VERSION = "1.5"
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


# Обороты, которые модель повторяла из письма в письмо в прогонах. Проверяется тестом.
OVERUSED_PHRASES = (
    "С уважением", "С теплом", "Рады вас приветствовать", "Что происходит…", "Путь вашего заказа",
    "Как продвигается…", "прямо сейчас", "Каждый этап проходит контроль", "отдел логистики / комплектации",
)


def _copy_block(brief: dict) -> str:
    """Тексты, выданные сервером. Старые задания без этих полей получают прежнюю общую подсказку."""
    greeting, heading, signoff = brief.get("greeting"), brief.get("heading"), brief.get("signoff")
    if not (greeting and heading and signoff):
        return (
            "Заголовок блока сообщения (h2) придумай свой, не «Что происходит сейчас», не «Статус заказа». "
            "Приветствие и подпись команды сформулируй по-новому, не «Здравствуйте, {{name}}» и не «С теплом, команда».\n"
            "Не используй обороты: " + ", ".join("«%s»" % item for item in OVERUSED_PHRASES) + ".\n"
        )
    block = (
        "ТЕКСТЫ ЭТОГО ПИСЬМА (используй дословно, ничего не заменяй синонимами):\n"
        "Приветствие — тег <h1>, 22–26px, ровно этот текст: " + greeting + "\n"
        "Заголовок блока сообщения — тег <h2>, ровно этот текст: " + heading + "\n"
        "Подпись — последний абзац с font-style:italic, ровно этот текст: " + signoff + "\n"
    )
    voice = brief.get("voice")
    if voice:
        block += "Манера остальных текстов: " + voice + "\n"
    block += (
        "Остальные тексты придумай сам. Не используй обороты: "
        + ", ".join("«%s»" % item for item in OVERUSED_PHRASES)
        + ". Не выдумывай подробности, которых ты не знаешь (склад, курьер, трек-номер, мастера, сроки). "
        "Восклицательных знаков во всём письме не больше одного. "
        "Слова «ваш заказ» и «статус заказа» не используй: пиши просто «заказ» или «покупка». "
        "Размер текста не меньше 15px, line-height от 1.4 до 1.6.\n"
    )
    return block


def build_user_message(brief: dict) -> str:
    brief = _brief_mapping(brief)
    head = (
        "ПАРАМЕТРЫ ЗАПУСКА (обязательны к исполнению, выбирать самому не нужно):\n"
        "Способ оформления знака: №{currency_method}\n"
        "Палитра (используй ровно эти hex-коды, свои цвета не добавляй): {palette_family}\n"
        "Шрифты: {fonts}\n"
        "Композиция шапки: {header}\n"
        "Тип статус-индикатора: {indicator}\n"
        "Номер запуска (для разнообразия формулировок): {seed}\n"
    ).format(
        currency_method=brief["currency_method"], palette_family=brief["palette_family"], fonts=brief["fonts"],
        header=brief["header"], indicator=brief["indicator"], seed=brief["seed"],
    )
    # Эти части добавляются без .format(): в них живые плейсхолдеры {{name}}, {{brandName}} и т.д.
    tail = (
        "Напоминание по частым ошибкам: в блоке деталей порядок строго «товар {{productName}}, номер заказа "
        "{{transactionalId}}, сумма {{price}}»; список из трёх пунктов в блоке сообщения — настоящий <ul> с тремя "
        "<li>, не строки таблицы и не символ «•». В блоке сообщения не больше двух абзацев <p> до списка, подпись — "
        "отдельным <p> после <hr>. Прехедер 50–110 символов. Кружки и точки индикатора делай вложенной таблицей "
        "фиксированной ширины, а не ячейкой в одной колонке с подписью. Знак валюты и сумму держи в одном абзаце "
        "обычными <span>, без вложенных таблиц и без padding/margin на <span>.\n"
        "Выполни задание из системной инструкции. Верни ответ в формате из раздела «ФОРМАТ ОТВЕТА»; "
        "файл template.html допустимо вернуть одним блоком ```html."
    )
    return head + _copy_block(brief) + tail


_REPAIR_INSTRUCTIONS = {
    "T17": (
        "После маркера <!-- Здесь вставляется тело письма --> в блоке сообщения должны быть по порядку: "
        "<h2>, минимум один абзац <p>, минимум один <strong>...</strong> внутри текста, настоящий "
        "маркированный список <ul> ровно с тремя <li> (не строки таблицы и не символы «•»), затем "
        "настоящий <hr style=\"border:none;border-top:1px solid ...;margin:...\"> и курсивная подпись "
        "(font-style:italic). Каждому тегу задай inline style. Не переписывай остальное письмо."
    ),
    "T18": (
        "Оставь ровно один <!-- Здесь вставляется тело письма --> и поставь его вплотную перед первым "
        "<h2> после {{transactionalId}}, внутри той же ячейки <td>; между маркером и <h2> не оставляй "
        "пробелов, тегов или декора. Полоску и отступ поставь до маркера."
    ),
    "T21": (
        "Проверь типографику в блоке сообщения: не оставляй пустых <p>, лишних пустых строк и "
        "повторяющихся пробелов. Сохрани смысл текста, поправь только разметку."
    ),
    "V02": (
        "Разрешены только плейсхолдеры: {{brandName}} {{name}} {{productName}} {{transactionalId}} "
        "{{price}} {{supportEmail}} {{email}}. Удали или замени любой другой {{...}} на обычный текст "
        "или один из разрешённых. Не придумывай новые переменные."
    ),
    "V07": (
        "Оставь ровно одну ссылку href=\"mailto:{{supportEmail}}\"; {{supportEmail}} должен встретиться "
        "только в этом href. Подпись ссылки — «Напишите нам», не адрес и не переменная; другие ссылки "
        "замени обычным текстом. Поставь {{email}} вне <a> в отдельный <p> или <span> после ссылки."
    ),
    "V08": (
        "Исправь оформление суммы/валюты: {{price}} должен встретиться ровно один раз, без лишнего "
        "символа $ рядом, без <sup>, без двойного знака валюты. Следуй currency_method из параметров."
    ),
    "V12": (
        "Выведи {{brandName}} ровно один раз в шапке как обычный переносимый текст; убери у окружающих "
        "элементов фиксированные width/height, круги и line-height меньше font-size. Акцент поставь рядом."
    ),
    "A01": (
        "Исправь контраст текста по сообщению валидатора: замени указанные цвета color/background "
        "на предложенные значения. Не меняй другие цвета и структуру. Если валидатор дал точные hex — "
        "используй их буквально."
    ),
    "A02": (
        "Исправь accessibility-разметку (контраст/структура) строго по тексту ошибки валидатора. "
        "Не перестраивай письмо целиком."
    ),
    "S01": (
        "Удали или перефразируй запрещённое слово/выражение из текста ошибки. Замени нейтральной "
        "формулировкой без маркетинговых/спам-триггеров. Остальной текст и разметку не трогай."
    ),
    "S04": (
        "Удали text-transform:uppercase и перепиши заглавные подписи/заголовки обычным регистром "
        "(кроме стандартных аббревиатур)."
    ),
    "S13": "Удали text-transform:uppercase; используй обычный регистр.",
    "P01": (
        "Замени красный, оранжевый или коралловый цвет на цвет из выданной палитры (акцент, цвет «выполнено» "
        "или основной текст). Остальное не меняй."
    ),
    "A03": (
        "Поставь символ &#10003; перед подписью первого этапа индикатора и закрась его цветом «выполнено». "
        "Остальное не меняй."
    ),
    "A05": (
        "Каждый круг задай строго квадратным: width и height одинаковые, line-height равен высоте, "
        "text-align:center, border-radius не меньше половины стороны. Остальное не меняй."
    ),
    "A06": (
        "Каждый круг индикатора помести в собственную вложенную таблицу фиксированной ширины "
        "(width равен стороне круга, одна ячейка), а подпись держи в другой строке или другой колонке. Остальное не меняй."
    ),
    "T10": (
        "Прехедер — первый элемент body, с полным набором скрывающих стилей, текст 50–110 символов "
        "({{transactionalId}} считай как 10 цифр) и в конце минимум шесть пар &nbsp;&zwnj;. Остальное не меняй."
    ),
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
        # Для A01/A02/S01 текст валидатора часто содержит точный фикс (hex, слово) — всегда включаем.
        message = str(issue.get("message", "")).strip()
        instruction = _REPAIR_INSTRUCTIONS.get(rule) or message
        item = "- [{rule}] Исправление: {instruction}".format(rule=rule, instruction=instruction)
        if message and rule in {"A01", "A02", "S01", "V02", "V08"} and message not in instruction:
            item += " | детали валидатора: " + message[:220]
        excerpt = str(issue.get("where") or "").strip()
        item += " — проблемный фрагмент: " + (excerpt[:200] if excerpt else "не локализован валидатором")
        entries.append(item)
    if compact:
        opening = (
            "Ты верстальщик email-писем. Перезапусти исправление: ниже — только обязательные нарушения. "
            "Исправь их точечно: не меняй палитру, шрифты, композицию, плейсхолдеры и тексты, "
            "если ошибка этого прямо не требует. Не собирай письмо с нуля.\n"
        )
    else:
        opening = (
            "Ты верстальщик email-писем. Тебе уже дан предыдущий HTML в истории диалога. "
            "Проверка нашла нарушения. Исправь ТОЛЬКО их точечно; дизайн, палитру, тексты, "
            "структуру таблиц и выбранные параметры не меняй, если ошибка этого не требует.\n"
        )
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
