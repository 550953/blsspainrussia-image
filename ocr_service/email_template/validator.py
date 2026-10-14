"""Детерминированная проверка HTML по требованиям шаблона письма."""
import html
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional

from ..config import (
    TEMPLATE_CONTRAST_SUGGEST, TEMPLATE_DISABLED_RULES, TEMPLATE_HTML_ERROR_BYTES, TEMPLATE_MAX_HTML_BYTES,
    TEMPLATE_STRICT_OUTLOOK,
)
from .contrast import (
    color_tokens, is_pure_text_color, is_red_orange, is_warm_fill, node_bg, node_fg, ratio,
    suggest_background_fix, suggest_fix,
)

ALLOWED_VARS = ("brandName", "name", "productName", "transactionalId", "price", "supportEmail", "email")
MARKER = "<!-- Здесь вставляется тело письма -->"
BANNED = (
    "urgent", "action required", "act now", "act fast", "limited time", "don't delay", "dont delay",
    "verify now", "verify immediately", "get paid now", "unlock funds", "funds on hold", "payment hold",
    "delays may apply", "within 24 hours", "last chance", "sold", "hot", "срочн*", "немедленн*", "прямо сейчас",
    "не откладыва*", "успейте", "успей", "быстрее", "последний шанс", "иначе потеря*",
    "в течение 24 час*", "средства под блокировк*", "заблокир*", "не упустите", "торопитесь", "поторопитесь",
)


@dataclass
class Issue:
    rule: str
    severity: str
    message: str
    where: str = ""


@dataclass
class Report:
    ok: bool
    errors: List[Issue] = field(default_factory=list)
    warnings: List[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [asdict(item) for item in self.errors],
            "warnings": [asdict(item) for item in self.warnings],
            "stats": self.stats,
        }


class _Node:
    def __init__(self, tag: str, attrs: dict, parent=None, start_pos: int = 0):
        self.tag, self.attrs, self.children = tag, attrs, []
        self.text = ""
        self.parent = parent
        self.start_pos = start_pos


class _Tree(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.tags = []
        self.comments = []
        self.source = source
        self._line_starts = [0]
        self._line_starts.extend(idx + 1 for idx, char in enumerate(source) if char == "\n")
        self.handle_data(" ")

    def _position(self):
        line, column = self.getpos()
        if line <= len(self._line_starts):
            return self._line_starts[line - 1] + column
        return len(self.source)

    def handle_starttag(self, tag, attrs):
        values = {k.lower(): (v or "") for k, v in attrs}
        node = _Node(tag.lower(), values, self.stack[-1], self._position())
        self.stack[-1].children.append(node)
        self.tags.append((tag.lower(), values))
        if tag.lower() not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID and len(self.stack) > 1:
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                break

    def handle_data(self, data):
        if len(self.stack) > 1:
            self.stack[-1].text += data

    def handle_comment(self, data):
        self.comments.append(data.strip())


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node):
    result = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.text:
            result.append(current.text)
        stack.extend(reversed(current.children))
    return "".join(result)


def _outer_footer_row(mail_node, main_table=None, after=-1):
    """Внешний tr, в котором находится mailto-ссылка (строка-подвал).

    Если письмо целиком лежит в одной ячейке основной таблицы (вложенная таблица внутри
    единственного tr), самый внешний tr начинается ДО маркера и содержит всё письмо.
    Поэтому при заданной позиции маркера (after >= 0) берутся только строки, которые
    начинаются после него. Нет такой строки -> None (подвал считается до конца документа).
    """
    rows = []
    current = mail_node
    while current is not None and current is not main_table:
        if current.tag == "tr":
            rows.append(current)
        current = current.parent
    if after >= 0:
        rows = [row for row in rows if row.start_pos > after]
    return rows[-1] if rows else None


def _style(node):
    return dict((m.group(1).strip().lower(), m.group(2).strip()) for m in re.finditer(r"([\w-]+)\s*:\s*([^;]+)", node.attrs.get("style", "")))


def _declared_px(node, name: str):
    """Ширина/высота в px из style или числового атрибута td; None, если не задана."""
    found = re.fullmatch(r"(\d+(?:\.\d+)?)px", _style(node).get(name, "").strip(), re.I)
    if found:
        return float(found.group(1))
    if node.tag == "td":
        raw = str(node.attrs.get(name, "")).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return float(raw)
    return None


def round_cell_side(node):
    """Сторона круглой ячейки td до 80px (почти квадрат с радиусом от половины стороны) или None."""
    if node.tag != "td":
        return None
    radius_text = _style(node).get("border-radius", "").strip()
    width, height = _declared_px(node, "width"), _declared_px(node, "height")
    if not (radius_text and width and height) or width > 80 or height > 80 or min(width, height) / max(width, height) < 0.6:
        return None
    first = radius_text.split()[0]
    if first.endswith("%"):
        round_shape = float(re.sub(r"[^\d.]", "", first) or 0) >= 50
    else:
        round_shape = bool(re.fullmatch(r"\d+(?:\.\d+)?px", first, re.I)) and float(first[:-2]) >= max(width, height) / 2 - 0.5
    return max(width, height) if round_shape else None


def _table_rows(table):
    """Строки таблицы: прямые tr и tr внутри tbody/thead/tfoot."""
    rows = []
    for child in table.children:
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in {"tbody", "thead", "tfoot"}:
            rows.extend(grand for grand in child.children if grand.tag == "tr")
    return rows


def _row_cells(row):
    return [cell for cell in row.children if cell.tag in {"td", "th"}]


def find_stretched_circles(nodes) -> list:
    """Круглые td, которые растянутся в овал: в той же колонке другой строки лежит ячейка с текстом.

    Колонка берёт ширину самой широкой ячейки, поэтому width у td-круга не работает.
    Лечится вложенной таблицей фиксированной ширины вокруг круга (автофикс AF30).
    """
    found = []
    for table in (node for node in nodes if node.tag == "table"):
        rows = _table_rows(table)
        grid = [_row_cells(row) for row in rows]
        for r_idx, cells in enumerate(grid):
            for c_idx, cell in enumerate(cells):
                if round_cell_side(cell) is None:
                    continue
                for o_idx, other_cells in enumerate(grid):
                    if o_idx == r_idx or c_idx >= len(other_cells):
                        continue
                    other = other_cells[c_idx]
                    if _text(other).replace("\u00a0", " ").strip() and "table" not in {child.tag for child in other.children}:
                        found.append(cell)
                        break
    return found


def _issue(report, rule, severity, message, where=""):
    if rule in TEMPLATE_DISABLED_RULES and severity == "W":
        return
    target = report.errors if severity == "E" else report.warnings
    target.append(Issue(rule, severity, message, re.sub(r"\s+", " ", str(where)).strip()[:200]))


