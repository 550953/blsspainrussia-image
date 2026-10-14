"""Ограниченные механические исправления разметки без изменения текста письма."""
import re
from ..config import TEMPLATE_AUTOFIX_CONTRAST
import colorsys
from .contrast import color_tokens, is_red_orange, parse_color, ratio, suggest_fix
from .validator import validate as _validate
from .validator import ALLOWED_VARS, MARKER, _Tree, _outer_footer_row, _style, _text, _walk, find_stretched_circles, round_cell_side


_MARKER_PATTERN = re.compile(
    r"<!--\s*Здесь\s+вставляется\s+тело\s+письма\s*-->", re.I,
)
_ANCHOR_PATTERN = re.compile(r"<a\b([^>]*)>([\s\S]*?)</a\s*>", re.I)
_COMMENT_PATTERN = re.compile(r"<!--[\s\S]*?-->", re.I)


def _tag_end(source, start):
    quote = None
    for position in range(start, len(source)):
        char = source[position]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ">":
            return position + 1
    return min(len(source), start + 1)


def _style_value(tag):
    return re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)


def _set_style_property(tag, name, value):
    style = _style_value(tag)
    if style:
        old = style.group(2)
        prop = re.compile(r"(^|;)\s*" + re.escape(name) + r"\s*:\s*[^;]*", re.I)
        if prop.search(old):
            updated = prop.sub(lambda match: match.group(1) + name + ":" + value, old, count=1)
        else:
            updated = old.rstrip().rstrip(";") + ";" + name + ":" + value
        if updated == old:
            return tag, False
        return tag[:style.start(2)] + updated + tag[style.end(2):], True
    return tag[:-1].rstrip() + ' style="' + name + ":" + value + '">' , True


def _edit_node_tags(source, transforms):
    """Apply opening-tag transforms from right to left so stored offsets stay valid."""
    changes = []
    for start, callback in transforms:
        end = _tag_end(source, start)
        tag = source[start:end]
        updated, changed = callback(tag)
        if changed:
            changes.append((start, end, updated))
    for start, end, updated in sorted(changes, reverse=True):
        source = source[:start] + updated + source[end:]
    return source, bool(changes)


def _tree(source):
    tree = _Tree(source)
    tree.feed(source)
    tree.close()
    return tree


def _fix_comments(source):
    def keep_or_remove(match):
        comment = match.group(0)
        content = comment[4:-3].strip()
        if _MARKER_PATTERN.fullmatch(comment):
            return MARKER
        if re.match(r"\[if\b", content, re.I) or re.match(r"<!\[endif\]", content, re.I):
            return comment
        return ""
    return _COMMENT_PATTERN.sub(keep_or_remove, source)


def _fix_rounded_table_cells(source):
    tree = _tree(source)
    targets = {}
    has_cell_radius = False
    for node in _walk(tree.root):
        if node.tag not in {"table", "td"} or "border-radius" not in _style(node):
            continue
        table = node
        if node.tag != "table":
            has_cell_radius = True
            table = node.parent
            while table is not None and table.tag != "table":
                table = table.parent
        if table is None:
            continue
        styles = _style(table)
        if "separate" in styles.get("border-collapse", "").lower():
            continue
        targets.setdefault(table.start_pos, "border-collapse")

    source, changed = _edit_node_tags(
        source,
        [
            (position, lambda tag: _set_style_property(tag, "border-collapse", "separate"))
            for position in targets
        ],
    )
    return source, changed, has_cell_radius and changed


def _hidden_preheader(node):
    current = node
    while current is not None:
        style = _style(current)
        if (
            current.tag in {"div", "span"}
            and style.get("display", "").replace(" ", "").lower() == "none"
            and style.get("font-size", "").replace(" ", "").lower() == "1px"
        ):
            return True
        current = current.parent
    return False


def _fix_small_fonts(source):
    tree = _tree(source)
    edits = []
    for node in _walk(tree.root):
        value = _style(node).get("font-size", "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*px", value, re.I)
        if not match or _hidden_preheader(node):
            continue
        size = float(match.group(1))
        if size >= 12 or (size == 0 and not _text(node).replace("\xa0", " ").strip()):
            continue
        edits.append((node.start_pos, lambda tag: (
            re.subn(
                r"(\bfont-size\s*:\s*)\d+(?:\.\d+)?\s*px",
                lambda found: found.group(1) + "12px",
                tag, count=1, flags=re.I,
            )[0],
            True,
        )))
    return _edit_node_tags(source, edits)


def _known_background(node):
    current = node
    while current is not None:
        styles = _style(current)
        candidate = styles.get("background-color") or current.attrs.get("bgcolor", "")
        if candidate and candidate.strip().lower() not in {"transparent", "inherit", "initial", "unset"}:
            if parse_color(candidate) is not None:
                return candidate
        shorthand = styles.get("background", "")
        if shorthand:
            if "gradient(" in shorthand.lower() and ratio("#000000", shorthand) is not None:
                return shorthand
            colors = color_tokens(shorthand)
            if colors and parse_color(colors[0]) is not None:
                return colors[0]
        current = current.parent
    return None


def _fix_contrast(source):
    if not TEMPLATE_AUTOFIX_CONTRAST:
        return source, False
    tree = _tree(source)
    edits = []
    for node in _walk(tree.root):
        if not (node.text or "").strip() or _hidden_preheader(node):
            continue
        foreground = _style(node).get("color")
        background = _known_background(node)
        if not foreground or not background or ratio(foreground, background) is None:
            continue
        if ratio(foreground, background) >= 4.5:
            continue
        replacement = suggest_fix(foreground, background, 4.5)
        if not replacement or ratio(replacement, background) is None or ratio(replacement, background) < 4.5:
            continue

        def recolor(tag, old=foreground, new=replacement):
            style = _style_value(tag)
            if not style:
                return tag, False
            updated, count = re.subn(
                r"(\bcolor\s*:\s*)" + re.escape(old) + r"(?=\s*(?:;|$))",
                lambda match: match.group(1) + new,
                style.group(2), count=1, flags=re.I,
            )
            if not count:
                return tag, False
            return tag[:style.start(2)] + updated + tag[style.end(2):], True

        edits.append((node.start_pos, recolor))
    return _edit_node_tags(source, edits)


def _message_heading(source: str):
    transaction = source.find("{{transactionalId}}")
    headings = list(re.finditer(r"<h2\b", source, re.I))
    if transaction >= 0:
        heading = next((item for item in headings if item.start() > transaction), None)
        if heading:
            return heading
    body = re.search(r"<body\b[^>]*>", source, re.I)
    return next((item for item in headings if body is None or item.start() > body.end()), None)


