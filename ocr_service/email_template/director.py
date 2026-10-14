"""Серверный выбор дизайна письма из белых списков."""
import random
import secrets
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

def _palette(name, text, accent, soft, done, page):
    return (
        "{name}: заголовки и основной текст {text}; акцент {accent} (только для декора: полоски, точки, фоны без текста); "
        "светлый фон блока деталей {soft}; цвет «выполнено» {done} (белый текст на нём допустим); "
        "фон страницы {page}; основная таблица письма белая #ffffff. Других цветов не добавляй, "
        "красный, оранжевый, коралловый, тёмно-синий и серый не используй."
    ).format(name=name, text=text, accent=accent, soft=soft, done=done, page=page)


# Все пары проверены на контраст: текст на фоне блока и на странице не ниже 7:1, белый на цвете «выполнено» не ниже 4.8:1.
# Синий с белым и серый минимализм исключены условием задания, поэтому в палитрах нет тёмно-синих текстов.
PALETTES: Dict[str, str] = {
    "P1": _palette("хвойно-лимонная", "#1f4d3a", "#f2c94c", "#e8f5ec", "#2e7d4f", "#f6faf5"),
    "P2": _palette("бирюзово-сиреневая", "#0f5257", "#9b7fd1", "#e3f4f6", "#1f7f74", "#f5fafb"),
    "P3": _palette("сливово-небесная", "#5b2a5e", "#4fb3e8", "#f1e9f7", "#2f7d58", "#faf6fc"),
    "P4": _palette("оливково-малиновая", "#4a5a1a", "#d6458f", "#f1f5df", "#46802a", "#fafbf2"),
    "P5": _palette("изумрудно-золотая", "#0b5d45", "#f4c20d", "#eaf4e0", "#1f7a45", "#f8fbf3"),
    "P6": _palette("графитово-бирюзовая", "#26403a", "#19b5a5", "#e6f3f1", "#2a7f55", "#f6faf9"),
    "P7": _palette("фиолетово-салатовая", "#5a3d7a", "#b5e04a", "#f2ecf8", "#3a7d57", "#faf7fd"),
    "P8": _palette("бирюзово-розовая", "#0e5560", "#e0558f", "#e4f4f1", "#23805f", "#f5fbfa"),
    "P9": _palette("фисташково-малиновая", "#3b5b2a", "#c2358a", "#eef6e6", "#3a7f3f", "#f9fcf5"),
    "P10": _palette("индиго-мятная", "#4a3a8a", "#3cc9a0", "#efeafb", "#2a7d63", "#faf8fe"),
    "P11": _palette("морская с жёлтым", "#0b5a5a", "#e2c41d", "#f3f7e3", "#237d52", "#fbfcf4"),
    "P12": _palette("хвойно-фуксиевая", "#1d4d3d", "#d6458f", "#eaf5ef", "#2b7a4b", "#f6fbf8"),
}
# «заголовки / основной текст». Georgia встречается только в двух парах из восьми.
FONTS = [
    "заголовки Trebuchet MS, основной текст Verdana",
    "заголовки Tahoma, основной текст Arial",
    "заголовки Verdana, основной текст Trebuchet MS",
    "заголовки Arial, основной текст Verdana",
    "заголовки Helvetica, основной текст Tahoma",
    "заголовки Georgia, основной текст Verdana",
    "заголовки Tahoma, основной текст Trebuchet MS",
    "заголовки Georgia, основной текст Arial",
]
HEADERS = [
    "смещённая вправо шапка с цветной полоской слева",
    "шапка-плашка со скруглением и точкой-акцентом",
    "шапка с многоцветной полоской из 3–4 сегментов сверху",
    "шапка по центру с тонкой рамкой",
    "шапка с маленькой текст-свободной точкой в отдельной ячейке рядом с названием",
    "шапка из двух аккуратно разнесённых ячеек без наложения",
    "шапка с вертикальной чертой рядом с названием, не внутри текста",
    "шапка с цветной подложкой-маркером под названием",
]
# Каждый индикатор описан так, чтобы он собирался в почтовой вёрстке без искажений и без вида кнопок.
# Общее для всех: у первого этапа символ &#10003; и цвет «выполнено», второй этап жирный и с акцентом, третий приглушённый.
INDICATORS = [
    "три колонки в одной таблице: над подписью цветная полоска высотой 6px (без заливки под текстом, без рамок и теней), подпись обычным текстом, у первого этапа &#10003;",
    "точки с соединяющими линиями: каждая точка 14x14 лежит в собственной вложенной таблице width=\"14\" (атрибут и style), линия — отдельная ячейка width=\"40\" высотой 3px с bgcolor и &nbsp; внутри, подписи под точками",
    "прогресс-бар из трёх секций высотой 8px с подписями под ним",
    "три карточки со ступенчатым сдвигом через padding-left 0 / 16px / 32px, карточки без скруглений-таблеток",
    "круги с цифрами: каждый круг — собственная вложенная таблица width=\"28\" (атрибут и style) с одной ячейкой 28x28, line-height:28px, text-align:center, border-radius:14px; у первого вместо цифры &#10003;; подпись лежит в другой строке, а не в ячейке круга",
    "три строки-этапа друг под другом, слева у каждой квадрат-маркер 12x12 и цветная вертикальная полоска 3px у края строки",
    "три кружка с галочкой / точкой / пустым контуром: каждый кружок — собственная вложенная таблица width=\"26\" (атрибут и style) с одной ячейкой 26x26, line-height:26px; подпись лежит под ним в другой строке",
    "лента из трёх шагов без плашек: цветная линия 4px над каждой подписью (у первого этапа сплошная «выполнено», у второго акцентная, у третьего светлая), подписи обычным текстом",
]

