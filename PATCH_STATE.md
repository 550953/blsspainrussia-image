# PATCH_STATE

Раунд 2 (prompt 1.5): сделан, см. CHANGES_prompt_1.5.md.

## Раунд 3
- [x] Пакет A. text/plain: ocr_service/email_template/postprocess.py (_tidy_plain_lines, сброс стека на <body>),
      tests/test_plain_text_layout.py, фикстуры tests/fixtures/real_batch_lite_01..10.html (взяты досрочно из D.5).
- [ ] Пакет B. Пулы 30+, история при малом пуле, запреты и S14, промпт про «три шага».
- [ ] Пакет C. T26/T27.
- [ ] Пакет D. where, группировка предупреждений, TEMPLATE_KEEP_ATTEMPT_HTML, метрики eval, --seed-base.

Пакет A-fix (после ревью): «(сейчас)» определяется по маркерам (✓ = пройден, первый непройденный = текущий; если пройденных нет или все пройдены — метки нет);
индикатор 3–5 этапов, не привязан к «Сумма:»; заголовки h1–h3 не считаются этапами (служебная метка в парсере);
три короткие строки без маркеров статусом не становятся; после склейки суммы сохраняется пустая строка.
Устаревший тест prompt_version в test_api_contract.py теперь берёт PROMPT_VERSION.

Проверено: python -m pytest -q → 344 passed, 1 skipped (pytest, httpx, fastapi, uvicorn установлены).
Живой Gemini не запускался.
Следующий пакет: B.
