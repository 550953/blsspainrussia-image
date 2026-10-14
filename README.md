# BLS OCR + HTML email templates

Сервис распознаёт изображения BLS и по отдельному флагу генерирует адаптивные HTML-шаблоны транзакционных писем. Подсистема писем не меняет OCR API и по умолчанию выключена.

## Включение и запуск

Добавьте в окружение:

```env
TEMPLATE_ENABLED=1
TEMPLATE_API_KEYS=<private-client-key>
TEMPLATE_MODELS=<copy-model-from-GEMINI_MODELS-or-GEMINI_MODEL>
GEMINI_API_KEY_1=<gemini-key>
```

`TEMPLATE_MODELS` — отдельный allow-list для писем; начните с модели, уже используемой OCR (`GEMINI_MODELS`, либо `GEMINI_MODEL`, если цепочка не задана). Не копируйте старые примеры с `gemini-2.5-flash`: эта модель закрыта для новых проектов. `TEMPLATE_API_KEYS` и ключи Gemini храните в Replit Secrets/Render/Infisical, а не в репозитории. Перед подключением submit-клиентов проверьте доступность модели через `/template/selfcheck` (HTTP Basic Auth из `STATUS_USER`/`STATUS_PASSWORD`); маршрут регистрируется при `TEMPLATE_ENABLED=1`. При включении также можно задать `GEMINI_API_KEY_2` и `GEMINI_PROXIES`.

Если одинаковые ошибки сохраняются, последняя попытка по умолчанию перезапускает ремонт с коротким списком обязательных исправлений. Необязательный `TEMPLATE_REPAIR_MODEL=next` переключит её на следующую модель из `TEMPLATE_MODELS`; можно также указать точное имя другой модели из списка.

```bash
curl -u "$STATUS_USER:$STATUS_PASSWORD" http://localhost:8000/template/selfcheck
```

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Задания и идемпотентность хранятся в памяти процесса: запускайте один worker. После перезапуска незавершённое задание нужно отправить повторно; клиенту следует повторять `POST` с теми же `account_id` и `client_request_id`, затем проверять `reused` и продолжать polling по возвращённому `job_id`.

## Клиентский поток

Принимается краткий бриф и параметры из белых списков, а не произвольный промпт. Пример:

```bash
curl -X POST http://localhost:8000/template/submit \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <private-client-key>' \
  -d '{"account_id":"store-1","client_request_id":"mail-42","seed":12345,"currency_method":14}'
```

Опрос `GET /template/result/{job_id}`: обычно раз в 3–5 секунд, используя `poll_after_ms`, если он возвращён сервером; `?html=0` уменьшает ответ до статуса. На `done` проверяйте `template.valid`, `template.report` и `template.attempts`; статус `done` означает, что лучшая версия сохранена, но она может содержать ошибки валидации. При `status=error` смотрите публичный `error` и безопасный `error_detail` (категория/HTTP-статус, модель, имя переменной ключа, число и длительность попыток, число отказов preflight); секреты, прокси URL и содержимое промпта не возвращаются. Если после рестарта вы получили `404`, повторите `POST` с теми же `account_id` и `client_request_id`, затем продолжите опрос по возвращённому `job_id`. Для отмены используйте `POST /template/cancel/{job_id}` с тем же `X-API-Key`.

Для внешнего клиента держите polling не дольше **12 минут от принятия задания**: очередь может ждать до 5 минут, после неё генерации даётся до 7 минут. Результат хранится до 60 минут, но рестарт процесса очищает его раньше.

Дополнительные endpoints:

- `POST /template/validate` — локальная проверка переданного HTML без вызова Gemini.
- `GET /template/rules` — реестр проверок.
- `GET /template/selfcheck` — один короткий вызов первой модели, закрытый HTTP Basic; не чаще 6 раз в минуту.
- `GET /template/result/{job_id}/preview` — HTML-превью, закрытое HTTP Basic.
- `/health` и `/status.json` — состояние сервиса и очередей.

Некорректный JSON/бриф возвращает `400`, слишком большое тело — `413`, а временные лимиты — `429` или `503` с `Retry-After`. `/template/validate` и submit имеют независимые rate limits.

### Основные лимиты и таймауты