def _fragment(source: str, start: int, end: Optional[int] = None, limit: int = 200) -> str:
    """Return a compact source excerpt around a validator finding."""
    if not source:
        return ""
    start = max(0, min(len(source), int(start)))
    end = start if end is None else max(start, min(len(source), int(end)))
    left = max(0, start - limit // 3)
    right = min(len(source), max(end, start + 1) + (limit - (start - left)))
    excerpt = re.sub(r"\s+", " ", source[left:right]).strip()
    if left:
        excerpt = "…" + excerpt
    if right < len(source):
        excerpt += "…"
    return excerpt[:limit]


_WHERE_LIMIT = 120
_CONTENT_PATTERNS = {
    "S06": r"ваш заказ оформлен|спасибо за покупку|мы рады сообщить|уважаемый клиент",
    "S08": r"\b(?:статус заказа|ваш заказ)\b",
    "S10": r"\b(ты|тебе|тебя|твой|твоя|твое|твои|тобой)\b",
    "S11": r"\b(?:нажмите|кликните|жмите|click\s+here)\b",
    "S12": r"\b(?:акци\w*|скидк\w*|распродаж\w*|sale|discount|offer)\b",
    "S02": r"!",
}
_SOURCE_PATTERNS = {
    "V01": r"<title\b", "V03": r"<title\b", "T11": r"border-radius", "T17": r"<ul\b", "T01": r"<!doctype|<html\b",
    "S05": r"<button\b|<input\b", "T25": r"font-size\s*:\s*[\d.]+(?:pt|em|rem|%)", "T07": r"<img\b", "T09": r"<img\b",
    "T08": r"<a\b", "T06": r"<(?:div|section|article|header|footer)\b",
}


def _fill_where(report, source, content_text, nodes, preheader):
    """Каждому замечанию без фрагмента подбирает короткий фрагмент (до 120 символов): по одному коду не понять, что сработало."""
    def around(text, match):
        return _fragment(text, match.start(), match.end(), _WHERE_LIMIT)

    def locate(rule):
        if rule == "V02":
            bad = re.search(r"\{\{\{|\{%|\$\{|\[\[|&#123;|\{\s|\s\}|(?<!\{)\{(?!\{)|(?<!\})\}(?!\})", source)
            if bad:
                return around(source, bad)
            allowed = {"{{" + name + "}}" for name in ALLOWED_VARS}
            for token in re.finditer(r"\{\{[\s\S]*?\}\}", source):
                if token.group(0) not in allowed:
                    return around(source, token)
        elif rule == "S01":
            for term in BANNED:
                phrase = term[:-1] if term.endswith("*") else term
                pattern = re.escape(phrase).replace(r"\ ", r"\s+") + (r"\w*" if term.endswith("*") else "")
                hit = re.search(r"(?<!\w)" + pattern + r"(?!\w)", content_text)
                if hit:
                    return around(content_text, hit)
        elif rule in _CONTENT_PATTERNS:
            hit = re.search(_CONTENT_PATTERNS[rule], content_text)
            if hit:
                return around(content_text, hit)
        elif rule == "T10":
            if preheader:
                return _fragment(source, preheader.start(), preheader.end(), _WHERE_LIMIT)
            body = re.search(r"<body\b[^>]*>", source, re.I)
            if body:
                return around(source, body)
        elif rule == "A06":
            for cell in find_stretched_circles(nodes):
                pos = getattr(cell, "start_pos", None)
                if pos is not None:
                    return _fragment(source, pos, None, _WHERE_LIMIT)
            hit = re.search(r"border-radius", source)
            if hit:
                return around(source, hit)
        elif rule in _SOURCE_PATTERNS:
            hit = re.search(_SOURCE_PATTERNS[rule], source, re.I)
            if hit:
                return around(source, hit)
        return "весь документ: " + _fragment(source, 0, None, 60)

    for issues in (report.errors, report.warnings):
        for issue in issues:
            if not issue.where:
                issue.where = locate(issue.rule)[:_WHERE_LIMIT]
            else:
                issue.where = issue.where[:_WHERE_LIMIT]



def _contrast_message(label, foreground, background, observed):
    message = f"{label}: контраст {observed:.2f}:1, требуется не менее 4.5:1."
    if TEMPLATE_CONTRAST_SUGGEST:
        if is_pure_text_color(foreground):
            # Белый/чёрный текст на цветной плашке: правим плашку, а не текст.
            bg_suggested = suggest_background_fix(foreground, background, 4.5)
            if bg_suggested:
                return message + f" Замените цвет фона {background} на {bg_suggested}, цвет текста {foreground} оставьте."
        suggested = suggest_fix(foreground, background, 4.5)
        if suggested:
            if ratio(suggested, background) is not None and ratio(suggested, background) >= 4.5:
                message += f" Замените цвет текста {foreground} на {suggested}."
            elif ratio(foreground, suggested) is not None and ratio(foreground, suggested) >= 4.5:
                message += f" Замените цвет фона {background} на {suggested}."
    return message


def validate(source: str, currency_method: Optional[int] = None) -> Report:
    source = (source or "").lstrip("\ufeff \t\r\n")
    report = Report(ok=False)
    byte_size = len(source.encode("utf-8"))
    report.stats = {"bytes": byte_size, "tables": len(re.findall(r"<table\b", source, re.I)),
                    "placeholders": 0, "colors": len(set(re.findall(r"#[0-9a-fA-F]{3,8}\b", source))),
                    "contrast_checked": 0, "contrast_unverified": 0}
    if byte_size > TEMPLATE_MAX_HTML_BYTES:
        _issue(report, "T23", "E", "Размер HTML превышает установленный лимит")
        return report
    if byte_size > TEMPLATE_HTML_ERROR_BYTES:
        _issue(report, "T23", "E", "HTML слишком велик для надёжной доставки в почтовых клиентах")
    elif byte_size > 80000:
        _issue(report, "T23", "W", "Размер HTML приближается к лимиту обрезки Gmail")

    try:
        tree = _Tree(source)
        tree.feed(source)
        tree.close()
    except Exception:
        _issue(report, "T21", "E", "HTML не удалось безопасно разобрать")
        return report
    nodes = list(_walk(tree.root))
    tags = [node.tag for node in nodes]
    lower = source.lower()
    normalized_text = html.unescape(re.sub(r"<[^>]*>", " ", source))
    visible_case_text = html.unescape(re.sub(r"<[^>]*>", " ", source))
    for invisible in ("\u00a0", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
        normalized_text = normalized_text.replace(invisible, " ")
        visible_case_text = visible_case_text.replace(invisible, " ")
    normalized_text = normalized_text.casefold().replace("ё", "е")
    normalized_text = re.sub(r"\{\{[^}]*\}\}", " ", normalized_text)
    auxiliary_text = " ".join(
        value for node in nodes for name, value in node.attrs.items() if name in {"alt", "title"}
    )
    content_text = (normalized_text + " " + html.unescape(auxiliary_text)).replace("ё", "е").casefold()
    content_case_text = visible_case_text + " " + html.unescape(auxiliary_text)

    # Переменные и плейсхолдеры
    allowed_tokens = ["{{" + name + "}}" for name in ALLOWED_VARS]
    for name, token in zip(ALLOWED_VARS, allowed_tokens):
        if token not in source:
            _issue(report, "V01", "E", "Отсутствует обязательная переменная " + token)
    found = re.findall(r"\{\{[\s\S]*?\}\}", source)
    report.stats["placeholders"] = len(found)
    bad = [token for token in found if token not in allowed_tokens]
    if bad or re.search(r"\{\s|\s\}|\{\{\{|{%|\$\{|\[\[|&#123;|(?<!\{)\{(?!\{)|(?<!\})\}(?!\})", source):
        _issue(report, "V02", "E", "Обнаружен неизвестный или некорректный плейсхолдер")
    titles = [node for node in nodes if node.tag == "title"]
    if not titles or "{{productName}}" not in _text(titles[0]):
        _issue(report, "V03", "E", "{{productName}} должен быть в <title>")
    marker_matches = list(re.finditer(r"<!--\s*Здесь\s+вставляется\s+тело\s+письма\s*-->", source, re.I))
    marker_pos = marker_matches[0].start() if marker_matches else -1
    body_start_pos = next((node.start_pos for node in nodes if node.tag == "body"), 0)
    detail_start = source.find("{{productName}}", body_start_pos)
    if detail_start < 0 or (marker_pos >= 0 and detail_start >= marker_pos):
        detail_start = -1
    message_heading = next(
        (node for node in nodes if node.tag == "h2" and node.start_pos > source.find("{{transactionalId}}")),
        next((node for node in nodes if node.tag == "h2" and node.start_pos > body_start_pos), None),
    )
    message_cell = message_heading
    while message_cell is not None and message_cell.tag != "td":
        message_cell = message_cell.parent
    if not marker_matches:
        heading_at = message_heading.start_pos if message_heading else max(0, body_start_pos)
        _issue(
            report, "T18", "E", "Нет обязательного комментария-маркера",
            _fragment(source, heading_at, heading_at + 120),
        )
    elif len(marker_matches) != 1:
        duplicate = marker_matches[1]
        _issue(
            report, "T18", "E", "Маркер должен присутствовать ровно один раз",
            _fragment(source, duplicate.start(), duplicate.end()),
        )
    elif not re.match(r"<h2\b", source[marker_matches[0].end():], re.I):
        marker = marker_matches[0]
        _issue(
            report, "T18", "E", "Маркер должен стоять вплотную перед <h2> сообщения",
            _fragment(source, marker.start(), marker.end() + 80),
        )
    elif (
        message_heading is None
        or message_cell is None
        or marker_matches[0].end() != message_heading.start_pos
    ):
        marker = marker_matches[0]
        _issue(
            report, "T18", "E", "Маркер и <h2> сообщения должны стоять вплотную в одной ячейке",
            _fragment(source, marker.start(), marker.end() + 80),
        )
    body_before_marker = source[:marker_pos] if marker_pos >= 0 else source
    if detail_start < 0:
        _issue(report, "V04", "E", "{{productName}} должен быть в деталях заказа")
    if "{{brandName}}" not in body_before_marker or body_before_marker.find("{{brandName}}") > body_before_marker.find("{{name}}") >= 0:
        _issue(report, "V05", "E", "{{brandName}} должен быть в шапке до приветствия")
    header_end = source.find("{{name}}", body_start_pos)
    if header_end < 0:
        header_end = marker_pos if marker_pos >= 0 else len(source)
    header_brand_matches = [
        item for item in re.finditer(r"\{\{brandName\}\}", source[body_start_pos:header_end])
    ]
    if len(header_brand_matches) > 1:
        duplicate_brand = header_brand_matches[1]
        absolute_start = body_start_pos + duplicate_brand.start()
        _issue(
            report, "V12", "W", "{{brandName}} должен встречаться в шапке только один раз",
            _fragment(source, absolute_start, absolute_start + len("{{brandName}}")),
        )
    brand_node = next(
        (
            node for node in nodes
            if "{{brandName}}" in node.text and body_start_pos <= node.start_pos < header_end
        ),
        None,
    )
    if brand_node is not None:
        ancestors = []
        current = brand_node
        while current is not None and current.tag not in {"body", "html", "root", "table", "tr"}:
            ancestors.append(current)
            current = current.parent

        def fixed_dimension(value):
            return bool(re.fullmatch(r"\s*\d+(?:\.\d+)?(?:px|pt|em|rem)?\s*", value or "", re.I))

        geometry_problem = False
        font_size_px = None
        line_height_px = None
        line_height_ratio = None
        rounded_circle = False
        for node in ancestors:
            style = _style(node)
            if any(
                fixed_dimension(style.get(key, "")) or fixed_dimension(node.attrs.get(key, ""))
                for key in ("width", "height")
            ):
                geometry_problem = True
            radius = style.get("border-radius", "")
            rounded_circle = rounded_circle or bool(re.search(r"(?:50\s*%|9999(?:px|pt)?)", radius, re.I))
            if font_size_px is None and (match := re.fullmatch(r"\s*(\d+(?:\.\d+)?)px\s*", style.get("font-size", ""), re.I)):
                font_size_px = float(match.group(1))
            if line_height_px is None and (match := re.fullmatch(r"\s*(\d+(?:\.\d+)?)px\s*", style.get("line-height", ""), re.I)):
                line_height_px = float(match.group(1))
            if line_height_ratio is None and (match := re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", style.get("line-height", ""))):
                line_height_ratio = float(match.group(1))
        if rounded_circle or (
            font_size_px is not None and line_height_px is not None and line_height_px < font_size_px
        ) or (line_height_ratio is not None and line_height_ratio < 1):
            geometry_problem = True
        if geometry_problem:
            brand_start = source.find("{{brandName}}", brand_node.start_pos)
            _issue(
                report, "V12", "W",
                "Уберите фиксированные width/height, круг или line-height меньше текста вокруг {{brandName}}",
                _fragment(source, brand_start, brand_start + len("{{brandName}}")),
            )
    body_name_pos = source.find("{{name}}", body_start_pos)
    body_transaction_pos = source.find("{{transactionalId}}", body_name_pos + 1) if body_name_pos >= 0 else -1
    if not (0 <= body_name_pos < body_transaction_pos < marker_pos and marker_pos >= 0):
        _issue(report, "V06", "E", "Приветствие и детали заказа должны содержать имя и номер")
    anchors = [node for node in nodes if node.tag == "a"]
    support_anchors = [
        node for node in anchors
        if node.attrs.get("href", "").strip() == "mailto:{{supportEmail}}"
    ]
    source_anchors = list(re.finditer(r"<a\b[^>]*>[\s\S]*?</a\s*>", source, re.I))
    support_source = next(
        (
            item for item in source_anchors
            if re.search(r"\bhref\s*=\s*([\"'])mailto:\{\{supportEmail\}\}\1", item.group(0), re.I)
        ),
        None,
    )
    support_label = _text(support_anchors[0]).strip() if support_anchors else ""
    if len(anchors) != 1 or len(support_anchors) != 1:
        duplicate = next(
            (
                item for item in source_anchors
                if not re.search(
                    r"\bhref\s*=\s*([\"'])mailto:\{\{supportEmail\}\}\1",
                    item.group(0), re.I,
                )
            ),
            source_anchors[1] if len(source_anchors) > 1 else (
                source_anchors[0] if source_anchors else None
            ),
        )
        where = _fragment(source, duplicate.start(), duplicate.end()) if duplicate else (
            _fragment(source, body_start_pos, body_start_pos + 100)
        )
        _issue(
            report, "V07", "E",
            "Нужен ровно один <a> и его href должен быть mailto:{{supportEmail}}",
            where,
        )
    if support_anchors and "{{supportEmail}}" in support_label:
        support_match = next(
            (
                item for item in source_anchors
                if re.search(
                    r"\bhref\s*=\s*([\"'])mailto:\{\{supportEmail\}\}\1",
                    item.group(0), re.I,
                )
            ),
            None,
        )
        _issue(
            report, "V07", "W",
            "Предпочтительнее обычная подпись ссылки; {{supportEmail}} в её тексте допустим",
            _fragment(source, support_match.start(), support_match.end()) if support_match else "",
        )

    anchor_spans = [(match.start(), match.end()) for match in source_anchors]
    email_locations = [match.start() for match in re.finditer(r"\{\{email\}\}", source)]
    support_end = support_source.end() if support_source else -1
    allowed_email_tags = {"td", "p", "span", "div"}
    invalid_email_locations = []
    for position in email_locations:
        in_anchor = any(start <= position < end for start, end in anchor_spans)
        allowed_text_node = False
        for node in nodes:
            if node.tag not in allowed_email_tags or "{{email}}" not in node.text:
                continue
            first_email = source.find("{{email}}", node.start_pos)
            if first_email < 0 or first_email > position:
                continue
            ancestor = node
            while ancestor is not None and ancestor.tag != "a":
                ancestor = ancestor.parent
            if ancestor is None:
                allowed_text_node = True
                break
        if in_anchor or position <= support_end or not allowed_text_node:
            invalid_email_locations.append(position)
    if not email_locations or invalid_email_locations:
        offending = invalid_email_locations[0] if invalid_email_locations else (
            support_anchors[0].start_pos if support_anchors else body_start_pos
        )
        _issue(
            report, "V07", "E",
            "{{email}} должен находиться вне <a> и после ссылки в td, p, span или div",
            _fragment(source, offending, offending + len("{{email}}")),
        )
    if any(re.search(r"mailto:[^\"'>]*\?", node.attrs.get("href", ""), re.I) for node in anchors):
        _issue(report, "V09", "W", "Уберите параметры запроса из mailto-ссылки")
    price_at = source.find("{{price}}")
    # Знак и сумма часто лежат в соседних ячейках вложенных таблиц, поэтому смотрим на видимый текст
    # всего документа со свёрнутыми пробелами, а не на одну <tr> и не на «сырые» символы с отступами.
    visible_all = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", source)))
    has_currency_sign = (
        re.search(r"\$\s*\{\{price\}\}|\{\{price\}\}[\s\S]{0,40}\$", visible_all, re.I)
        if currency_method in (8, 15)
        else re.search(r"\$\s*\{\{price\}\}", visible_all, re.I)
    )
    if not has_currency_sign and not (
        currency_method == 15 and re.search(r"\{\{price\}\}[\s\S]{0,40}\bUSD\b", visible_all, re.I)
    ):
        _issue(report, "V08", "E", "Рядом с суммой должен быть знак $ или подпись USD")
    if price_at >= 0:
        price_node = next((node for node in nodes if "{{price}}" in node.text), None)
        current = price_node
        font_size = None
        while current is not None and font_size is None:
            font_size = _style(current).get("font-size")
            current = current.parent
        price_size = re.search(r"(\d+(?:\.\d+)?)\s*px", font_size or "", re.I)
        if not price_size or not 24 <= float(price_size.group(1)) <= 30:
            _issue(report, "V11", "W", "Для суммы рекомендуется размер текста 24–30 px")

    # Безопасность и почтовая разметка
    if re.search(r"<style\b", source, re.I): _issue(report, "T01", "E", "Использовать только inline-стили")
    if re.search(r"\bclass\s*=", source, re.I): _issue(report, "T02", "E", "Атрибут class запрещён")
    blocked = {"img", "svg", "picture", "video", "audio", "canvas", "iframe", "object", "embed", "form", "input", "select", "textarea", "script", "link", "base", "noscript", "button"}
    for tag in sorted(blocked.intersection(tags)):
        _issue(report, "T03", "E", "Запрещённый тег <" + tag + ">")
    for node in nodes:
        attrs = node.attrs
        external_values = [value for name, value in attrs.items() if name in {"href", "src", "action"}]
        style_value = attrs.get("style", "")
        if any(re.search(r"(?:https?:)?//|javascript:|data:", value, re.I) and not re.search(r"^mailto:", value.strip(), re.I)
               for value in external_values):
            _issue(report, "T05", "E", "Внешние адреса и схемы запрещены", node.tag)
        if re.search(r"url\s*\(\s*['\"]?(?:https?:)?//|url\s*\(\s*['\"]?(?:javascript|data):", style_value, re.I):
            _issue(report, "T05", "E", "Внешние ресурсы в CSS запрещены", node.tag)
        st = style_value.lower()
        if re.search(r"display\s*:\s*(?:inline-flex|flex|grid|inline-grid)|position\s*:\s*(?:absolute|fixed|sticky)|@media|@import|@font-face|url\s*\(|expression\s*\(|var\s*\(", st):
            _issue(report, "T04", "E", "Недопустимое CSS-свойство", node.tag)
        if re.search(r"(?:^|;)\s*float\s*:", st):
            _issue(report, "T04", "W", "Избегайте float в почтовой вёрстке", node.tag)
    if not re.match(r"\s*<!doctype\s+html", source, re.I): _issue(report, "T06", "E", "Документ должен начинаться с <!DOCTYPE html>")
    html_nodes = [node for node in nodes if node.tag == "html"]
    if len(html_nodes) != 1 or html_nodes[0].attrs.get("lang", "").lower() != "ru": _issue(report, "T06", "E", "Нужен один <html lang=\"ru\">")
    if len([n for n in nodes if n.tag == "head"]) != 1 or len([n for n in nodes if n.tag == "body"]) != 1: _issue(report, "T06", "E", "Нужны ровно по одному head и body")
    if not re.search(r'<meta\b[^>]*charset\s*=\s*["\']?utf-8', source, re.I): _issue(report, "T06", "E", "Отсутствует meta charset UTF-8")
    viewport_ok = any(
        node.tag == "meta" and node.attrs.get("name", "").lower() == "viewport"
        and "width=device-width" in node.attrs.get("content", "").replace(" ", "").lower()
        for node in nodes
    )
    if not viewport_ok: _issue(report, "T06", "E", "Отсутствует viewport для мобильных")
    if not re.search(r"X-UA-Compatible", source, re.I): _issue(report, "T06", "E", "Отсутствует X-UA-Compatible")
    for table in (node for node in nodes if node.tag == "table"):
        if table.attrs.get("role") != "presentation" or table.attrs.get("cellpadding") != "0" or table.attrs.get("cellspacing") != "0" or table.attrs.get("border") != "0":
            _issue(report, "T08", "E", "У каждой таблицы должны быть email-атрибуты role/cellpadding/cellspacing/border")
    main_table = next(
        (node for node in nodes if node.tag == "table" and re.fullmatch(r"\d+", node.attrs.get("width", ""))
         and 520 <= int(node.attrs["width"]) <= 600), None
    )
    if main_table is None:
        _issue(report, "T07", "E", "Основная таблица должна иметь ширину 520–600 px")
    else:
        table_width = int(main_table.attrs["width"])
        style = _style(main_table)
        max_width = re.fullmatch(r"(\d+(?:\.\d+)?)px", style.get("max-width", ""), re.I)
        width_percent = re.fullmatch(r"(\d+(?:\.\d+)?)%", style.get("width", ""), re.I)
        if not 520 <= table_width <= 600 or not max_width or float(max_width.group(1)) != table_width or not width_percent or float(width_percent.group(1)) != 100:
            _issue(report, "T07", "E", "Основная таблица должна иметь width 520–600 и width:100% с совпадающим max-width")
    body_node = next((node for node in nodes if node.tag == "body"), None)
    for label, node in (("body", body_node), ("основная таблица", main_table)):
        if node is not None and (
            not node.attrs.get("bgcolor")
            or not _style(node).get("background-color")
        ):
            _issue(report, "T09", "E", "Для " + label + " нужны bgcolor и background-color")
    nonconditional_comments = [
        comment for comment in tree.comments
        if not re.match(r"(?:\[if\b|<!\[endif\])", comment, re.I)
    ]
    if nonconditional_comments != ["Здесь вставляется тело письма"]:
        _issue(report, "T19", "E", "Разрешён только обязательный HTML-комментарий перед телом сообщения")
    if len(titles) != 1 or not _text(titles[0]).strip(): _issue(report, "T20", "E", "Нужен непустой title")
    if re.search(r"<ol\b", source, re.I): _issue(report, "T24", "E", "Для шагов используйте маркированный список")
    if len([node for node in nodes if node.tag == "a"]) and any(not _style(n).get("color") or _style(n).get("text-decoration", "").lower() != "underline" for n in anchors):
        _issue(report, "T22", "E", "Ссылки должны иметь цвет и подчёркивание")
    if len(re.findall(r"<table\b", source, re.I)) > 60: _issue(report, "T25", "W", "В письме более 60 таблиц")
    if any(
        "font-size" in _style(node) and not re.search(r"(?:px|rem|em|%)\s*$", _style(node)["font-size"], re.I)
        for node in nodes
    ):
        _issue(report, "T25", "W", "Задавайте размер шрифта в px")
    preheader = re.search(
        r"<body\b[^>]*>(?:(?:\s)|<!--[\s\S]*?-->)*<(div|span)\b([^>]*)>([\s\S]*?)</\1\s*>",
        source, re.I,
    )
    if not preheader:
        _issue(report, "T10", "E", "Скрытый прехедер должен быть первым элементом body")
    else:
        pre_style = re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", preheader.group(2), re.I | re.S)
        style_text = (pre_style.group(2) if pre_style else "").lower().replace(" ", "")
        required = ("display:none", "font-size:1px", "line-height:1px", "max-height:0", "max-width:0",
                    "opacity:0", "overflow:hidden", "mso-hide:all")
        missing_styles = [item for item in required if item not in style_text]
        if missing_styles:
            _issue(report, "T10", "E", "Прехедер должен содержать полный набор скрывающих стилей; не хватает: " + ", ".join(missing_styles))
        visible = re.sub(r"&nbsp;&zwnj;|&#160;&#8204;", "", preheader.group(3), flags=re.I)
        visible = html.unescape(re.sub(r"<[^>]*>", "", visible)).replace("{{transactionalId}}", "1234567890").strip()
        report.stats["preheader_chars"] = len(visible)
        if not 50 <= len(visible) <= 110:
            _issue(report, "T10", "E", "Текст прехедера должен быть длиной 50–110 символов; сейчас %d (с {{transactionalId}} как 10 цифр)" % len(visible))
        pair_count = len(re.findall(r"(?:&nbsp;&zwnj;|&#160;&#8204;)", preheader.group(3), re.I))
        report.stats["preheader_pairs"] = pair_count
        if pair_count < 3:
            _issue(report, "T10", "E", "В конце прехедера нужны минимум три пары &nbsp;&zwnj;; сейчас %d" % pair_count)
    email_node = next((node for node in nodes if "{{email}}" in node.text), None)
    mail_node = anchors[0] if anchors else None
    footer_root = _outer_footer_row(mail_node, main_table, marker_pos) if mail_node else None
    footer_nodes = set()
    if footer_root is not None:
        for node in nodes:
            current = node
            while current is not None and current is not footer_root:
                current = current.parent
            if current is footer_root:
                footer_nodes.add(id(node))
    else:
        current = email_node
        while current is not None:
            footer_nodes.add(id(current))
            current = current.parent
    preheader_node = next(
        (node for node in nodes if node.tag in {"div", "span"} and _style(node).get("display", "").replace(" ", "").lower() == "none"
         and _style(node).get("font-size", "").replace(" ", "").lower() == "1px"),
        None,
    )
    for node in nodes:
        st = _style(node)
        if node.tag in {"table", "td"} and ("border-radius" in st):
            parent_table = node
            while parent_table is not None and parent_table.tag != "table":
                parent_table = parent_table.parent
            if parent_table is None or "separate" not in _style(parent_table).get("border-collapse", "").lower():
                _issue(report, "T11", "E", "Для border-radius требуется border-collapse:separate")
        size = re.search(r"(\d+(?:\.\d+)?)\s*px", st.get("font-size", ""))
        size_value = float(size.group(1)) if size else None
        # Пустые ячейки-распорки и кружки индикатора не несут текста: предупреждения T13/T14 на них шум.
        has_text = bool(_text(node).replace("\u00a0", " ").strip())
        is_round = round_cell_side(node) is not None
        zero_spacer = bool(
            size_value == 0
            and not html.unescape(node.text or "").replace("\xa0", " ").strip()
        )
        if size and size_value < 12 and id(node) != id(preheader_node) and not zero_spacer:
            _issue(report, "T12", "E", "Размер текста не должен быть меньше 12 px", node.tag)
        elif size and 12 <= float(size.group(1)) < 15 and has_text and not is_round and id(node) not in footer_nodes and not (
            detail_start >= 0 and detail_start <= node.start_pos < marker_pos
            and len((node.text or "").strip()) <= 30
        ):
            _issue(report, "T13", "W", "Для основного текста рекомендуется не менее 15 px", node.tag)
        family = st.get("font-family")
        if family:
            allowed_fonts = {"arial", "georgia", "trebuchet ms", "verdana", "tahoma", "helvetica", "serif", "sans-serif"}
            normalized_family = re.sub(r"&quot;([^&]+)&quot;", r"'\1'", family, flags=re.I)
            families = [part.strip().lower().replace('"', "").replace("'", "") for part in normalized_family.split(",")]
            if any(part not in allowed_fonts for part in families) or families[-1] not in {"serif", "sans-serif"}:
                _issue(report, "T15", "E", "Используйте безопасные почтовые шрифты с fallback", node.tag)
        line_height = st.get("line-height")
        font_size = re.search(r"(\d+(?:\.\d+)?)px", st.get("font-size", ""), re.I)
        line_px = re.fullmatch(r"(\d+(?:\.\d+)?)px", line_height or "", re.I)
        line_ratio = float(line_height) if line_height and re.fullmatch(r"\d+(?:\.\d+)?", line_height) else (
            float(line_px.group(1)) / float(font_size.group(1))
            if line_px and font_size and float(font_size.group(1)) > 0 else None
        )
        if line_ratio is not None and has_text and not is_round and id(node) != id(preheader_node) and not 1.4 <= line_ratio <= 1.6:
            _issue(report, "T14", "W", "Рекомендуемый межстрочный интервал: 1.4–1.6", node.tag)
        if node.tag == "a" and ("padding" in st or "background" in st):
            _issue(report, "S05", "E", "Ссылка не должна выглядеть как кнопка")
    if re.search(r"\brole\s*=\s*['\"]?button", source, re.I):
        _issue(report, "S05", "E", "Кнопки запрещены в письме")
    if currency_method:
        sign_node = next((node for node in nodes if "$" in node.text), None)
        price_node = next((node for node in nodes if "{{price}}" in node.text), None)
        price_pos = source.find("{{price}}")
        sign_pos = source.find("$")
        price_area = source[max(0, price_pos - 300):price_pos + 300] if price_pos >= 0 else ""
        sign_area = source[max(0, sign_pos - 300):sign_pos + 150] if sign_pos >= 0 else ""
        if currency_method not in (8, 15):
            # Близость уже проверена по видимому тексту (V08 выше); здесь только наличие знака.
            if sign_pos < 0 or price_pos < 0:
                _issue(report, "V08", "E", "Знак валюты должен находиться рядом с {{price}}")
        elif price_pos >= 0 and not re.search(r"\{\{price\}\}[\s\S]{0,300}(?:\$|\bUSD\b)", price_area, re.I):
            _issue(report, "M08", "W", "Для способа 8 или 15 знак или USD должны идти после суммы")
        if currency_method == 15 and price_pos >= 0 and not re.search(r"\{\{price\}\}[\s\S]{0,300}\bUSD\b", price_area, re.I):
            _issue(report, "M15", "W", "Для способа 15 добавьте подпись USD после суммы")

        def inherited_style(node):
            chain = []
            while node is not None:
                chain.append(node)
                node = node.parent
            values = {}
            for item in reversed(chain):
                values.update(_style(item))
            return values

        sign_style = inherited_style(sign_node) if sign_node else {}
        price_style = inherited_style(price_node) if price_node else {}
        sign_inline_style = _style(sign_node) if sign_node else {}
        price_inline_style = _style(price_node) if price_node else {}
        sign_css = ";".join(f"{key}:{value}" for key, value in sign_inline_style.items())
        price_css = ";".join(f"{key}:{value}" for key, value in price_inline_style.items())
        sign_size = re.search(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", sign_css, re.I)
        amount_size = re.search(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", price_css, re.I)
        width = re.search(r"width\s*:\s*(\d+(?:\.\d+)?)px", sign_css, re.I)
        height = re.search(r"height\s*:\s*(\d+(?:\.\d+)?)px", sign_css, re.I)
        radius_px = re.search(r"border-radius\s*:\s*(\d+(?:\.\d+)?)px", sign_css, re.I)
        radius_pct = re.search(r"border-radius\s*:\s*(\d+(?:\.\d+)?)%", sign_css, re.I)
        circular = bool(
            width and height and abs(float(width.group(1)) - float(height.group(1))) < 0.01
            and ((radius_px and float(radius_px.group(1)) >= float(width.group(1)) / 2)
                 or (radius_pct and float(radius_pct.group(1)) >= 50))
            and re.search(r"display\s*:\s*inline-block", sign_css, re.I)
            and re.search(r"line-height\s*:\s*" + re.escape(height.group(1)) + r"px", sign_css, re.I)
            and re.search(r"text-align\s*:\s*center", sign_css, re.I)
        )
        if currency_method in (1, 2) and not re.search(r"vertical-align\s*:\s*super", sign_area, re.I):
            _issue(report, "M%02d" % currency_method, "W", "Для выбранного способа знак должен быть приподнят")
        if currency_method in (3, 17, 19) and not circular:
            _issue(report, "M%02d" % currency_method, "W", "Для способа нужна круглая inline-плашка знака")
        if currency_method in (4, 16, 18):
            pill_ok = bool(width and height and width.group(1) == height.group(1)
                           and re.search(r"display\s*:\s*inline-block", sign_css, re.I))
            if currency_method == 4:
                pill_ok = pill_ok and radius_px is not None and 6 <= float(radius_px.group(1)) <= 8
            if not pill_ok:
                _issue(report, "M%02d" % currency_method, "W", "Для способа нужны равные размеры inline-плашки")
        if currency_method == 5 and (not re.search(r"border\s*:\s*2px", sign_css, re.I)
                                     or re.search(r"background-color\s*:", sign_css, re.I)):
            _issue(report, "M05", "W", "Для способа 5 нужна обводка 2px без фона")
        if currency_method == 6 and (not sign_size or not amount_size or sign_size.group(1) != amount_size.group(1)):
            _issue(report, "M06", "W", "Размер знака должен совпадать с размером суммы")
        if currency_method == 7 and (
            not re.search(r"vertical-align\s*:\s*middle", sign_area, re.I) or not sign_size or not amount_size
            or float(sign_size.group(1)) >= float(amount_size.group(1))
        ):
            _issue(report, "M07", "W", "Знак должен быть меньше суммы и выровнен по центру")
        if currency_method == 9 and not re.search(r"border-bottom\s*:\s*3px", sign_css, re.I):
            _issue(report, "M09", "W", "Для способа 9 нужно подчёркивание толщиной 3px")
        if currency_method == 10 and (
            not re.search(r"font-style\s*:\s*italic", sign_css, re.I)
            or not re.search(r"font-weight\s*:\s*(?:bold|[7-9]00)", price_css, re.I)
        ):
            _issue(report, "M10", "W", "Знак курсивный, сумма полужирная")
        if currency_method == 11 and not (price_inline_style.get("background-color") or price_inline_style.get("background")):
            _issue(report, "M11", "W", "Задайте фон ближайшему контейнеру суммы")
        if currency_method == 12 and not (
            sign_node and sign_node.tag in {"td", "div", "span"}
            and (sign_inline_style.get("background-color") or sign_inline_style.get("background"))
        ):
            _issue(report, "M12", "W", "Поместите знак в отдельную цветную ячейку или плашку")
        if currency_method == 13 and (
            not sign_node or not price_node or node_fg(sign_node).lower() == node_fg(price_node).lower()
            or not sign_size or not amount_size or sign_size.group(1) != amount_size.group(1)
        ):
            _issue(report, "M13", "W", "Знак отличается цветом и совпадает с суммой по размеру")
        if currency_method == 14 and not re.search(r"border-(?:left|right)\s*:\s*[234]px", sign_css, re.I):
            _issue(report, "M14", "W", "Для способа 14 нужна вертикальная линия толщиной 2–4px")
        if currency_method in (16, 18) and not (radius_px or radius_pct):
            _issue(report, "M%02d" % currency_method, "W", "Для выбранного способа задайте скругление плашки")
        if currency_method == 20 and (
            not sign_size or not 14 <= float(sign_size.group(1)) <= 20
            or not re.search(r"border-right\s*:", sign_css, re.I)
        ):
            _issue(report, "M20", "W", "Для способа 20 нужен знак 14–20px и вертикальная черта справа")
    mailto_node = next((node for node in nodes if node.tag == "a" and node.attrs.get("href", "").lower().startswith("mailto:")), None)
    footer_container = _outer_footer_row(mailto_node, main_table, marker_pos) if mailto_node else None
    message_end = footer_container.start_pos if footer_container is not None else len(source)
    for node in nodes:
        if marker_pos >= 0 and marker_pos <= node.start_pos < message_end:
            if node.tag in {"h1", "h2", "h3", "p", "ul", "li", "hr"} and "style" not in node.attrs:
                _issue(report, "T16", "E", "Теги тела сообщения должны иметь inline style", node.tag)
            elif node.tag in {"strong", "em", "i", "b"} and "style" not in node.attrs:
                _issue(report, "T16", "W", "Добавьте inline style на выделение текста", node.tag)
    if marker_pos >= 0:
        message_area = source[marker_pos:message_end]
        for tag, expression, message in (
            ("h2", r"<h2\b", "В блоке сообщения обязателен h2"),
            ("p", r"<p\b", "В блоке сообщения обязателен абзац"),
            ("strong", r"<strong\b", "В блоке сообщения нужен strong"),
            ("ul", r"<ul\b", "В блоке сообщения нужен настоящий маркированный список: <ul> с тремя <li> (не строки таблицы и не символы «•»)"),
            ("hr", r"<hr\b", "В блоке сообщения нужен разделитель"),
            ("italic", r"<(?:em|i)\b|font-style\s*:\s*italic", "В блоке сообщения нужна курсивная подпись"),
        ):
            if not re.search(expression, message_area, re.I): _issue(report, "T17", "E", message)
        lis = re.findall(r"<li\b", message_area, re.I)
        if len(lis) != 3: _issue(report, "T17", "E", "В списке сообщения должно быть ровно три пункта")
        message_paragraphs = [
            node for node in nodes
            if node.tag == "p" and marker_pos <= node.start_pos < message_end
            and _style(node).get("font-style", "").lower() != "italic"
            and not any(
                descendant is not node and (
                    descendant.tag in {"em", "i"}
                    or _style(descendant).get("font-style", "").lower() == "italic"
                )
                for descendant in _walk(node)
            )
        ]
        if len(message_paragraphs) > 2:
            _issue(report, "T17", "W", "В блоке сообщения рекомендуется не более двух абзацев")
    if marker_pos >= 0:
        body_pos = next((node.start_pos for node in nodes if node.tag == "body"), 0)

        def token_position(token, after=0):
            for item in nodes:
                if token not in item.text:
                    continue
                at = source.find(token, max(item.start_pos, after))
                if at >= 0 and (marker_pos < 0 or at < marker_pos or token in {"{{supportEmail}}", "{{email}}"}):
                    return at
            return -1

        def tag_position(tag, after):
            return next((node.start_pos for node in nodes if node.tag == tag and node.start_pos >= after), -1)

        support_position = next(
            (node.start_pos for node in anchors if node.attrs.get("href") == "mailto:{{supportEmail}}"), -1
        )
        italic_position = next(
            (node.start_pos for node in nodes if node.start_pos >= marker_pos and (
                node.tag in {"em", "i"} or _style(node).get("font-style", "").lower() == "italic"
            )), -1
        )
        brand_position = token_position("{{brandName}}")
        name_position = token_position("{{name}}", max(body_pos, brand_position))
        product_position = token_position("{{productName}}", body_pos)
        transactional_position = token_position("{{transactionalId}}", product_position + 1)
        price_position = token_position("{{price}}", transactional_position + 1)
        h2_position = tag_position("h2", marker_pos)
        ul_position = tag_position("ul", h2_position)
        hr_position = tag_position("hr", ul_position)
        positions = [
            brand_position, name_position, product_position, transactional_position,
            price_position, marker_pos, h2_position, ul_position, hr_position,
            italic_position, support_position, token_position("{{email}}", support_position + 1),
        ]
        position_labels = [
            "{{brandName}} (шапка)", "{{name}} (приветствие)", "{{productName}} (товар в блоке деталей)",
            "{{transactionalId}} (номер заказа)", "{{price}} (сумма)", "маркер тела письма",
            "<h2>", "<ul>", "<hr>", "курсивная подпись", "{{supportEmail}}", "{{email}}",
        ]
        # h2/ul/hr/курсив: если блока нет, об этом сообщает T17; L01 не должен дублировать ошибку каскадом.
        kept = [
            (label, pos) for index, (label, pos) in enumerate(zip(position_labels, positions))
            if not (pos < 0 and 6 <= index <= 9)  # индексы h2, ul, hr, курсив в списке выше
        ]
        positions = [pos for _label, pos in kept]
        if any(pos < 0 for pos in positions) or positions != sorted(positions):
            # Называем конкретное место: так модель в ремонте понимает, что именно переставить.
            culprit = next((label for label, pos in kept if pos < 0), None)
            if culprit is None:
                culprit = next(
                    (kept[i + 1][0] for i in range(len(kept) - 1) if kept[i][1] > kept[i + 1][1]), None,
                )
            _issue(
                report, "L01", "E",
                "Нарушен порядок блоков. Нужно строго сверху вниз: шапка {{brandName}} → приветствие {{name}} → "
                "детали: товар {{productName}}, затем номер заказа {{transactionalId}}, затем сумма {{price}} → "
                "индикатор → маркер → h2 → ul → hr → курсивная подпись → {{supportEmail}} → {{email}}."
                + (" Сейчас не на своём месте: " + culprit + "." if culprit else ""),
            )
    for tag in ("table", "tr", "td", "ul", "li", "p", "h1", "h2", "h3", "a", "html", "head", "body"):
        opened = len(re.findall(r"<" + tag + r"\b", source, re.I))
        closed = len(re.findall(r"</" + tag + r"\s*>", source, re.I))
        if opened != closed:
            _issue(report, "T21", "E", "Незакрытый или лишний тег <" + tag + ">")

    # Содержание. Текст перед отправкой нормализуется, но HTML-маркеры исключены.
    for term in BANNED:
        prefix = term.endswith("*")
        phrase = term[:-1] if prefix else term
        pattern = re.escape(phrase).replace(r"\ ", r"\s+")
        if prefix:
            pattern += r"\w*"
        if re.search(r"(?<!\w)" + pattern + r"(?!\w)", content_text):
            _issue(report, "S01", "E", "Найдено запрещённое слово или выражение")
            break
    exclamations = content_text.count("!")
    report.stats["exclamations"] = exclamations
    if exclamations > 2: _issue(report, "S02", "E", "В письме больше двух восклицательных знаков")
    elif exclamations == 2: _issue(report, "S02", "W", "В письме два восклицательных знака")
    if re.search(r"ваш заказ оформлен|спасибо за покупку|мы рады сообщить|уважаемый клиент", content_text):
        _issue(report, "S06", "E", "Найдена шаблонная формулировка из списка запретов")
    if re.search(r"\b(?:статус заказа|ваш заказ)\b", content_text):
        _issue(report, "S08", "W", "Замените типовой заголовок статуса или заказа")
    if re.search(r"\b(?:нажмите|кликните|жмите|click\s+here)\b", content_text):
        _issue(report, "S11", "W", "Избегайте прямого призыва нажать или кликнуть")
    if re.search(r"\b(?:акци\w*|скидк\w*|распродаж\w*|sale|discount|offer)\b", content_text):
        _issue(report, "S12", "W", "Проверьте коммерческую лексику в транзакционном письме")
    if re.search(r"\b(ты|тебе|тебя|твой|твоя|твое|твои|тобой)\b", content_text):
        _issue(report, "S10", "E", "Обращение должно быть на «вы»")
    message_text = ""
    if marker_pos >= 0:
        footer_at = source.lower().find("mailto:", marker_pos)
        message_text = html.unescape(re.sub(r"<[^>]*>", " ", source[marker_pos:footer_at if footer_at >= 0 else len(source)]))
    status_text = ""
    if detail_start >= 0 and marker_pos > detail_start:
        status_text = html.unescape(re.sub(r"<[^>]*>", " ", source[detail_start:marker_pos])).casefold()
    timing_promise = re.search(
        r"\b(?:"
        r"завтра|сегодня\s+до|в\s+ближайш\w*"
        r"|не\s+(?:более|дольше|позже)\s+(?:пары|пару|двух|трех|трёх|нескольких|\d+)\s+"
        r"(?:рабочих\s+)?(?:дн\w*|час\w*|сут\w*|недел\w*)"
        r"|в\s+течение(?:\s+(?:ближайш\w*|следующ\w*|пары|пару|двух|трех|трёх|нескольких|\d+)"
        r"(?:\s+(?:рабочих\s+)?(?:дн\w*|час\w*|сут\w*|недел\w*))?)?"
        r"|за\s+(?:пару|пары|двух|трех|трёх|нескольких|\d+)\s+"
        r"(?:рабочих\s+)?(?:дн\w*|час\w*|сут\w*|недел\w*)"
        r"|через\s+(?:пару|пары|двух|трех|трёх|нескольких|\d+)\s+"
        r"(?:рабочих\s+)?(?:дн\w*|час\w*|сут\w*|недел\w*)"
        r"|(?:обычно\s+)?(?:это\s+)?(?:занимает|потребует)\s+(?:не\s+более\s+)?"
        r"(?:пару|пары|двух|трех|трёх|нескольких|\d+)\s+"
        r"(?:рабочих\s+)?(?:дн\w*|час\w*|сут\w*|недел\w*)"
        r"|(?:within|in)\s+\d+\s+(?:business\s+)?(?:days|hours|weeks)"
        r"|no\s+more\s+than\s+(?:a\s+)?(?:couple|two|three|\d+)\s+(?:days|hours|weeks)"
        r"|(?:usually\s+)?takes\s+(?:up\s+to\s+)?(?:a\s+)?(?:couple|two|three|\d+)\s+(?:days|hours|weeks)"
        r")\b",
        content_text,
        re.I,
    )
    if timing_promise:
        _issue(
            report, "S07", "W",
            "Проверьте обещание срока: промпт запрещает обещать сроки.",
            timing_promise.group(0),
        )
    for heading in (
        node for node in nodes
        if node.tag in {"h1", "h2", "h3", "em", "i"}
        or _style(node).get("font-style", "").lower() == "italic"
    ):
        text = _text(heading)
        caps = re.search(r"\b[A-ZА-ЯЁ]{3,}\b", text)
        if caps and not re.search(r"\b(?:USD|ID|PDF|SMS|OK|FAQ)\b", text):
            _issue(
                report, "S04", "E", "Не используйте слова целиком заглавными в заголовках и подписи",
                _fragment(source, heading.start_pos, heading.start_pos + 180),
            )
    if re.search(r"\b[A-ZА-ЯЁ]{4,}\b", content_case_text):
        _issue(report, "S04", "W", "Проверьте слова целиком заглавными в тексте")
    uppercase_node = next(
        (node for node in nodes if re.search(r"\buppercase\b", _style(node).get("text-transform", ""), re.I)),
        None,
    )
    if uppercase_node is not None:
        _issue(
            report, "S13", "E", "Удалите text-transform:uppercase",
            _fragment(source, uppercase_node.start_pos, uppercase_node.start_pos + 160),
        )
    greeting_node = next((node for node in nodes if "{{name}}" in node.text), None)
    greeting_large = False
    current = greeting_node
    while current is not None:
        size = re.search(r"(\d+(?:\.\d+)?)\s*px", _style(current).get("font-size", ""), re.I)
        if size and float(size.group(1)) >= 22:
            greeting_large = True
            break
        current = current.parent
    if not any(node.tag == "h1" for node in nodes) and not greeting_large:
        _issue(report, "S09", "W", "Рассмотрите h1 или заметный текст приветствия размером от 22 px")

    indicator_end = marker_pos if marker_pos >= 0 else len(source)
    indicator_html = source[detail_start:indicator_end] if detail_start >= 0 else ""
    labels = re.findall(r">([^<>]+)<", indicator_html)
    short_labels = set()
    for item in labels:
        label = html.unescape(re.sub(r"\{\{[^}]+\}\}", "", item)).strip()
        if label and len(label) <= 30:
            short_labels.add(label)
    if len(short_labels) < 3:
        _issue(report, "L02", "E", "Между деталями заказа и маркером нужны три подписи этапов")

    # Проверяем контраст с наследованием цвета и фона по родителям.
    checked_pairs = set()
    for node in nodes:
        text_value = html.unescape((node.text or "")).replace("\xa0", " ").strip()
        if not text_value or node.tag in {"root", "head", "title"}:
            continue
        current = node
        hidden = False
        while current is not None:
            style = _style(current)
            if style.get("display", "").replace(" ", "").lower() == "none" or style.get("visibility", "").lower() == "hidden":
                hidden = True
                break
            current = current.parent
        if hidden:
            continue
        foreground, background = node_fg(node), node_bg(node)
        pair = (foreground.lower(), background.lower())
        if pair in checked_pairs:
            continue
        checked_pairs.add(pair)
        contrast = ratio(foreground, background)
        if contrast is None:
            report.stats["contrast_unverified"] += 1
        else:
            report.stats["contrast_checked"] += 1
            if contrast < 4.5:
                _issue(
                    report, "A01", "E",
                    _contrast_message("Контраст текста", foreground, background, contrast),
                    text_value[:40],
                )
    price_node = next((node for node in nodes if "{{price}}" in node.text), None)
    sign_node = next((node for node in nodes if "$" in node.text), None)
    if sign_node is not None and price_node is not None:
        sign_contrast = ratio(node_fg(sign_node), node_bg(sign_node))
        if sign_contrast is None:
            report.stats["contrast_unverified"] += 1
            _issue(report, "A02", "E", "Контраст знака валюты не удалось проверить", "$")
        elif sign_contrast < 4.5:
            _issue(
                report, "A02", "E",
                _contrast_message("Контраст знака валюты", node_fg(sign_node), node_bg(sign_node), sign_contrast),
                "$",
            )
    if checked_pairs and report.stats["contrast_unverified"] / len(checked_pairs) > 0.3:
        _issue(report, "A04", "W", "Слишком много непроверенных цветовых пар")
    indicator_source = source[max(0, detail_start):marker_pos] if detail_start >= 0 and marker_pos >= 0 else ""
    if not (
        re.search(r"(?:&#10003;|&#x2713;|✓|&#x2714;)", indicator_source, re.I)
        or re.search(r"\b(?:готово|завершен\w*|доставлен\w*|выполнен\w*|completed|done)\b", html.unescape(re.sub(r"<[^>]*>", " ", indicator_source)), re.I)
    ):
        _issue(report, "A03", "E", "Добавьте текстовую или графическую отметку завершённого этапа: ✓ или слово «готово» у первого этапа")
    for node in nodes:
        node_style = _style(node)
        radius_text = node_style.get("border-radius", "").strip()
        w_match = re.fullmatch(r"(\d+(?:\.\d+)?)px", node_style.get("width", "").strip(), re.I)
        h_match = re.fullmatch(r"(\d+(?:\.\d+)?)px", node_style.get("height", "").strip(), re.I)
        if node.tag == "td":
            w_match = w_match or re.fullmatch(r"(\d+(?:\.\d+)?)", node.attrs.get("width", "").strip())
            h_match = h_match or re.fullmatch(r"(\d+(?:\.\d+)?)", node.attrs.get("height", "").strip())
        if not (radius_text and w_match and h_match):
            continue
        w_px, h_px = float(w_match.group(1)), float(h_match.group(1))
        if not w_px or not h_px or w_px > 80 or h_px > 80 or min(w_px, h_px) / max(w_px, h_px) < 0.6:
            continue
        first_radius = radius_text.split()[0]
        if first_radius.endswith("%"):
            round_shape = float(re.sub(r"[^\d.]", "", first_radius) or 0) >= 50
        else:
            round_shape = bool(re.fullmatch(r"\d+(?:\.\d+)?px", first_radius, re.I)) and float(first_radius[:-2]) >= max(w_px, h_px) / 2 - 0.5
        if not round_shape:
            continue
        has_text = bool(_text(node).strip().replace("\u00a0", ""))
        line_match = re.fullmatch(r"(\d+(?:\.\d+)?)px", node_style.get("line-height", "").strip(), re.I)
        if w_px != h_px or (has_text and (not line_match or float(line_match.group(1)) != h_px)):
            _issue(report, "A05", "E", "Круг должен быть квадратным: width и height равны, line-height равен высоте, иначе он станет овалом")
            break
    color_values = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", source))
    palette_values = []
    for node in nodes:
        styles = _style(node)
        palette_values.extend(color_tokens(styles.get("color", "")))
        palette_values.extend(color_tokens(styles.get("background-color", "")))
        palette_values.extend(color_tokens(styles.get("background", "")))
        palette_values.extend(color_tokens(node.attrs.get("bgcolor", "")))
    if any(is_red_orange(color) for color in palette_values):
        _issue(report, "P01", "E", "В палитре найден насыщенный красный или оранжевый цвет: замени его цветом из выданной палитры")
    if not 4 <= len(color_values) <= 10:
        _issue(report, "P02", "W", "Рекомендуется использовать от 4 до 10 различных hex-цветов")
    emojis = [
        char for char in re.findall(
            r"[\U0001F000-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF\U00002B00-\U00002BFF\ufe0f]",
            content_text,
        ) if ord(char) != 0x2713
    ]
    report.stats["emoji"] = len(emojis)
    important_text = " ".join(_text(node) for node in nodes if node.tag in {"title", "h1", "h2", "h3"}).replace("✓", "")
    preheader_text = preheader.group(3) if preheader else ""
    if any(char in content_text for char in "🚨⚡⚠️❗‼") or len(emojis) > 1 or re.search(r"[\U0001F000-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF\U00002B00-\U00002BFF\ufe0f]", important_text + preheader_text):
        _issue(report, "S03", "E", "Эмодзи в письме запрещены правилами шаблона")
    # --- A06: круг-ячейка растягивается по колонке с подписью и становится овалом
    if find_stretched_circles(nodes):
        _issue(report, "A06", "E", "Круг лежит в одной колонке с подписью и растянется в овал: вложи его в отдельную таблицу фиксированной ширины (width=размер круга)")
    # --- T26: таблица внутри строки с текстом ломает выравнивание в Outlook
    outlook_severity = "E" if TEMPLATE_STRICT_OUTLOOK else "W"
    for node in nodes:
        if node.tag != "table":
            continue
        inline_style = _style(node).get("display", "").lower() in {"inline-table", "inline-block", "inline"}
        ancestor, inside_text = node.parent, False
        while ancestor is not None and ancestor.tag not in {"td", "th", "body", "root"}:
            if ancestor.tag in {"p", "span", "a", "li", "h1", "h2", "h3", "em", "strong", "b", "i"}:
                inside_text = True
                break
            ancestor = ancestor.parent
        if inline_style or inside_text:
            _issue(report, "T26", outlook_severity, "Таблица внутри строки текста или display:inline-table: Outlook сломает выравнивание. Делай знак и сумму обычными span в одном абзаце", node.tag)
            break
    # --- P03: тёплая (красно-оранжевая, терракотовая) заливка ячейки в блоке индикатора
    if detail_start >= 0 and marker_pos >= 0:
        for node in nodes:
            if node.tag not in {"td", "th", "table", "tr"} or not detail_start <= node.start_pos < marker_pos:
                continue
            fills = color_tokens(node.attrs.get("bgcolor", "")) + color_tokens(_style(node).get("background-color", "")) + color_tokens(_style(node).get("background", ""))
            if any(is_warm_fill(color) for color in fills):
                _issue(report, "P03", outlook_severity, "Тёплая заливка в блоке деталей и индикатора: используй цвета из выданной палитры", node.tag)
                break
    # --- T27: padding/margin на span Outlook игнорирует
    for node in nodes:
        if node.tag == "span" and any(prop.startswith(("padding", "margin")) for prop in _style(node)):
            _issue(report, "T27", "W", "Outlook игнорирует padding и margin на span: отступы задавай ячейкой таблицы или пробелом", node.tag)
            break
    # --- S14: выдуманные подробности (склад, мастера, курьер, трек) до блока сообщения
    if marker_pos >= 0:
        head_text = html.unescape(re.sub(r"<[^>]*>", " ", source[:marker_pos]))
        invented = re.search(
            r"\bмастер\w*|\bсклад(?:а|е|у|ом|ы|ов|ах)?\b|\bкурьер\w*|\bтрек(?:[-\s]?номер\w*)?\b",
            head_text, re.I,
        )
        if invented:
            _issue(report, "S14", "W", "Не выдумывай подробности, которых ты не знаешь (склад, мастера, курьер, трек-номер, сроки): «%s»" % invented.group(0), invented.group(0))
    _fill_where(report, source, content_text, nodes, preheader)
    report.ok = not report.errors
    return report


_WARNING_RULES = {
    "V09", "V10", "V11", "V12", "T13", "T14", "T25", "S02", "S07", "S08", "S09", "S11", "S12",
    "A04", "P02", "T27", "S14",
    *(f"M{number:02d}" for number in range(1, 21)),
}
_MIXED_SEVERITY_RULES = {"T04", "T16", "T17", "T23", "S02", "S04", "T26", "P03"}
RULES: Dict[str, dict] = {
    rule: {
        "id": rule,
        "severity": "E/W" if rule in _MIXED_SEVERITY_RULES else ("W" if rule in _WARNING_RULES else "E"),
        "title": title,
        "enabled": True,
    }
    for rule, title in {
        "V01": "Обязательные переменные", "V02": "Допустимые плейсхолдеры", "V03": "Переменная в title",
        "V04": "Переменная в деталях", "V05": "Название бренда", "V06": "Приветствие и заказ",
        "V07": "Ссылка поддержки и получатель", "V08": "Валюта и сумма", "V09": "Чистый mailto",
        "V11": "Размер цены", "V12": "Читаемость названия в шапке", "T01": "Только inline CSS",
        "T02": "Нет class", "T03": "Нет запрещённых тегов", "T04": "Безопасный CSS",
        "T05": "Нет внешних ресурсов", "T06": "Документ и метаданные", "T07": "Ширина письма",
        "T08": "Атрибуты email-таблиц", "T16": "Inline-стили сообщения", "T17": "Структура сообщения",
        "T09": "Фон страницы и таблицы", "T10": "Скрытый прехедер", "T11": "Совместимость скруглений",
        "T12": "Минимальный размер шрифта", "T13": "Размер основного текста", "T14": "Межстрочный интервал",
        "T15": "Безопасные шрифты", "T18": "Маркер тела сообщения", "T19": "HTML-комментарии",
        "T20": "Заголовок документа", "T21": "Сбалансированные теги", "T25": "Количество таблиц",
        "T22": "Стили ссылок", "T23": "Размер HTML", "T24": "Маркированный список",
        "S01": "Запрещённые формулировки", "S02": "Восклицательные знаки", "S04": "Заглавные подписи и стили",
        "S03": "Эмодзи", "S05": "Отсутствие кнопок", "S06": "Канцелярские фразы", "S07": "Обещания сроков",
        "S08": "Нешаблонный заголовок", "S09": "Заметное приветствие", "S10": "Обращение на «ты»",
        "S11": "Ненавязчивый призыв", "S12": "Транзакционный тон",
        "S13": "Запрещённый text-transform",
        "L01": "Порядок блоков", "L02": "Индикатор этапов", "A01": "Контраст текста",
        "A02": "Контраст знака валюты", "A03": "Индикатор без опоры на цвет", "A04": "Проверка контраста",
        "P01": "Ограниченная палитра", "P02": "Количество цветов", "A05": "Круги без искажения",
        "A06": "Круг не растянется по колонке", "T26": "Таблица внутри строки текста (Outlook)",
        "T27": "Padding/margin на span (Outlook)", "P03": "Тёплая заливка индикатора",
        "S14": "Выдуманные подробности",
        "V10": "Способ оформления валюты", "M01": "Выбранное оформление знака валюты",
        "M07": "Выравнивание знака", "M08": "Знак после суммы", "M09": "Подчёркивание знака",
        "M02": "Приподнятый знак валюты", "M03": "Круглая плашка знака", "M04": "Скруглённая плашка",
        "M05": "Обводка знака", "M06": "Размер знака", "M11": "Подложка суммы", "M13": "Контрастный знак",
        "M16": "Радиус плашки", "M17": "Индикатор-круг", "M18": "Радиус индикатора",
        "M19": "Индикатор-круг", "M10": "Курсивный знак", "M12": "Фоновая плашка знака",
        "M14": "Разделительная линия", "M15": "Подпись USD", "M20": "Вертикальная линия знака",
    }.items()
}


def rules() -> list:
    return [dict(item, enabled=item["id"] not in TEMPLATE_DISABLED_RULES) for item in RULES.values()]
