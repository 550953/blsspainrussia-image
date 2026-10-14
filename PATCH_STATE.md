# PATCH_STATE

Раунд 2 (prompt 1.5): сделан, см. CHANGES_prompt_1.5.md.

## Раунд 3
- [x] Пакет A. text/plain: ocr_service/email_template/postprocess.py (_tidy_plain_lines, сброс стека на <body>),
      tests/test_plain_text_layout.py, фикстуры tests/fixtures/real_batch_lite_01..10.html (взяты досрочно из D.5).
- [ ] Пакет B. Пулы 30+, история при малом пуле, запреты и S14, промпт про «три шага».
- [ ] Пакет C. T26/T27.
- [ ] Пакет D. where, группировка предупреждений, TEMPLATE_KEEP_ATTEMPT_HTML, метрики eval, --seed-base.

Проверено без pytest/httpx/fastapi (мини-раннер): 175/177, два падения — отсутствие httpx.
Живой Gemini не запускался.
Следующий пакет: B.