def _fix_marker(source: str):
    heading = _message_heading(source)
    if not heading:
        return source, False
    without_markers = _MARKER_PATTERN.sub("", source)
    heading = _message_heading(without_markers)
    if not heading:
        return source, False
    before = without_markers[:heading.start()].rstrip()
    after = without_markers[heading.start():]
    updated = before + MARKER + after
    return updated, updated != source


def _anchor_href(attrs: str):
    match = re.search(r"\bhref\s*=\s*([\"'])(.*?)\1", attrs, re.I | re.S)
    return match.group(2).strip() if match else ""


def _visible_anchor_text(inner: str):
    return re.sub(r"<[^>]*>", "", inner)


def _email_has_allowed_container(source, position, support_end):
    anchor_spans = [(item.start(), item.end()) for item in _ANCHOR_PATTERN.finditer(source)]
    if position <= support_end or any(start <= position < end for start, end in anchor_spans):
        return False
    for item in re.finditer(r"<(td|p|span|div)\b[^>]*>", source, re.I):
        if item.start() > position:
            continue
        close = re.search(r"</" + item.group(1) + r"\s*>", source[item.end():], re.I)
        if close and item.end() + close.end() > position:
            return True
    return False


def _fix_support_links(source: str):
    anchors = list(_ANCHOR_PATTERN.finditer(source))
    canonical = [
        item for item in anchors
        if _anchor_href(item.group(1)) == "mailto:{{supportEmail}}"
    ]
    if not canonical:
        return source, False

    keep = canonical[0]
    pieces = []
    cursor = 0
    for item in anchors:
        pieces.append(source[cursor:item.start()])
        inner = item.group(2)
        if item is keep:
            visible = _visible_anchor_text(inner).strip()
            if not visible:
                inner = "Напишите нам"
            inner = inner.replace("{{email}}", "")
            pieces.append("<a" + item.group(1) + ">" + inner + "</a>")
        else:
            visible = _visible_anchor_text(inner).replace("{{supportEmail}}", "Напишите нам")
            pieces.append(visible.replace("{{email}}", ""))
        cursor = item.end()
    pieces.append(source[cursor:])
    updated = "".join(pieces)

    canonical_match = next(
        (
            item for item in _ANCHOR_PATTERN.finditer(updated)
            if _anchor_href(item.group(1)) == "mailto:{{supportEmail}}"
        ),
        None,
    )
    if canonical_match is None:
        return source, False

    before, kept_link, after = (
        updated[:canonical_match.start()],
        canonical_match.group(0),
        updated[canonical_match.end():],
    )
    updated = before + kept_link + after
    canonical_match = next(
        item for item in _ANCHOR_PATTERN.finditer(updated)
        if _anchor_href(item.group(1)) == "mailto:{{supportEmail}}"
    )
    email_matches = list(re.finditer(r"\{\{email\}\}", updated))
    valid_email = next(
        (
            item for item in email_matches
            if _email_has_allowed_container(updated, item.start(), canonical_match.end())
        ),
        None,
    )
    if valid_email is None:
        updated = updated[:canonical_match.end()] + "<span>{{email}}</span>" + updated[canonical_match.end():]
    elif email_matches:
        valid_position = valid_email.start()
        keep = True

        def keep_one_email(match):
            nonlocal keep
            if match.start() == valid_position and keep:
                keep = False
                return "{{email}}"
            return ""

        updated = re.sub(r"\{\{email\}\}", keep_one_email, updated)
    return updated, updated != source



# ---------------------------------------------------------------------------
# Дополнительные механические исправления (AF19–AF27).
# Цель: убрать причины, из-за которых после первого автофикса оставались T21, T17, T16, T09, T10,
# и тем самым не тратить второй вызов модели.
# ---------------------------------------------------------------------------
_BALANCE_TAGS = {
    "html", "head", "body", "table", "tbody", "thead", "tr", "td", "th", "ul", "li", "p",
    "h1", "h2", "h3", "a", "div", "span", "strong", "em", "b", "i",
}
_BALANCE_COUNTED = ("table", "tr", "td", "ul", "li", "p", "h1", "h2", "h3", "a", "html", "head", "body")
_BLOCK_CLOSES_P = {"p", "h1", "h2", "h3", "ul", "table", "div"}
_TOKEN = re.compile(r"<!--[\s\S]*?-->|<(/?)([a-zA-Z][a-zA-Z0-9]*)((?:\"[^\"]*\"|'[^']*'|[^'\">])*)>")


def _counts_balanced(source):
    for tag in _BALANCE_COUNTED:
        if len(re.findall(r"<" + tag + r"\b", source, re.I)) != len(re.findall(r"</" + tag + r"\s*>", source, re.I)):
            return False
    return True


def _fix_tag_balance(source):
    """AF19: расставляет недостающие закрывающие теги и убирает лишние (по стеку).

    Запускается только если счётчики открытий и закрытий не совпадают, исправный документ не трогает.
    """
    if _counts_balanced(source):
        return source, False
    out = []
    stack = []
    cursor = 0

    def close_until(tag):
        while stack:
            top = stack.pop()
            out.append("</" + top + ">")
            if top == tag:
                break

    def nearest(tag, stop):
        for item in reversed(stack):
            if item == tag:
                return True
            if item in stop:
                return False
        return False

    for match in _TOKEN.finditer(source):
        out.append(source[cursor:match.start()])
        cursor = match.end()
        whole = match.group(0)
        if whole.startswith("<!--"):
            out.append(whole)
            continue
        closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if name not in _BALANCE_TAGS:
            out.append(whole)
            continue
        if closing:
            if name in stack:
                while stack and stack[-1] != name:
                    out.append("</" + stack.pop() + ">")
                stack.pop()
                out.append(whole)
            # лишний закрывающий тег без пары выбрасываем
            continue
        if name == "li" and nearest("li", {"ul"}):
            close_until("li")
        elif name in ("td", "th") and (nearest("td", {"tr", "table"}) or nearest("th", {"tr", "table"})):
            while stack and stack[-1] not in ("td", "th"):
                out.append("</" + stack.pop() + ">")
            if stack:
                out.append("</" + stack.pop() + ">")
        elif name == "tr" and nearest("tr", {"table"}):
            close_until("tr")
        elif name in _BLOCK_CLOSES_P and nearest("p", {"td", "th", "li", "body", "table"}):
            close_until("p")
        out.append(whole)
        if not attrs.rstrip().endswith("/"):
            stack.append(name)
    out.append(source[cursor:])
    result = "".join(out)
    # Хвост: недостающие закрытия вставляем перед </body></html>, если они есть, иначе в конец.
    inner = "".join("</" + tag + ">" for tag in reversed(stack) if tag not in ("body", "html"))
    outer = ("</body>" if "body" in stack else "") + ("</html>" if "html" in stack else "")
    if inner or outer:
        end = re.search(r"</body\s*>\s*(?:</html\s*>)?\s*$", result, re.I)
        if end and not outer:
            result = result[:end.start()] + inner + result[end.start():]
        else:
            result = result.rstrip() + inner + outer
    return result, result != source