Все значения можно переопределить переменными окружения; действующие defaults и минимумы заданы в `ocr_service/config.py`.

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `TEMPLATE_ENABLED` | `0` | Включить API генерации |
| `TEMPLATE_API_KEYS` | пусто | Список клиентских ключей через запятую |
| `TEMPLATE_MODELS` | пусто | Разрешённые модели для писем; задайте модель, уже используемую OCR, и проверьте через `/template/selfcheck` |
| `TEMPLATE_REPAIR_MODEL` | пусто | Необязательная модель для последней попытки при повторяющихся ошибках; `next` выбирает следующую модель из `TEMPLATE_MODELS` |
| `TEMPLATE_PINNED_FALLBACK` | `1` | Если в submit указана `model`, она идёт первой, остальные из `TEMPLATE_MODELS` остаются запасными при 503/429; `0` = строго одна модель |
| `TEMPLATE_MAX_CONCURRENCY` | `3` | Параллельные генерации, ограничиваются доступными ключами |
| `TEMPLATE_RESERVE_KEYS_FOR_OCR` | `5` | Резерв ключей Gemini для OCR |
| `TEMPLATE_YIELD_TO_OCR` | `1` | Приоритет ожидающим/активным OCR-заданиям |
| `TEMPLATE_MAX_ATTEMPTS` | `3` | Генерация + попытки исправления (максимум 5) |
| `TEMPLATE_INFRA_RETRIES` | `4` | Повторы инфраструктурного вызова внутри попытки |
| `TEMPLATE_JOB_DEADLINE_SECONDS` | `420` | Дедлайн генерации после старта |
| `TEMPLATE_QUEUE_MAX_WAIT_SECONDS` | `300` | Максимум ожидания семафора |
| `TEMPLATE_MIN_ATTEMPT_SECONDS` | `90` | Устаревшая настройка; порог ремонта теперь `max(60 с, p50 успешного вызова модели)` |
| `TEMPLATE_MIN_CALL_SECONDS` | `45` | Минимальный остаток для нового вызова Gemini |
| `TEMPLATE_DEADLINE_MARGIN` | `5` | Запас времени до дедлайна на завершение/сохранение |
| `TEMPLATE_HARD_TIMEOUT` | `150` | Жёсткий таймаут одного вызова Gemini |
| `TEMPLATE_HTTP_READ_TIMEOUT` | `130` | Таймаут чтения ответа Gemini |
| `TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT` | `25` | Максимальное ожидание первого байта SSE-ответа |
| `TEMPLATE_STREAM_STALL_TIMEOUT` | `30` | Максимальный простой между частями потокового ответа |
| `TEMPLATE_CONNECT_TIMEOUT` | `8` | Таймаут соединения с Gemini |
| `TEMPLATE_PROXY_PREFLIGHT_TIMEOUT` | `7` | Короткая проверка прокси до аренды ключа; DIRECT пропускает её |
| `TEMPLATE_LEASE_TTL` | `180` | Аренда ключа для вызова (не меньше hard timeout + 30) |
| `TEMPLATE_MODEL_COOLDOWN` | `60` | Пауза пары ключ/модель после краткой квоты |
| `TEMPLATE_QUOTA_BLOCK_SECONDS` | `900` | Временная блокировка пары ключ/модель при дневной квоте |
| `TEMPLATE_CONTRAST_SUGGEST` | `1` | Добавлять конкретный цвет-коррекцию в предупреждение о контрасте |
| `TEMPLATE_AUTOFIX_CONTRAST` | `0` | Автоматически менять только цвет текста при известном фоне; включать после проверки фикстурой |
| `TEMPLATE_SLOW_ROUTE_COOLDOWN` | `600` | Базовый штраф пары ключ/прокси после таймаута, при повторе удваивается |
| `TEMPLATE_API_KEY_MIN_LENGTH` | `24` | Порог предупреждения о коротком клиентском ключе; короткий ключ не выводится и не отклоняется |
| `TEMPLATE_RESULT_TTL_SECONDS` | `3600` | Хранение готового результата |
| `TEMPLATE_MAX_PENDING` | `6` | Максимум активных/ожидающих заданий |
| `TEMPLATE_DAILY_CAP` | `100` | Максимум принятых заданий в UTC-день |
| `TEMPLATE_DAILY_TOKEN_CAP` | `20000000` | Суточный предел токенов |
| `TEMPLATE_BREAKER_FAILS` | `5` | Число последовательных ошибок до паузы |
| `TEMPLATE_BREAKER_PAUSE_SECONDS` | `120` | Длительность паузы breaker |
| `TEMPLATE_MAX_HTML_BYTES` | `300000` | Максимальный размер HTML |
| `TEMPLATE_HTML_ERROR_BYTES` | `98000` | Размер, выше которого результат отклоняется для доставки |
| `TEMPLATE_MAX_OUTPUT_TOKENS` | `16384` | Максимальный ответ модели |
| `TEMPLATE_TEMPERATURE` | `1.0` | Температура первой генерации |
| `TEMPLATE_REPAIR_TEMPERATURE` | `0.4` | Температура исправления |
| `TEMPLATE_THINKING_BUDGET` | `0` | Для Gemini 2.5: thinking tokens; `0` передаётся явно, `-1` включает динамический бюджет |
| `TEMPLATE_THINKING_LEVEL` | `low` | Уровень Gemini 3/4: `minimal`, `low`, `medium`, `high` (поддержка зависит от модели) |
| `TEMPLATE_HISTORY` | `12` | История для разнообразия брифов |
| `TEMPLATE_STORE_MAX` | `100` | Максимум сохранённых заданий |
| `TEMPLATE_VALIDATE_RATE_PER_MIN` | `30` | Лимит валидации на ключ в минуту |
| `TEMPLATE_SUBMIT_RATE_PER_MIN` | `20` | Лимит submit на ключ в минуту |
| `TEMPLATE_DISABLED_RULES` | пусто | Отключение warning-правил по ID |

