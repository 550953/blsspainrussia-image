"""Ограниченные механические исправления разметки без изменения текста письма."""
import re
from .validator import ALLOWED_VARS, MARKER


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
    for tag in ("body", "table"):
        updated = re.sub(r"<" + tag + r"\b([^>]*)>", sync_background, source, count=1, flags=re.I)
        if updated != source:
            source = updated
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
    return source, list(dict.fromkeys(fixes))
