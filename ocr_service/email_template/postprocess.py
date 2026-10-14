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
_HEADING = "\x01"
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
        if tag == "body":
            # Незакрытый или битый <head> (например, «</>») не должен скрывать всё письмо.
            self.stack.clear()
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
                if tag in ("h1", "h2", "h3"):
                    self.out.append(_HEADING)  # служебная метка: заголовок не бывает этапом индикатора
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


_VALUE_TOKEN = re.compile(r"^(?:[$€£₽]|USD|EUR|RUB|\{\{price\}\})$", re.I)
_MARKER = re.compile(r"[✓✔•■▪●○◦·]|\d")
_DONE = "✓✔"
_MAX_STAGES = 5


def _is_stage(line: str) -> bool:
    cand = line.strip()
    return bool(cand) and len(cand) <= 40 and ":" not in cand and not cand.startswith("- ") \
        and not re.search(r"[.!?,]$", cand) and "{{" not in cand and not cand.startswith(_HEADING)


def _collect_stages(lines: List[str], start: int):
    """Собирает индикатор этапов от строки start: маркеры (✓ • ■ цифра) и подписи этапов.
    Возвращает (этапы [(подпись, пройден)], индекс после последней подписи, были ли маркеры)."""
    queue, stages, seen_marker, end, k = [], [], False, start, start
    while k < len(lines) and len(stages) < _MAX_STAGES:
        cand = lines[k].strip()
        if not cand:
            k += 1
            continue
        if cand.startswith(_HEADING):
            break
        if _MARKER.fullmatch(cand):
            queue.append(cand)
            seen_marker = True
            k += 1
            continue
        done, label = False, cand
        if cand[0] in _DONE and len(cand) > 1:
            done, label, seen_marker = True, cand[1:].strip(), True
        if not _is_stage(label):
            break
        if queue and queue.pop(0) in _DONE:
            done = True
        stages.append((label, done))
        k += 1
        end = k
    return stages, end, seen_marker


def _tidy_plain_lines(text: str) -> str:
    """Лечит раскладку таблиц в text/plain: «Сумма: $ {{price}}» одной строкой, индикатор этапов одной строкой, одиночные символы."""
    lines = [line.strip() for line in text.split("\n")]
    # 1) «Сумма:» + знак валюты + значение -> одна строка
    merged, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.endswith(":") and "{{" not in line[:-1] and not line.startswith(_HEADING):
            j, tokens = i + 1, []
            while j < len(lines) and len(tokens) < 3:
                if not lines[j]:
                    j += 1
                    continue
                if len(lines[j]) <= 12 and _VALUE_TOKEN.match(lines[j]):
                    tokens.append(lines[j])
                    j += 1
                    continue
                break
            if tokens:
                merged.extend([line + " " + " ".join(tokens), ""])
                i = j
                continue
        merged.append(line)
        i += 1
    # 2) индикатор этапов (3–5 подписей с маркерами) -> «Статус: ✓ А → Б (сейчас) → В»
    result, i = [], 0
    while i < len(merged):
        cand = merged[i]
        if cand and (_MARKER.fullmatch(cand) or (cand[0] in _DONE and len(cand) > 1)):
            stages, end, seen = _collect_stages(merged, i)
            if seen and len(stages) >= 3:
                current = next((k for k, (_, done) in enumerate(stages) if not done), None)
                has_done = any(done for _, done in stages)
                parts = []
                for k, (label, done) in enumerate(stages):
                    if done:
                        label = "✓ " + label
                    elif k == current and has_done:
                        label += " (сейчас)"
                    parts.append(label)
                result.extend(["", "Статус: " + " → ".join(parts), ""])
                i = end
                continue
        result.append(cand)
        i += 1
    # 3) одиночные символы отдельными строками не печатаем (✓ переезжает к следующей подписи)
    out, pending = [], ""
    for line in result:
        line = line.replace(_HEADING, "")
        bare = line.strip()
        if bare and len(bare) <= 2 and not re.search(r"[^\W_]{2}", bare) and not re.search(r"\{|\}", bare) and not re.fullmatch(r"[$€£₽]", bare):
            if bare == "✓":
                pending = "✓ "
            continue
        if bare and pending:
            line = pending + bare
            pending = ""
        out.append(line)
    return "\n".join(out)


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
    text = _tidy_plain_lines("\n".join(lines))
    text = text.replace(_HEADING, "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(- [^\n]*)\n\n(?=- )", r"\1\n", text).strip()
    return text + "\n" if text else ""