Локальный пример `.env` (не добавляйте файл с реальными ключами в git):

```env
TEMPLATE_ENABLED=0
TEMPLATE_API_KEYS=replace-with-private-client-key
TEMPLATE_MODELS=gemini-3.1-flash-lite
GEMINI_API_KEY_1=replace-with-secret
TEMPLATE_MAX_CONCURRENCY=3
TEMPLATE_RESERVE_KEYS_FOR_OCR=5
TEMPLATE_YIELD_TO_OCR=1
TEMPLATE_MAX_ATTEMPTS=3
TEMPLATE_JOB_DEADLINE_SECONDS=420
TEMPLATE_QUEUE_MAX_WAIT_SECONDS=300
TEMPLATE_DAILY_CAP=100
TEMPLATE_DAILY_TOKEN_CAP=20000000
TEMPLATE_PROXY_PREFLIGHT_TIMEOUT=7
TEMPLATE_STREAM_FIRST_BYTE_TIMEOUT=25
TEMPLATE_STREAM_STALL_TIMEOUT=30
TEMPLATE_SLOW_ROUTE_COOLDOWN=600
TEMPLATE_AUTOFIX_CONTRAST=0
TEMPLATE_THINKING_BUDGET=0
TEMPLATE_THINKING_LEVEL=low
```

Ключи API и модельные ключи берутся из Replit Secrets/Render/Infisical. Не помещайте их в `.env.example`, логи или клиентский код. Клиент не присылает произвольный промпт генерации: сервер строит его из брифа и белых списков. Поле `TEMPLATE_THINKING_BUDGET=0` не превращается в `None`: для поддерживаемых моделей Gemini 2.5 передаётся именно нулевой бюджет; `-1` означает динамический бюджет. Для Gemini 2.5 Pro ноль не поддерживается и настройка пропускается — модель сохраняет своё thinking по умолчанию.

Thinking-параметр выбирается по модели в `thinkingConfig`, а не отправляется неподходящему семейству:

| Семейство/модель | Поле API | Поддержка и поведение |
|---|---|---|
| Gemini 2.5 Flash / Flash Lite | `thinkingBudget` | `0..24576`, `0` явно выключает thinking, `-1` — динамический бюджет |
| Gemini 2.5 Pro | `thinkingBudget` | `128..32768` или `-1`; отключить thinking нельзя, при `0` используется default модели |
| Gemini 3.8 / 3.7 Flash | `thinkingLevel` | `low`, `medium`, `high`; `minimal` не поддерживается |
| Gemini 3.6 Flash, 3.5/3.1 Flash-Lite, Gemini 3 Flash | `thinkingLevel` | `minimal`, `low`, `medium`, `high` |
| Gemini 4 Argon | `thinkingLevel` | `low`, `medium`, `high`; отключить thinking нельзя |
| Остальные модели | — | параметр не посылается, действует настройка модели по умолчанию |

Точная совместимость может меняться вместе с каталогом Gemini; при переходе на другую модель сверьте [официальную таблицу thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking). `tpl_gemini_attempt` пишет только число `thoughts` из usage и применённую настройку, не текст внутреннего рассуждения.