def _promote_h3(source):
    """AF21a: если блока h2 нет, а после номера заказа есть h3 — это и есть заголовок сообщения."""
    if re.search(r"<h2\b", source, re.I):
        return source, False
    transaction = source.find("{{transactionalId}}")
    match = re.search(r"<h3\b", source[max(transaction, 0):], re.I)
    if not match:
        return source, False
    start = max(transaction, 0) + match.start()
    close = re.search(r"</h3\s*>", source[start:], re.I)
    if not close:
        return source, False
    end = start + close.start()
    updated = (
        source[:start] + "<h2" + source[start + 3:end] + "</h2>" + source[end + len(close.group(0)):]
    )
    return updated, True


def _message_bounds(source):
    marker = _MARKER_PATTERN.search(source)
    if not marker:
        return None
    tree = _tree(source)
    nodes = list(_walk(tree.root))
    main_table = next(
        (n for n in nodes if n.tag == "table" and re.fullmatch(r"\d+", n.attrs.get("width", ""))
         and 520 <= int(n.attrs["width"]) <= 600), None,
    )
    mailto = next((n for n in nodes if n.tag == "a" and n.attrs.get("href", "").lower().startswith("mailto:")), None)
    end = len(source)
    if mailto is not None:
        row = _outer_footer_row(mailto, main_table, marker.start())
        if row is not None:
            end = row.start_pos
    return marker.end(), max(end, marker.end())


def _message_text_color(area):
    for tag in ("h2", "p", "li"):
        found = re.search(r"<" + tag + r"\b[^>]*style\s*=\s*[\"'][^\"']*?(?<![\w-])color\s*:\s*(#[0-9a-fA-F]{3,8})", area, re.I)
        if found:
            return found.group(1)
    return "#222222"


def _fix_message_styles(source):
    """AF20: теги тела сообщения без inline style получают стиль (T16)."""
    bounds = _message_bounds(source)
    if not bounds:
        return source, False
    start, end = bounds
    area = source[start:end]
    color = _message_text_color(area)
    styles = {
        "h1": "margin:0 0 12px;font-size:22px;line-height:1.4;color:%s" % color,
        "h2": "margin:0 0 12px;font-size:20px;line-height:1.4;color:%s" % color,
        "h3": "margin:0 0 12px;font-size:18px;line-height:1.4;color:%s" % color,
        "p": "margin:0 0 14px;font-size:15px;line-height:1.5;color:%s" % color,
        "ul": "margin:0 0 14px;padding-left:20px;font-size:15px;line-height:1.5;color:%s" % color,
        "li": "margin:0 0 6px;font-size:15px;line-height:1.5;color:%s" % color,
        "hr": "border:none;border-top:1px solid #d6d6d6;margin:16px 0",
        "strong": "font-weight:700", "b": "font-weight:700",
        "em": "font-style:italic", "i": "font-style:italic",
    }

    def add(match):
        name, attrs = match.group(1).lower(), match.group(2)
        if re.search(r"\bstyle\s*=", attrs, re.I):
            return match.group(0)
        slash = ""
        if attrs.rstrip().endswith("/"):
            attrs = attrs.rstrip()[:-1]
            slash = " /"
        return "<" + match.group(1) + attrs.rstrip() + ' style="' + styles[name] + '"' + slash + ">"

    updated = re.sub(r"<(h1|h2|h3|p|ul|li|hr|strong|em|b|i)\b([^>]*)>", add, area, flags=re.I)
    if updated == area:
        return source, False
    return source[:start] + updated + source[end:], True


def _fix_message_structure(source):
    """AF21: механически дотягивает блок сообщения до T17 (hr, strong, курсив, ровно три li)."""
    bounds = _message_bounds(source)
    if not bounds:
        return source, False
    start, end = bounds
    area = source[start:end]
    before = area
    color = _message_text_color(area)

    # ровно три пункта: лишние отбрасываем
    items = list(re.finditer(r"<li\b[^>]*>[\s\S]*?</li\s*>", area, re.I))
    if len(items) > 3 and len(re.findall(r"<li\b", area, re.I)) == len(items):
        for item in reversed(items[3:]):
            area = area[:item.start()] + area[item.end():]

    # разделитель после списка
    if not re.search(r"<hr\b", area, re.I):
        close = re.search(r"</ul\s*>", area, re.I)
        if close:
            hr = '<hr style="border:none;border-top:1px solid #d6d6d6;margin:16px 0">'
            area = area[:close.end()] + hr + area[close.end():]

    # выделение strong в первом абзаце: первые слова простого текста
    if not re.search(r"<strong\b", area, re.I):
        first = re.search(r"(<p\b[^>]*>)(\s*)([^<\s][^<]*)", area, re.I)
        if first:
            words = first.group(3).split(" ")
            take = min(3, len(words))
            lead, rest = " ".join(words[:take]), " ".join(words[take:])
            replacement = (
                first.group(1) + first.group(2) + '<strong style="font-weight:700">' + lead + "</strong>"
                + ((" " + rest) if rest else "")
            )
            area = area[:first.start()] + replacement + area[first.end():]

    # курсивная подпись: последний абзац блока
    if not re.search(r"<(?:em|i)\b|font-style\s*:\s*italic", area, re.I):
        paragraphs = list(re.finditer(r"<p\b[^>]*>", area, re.I))
        if paragraphs:
            last = paragraphs[-1]
            tag, changed = _set_style_property(last.group(0), "font-style", "italic")
            if changed:
                area = area[:last.start()] + tag + area[last.end():]

    if area == before:
        return source, False
    return source[:start] + area + source[end:], True


def _fix_ordered_list(source):
    """AF22: <ol> запрещён (T24): заменяем на <ul>."""
    updated = re.sub(r"<(/?)ol\b", r"<\1ul", source, flags=re.I)
    return updated, updated != source


