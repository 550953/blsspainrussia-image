"""Безопасная подстановка демонстрационных значений для предпросмотра."""
import html
import re
from .validator import ALLOWED_VARS

DEMO = {
    "brandName": "Лавка Тестов", "name": "Анна", "productName": "Набор «Тест»",
    "transactionalId": "A-10492", "price": "49.90", "supportEmail": "support@example.com",
    "email": "anna@example.com",
}


def render(source: str, values=None) -> str:
    replacements = dict(DEMO)
    if values:
        replacements.update({key: value for key, value in values.items() if key in ALLOWED_VARS})
    for name in ALLOWED_VARS:
        value = html.escape(str(replacements[name]), quote=True)
        source = source.replace("{{" + name + "}}", value)
    return source