`TEMPLATE_DAILY_TOKEN_CAP=20000000` выбран по оценке, а не по live Gemini-замерам: текущие system prompt и стартовый бриф занимают около 12,662 символов (примерно 3,166 токенов при грубой оценке 4 символа/токен); при стандартных 3 попытках и `maxOutputTokens=16384` предыдущие ответы также повторяются в repair-контексте. Грубая верхняя оценка — около 108 тыс. токенов на задание и 10.8 млн на 100 заданий; 20 млн оставляет примерно 1.8× запас на repair-текст и вариативность токенизации. Запросы могут разрешить до 5 попыток, поэтому дневной лимит остаётся жёстким предохранителем и при такой нагрузке может остановить последующие repair-попытки раньше.

Прокси генерации перед арендой Gemini-ключа проверяется отдельным коротким GET к Google API (лимит 7 секунд). Любой HTTP-ответ подтверждает транспортную доступность; сетевой сбой помещает только этот канал в cooldown, не использует ключ и не считается API-попыткой. `DIRECT` пропускает preflight. Для генерации используется SSE: первый байт ограничен 25 секундами, пауза между частями — 30 секундами; резервный `generateContent` вызывается только при недоступности потокового endpoint, не после таймаута. При таймауте штрафуется конкретная пара (ключ, прокси): сначала 10 минут, затем удвоение при повторном таймауте; следующая попытка предпочитает другие ключ и маршрут. `/status.json` показывает p50/p95 длительности вызовов отдельно от OCR, с разбивкой по модели, имени ключа и безопасной метке прокси. `/health.template.slow_route_penalties` показывает число активных штрафов; короткие `TEMPLATE_API_KEYS` дают безопасное предупреждение без вывода значения. Секреты и URL с логином/паролем не публикуются.

Ремонту оставляется отдельный бюджет: первая генерация ограничивается дедлайном с резервом под repair-вызов. Порог начала ремонта рассчитывается по p50 успешных вызовов выбранной модели, минимум 60 секунд. Если времени меньше, лучшая готовая версия сохраняется с `generation_error=repair_skipped_no_time`, второй вызов не начинается. `api_trace` результата и `tpl_api_attempt` фиксируют ожидание OCR/ключа, preflight, длительность вызова, модель, имя ключа, безопасную метку прокси, исход и HTTP-статус.

### Окна и аренда: OCR против email

| Параметр | OCR | Email generation |
|---|---|---|
| Профиль сетевого вызова | Short: connect 8с, HTTP 15с, hard timeout 45с | Long: connect 8с, чтение до 130с, hard timeout 150с; preflight прокси до 7с до аренды ключа |
| Аренда Gemini-ключа | 60с по умолчанию | `max(TEMPLATE_LEASE_TTL, hard timeout + 30с)`, по умолчанию 180с |
| Дедлайн задания | 60с (`OCR_JOB_MAX_SECONDS`) | 420с после запуска генерации |
| Очередь / параллельность | До 8 OCR workers | Ожидание семафора до 300с; до 3 параллельных задач с учётом резерва 5 ключей OCR |
| Очередь и дневные ограничения | Общий OCR job pool, результат хранится 300с | До 6 pending/running, 100 принятых заданий и 20 млн учтённых токенов за UTC-день |
| Результат | Сохраняется в in-memory OCR job | In-memory до 3600с; restart теряет задания |

Локальная проверка интеграции (fake Gemini API и локальные HTTP-прокси, внешний Gemini не вызывается): `TEMPLATE_SMOKE_READ_TIMEOUT=0.15 PYTHONPATH=. python scripts/smoke_template.py`. Она печатает временную шкалу всех 13 сценариев, включая 400/403/404, медленный живой ответ и оба исхода отмены, и завершает работу с ненулевым кодом, если нарушен хотя бы один итоговый инвариант. Таймаут здесь искусственно укорочен только для smoke-теста.

### Ограничения и эксплуатация

- `TEMPLATE_JOBS` — отдельное in-memory хранилище заданий писем, не общий `JOBS` OCR. Это сохраняет независимые формы данных и исключает влияние письма на OCR-метрики; после перезапуска задания и idempotency-кэш теряются.
- Запускайте один worker. Приложение создаёт email-семафор лениво при первой генерации и рассчитывает его размер по доступным ключам/резерву в тот момент; он не пересчитывается при изменении ключей или резерва без перезапуска процесса.
- In-memory rate limits, дневные счётчики, circuit breaker и история брифов относятся к процессу; суточные лимиты сбрасываются на границе UTC-дня. Не используйте несколько реплик как общий планировщик/хранилище.
- `TEMPLATE_ENABLED=0` снимает маршруты `/template/*`, не меняя `/ocr/*`. Клиент `/template/validate` может проверять HTML локально внутри сервиса и не расходует токены Gemini.
- Параллельность писем ограничена `TEMPLATE_MAX_CONCURRENCY` и количеством доступных ключей сверх `TEMPLATE_RESERVE_KEYS_FOR_OCR`. Для 429 cooldown задаётся на пару ключ/модель; глобальные состояния перегрузки модели могут быть общими с OCR, если совпадают имена моделей.
- OCR-эндпоинты `/ocr/*` остаются без API-аутентификации, как в v4.6; этот релиз не меняет их доступность.