def _fix_page_backgrounds(source):
    """AF23: T09. bgcolor и background-color нужны у body и основной таблицы; цвет берём у соседей."""
    tree = _tree(source)
    nodes = list(_walk(tree.root))
    body = next((n for n in nodes if n.tag == "body"), None)
    main = next(
        (n for n in nodes if n.tag == "table" and re.fullmatch(r"\d+", n.attrs.get("width", ""))
         and 520 <= int(n.attrs["width"]) <= 600), None,
    )

    def own_color(node):
        value = node.attrs.get("bgcolor") or _style(node).get("background-color") or ""
        value = value.strip()
        return value if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value) else ""

    def inherited(node):
        current = node.parent if node is not None else None
        while current is not None:
            color = own_color(current)
            if color:
                return color
            current = current.parent
        return ""

    first_table = next((n for n in nodes if n.tag == "table"), None)
    transforms = []
    for node in (body, main):
        if node is None or (node.attrs.get("bgcolor") and _style(node).get("background-color")):
            continue
        color = own_color(node) or inherited(node)
        if not color and node is body and first_table is not None:
            color = own_color(first_table)
        color = color or "#ffffff"

        def make(color):
            def edit(tag):
                changed = False
                if not re.search(r"\bbgcolor\s*=", tag, re.I):
                    tag = tag[:-1].rstrip() + ' bgcolor="' + color + '">'
                    changed = True
                if not re.search(r"background-color\s*:", tag, re.I):
                    tag, did = _set_style_property(tag, "background-color", color)
                    changed = changed or did
                return tag, changed
            return edit
        transforms.append((node.start_pos, make(color)))
    if not transforms:
        return source, False
    return _edit_node_tags(source, transforms)


_PREHEADER_FILLERS = (" — детали вашего заказа уже готовы к просмотру", ". Сообщим, когда статус изменится")
_PREHEADER_STYLE = (
    ("display", "none"), ("font-size", "1px"), ("line-height", "1px"), ("max-height", "0"),
    ("max-width", "0"), ("opacity", "0"), ("overflow", "hidden"), ("mso-hide", "all"),
)


def _fix_preheader(source):
    """AF24: T10. Прехедер первым в body, полный набор скрывающих стилей, длина не больше 110."""
    body = re.search(r"<body\b[^>]*>", source, re.I)
    if not body:
        return source, False
    original = source
    pattern = r"<body\b[^>]*>(?:\s|<!--[\s\S]*?-->)*(<(?:div|span)\b[^>]*>)([\s\S]*?)(</(?:div|span)\s*>)"
    first = re.search(pattern, source, re.I)
    if not first or not re.search(r"display\s*:\s*none", first.group(1), re.I):
        hidden = re.search(
            r"<(div|span)\b[^>]*display\s*:\s*none[^>]*>[\s\S]*?</\1\s*>", source[body.end():], re.I,
        )
        if hidden:
            block = hidden.group(0)
            at = body.end() + hidden.start()
            source = source[:at] + source[at + len(block):]
            body = re.search(r"<body\b[^>]*>", source, re.I)
            source = source[:body.end()] + block + source[body.end():]
        first = re.search(pattern, source, re.I)
        if not first:
            return source, source != original
    open_tag, text = first.group(1), first.group(2)
    new_tag = open_tag
    flat = re.sub(r"\s", "", (_style_value(open_tag).group(2) if _style_value(open_tag) else "").lower())
    for name, value in _PREHEADER_STYLE:
        if (name + ":" + value) not in flat:
            new_tag, _ = _set_style_property(new_tag, name, value)
    pairs = re.findall(r"(?:&nbsp;&zwnj;|&#160;&#8204;)", text, re.I)
    visible = re.sub(r"(?:&nbsp;&zwnj;|&#160;&#8204;)", "", text, flags=re.I)
    plain = re.sub(r"<[^>]*>", "", visible)
    measured = plain.replace("{{transactionalId}}", "1234567890").strip()
    if len(measured) > 110 and "<" not in visible:
        words, kept = plain.strip().split(), []
        for word in words:
            candidate = " ".join(kept + [word])
            if len(candidate.replace("{{transactionalId}}", "1234567890")) > 108:
                break
            kept.append(word)
        trimmed = " ".join(kept).rstrip(",;:—-– ")
        if len(trimmed.replace("{{transactionalId}}", "1234567890")) >= 50:
            visible = trimmed
    # Слишком короткий прехедер (меньше 50 символов) дополняем нейтральной фразой без новых фактов.
    plain_now = re.sub(r"<[^>]*>", "", visible)
    if "<" not in visible and len(plain_now.replace("{{transactionalId}}", "1234567890").strip()) < 50:
        for filler in _PREHEADER_FILLERS:
            candidate = (plain_now.rstrip(" .,;:") + filler).strip()
            if len(candidate.replace("{{transactionalId}}", "1234567890")) > 108:
                break
            plain_now = candidate
            if len(plain_now.replace("{{transactionalId}}", "1234567890")) >= 50:
                break
        visible = plain_now
    needs_pairs = len(pairs) < 6
    trimmed_now = visible != re.sub(r"(?:&nbsp;&zwnj;|&#160;&#8204;)", "", text, flags=re.I)
    if new_tag == open_tag and not needs_pairs and not trimmed_now:
        return source, source != original
    text = (visible + "&nbsp;&zwnj;" * 6) if (needs_pairs or trimmed_now) else text
    replacement = new_tag + text + first.group(3)
    source = source[:first.start(1)] + replacement + source[first.end(3):]
    return source, source != original