# Тексты письма выдаёт сервер, а не модель: иначе она из раза в раз пишет одни и те же приветствие, заголовок и подпись.
# В текстах нет «ваш заказ» (S08), «спасибо за покупку» (S06), «прямо сейчас» (S01), обращения на «ты», склада, курьера и сроков.
GREETINGS = [
    "Привет, {{name}}",
    "{{name}}, заказ в системе",
    "{{name}}, заказ принят, дальше мы сами",
    "Добрый день, {{name}}",
    "Рады знакомству, {{name}}",
    "Вот и вы, {{name}}",
    "{{name}}, всё получилось",
    "Здравствуйте, {{name}}",
    "{{name}}, мы начали работу",
    "Приятно познакомиться, {{name}}",
]
HEADINGS = [
    "Следующие шаги",
    "Заглянем за кулисы",
    "Как мы готовим заказ",
    "Что будет дальше",
    "Закулисье заказа",
    "Маршрут заказа",
    "Три шага до отправки",
    "Как всё устроено",
    "От заявки до отправки",
    "Что мы делаем дальше",
]
SIGNOFFS = [
    "Спокойного дня от {{brandName}}",
    "Остаёмся на связи, {{brandName}}",
    "Всего хорошего, {{brandName}}",
    "Будем держать вас в курсе, {{brandName}}",
    "До связи, {{brandName}}",
    "Хорошего дня, {{brandName}}",
    "Пишите, если что-то непонятно, — {{brandName}}",
    "Мы рядом, {{brandName}}",
    "Берегите себя, {{brandName}}",
    "На связи, {{brandName}}",
]
VOICES = [
    "через конкретику: что именно будет сделано на каждом этапе, без общих слов",
    "первый абзац начни с вопроса, который читатель мог бы задать сам, и сразу ответь на него",
    "спокойно и уверенно: заказ принят, всё идёт по плану, без оправданий и восторгов",
    "от первого лица множественного числа: «мы проверяем», «мы сообщим», живыми короткими фразами",
    "коротко и по делу: два предложения без образов",
    "с одной лёгкой шуткой про ожидание, остальное строго по делу",
    "как сообщение коллеги: просто, тепло, без канцелярита и без пафоса",
    "через сравнение с обычной жизнью, но одно и только понятное",
]
CURRENCY_METHODS = list(range(1, 21))
HISTORY_DEFAULT = 12
_lock = threading.Lock()
_history: Deque[Tuple[str, str, str, int]] = deque(maxlen=HISTORY_DEFAULT)
_text_history: Deque[Tuple[str, str, str, str]] = deque(maxlen=HISTORY_DEFAULT)
# Совместимое имя для кода, который раньше читал историю напрямую.
HISTORY = _history


