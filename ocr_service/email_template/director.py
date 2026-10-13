"""Серверный выбор дизайна письма из белых списков."""
import random
import secrets
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

PALETTES: Dict[str, str] = {
    "P1": "хвойный зелёный текст, лимонно-жёлтый акцент, светло-мятный фон блока",
    "P2": "бирюзовый текст, сиреневый акцент, холодный светло-голубой фон блока",
    "P3": "сливовый текст, небесно-голубой акцент, светло-лавандовый фон блока",
    "P4": "морская синь текст, лаймовый акцент, светло-аквамариновый фон блока",
    "P5": "оливковый текст, малиновый декоративный акцент, светло-фисташковый фон блока",
    "P6": "изумрудный текст, золотисто-жёлтый акцент, светлый шалфейный фон блока",
    "P7": "графитово-зелёный текст, бирюзовый акцент, светлый мятно-ледяной фон блока",
    "P8": "черничный текст, мятный акцент, светло-голубой фон блока",
    "P9": "глубокий лазурный текст, подсолнечно-жёлтый акцент, светло-небесный фон блока",
    "P10": "тёмно-бирюзовый текст, коралловый декоративный акцент, светло-мятный фон блока",
    "P11": "пыльно-фиолетовый текст, салатовый акцент, светлый пудрово-лиловый фон блока",
    "P12": "хвойно-синий текст, пурпурно-малиновый декоративный акцент, светлый льдистый фон блока",
}
FONTS = [
    "Georgia + Verdana", "Trebuchet MS + Georgia", "Arial + Georgia", "Tahoma + Georgia",
    "Verdana + Trebuchet MS", "Helvetica + Georgia", "Georgia + Tahoma", "Trebuchet MS + Verdana",
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
INDICATORS = [
    "три сегмента-плашки с подписями внутри", "точки с соединяющими линиями",
    "прогресс-бар из трёх секций", "три карточки со ступенчатым сдвигом",
    "круги с цифрами и соединением", "вертикальная линия с тремя точками",
    "три кружка с галочкой/точкой/пустым кружком и подписями под ними",
    "лента из трёх скруглённых шагов с разной заливкой",
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
