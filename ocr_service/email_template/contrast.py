"""WCAG 2.x контраст и безопасные цветовые подсказки."""
import colorsys
import re
from typing import Optional, Tuple

NAMED = {
    "black": "#000000", "white": "#ffffff", "gray": "#808080", "grey": "#808080",
    "silver": "#c0c0c0", "navy": "#000080", "teal": "#008080", "green": "#008000",
    "maroon": "#800000", "purple": "#800080", "olive": "#808000", "blue": "#0000ff",
    "red": "#ff0000", "yellow": "#ffff00", "lime": "#00ff00", "aqua": "#00ffff",
    "fuchsia": "#ff00ff", "orange": "#ffa500",
}
_COLOR_RE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|\b(?:white|black|silver|gray|grey|navy|teal|green|"
    r"maroon|purple|olive|blue|red|yellow|lime|aqua|fuchsia|orange)\b",
    re.I,
)


def color_tokens(value: str) -> list:
    """Возвращает распознаваемые CSS-цвета из значений color/background."""
    return [item.group(0) for item in _COLOR_RE.finditer(value or "")]


def parse_color(value: str) -> Optional[Tuple[int, int, int]]:
    """Разбирает поддерживаемый CSS-цвет; прозрачные/контекстные цвета неизвестны."""
    value = (value or "").strip().lower()
    if value in {"transparent", "inherit", "currentcolor", "initial", "unset"}:
        return None
    value = NAMED.get(value, value)
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        value = "#" + "".join(ch * 2 for ch in value[1:])
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    match = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*([0-9.]+))?\s*\)", value)
    if match:
        channels = tuple(int(item) for item in match.groups()[:3])
        alpha = float(match.group(4)) if match.group(4) is not None else 1.0
        if max(channels) <= 255 and alpha >= 1:
            return channels
    return None


def luminance(color: Tuple[int, int, int]) -> float:
    """Относительная яркость sRGB."""
    vals = []
    for item in color:
        channel = item / 255.0
        vals.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def _single_ratio(foreground: str, background: str) -> Optional[float]:
    fg, bg = parse_color(foreground), parse_color(background)
    if fg is None or bg is None:
        return None
    first, second = luminance(fg), luminance(bg)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def ratio(foreground: str, background: str) -> Optional[float]:
    """Контраст пары; у градиента возвращает худший контраст из найденных остановок."""
    if not background:
        return _single_ratio(foreground, background)
    if "gradient(" not in background.lower():
        return _single_ratio(foreground, background)
    stops = [item.group(0) for item in _COLOR_RE.finditer(background)]
    values = [_single_ratio(foreground, stop) for stop in stops]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _style(node) -> dict:
    attrs = getattr(node, "attrs", {}) or {}
    return dict((m.group(1).strip().lower(), m.group(2).strip()) for m in
                re.finditer(r"([\w-]+)\s*:\s*([^;]+)", attrs.get("style", "")))


def node_fg(node) -> str:
    """Цвет текста с наследованием по предкам; CSS default — чёрный."""
    current = node
    while current is not None:
        value = _style(current).get("color")
        if value and value.strip().lower() not in {"inherit", "unset", "initial", "currentcolor"}:
            return value
        current = getattr(current, "parent", None)
    return "#000000"


def node_bg(node) -> str:
    """Ближайший фон с наследованием, bgcolor и background shorthand."""
    current = node
    while current is not None:
        styles = _style(current)
        value = styles.get("background-color") or getattr(current, "attrs", {}).get("bgcolor")
        if not value and styles.get("background"):
            background = styles["background"]
            if "gradient(" in background.lower():
                value = background
            else:
                colors = [item.group(0) for item in _COLOR_RE.finditer(background)]
                value = colors[0] if colors else None
        if value:
            return value
        current = getattr(current, "parent", None)
    return "#ffffff"


