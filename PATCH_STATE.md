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
Пакет D (частично, сделан раньше B ради доказательной базы по V02):
- D.1 where: validator._fill_where заполняет фрагмент до 120 символов у каждой ошибки и предупреждения (V02, S01, A06, T10 и др. по месту; остальным «весь документ: …»).
- D.3 флаг TEMPLATE_KEEP_ATTEMPT_HTML (по умолчанию 0): до 3 попыток по 40 КБ (raw_html и final_html) хранятся в job; отдаются через GET /template/result/{id}?attempts_html=1 только с X-API-Key; без флага параметр игнорируется.
- error_details: severity + where, до 10; raw_error_details теперь по ВСЕМ правилам (раньше только T10/T17/T18/V08/A06), до 10.
- Не сделано: D.2 группировка T14 (count/suppressed), D.4 метрики eval и --seed-base, D.5 мутации для D.2.
Тесты: tests/test_where_and_attempt_html.py. pytest: 350 passed (1 плавающий тест test_rate_limit_quota_is_model_scoped… иногда падает на сравнении времени == 900; не связан с правками).
Живой Gemini не запускался.
Следующий пакет: B (затем D.2/D.4, C).
