"""Ограниченные механические исправления разметки без изменения текста письма."""
import re
from ..config import TEMPLATE_AUTOFIX_CONTRAST
from .contrast import color_tokens, parse_color, ratio, suggest_fix
from .validator import ALLOWED_VARS, MARKER, _Tree, _style, _text, _walk


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
        updated = re.sub(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", "{{" + name + "}}", source)
        if updated != source: fixes.append("AF02")
        source = updated
    updated = re.sub(r"<!--\s*Здесь\s+вставляется\s+тело\s+письма\s*-->", MARKER, source, flags=re.I)
    if updated != source: fixes.append("AF03")
    source = updated
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
    source = re.sub(
        r"<table\b([^>]*)>",
        lambda m: "<table" + m.group(1) +
        "".join(' {}="{}"'.format(k, v) for k, v in (("role", "presentation"), ("cellpadding", "0"), ("cellspacing", "0"), ("border", "0")) if not re.search(r"\b" + k + r"\s*=", m.group(1), re.I))
        + ">",
        source, flags=re.I,
    )
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
            parts = [part.strip().strip("\"'").lower() for part in value.split(",")]
            first = parts[0] if parts else ""
            allowed = {"arial", "georgia", "trebuchet ms", "verdana", "tahoma", "helvetica"}
            if first in allowed and parts[-1] not in {"serif", "sans-serif"}:
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
    updated, contrast_changed = _fix_contrast(source)
    if contrast_changed:
        source = updated
        fixes.append("AF18")
    return source, list(dict.fromkeys(fixes))