def suggest_fix(foreground: str, background: str, target: float = 4.5) -> Optional[str]:
    """Двигает светлоту цвета текста, сохраняя оттенок, к заданному контрасту."""
    fg = parse_color(foreground)
    if fg is None or ratio(foreground, background) is None:
        return None
    if ratio(foreground, background) >= target:
        return None
    red, green, blue = (channel / 255.0 for channel in fg)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    candidates = []
    for direction in (-1, 1):
        current = lightness
        for _ in range(100):
            current = min(1.0, max(0.0, current + direction * 0.01))
            rgb = colorsys.hls_to_rgb(hue, current, saturation)
            color = "#" + "".join("%02x" % round(channel * 255) for channel in rgb)
            contrast = ratio(color, background)
            if contrast is not None and contrast >= target:
                candidates.append((abs(current - lightness), color))
                break
    if candidates:
        return min(candidates)[1]
    bg = parse_color(background)
    if bg is None:
        return None
    red, green, blue = (channel / 255.0 for channel in bg)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    for direction in (-1, 1):
        current = lightness
        for _ in range(100):
            current = min(1.0, max(0.0, current + direction * 0.01))
            rgb = colorsys.hls_to_rgb(hue, current, saturation)
            color = "#" + "".join("%02x" % round(channel * 255) for channel in rgb)
            contrast = ratio(foreground, color)
            if contrast is not None and contrast >= target:
                candidates.append((abs(current - lightness), color))
                break
    return min(candidates)[1] if candidates else None


def suggest_background_fix(foreground: str, background: str, target: float = 4.5) -> Optional[str]:
    """Двигает светлоту фона (оттенок сохраняется), оставляя цвет текста прежним.

    Нужна для «чистого» белого/чёрного текста на цветной плашке: затемнять сам белый текст бессмысленно,
    правильнее чуть затемнить плашку (например, зелёный бейдж со знаком «$»).
    """
    bg = parse_color(background)
    if bg is None or ratio(foreground, background) is None:
        return None
    if ratio(foreground, background) >= target:
        return None
    red, green, blue = (channel / 255.0 for channel in bg)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    candidates = []
    for direction in (-1, 1):
        current = lightness
        for _ in range(100):
            current = min(1.0, max(0.0, current + direction * 0.01))
            rgb = colorsys.hls_to_rgb(hue, current, saturation)
            color = "#" + "".join("%02x" % round(channel * 255) for channel in rgb)
            contrast = ratio(foreground, color)
            if contrast is not None and contrast >= target:
                candidates.append((abs(current - lightness), color))
                break
    return min(candidates)[1] if candidates else None


def is_pure_text_color(value: str) -> bool:
    """True для почти белого или почти чёрного текста: такой цвет лучше не менять, а править фон."""
    rgb = parse_color(value)
    if rgb is None:
        return False
    _hue, lightness, _saturation = colorsys.rgb_to_hls(*(channel / 255.0 for channel in rgb))
    return lightness >= 0.92 or lightness <= 0.08


def is_red_orange(value: str) -> bool:
    """True для насыщенных красно-оранжевых цветов из палитры."""
    rgb = parse_color(value)
    if rgb is None:
        return False
    hue, lightness, saturation = colorsys.rgb_to_hls(*(channel / 255.0 for channel in rgb))
    degrees = hue * 360.0
    return (degrees >= 345 or degrees <= 45) and saturation >= 0.6 and 0.25 <= lightness <= 0.75


def is_warm_fill(value: str) -> bool:
    """True для красно-оранжевых и терракотовых заливок (шире is_red_orange: ловит приглушённые тона)."""
    rgb = parse_color(value)
    if rgb is None:
        return False
    hue, lightness, saturation = colorsys.rgb_to_hls(*(channel / 255.0 for channel in rgb))
    degrees = hue * 360.0
    return (degrees >= 345 or degrees <= 40) and saturation >= 0.35 and 0.3 <= lightness <= 0.75
