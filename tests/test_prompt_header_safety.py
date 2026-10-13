from ocr_service.email_template.director import HEADERS
from ocr_service.email_template.prompt import PROMPT_VERSION, system_instruction


def test_header_prompt_prevents_duplicate_wordmarks_and_overlap():
    prompt = system_instruction()

    assert PROMPT_VERSION == "1.2"
    assert "покажи текст {{brandName}} ровно один раз" in prompt
    assert "Декоративный акцент должен быть без текста" in prompt
    assert "Не используй position:absolute/fixed" in prompt
    assert "Структурные контракты (проверяются автоматически)" in prompt
    assert "должен быть ровно один и стоять вплотную" in prompt
    assert "{{supportEmail}} используй ровно один раз и только в href" in prompt
    assert "Название длиной 3–40 символов" in prompt
    assert "Не используй text-transform:uppercase" in prompt
    assert not any("начальной буквы бренда" in header for header in HEADERS)
    assert any("без наложения" in header for header in HEADERS)
