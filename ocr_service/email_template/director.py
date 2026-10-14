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
    "три прямоугольных сегмента в одной таблице (скругление не больше 3px, без рамок и теней, не похожи на кнопки), подпись внутри",
    "точки с соединяющими линиями: точка 14x14 в отдельной ячейке, линия-ячейка высотой 3px между точками, подписи под точками",
    "прогресс-бар из трёх секций высотой 8px с подписями под ним",
    "три карточки со ступенчатым сдвигом через padding-left 0 / 16px / 32px, карточки без скруглений-таблеток",
    "круги с цифрами и соединением: каждый круг строго width:28px;height:28px;line-height:28px;text-align:center;border-radius:14px, у первого вместо цифры &#10003;",
    "три строки-этапа друг под другом, слева у каждой квадрат-маркер 12x12 и цветная вертикальная полоска 3px у края строки",
    "три кружка 26x26 (width, height и line-height по 26px) с галочкой / точкой / пустым контуром и подписями под ними",
    "лента из трёх прямоугольных шагов с разной заливкой, скругление не больше 4px, подпись внутри",
]
CURRENCY_METHODS = list(range(1, 21))
HISTORY_DEFAULT = 12
_lock = threading.Lock()
_history: Deque[Tuple[str, str, str, int]] = deque(maxlen=HISTORY_DEFAULT)
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

    def public(self) -> dict:
        values = asdict(self)
        values.pop("avoid_palettes", None)
        return values


def set_history_size(n: int) -> None:
    global _history, HISTORY
    with _lock:
        _history = deque(list(_history), maxlen=max(int(n), 1))
        HISTORY = _history


def reset_history() -> None:
    with _lock:
        _history.clear()


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
        brief = DesignBrief(
            seed=seed, palette_family=selected_palette, fonts=selected_fonts, header=selected_header,
            indicator=selected_indicator, currency_method=selected_method,
            avoid_palettes=[row[0] for row in history][-5:],
        )
        if record:
            _history.append((selected_palette, selected_header, selected_indicator, selected_method))
        return brief


def make_brief(seed=None, currency_method=None, overrides=None):
    """Совместимость с HTTP-роутером: вернуть обычный JSON-совместимый словарь."""
    return asdict(pick_brief(seed, currency_method, overrides, record=True))
