"""Детерминированный постпроцессор письма (без LLM) и text/plain.

Запускается после автофикса и валидации. Тексты, цвета и структуру не трогает:
добавляет только то, что улучшает отображение в почтовых клиентах:
  PP01  <meta name="x-apple-disable-message-reformatting"> (Apple Mail / iOS не подгоняют письмо)
  PP02  <meta name="color-scheme"> и <meta name="supported-color-schemes"> (Gmail это в основном игнорирует)
  PP03  -webkit-text-size-adjust:100% (и -ms-) на <body> (iOS/Android не раздувают шрифт)
  PP04  mso-table-lspace/rspace:0pt на таблицах (Outlook не добавляет боковые отступы)
Идемпотентно. Откат при ухудшении валидации делает вызывающий код (generate.py).
"""
import html as _html
import re
from html.parser import HTMLParser
from typing import List, Tuple

_HEAD_CLOSE = re.compile(r"</head\s*>", re.I)
_BODY_OPEN = re.compile(r"<body\b([^>]{0,2000})>", re.I)
_TABLE_OPEN = re.compile(r"<table\b([^>]{0,2000})>", re.I)
_STYLE_ATTR = re.compile(r"(\sstyle\s*=\s*)([\"'])(.*?)\2", re.I | re.S)

BODY_PROPS = (("-webkit-text-size-adjust", "100%"), ("-ms-text-size-adjust", "100%"))
TABLE_PROPS = (("mso-table-lspace", "0pt"), ("mso-table-rspace", "0pt"))
METAS = (
    ("PP01", r"x-apple-disable-message-reformatting", '<meta name="x-apple-disable-message-reformatting">'),
    ("PP02", r"""name\s*=\s*["']color-scheme["']""", '<meta name="color-scheme" content="light">'),
    ("PP02", r"""name\s*=\s*["']supported-color-schemes["']""", '<meta name="supported-color-schemes" content="light">'),
)


def _has_prop(style: str, name: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(name) + r"\s*:", style, re.I) is not None


def _add_props(attrs: str, props) -> Tuple[str, bool]:
    """Дописывает недостающие свойства в style внутри строки атрибутов тега."""
    m = _STYLE_ATTR.search(attrs)
    if m:
        value = m.group(3)
        missing = [(k, v) for k, v in props if not _has_prop(value, k)]
        if not missing:
            return attrs, False
        base = value.rstrip().rstrip(";")
        extra = ";".join("%s:%s" % kv for kv in missing)
        new_value = (base + ";" if base else "") + extra
        return attrs[:m.start(3)] + new_value + attrs[m.end(3):], True
    extra = ";".join("%s:%s" % kv for kv in props)
    return attrs.rstrip() + ' style="%s"' % extra, True


def apply(source: str) -> Tuple[str, List[str]]:
    """Возвращает (html, [id применённых правок])."""
    ids: List[str] = []
    out = source or ""

    # PP01/PP02: метатеги перед </head>
    head = _HEAD_CLOSE.search(out)
    if head:
        add = []
        for rule_id, pattern, tag in METAS:
            if not re.search(pattern, out, re.I):
                add.append(tag)
                if rule_id not in ids:
                    ids.append(rule_id)
        if add:
            out = out[:head.start()] + "".join(add) + out[head.start():]

    # PP03: body
    m = _BODY_OPEN.search(out)
    if m:
        attrs, changed = _add_props(m.group(1), BODY_PROPS)
        if changed:
            out = out[:m.start()] + "<body" + attrs + ">" + out[m.end():]
            ids.append("PP03")

    # PP04: таблицы
    changed_any = False

    def _table(match):
        nonlocal changed_any
        attrs, changed = _add_props(match.group(1), TABLE_PROPS)
        if changed:
            changed_any = True
            return "<table" + attrs + ">"
        return match.group(0)

    out = _TABLE_OPEN.sub(_table, out)
    if changed_any:
        ids.append("PP04")
    return out, ids


# ---------------------------------------------------------------- text/plain

_SKIP = {"head", "title", "style", "script"}
_VOID = {"br", "hr", "meta", "link", "img", "input", "area", "base", "col", "embed", "param", "source", "track", "wbr"}
_BLOCK_START = {"p", "div", "h1", "h2", "h3", "ul", "ol", "table", "tr"}
_BLOCK_END = {"p", "h1", "h2", "h3", "ul", "ol", "table"}


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.stack: List[Tuple[str, bool]] = []
        self._link = None  # (адрес mailto, позиция в out)

    def _hidden(self) -> bool:
        return any(flag for _, flag in self.stack)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        style = dict((k.lower(), v or "") for k, v in attrs).get("style", "")
        hidden = tag in _SKIP or re.search(r"display\s*:\s*none", style, re.I) is not None
        if tag in _VOID:
            if not self._hidden():
                if tag == "br":
                    self.out.append("\n")
                elif tag == "hr":
                    self.out.append("\n--------\n")
            return
        if tag == "a" and not self._hidden():
            href = dict((k.lower(), v or "") for k, v in attrs).get("href", "").strip()
            if href.lower().startswith("mailto:"):
                self._link = (href[7:].split("?")[0], len(self.out))
        if not self._hidden() and not hidden:
            if tag == "li":
                self.out.append("\n- ")
            elif tag in _BLOCK_START:
                self.out.append("\n")
        self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID and self.stack:
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        idx = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                idx = i
                break
        if idx is None:
            return
        was_hidden = self._hidden()
        del self.stack[idx:]
        if was_hidden and not self._hidden() and tag not in _SKIP:
            return
        if self._hidden():
            return
        if tag == "a" and self._link is not None:
            addr, start = self._link
            self._link = None
            if addr and addr not in "".join(self.out[start:]):
                self.out.append(" (%s)" % addr)
        if tag in ("td", "th"):
            self.out.append(" ")
        elif tag in _BLOCK_END:
            self.out.append("\n\n")
        elif tag in ("tr", "li", "div"):
            self.out.append("\n")

    def handle_data(self, data):
        if not self._hidden():
            self.out.append(data)


def plain_text(source: str) -> str:
    """text/plain версия письма. Плейсхолдеры {{...}} остаются как есть."""
    parser = _PlainText()
    try:
        parser.feed(source or "")
        parser.close()
    except Exception:
        return ""
    text = "".join(parser.out)
    text = _html.unescape(text).replace("\xa0", " ")
    for ch in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
        text = text.replace(ch, "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(- [^\n]*)\n\n(?=- )", r"\1\n", text).strip()
    return text + "\n" if text else ""