def _fix_round_shapes(source):
    """AF25: круги этапов и знака должны быть квадратными: width == height, line-height по высоте."""
    tree = _tree(source)
    transforms = []
    for node in _walk(tree.root):
        style = _style(node)
        radius = style.get("border-radius", "")
        if not radius:
            continue
        width = re.fullmatch(r"(\d+(?:\.\d+)?)px", style.get("width", "").strip(), re.I)
        height = re.fullmatch(r"(\d+(?:\.\d+)?)px", style.get("height", "").strip(), re.I)
        if node.tag == "td":
            width = width or re.fullmatch(r"(\d+(?:\.\d+)?)", node.attrs.get("width", "").strip())
            height = height or re.fullmatch(r"(\d+(?:\.\d+)?)", node.attrs.get("height", "").strip())
        if not (width and height):
            continue
        w, h = float(width.group(1)), float(height.group(1))
        radius_value = radius.strip()
        full_round = radius_value.endswith("%") and float(re.sub(r"[^\d.]", "", radius_value.split()[0]) or 0) >= 50
        if not full_round and re.fullmatch(r"\d+(?:\.\d+)?px", radius_value, re.I):
            full_round = float(radius_value[:-2]) >= max(w, h) / 2 - 0.5
        # Круг — почти квадратная фигура. Узкие плашки и полоски (32x6) трогать нельзя.
        if not full_round or w > 80 or h > 80 or min(w, h) / max(w, h) < 0.6:
            continue
        side = max(w, h)
        line = style.get("line-height", "")
        wrong_line = bool(node.text.strip() or node.children) and line.strip() != ("%gpx" % side)
        if w == h and not wrong_line:
            continue

        def make(side, node=node):
            def edit(tag):
                changed = False
                for prop in ("width", "height"):
                    tag, did = _set_style_property(tag, prop, "%gpx" % side)
                    changed = changed or did
                tag, did = _set_style_property(tag, "line-height", "%gpx" % side)
                changed = changed or did
                for attr in ("width", "height"):
                    if re.search(r"\b" + attr + r"\s*=", tag, re.I):
                        tag = re.sub(r"(\b" + attr + r"\s*=\s*[\"']?)\d+(?:\.\d+)?", lambda m: m.group(1) + ("%g" % side), tag, count=1, flags=re.I)
                        changed = True
                return tag, changed
            return edit
        transforms.append((node.start_pos, make(side)))
    if not transforms:
        return source, False
    return _edit_node_tags(source, transforms)


def _fix_stretched_circles(source):
    """AF30: A06. Круг-ячейка в одной колонке с подписью растягивается в овал: оборачиваем его в таблицу фиксированной ширины."""
    tree = _tree(source)
    stretched = find_stretched_circles(list(_walk(tree.root)))
    if not stretched:
        return source, False
    edits = []
    for node in stretched:
        side = round_cell_side(node)
        start = node.start_pos
        open_end = _tag_end(source, start)
        close = re.compile(r"</td\s*>", re.I).search(source, open_end)
        if not side or not close or re.search(r"<(?:td|th|table)\b", source[open_end:close.start()], re.I):
            continue
        open_tag = source[start:open_end]
        outer_attrs = "".join(
            " " + found.group(0).strip() for found in (
                re.search(r"\b" + name + r"\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", open_tag, re.I) for name in ("colspan", "rowspan")
            ) if found
        )
        inner_tag = re.sub(r"\s\b(?:colspan|rowspan)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", open_tag, flags=re.I)
        for attr in ("width", "height"):
            if not re.search(r"\s" + attr + r"\s*=", inner_tag, re.I):
                inner_tag = "<td " + attr + '="%g"' % side + inner_tag[3:]
        # Выравнивание внешней ячейки берём у подписи в той же колонке, чтобы круг стоял над подписью.
        align = "left"
        parent_row = node.parent
        table = parent_row.parent if parent_row is not None else None
        while table is not None and table.tag != "table":
            table = table.parent
        if table is not None and parent_row is not None:
            column = [c for c in parent_row.children if c.tag in {"td", "th"}].index(node)
            for row in (r for r in table.children if r.tag in {"tr", "tbody"}):
                for tr in ([row] if row.tag == "tr" else [x for x in row.children if x.tag == "tr"]):
                    cells = [c for c in tr.children if c.tag in {"td", "th"}]
                    if tr is not parent_row and column < len(cells) and _text(cells[column]).strip():
                        label = cells[column]
                        found = (label.attrs.get("align") or _style(label).get("text-align") or "").strip().lower()
                        if found in {"left", "center", "right"}:
                            align = found
                        break
        wrapped = (
            '<td align="%s" valign="top" style="padding:0"%s>' % (align, outer_attrs)
            + '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="%g" '
              'style="width:%gpx;border-collapse:separate"><tr>' % (side, side)
            + inner_tag + source[open_end:close.start()] + "</td></tr></table></td>"
        )
        edits.append((start, close.end(), wrapped))
    if not edits:
        return source, False
    for start, end, wrapped in sorted(edits, reverse=True):
        source = source[:start] + wrapped + source[end:]
    return source, True


_SPACING_PROP = re.compile(r"^(?:padding|margin)(?:-(?:top|right|bottom|left))?$")
_SPAN_TOKEN = re.compile(r"<(/?)span\b[^>]*>", re.I)
MAX_SPAN_SPACING_PX = 8


def _spacing_px(value):
    """Список чисел px из значения spacing или None, если есть auto, em, % и т. п."""
    numbers = []
    for part in value.replace("!important", "").split():
        match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:px)?", part.strip().lower())
        if not match:
            return None
        numbers.append(float(match.group(1)))
    return numbers or None


def _span_sides(prop, numbers):
    """(left, right) для одного свойства spacing."""
    if prop.endswith("-left"):
        return numbers[0], 0.0
    if prop.endswith("-right"):
        return 0.0, numbers[0]
    if prop.endswith(("-top", "-bottom")):
        return 0.0, 0.0
    right = numbers[1] if len(numbers) > 1 else numbers[0]
    left = numbers[3] if len(numbers) > 3 else (numbers[1] if len(numbers) > 1 else numbers[0])
    return left, right


def _fix_span_spacing(source):
    """AF32: T27. margin/padding до 8px на <span> Outlook игнорирует: убираем их и ставим &nbsp; с нужной стороны.

    Больше 8px, значения не в px и span без style не трогаем. Идемпотентно; если число ошибок выросло, возвращаем исходный текст.
    """
    stack, edits = [], []
    for token in _SPAN_TOKEN.finditer(source):
        if token.group(1):
            if stack:
                opened = stack.pop()
                if opened is not None:
                    edits.append((opened, token.start()))
            continue
        tag = token.group(0)
        style = re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)
        plan = None
        if style:
            kept, left, right, drop, valid = [], 0.0, 0.0, False, True
            for declaration in style.group(2).split(";"):
                if ":" not in declaration:
                    if declaration.strip():
                        kept.append(declaration.strip())
                    continue
                name, value = declaration.split(":", 1)
                name_key = name.strip().lower()
                if not _SPACING_PROP.match(name_key):
                    kept.append(declaration.strip())
                    continue
                numbers = _spacing_px(value)
                if numbers is None or len(numbers) > 4 or max(abs(n) for n in numbers) > MAX_SPAN_SPACING_PX or min(numbers) < 0:
                    valid = False
                    break
                drop = True
                side_left, side_right = _span_sides(name_key, numbers)
                left, right = max(left, side_left), max(right, side_right)
            if valid and drop:
                new_style = ";".join(kept) + (";" if kept else "")
                new_tag = tag[:style.start(2)] + new_style + tag[style.end(2):]
                plan = (token.start(), token.end(), new_tag, left, right)
        stack.append(plan)
    if not edits:
        return source, False
    result = source
    pieces = []
    for plan, close_at in edits:
        start, end, new_tag, left, right = plan
        pieces.append((start, end, new_tag + ("&nbsp;" * min(2, max(1, round(left / 4))) if left > 0 else "")))
        if right > 0:
            pieces.append((close_at, close_at, "&nbsp;" * min(2, max(1, round(right / 4)))))
    for start, end, text in sorted(pieces, key=lambda item: (item[0], item[1]), reverse=True):
        result = result[:start] + text + result[end:]
    try:
        if len(_validate(result).errors) > len(_validate(source).errors):
            return source, False
    except Exception:
        return source, False
    return result, result != source


