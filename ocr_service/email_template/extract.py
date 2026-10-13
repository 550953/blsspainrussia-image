"""Извлечение HTML-документа и концепции из ответа модели."""
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Parsed:
    concept: str = ""
    html: str = ""
    tail: str = ""
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def codes(self) -> list:
        return [item["rule"] for item in self.errors + self.warnings]

    @property
    def ok(self) -> bool:
        return bool(self.html) and not self.errors


def parse(text: str, finish_reason: str = "", prompt_block_reason: str = "") -> Parsed:
    text = (text or "").lstrip("\ufeff \t\r\n").replace("\r\n", "\n").replace("\r", "\n")
    finish = str(finish_reason or "").upper()
    if prompt_block_reason or finish in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"}:
        return Parsed("", "", "", [{"rule": "X03", "message": "Модель заблокировала ответ"}], [])
    blocks = list(re.finditer(r"```(?:html)?\s*([\s\S]*?)```", text, re.I))
    selected = next((block for block in blocks if re.search(r"<!doctype\s+html", block.group(1), re.I)), None)
    source = selected.group(1).strip() if selected else text
    match = re.search(r"<!doctype\s+html", source, re.I)
    if not match:
        return Parsed("", "", "", [{"rule": "X01", "message": "В ответе нет HTML-документа"}], [])
    start = match.start()
    end = re.search(r"</html\s*>", source[start:], re.I)
    stop = start + end.end() if end else len(source)
    html = source[start:stop].strip()
    concept_source = text[:selected.start()] if selected else source[:start]
    concept = re.sub(r"```(?:html)?", "", concept_source, flags=re.I).strip()[:600]
    tail_source = text[selected.end():] if selected else source[stop:]
    tail = tail_source.strip()[:400]
    warnings = []
    if len(re.findall(r"<!doctype\s+html", text, re.I)) > 1:
        warnings.append({"rule": "X04", "message": "В ответе несколько HTML-документов; использован первый"})
    errors = []
    if not end:
        errors.append({"rule": "X02", "message": "HTML обрезан"})
    if finish == "MAX_TOKENS":
        errors.append({"rule": "X02", "message": "Достигнут лимит токенов; HTML может быть неполным"})
    return Parsed(concept, html, tail, errors, warnings)