### Деплой и откат

1. До включения генерации оставьте `TEMPLATE_ENABLED=0`; установите переменные и секреты, затем перезапустите сервис.
2. Проверьте прежние `/health`, `/status.json` и `/ocr`:

   ```bash
   export BASE_URL=https://<service>
   curl -fsS "$BASE_URL/health"
   curl -fsS -u "$STATUS_USER:$STATUS_PASSWORD" "$BASE_URL/status.json"
   IMAGE_B64="$(python -c 'import base64; print(base64.b64encode(open("sample.png","rb").read()).decode())')"
   curl -fsS -X POST "$BASE_URL/ocr" -H 'Content-Type: application/json' \
     -d "{\"images\":\"$IMAGE_B64\"}"
   ```

3. После установки `TEMPLATE_API_KEYS` и `TEMPLATE_MODELS` включите флаг в закрытой/staging-среде и перезапустите; до подключения submit-клиентов проверьте `/template/selfcheck` (Basic Auth) и `/template/rules`.
4. Проверьте хороший HTML без вызова Gemini, затем создайте одно задание и откройте результат/превью:

   ```bash
   export TEMPLATE_KEY='<private-client-key>'
   jq -Rs '{html: ., currency_method: 1}' tests/fixtures/good.html > /tmp/template-validate.json
   curl -fsS -X POST "$BASE_URL/template/validate" -H "X-API-Key: $TEMPLATE_KEY" \
     -H 'Content-Type: application/json' --data-binary @/tmp/template-validate.json
   SUBMIT="$(curl -fsS -X POST "$BASE_URL/template/submit" -H "X-API-Key: $TEMPLATE_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"account_id":"deploy-smoke","client_request_id":"deploy-smoke-1","seed":12345,"currency_method":1}')"
   JOB_ID="$(printf '%s' "$SUBMIT" | jq -r .job_id)"
   curl -fsS -H "X-API-Key: $TEMPLATE_KEY" "$BASE_URL/template/result/$JOB_ID?html=0"
   curl -fsS -u "$STATUS_USER:$STATUS_PASSWORD" "$BASE_URL/template/result/$JOB_ID/preview"
   ```

   Повторяйте `GET /template/result/{job_id}` по `poll_after_ms`, пока статус не станет финальным, и проверьте `template.valid`.
5. Запустите `scripts/eval_templates.py --base-url "$BASE_URL" --api-key "$TEMPLATE_KEY" --n 20 --out eval_out`, затем при наличии тестового изображения — `scripts/isolation_check.py --base-url "$BASE_URL" --api-key "$TEMPLATE_KEY" --image sample.png`. Сравните OCR p95 и убедитесь, что доступные ключи не опускаются ниже резерва.
6. Для отката задайте `TEMPLATE_ENABLED=0` и перезапустите процесс. Это отключит только маршруты генерации; OCR остаётся доступным. Откатить код можно отдельно, если это необходимо.

## Проверка качества

```bash
pip install -r requirements-dev.txt
pytest -q
```

`tests/fixtures/good.html` — эталон валидного письма. После деплоя можно проверить HTML без расхода токенов через `/template/validate`; `scripts/eval_templates.py` делает серию живых запусков, сохраняет HTML локально и формирует сводку, а `scripts/isolation_check.py` сравнивает latency OCR до и во время двух email jobs. Эти скрипты обращаются к работающему сервису и могут расходовать токены Gemini.

Для `eval_templates.py` базовый URL и клиентский ключ можно задавать через `TEMPLATE_BASE_URL` и `TEMPLATE_API_KEY`, не передавая ключ аргументом процесса. PowerShell-пример сравнения двух моделей:

```powershell
$env:TEMPLATE_BASE_URL = "https://<service>"
$env:TEMPLATE_API_KEY = "<client-key>"
python .\scripts\eval_templates.py --n 10 --model gemini-3.1-flash-lite --out eval_31
python .\scripts\eval_templates.py --n 10 --model gemini-3.8-flash --out eval_38
```

Для другой проверки:

```bash
python scripts/isolation_check.py --base-url https://<service> --api-key '<client-key>' --image sample.png
```