def _fix_text_metrics(source):
    """AF31: T13/T14. Межстрочный интервал 1.4–1.6 у абзацев, пунктов и ячеек с текстом; 12–14px в блоке сообщения -> 15px.

    Не трогаем кружки (у них line-height равен высоте), пустые ячейки-распорки, заголовки и span со знаком валюты.
    """
    marker = _MARKER_PATTERN.search(source)
    message_end = source.lower().find("</tr", marker.end()) if marker else -1
    tree = _tree(source)
    transforms = []
    for node in _walk(tree.root):
        if node.tag not in {"p", "li", "td"} or round_cell_side(node) is not None:
            continue
        own_text = html_text = (node.text or "").replace("\u00a0", " ").strip()
        if not own_text:
            continue
        style = _style(node)
        size = re.fullmatch(r"(\d+(?:\.\d+)?)px", style.get("font-size", "").strip(), re.I)
        size_value = float(size.group(1)) if size else None
        new_size = None
        if (
            marker and node.tag in {"p", "li"} and size_value is not None and 12 <= size_value < 15
            and marker.start() <= node.start_pos < (message_end if message_end > 0 else len(source))
        ):
            new_size = 15
            size_value = 15.0
        line = style.get("line-height", "").strip()
        ratio_value = None
        if re.fullmatch(r"\d+(?:\.\d+)?", line):
            ratio_value = float(line)
        else:
            line_px = re.fullmatch(r"(\d+(?:\.\d+)?)px", line, re.I)
            if line_px and size_value:
                ratio_value = float(line_px.group(1)) / size_value
        new_line = "1.5" if ratio_value is not None and not 1.4 <= ratio_value <= 1.6 else None
        if new_size is None and new_line is None:
            continue

        def make(new_size=new_size, new_line=new_line):
            def edit(tag):
                changed = False
                if new_size is not None:
                    tag, did = _set_style_property(tag, "font-size", "%gpx" % new_size)
                    changed = changed or did
                if new_line is not None:
                    tag, did = _set_style_property(tag, "line-height", new_line)
                    changed = changed or did
                return tag, changed
            return edit
        transforms.append((node.start_pos, make()))
    if not transforms:
        return source, False
    return _edit_node_tags(source, transforms)


def _fix_done_mark(source):
    """AF26: A03. Если в индикаторе нет отметки завершённого этапа, ставим ✓ перед подписью первого этапа."""
    body = re.search(r"<body\b", source, re.I)
    detail = source.find("{{productName}}", body.start() if body else 0)
    marker = _MARKER_PATTERN.search(source)
    if detail < 0 or not marker or detail >= marker.start():
        return source, False
    zone = source[detail:marker.start()]
    if re.search(r"(?:&#10003;|&#x2713;|✓|&#x2714;)", zone, re.I):
        return source, False
    plain = re.sub(r"<[^>]*>", " ", zone)
    if re.search(r"\b(?:готово|завершен\w*|доставлен\w*|выполнен\w*|completed|done)\b", plain, re.I):
        return source, False
    stage = re.search(r">\s*(?:\d+[.)]\s*)?((?:заказ\s+)?(?:подтвержд|принят|оформлен)[^<]*)<", zone, re.I)
    if not stage:
        return source, False
    at = detail + stage.start(1)
    return source[:at] + "&#10003; " + source[at:], True