@dataclass
class DesignBrief:
    seed: int
    palette_family: str
    fonts: str
    header: str
    indicator: str
    currency_method: int
    avoid_palettes: List[str] = field(default_factory=list)
    greeting: str = ""
    heading: str = ""
    signoff: str = ""
    voice: str = ""

    def public(self) -> dict:
        values = asdict(self)
        values.pop("avoid_palettes", None)
        return values


def set_history_size(n: int) -> None:
    global _history, HISTORY, _text_history
    with _lock:
        _history = deque(list(_history), maxlen=max(int(n), 1))
        _text_history = deque(list(_text_history), maxlen=max(int(n), 1))
        HISTORY = _history


def reset_history() -> None:
    with _lock:
        _history.clear()
        _text_history.clear()


def _resolve(pool, value, name):
    if value is None:
        return None
    if isinstance(pool, dict):
        if isinstance(value, str) and value in pool:
            return pool[value]
        if value in pool.values():
            return value
    elif value in pool:
        return value
    raise ValueError("Недопустимое значение " + name)


def _pick(rng, pool, recent, depth):
    recent_values = set(recent[-depth:]) if depth > 0 else set()
    candidates = [value for value in pool if value not in recent_values] or list(pool)
    return rng.choice(candidates)


def pick_brief(seed: Optional[int] = None, currency_method: Optional[int] = None,
               overrides: Optional[dict] = None, record: bool = True) -> DesignBrief:
    overrides = overrides or {}
    if currency_method is not None and (
        isinstance(currency_method, bool) or not isinstance(currency_method, int)
        or currency_method not in CURRENCY_METHODS
    ):
        raise ValueError("currency_method должен быть 1..20")
    palette = _resolve(PALETTES, overrides.get("palette"), "palette")
    fonts = _resolve(FONTS, overrides.get("fonts"), "fonts")
    header = _resolve(HEADERS, overrides.get("header"), "header")
    indicator = _resolve(INDICATORS, overrides.get("indicator"), "indicator")
    seed = secrets.randbelow(2 ** 31) if seed is None else int(seed)
    rng = random.Random(seed)

    with _lock:
        history = list(_history)
        selected_palette = palette or _pick(
            rng, list(PALETTES.values()), [row[0] for row in history], min(5, len(PALETTES) - 1),
        )
        selected_fonts = fonts or rng.choice(FONTS)
        selected_header = header or _pick(
            rng, HEADERS, [row[1] for row in history], min(5, len(HEADERS) - 1),
        )
        selected_indicator = indicator or _pick(
            rng, INDICATORS, [row[2] for row in history], min(5, len(INDICATORS) - 1),
        )
        selected_method = currency_method or _pick(
            rng, CURRENCY_METHODS, [row[3] for row in history], 8,
        )
        # Тексты выбираются после дизайна, чтобы выбор палитры, шапки и индикатора по одному seed не менялся.
        texts = list(_text_history)
        selected_greeting = _resolve(GREETINGS, overrides.get("greeting"), "greeting") or _pick(rng, GREETINGS, [row[0] for row in texts], min(5, len(GREETINGS) - 1))
        selected_heading = _resolve(HEADINGS, overrides.get("heading"), "heading") or _pick(rng, HEADINGS, [row[1] for row in texts], min(5, len(HEADINGS) - 1))
        selected_signoff = _resolve(SIGNOFFS, overrides.get("signoff"), "signoff") or _pick(rng, SIGNOFFS, [row[2] for row in texts], min(5, len(SIGNOFFS) - 1))
        selected_voice = _resolve(VOICES, overrides.get("voice"), "voice") or _pick(rng, VOICES, [row[3] for row in texts], min(4, len(VOICES) - 1))
        brief = DesignBrief(
            seed=seed, palette_family=selected_palette, fonts=selected_fonts, header=selected_header,
            indicator=selected_indicator, currency_method=selected_method,
            avoid_palettes=[row[0] for row in history][-5:],
            greeting=selected_greeting, heading=selected_heading, signoff=selected_signoff, voice=selected_voice,
        )
        if record:
            _history.append((selected_palette, selected_header, selected_indicator, selected_method))
            _text_history.append((selected_greeting, selected_heading, selected_signoff, selected_voice))
        return brief


def make_brief(seed=None, currency_method=None, overrides=None):
    """Совместимость с HTTP-роутером: вернуть обычный JSON-совместимый словарь."""
    return asdict(pick_brief(seed, currency_method, overrides, record=True))