def _fix_red_orange(source):
    """AF29: P01. Красный/оранжевый сдвигаем в тёплый зелёно-бирюзовый, сохраняя светлоту (контраст не страдает)."""
    changed = False

    def shift(match):
        nonlocal changed
        value = match.group(0)
        rgb = parse_color(value)
        if rgb is None or not is_red_orange(value):
            return value
        _h, light, sat = colorsys.rgb_to_hls(*(c / 255.0 for c in rgb))
        r, g, b = colorsys.hls_to_rgb(0.45, light, min(sat, 0.55))
        changed = True
        return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))

    def in_tag(match):
        tag = match.group(0)
        return re.sub(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", shift, tag)

    updated = re.sub(r"<[a-zA-Z][^>]*>", in_tag, source)
    return updated, changed


_SAFE_REPLACEMENTS = (
    (re.compile(r"прямо\s+сейчас", re.I), "сейчас"),
    # Типовой оборот из прогонов («Вот что происходит сейчас:», «Ниже описано то, что происходит сейчас:»).
    (re.compile(r"описано\s+то,\s+что\s+происходит(?:\s+сейчас|\s+дальше)?", re.I), "описаны наши шаги"),
    (re.compile(r"что\s+происходит(?:\s+сейчас|\s+дальше)?", re.I), "как идёт работа"),
)


def _fix_banned_phrases(source):
    """AF27: S01. Безопасная замена единичных стоп-фраз в видимом тексте (без правки тегов)."""
    changed = False

    def replace_text(match):
        nonlocal changed
        text = match.group(0)
        for pattern, value in _SAFE_REPLACEMENTS:
            def sub(found, value=value):
                word = found.group(0)
                return value.capitalize() if word[:1].isupper() else value
            new = pattern.sub(sub, text)
            if new != text:
                changed = True
                text = new
        return text

    updated = re.sub(r">[^<]+(?=<)", replace_text, source)
    return updated, changed


def _fix_missing_h1(source):
    """AF33: нет <h1>, приветствие лежит в <td>: оборачиваем текст ячейки (22-28px, жирный, с {{name}}) в <h1> с теми же стилями."""
    if re.search(r"<h1\b", source, re.I):
        return source, False
    marker = source.find("Здесь вставляется тело письма")
    limit = marker if marker > 0 else len(source)
    for cell in re.finditer(r"(<td\b([^>]*)>)\s*([^<>]*\{\{\s*name\s*\}\}[^<>]*?)\s*(</td>)", source[:limit], re.I):
        style = _style_value(cell.group(1))
        if not style:
            continue
        size = re.search(r"(?:^|;)\s*font-size\s*:\s*(\d+(?:\.\d+)?)px", style.group(2), re.I)
        if not size or not 22 <= float(size.group(1)) <= 28:
            continue
        keep = [part.strip() for part in style.group(2).split(";")
                if re.match(r"\s*(?:font-family|font-size|font-weight|color|line-height|text-align)\s*:", part, re.I)]
        h1 = '<h1 style="margin:0;padding:0;' + ";".join(keep) + ';">' + cell.group(3).strip() + "</h1>"
        return source[:cell.start(3)] + h1 + source[cell.end(3):], True
    return source, False


def apply(source: str):
    original = source
    fixes = []
    match = re.search(r"<!doctype[\s\S]*?</html\s*>", source, re.I)
    if match:
        prefix, suffix = source[:match.start()], source[match.end():]
        # Keep harmless whitespace around an already-complete document. Otherwise
        # AF01 would be reported on every pass solely because of a trailing newline.
        if prefix.strip() or suffix.strip():
            source = match.group(0)
            if source != original:
                fixes.append("AF01")
    elif re.match(r"\s*<html\b", source, re.I):
        source = "<!DOCTYPE html>" + source
        fixes.append("AF01")
    for name in ALLOWED_VARS:
        # Регистр и пробелы: {{ Name }} -> {{name}}.
        updated = re.sub(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", "{{" + name + "}}", source, flags=re.I)
        if updated != source: fixes.append("AF02")
        source = updated
    # Близкие написания известных переменных: {{transactionid}}, {{transaction_id}}, {{product_name}} (главный источник V02 в прогонах).
    canon = {re.sub(r"[^a-z0-9]", "", name.lower()): name for name in ALLOWED_VARS}
    canon["transactionid"] = "transactionalId"

    import difflib

    def _canonical(match):
        key = re.sub(r"[^a-z0-9]", "", match.group(1).lower())
        if key in canon:
            return "{{" + canon[key] + "}}"
        # Опечатка вроде {{transactionlId}}: одно однозначное совпадение с порогом 0,88; неизвестные переменные не трогаем.
        targets = {canon[k] for k in difflib.get_close_matches(key, list(canon), n=3, cutoff=0.88)}
        if len(targets) == 1:
            return "{{" + targets.pop() + "}}"
        return match.group(0)

    updated = re.sub(r"\{\{\s*([A-Za-z][A-Za-z0-9_\-]*)\s*\}\}", _canonical, source)
    if updated != source: fixes.append("AF02")
    source = updated
    updated = re.sub(r"<!--\s*Здесь\s+вставляется\s+тело\s+письма\s*-->", MARKER, source, flags=re.I)
    if updated != source: fixes.append("AF03")
    source = updated
    updated, balanced = _fix_tag_balance(source)
    if balanced:
        source = updated
        fixes.append("AF19")
    updated, ol_changed = _fix_ordered_list(source)
    if ol_changed:
        source = updated
        fixes.append("AF22")
    updated, h3_changed = _promote_h3(source)
    if h3_changed:
        source = updated
        fixes.append("AF28")
    updated, marker_changed = _fix_marker(source)
    if marker_changed:
        source = updated
        fixes.append("AF13")
    updated, links_changed = _fix_support_links(source)
    if links_changed:
        source = updated
        fixes.append("AF14")
    updated = _fix_comments(source)
    if updated != source:
        source = updated
        fixes.append("AF15")
    before_tables = source
    _TABLE_ATTRS = (("role", "presentation"), ("cellpadding", "0"), ("cellspacing", "0"), ("border", "0"))

    def fix_table_attrs(match):
        attrs = match.group(1)
        for key, value in _TABLE_ATTRS:
            found = re.search(r"\s" + key + r"\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", attrs, re.I)
            if found:
                # Неверное значение (border="1", role="none", cellpadding="8") T08 считает ошибкой: заменяем.
                attrs = attrs[:found.start()] + ' {}="{}"'.format(key, value) + attrs[found.end():]
            else:
                attrs += ' {}="{}"'.format(key, value)
        return "<table" + attrs + ">"

    source = re.sub(r"<table\b([^>]*)>", fix_table_attrs, source, flags=re.I)
    if source != before_tables: fixes.append("AF04")
    preheader = re.search(r"(<(?:div|span)\b[^>]*>)([\s\S]*?)(</(?:div|span)\s*>)", source, re.I)
    if preheader:
        content = preheader.group(2)
        pairs = len(re.findall(r"(?:&nbsp;&zwnj;|&#160;&#8204;)", content, re.I))
        if pairs < 6:
            source = source[:preheader.start(2)] + content + "&nbsp;&zwnj;" * (6 - pairs) + source[preheader.end(2):]
            fixes.append("AF05")
    if "<style" not in source.lower():
        updated = re.sub(r'\s+class\s*=\s*(["\']).*?\1', "", source, flags=re.I | re.S)
        if updated != source:
            source = updated
            fixes.append("AF06")
    def collapse_table(match):
        attrs = match.group(1)
        if "border-radius" not in attrs.lower() or re.search(r"border-collapse\s*:", attrs, re.I):
            return match.group(0)
        style_attr = re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", attrs, re.I | re.S)
        if style_attr:
            attrs = attrs[:style_attr.start(2)] + style_attr.group(2) + ";border-collapse:separate" + attrs[style_attr.end(2):]
        else:
            attrs += ' style="border-collapse:separate"'
        return "<table" + attrs + ">"
    updated = re.sub(r"<table\b([^>]*)>", collapse_table, source, flags=re.I)
    if updated != source:
        source = updated
        fixes.append("AF07")
    updated, rounded_cell_changed, cell_fix = _fix_rounded_table_cells(source)
    if rounded_cell_changed:
        source = updated
        if cell_fix:
            fixes.append("AF16")
        else:
            fixes.append("AF07")
    updated, font_changed = _fix_small_fonts(source)
    if font_changed:
        source = updated
        fixes.append("AF17")
    html_tag = re.search(r"<html\b([^>]*)>", source, re.I)
    lang_fixed = False
    if html_tag:
        attrs = html_tag.group(1)
        lang = re.search(r"\blang\s*=\s*([\"'])(.*?)\1", attrs, re.I)
        updated_attrs = attrs
        if lang:
            if lang.group(2).lower() != "ru":
                updated_attrs = attrs[:lang.start(2)] + "ru" + attrs[lang.end(2):]
        else:
            updated_attrs += ' lang="ru"'
        if updated_attrs != attrs:
            source = source[:html_tag.start(1)] + updated_attrs + source[html_tag.end(1):]
            lang_fixed = True
    head = re.search(r"<head\b[^>]*>([\s\S]*?)</head\s*>", source, re.I)
    if head:
        additions = []
        if not re.search(r"<meta\b[^>]*charset", head.group(1), re.I):
            additions.append('<meta charset="UTF-8">')
        if not re.search(r'<meta\b[^>]*name=["\']viewport', head.group(1), re.I):
            additions.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
        if "x-ua-compatible" not in head.group(1).lower():
            additions.append('<meta http-equiv="X-UA-Compatible" content="IE=edge">')
        if additions:
            source = source[:head.end(1)] + "".join(additions) + source[head.end(1):]
        if additions or lang_fixed:
            fixes.append("AF08")
    elif lang_fixed:
        fixes.append("AF08")
    def sync_background(match):
        attrs = match.group(1)
        bgcolor = re.search(r"\bbgcolor\s*=\s*([\"'])(.*?)\1", attrs, re.I)
        style = re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", attrs, re.I | re.S)
        color_style = re.search(r"background-color\s*:\s*([^;]+)", style.group(2), re.I) if style else None
        color = bgcolor.group(2) if bgcolor else (color_style.group(1).strip().strip("\"'") if color_style else "")
        if not color:
            return match.group(0)
        updated = attrs
        if not bgcolor:
            updated += ' bgcolor="' + color + '"'
        if not color_style:
            if style:
                new_style = style.group(2).rstrip().rstrip(";") + ";background-color:" + color
                updated = updated[:style.start(2)] + new_style + updated[style.end(2):]
            else:
                updated += ' style="background-color:' + color + '"'
        if updated == attrs:
            return match.group(0)
        tag_name = re.match(r"<([a-z0-9]+)", match.group(0), re.I).group(1)
        return "<" + tag_name + updated + ">"
    # body, первая таблица (внешняя обёртка) и основная таблица письма (width 520–600).
    # Раньше правилась только первая <table>, и у основной таблицы bgcolor не появлялся (T09).
    main_table_pattern = r"<table\b((?=[^>]*\bwidth\s*=\s*[\"']?(?:5[2-9]\d|600)\b)[^>]*)>"
    for pattern in (r"<body\b([^>]*)>", r"<table\b([^>]*)>", main_table_pattern):
        updated = re.sub(pattern, sync_background, source, count=1, flags=re.I)
        if updated != source:
            source = updated
            if "AF09" not in fixes:
                fixes.append("AF09")
    link = re.search(r"<a\b([^>]*)>", source, re.I)
    if link:
        style_match = re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", link.group(1), re.I | re.S)
        if style_match and re.search(r"\bcolor\s*:", style_match.group(2), re.I) and not re.search(r"text-decoration\s*:", style_match.group(2), re.I):
            attrs = link.group(1)
            attrs = attrs[:style_match.start(2)] + style_match.group(2) + ";text-decoration:underline" + attrs[style_match.end(2):]
            source = source[:link.start(1)] + attrs + source[link.end(1):]
            fixes.append("AF10")
    font_fallback_changed = False
    quotes_changed = False

    def fix_font_style(style, outer_quote):
        nonlocal font_fallback_changed, quotes_changed

        def normalize_quotes(match):
            nonlocal quotes_changed
            quotes_changed = True
            quote = "'" if outer_quote == '"' else '"'
            return quote + match.group(1) + quote

        updated_style = re.sub(r"&quot;([^&]+)&quot;", normalize_quotes, style, flags=re.I)

        def add_fallback(match):
            nonlocal font_fallback_changed
            value = match.group(2).strip()
            raw_parts = [part.strip() for part in value.split(",")]
            parts = [part.strip("\"'").lower() for part in raw_parts]
            first = parts[0] if parts else ""
            allowed = {"arial", "georgia", "trebuchet ms", "verdana", "tahoma", "helvetica"}
            generic = {"serif", "sans-serif"}
            if first in allowed:
                # Посторонние имена в списке (Geneva, Segoe UI, italic и т.п.) безвредны для вида письма,
                # но валидатор их не принимает: оставляем только разрешённые шрифты и generic-семейства.
                kept = [raw for raw, low in zip(raw_parts, parts) if low in allowed or low in generic]
                if kept != raw_parts:
                    font_fallback_changed = True
                    value = ", ".join(kept)
                    parts = [part.strip("\"'").lower() for part in kept]
                if parts[-1] not in generic:
                    font_fallback_changed = True
                    value += ", serif" if first == "georgia" else ", sans-serif"
            return match.group(1) + value

        return re.sub(r"(font-family\s*:\s*)([^;]+)", add_fallback, updated_style, flags=re.I)

    def fix_tag(match):
        tag = match.group(0)
        style_attr = re.search(r"\bstyle\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)
        if not style_attr:
            return tag
        changed_style = fix_font_style(style_attr.group(2), style_attr.group(1))
        if changed_style == style_attr.group(2):
            return tag
        return tag[:style_attr.start(2)] + changed_style + tag[style_attr.end(2):]

    updated = re.sub(r"<[^>]*>", fix_tag, source, flags=re.S)
    if updated != source:
        source = updated
    if font_fallback_changed:
        fixes.append("AF11")
    if quotes_changed:
        fixes.append("AF12")
    for fix_id, function in (
        ("AF20", _fix_message_styles), ("AF21", _fix_message_structure), ("AF23", _fix_page_backgrounds),
        ("AF24", _fix_preheader), ("AF25", _fix_round_shapes), ("AF30", _fix_stretched_circles), ("AF31", _fix_text_metrics), ("AF32", _fix_span_spacing), ("AF26", _fix_done_mark),
        ("AF27", _fix_banned_phrases), ("AF29", _fix_red_orange), ("AF33", _fix_missing_h1),
    ):
        try:
            updated, did = function(source)
        except Exception:
            continue
        if did:
            source = updated
            fixes.append(fix_id)
    updated, contrast_changed = _fix_contrast(source)
    if contrast_changed:
        source = updated
        fixes.append("AF18")
    return source, list(dict.fromkeys(fixes))
