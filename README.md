# BLS OCR + HTML email templates

Сервис распознаёт изображения BLS и по отдельному флагу генерирует адаптивные HTML-шаблоны транзакционных писем. Подсистема писем не меняет OCR API и по умолчанию выключена.

## Включение и запуск

Добавьте в окружение:

```env
TEMPLATE_ENABLED=1
TEMPLATE_API_KEYS=<private-client-key>
TEMPLATE_MODELS=<supported-gemini-model>
GEMINI_API_KEY_1=<gemini-key>
```

`TEMPLATE_API_KEYS` и ключи Gemini храните в Replit Secrets/Render/Infisical, а не в репозитории. `TEMPLATE_MODELS` — отдельный allow-list для писем. При включении также можно задать `GEMINI_API_KEY_2` и `GEMINI_PROXIES`.

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

Опрос `GET /template/result/{job_id}`: обычно раз в 3–5 секунд, используя `poll_after_ms`, если он возвращён сервером; `?html=0` уменьшает ответ до статуса. На `done` проверяйте `template.valid`, `template.report` и `template.attempts`; статус `done` означает, что лучшая версия сохранена, но она может содержать ошибки валидации. Если после рестарта вы получили `404`, повторите `POST` с теми же `account_id` и `client_request_id`, затем продолжите опрос по возвращённому `job_id`. Для отмены используйте `POST /template/cancel/{job_id}` с тем же `X-API-Key`.

Дополнительные endpoints:

- `POST /template/validate` — локальная проверка переданного HTML без вызова Gemini.
- `GET /template/rules` — реестр проверок.
- `GET /template/result/{job_id}/preview` — HTML-превью, закрытое HTTP Basic.
- `/health` и `/status.json` — состояние сервиса и очередей.

Некорректный JSON/бриф возвращает `400`, слишком большое тело — `413`, а временные лимиты — `429` или `503` с `Retry-After`. `/template/validate` и submit имеют независимые rate limits.

### Основные лимиты и таймауты

Все значения можно переопределить переменными окружения; действующие defaults и минимумы заданы в `ocr_service/config.py`.

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `TEMPLATE_ENABLED` | `0` | Включить API генерации |
| `TEMPLATE_API_KEYS` | пусто | Список клиентских ключей через запятую |
| `TEMPLATE_MODELS` | пусто | Разрешённые модели Gemini для писем |
| `TEMPLATE_MAX_CONCURRENCY` | `1` | Параллельные генерации, ограничиваются доступными ключами |
| `TEMPLATE_RESERVE_KEYS_FOR_OCR` | `1` | Резерв ключей Gemini для OCR |
| `TEMPLATE_YIELD_TO_OCR` | `1` | Приоритет ожидающим/активным OCR-заданиям |
| `TEMPLATE_MAX_ATTEMPTS` | `3` | Генерация + попытки исправления (максимум 5) |
| `TEMPLATE_INFRA_RETRIES` | `4` | Повторы инфраструктурного вызова внутри попытки |
| `TEMPLATE_JOB_DEADLINE_SECONDS` | `420` | Дедлайн генерации после старта |
| `TEMPLATE_QUEUE_MAX_WAIT_SECONDS` | `300` | Максимум ожидания семафора |
| `TEMPLATE_MIN_ATTEMPT_SECONDS` | `90` | Минимальный остаток для следующей попытки ремонта |
| `TEMPLATE_MIN_CALL_SECONDS` | `45` | Минимальный остаток для нового вызова Gemini |
| `TEMPLATE_DEADLINE_MARGIN` | `5` | Запас времени до дедлайна на завершение/сохранение |
| `TEMPLATE_HARD_TIMEOUT` | `150` | Жёсткий таймаут одного вызова Gemini |
| `TEMPLATE_HTTP_READ_TIMEOUT` | `130` | Таймаут чтения ответа Gemini |
| `TEMPLATE_CONNECT_TIMEOUT` | `8` | Таймаут соединения с Gemini |
| `TEMPLATE_LEASE_TTL` | `180` | Аренда ключа для вызова (не меньше hard timeout + 30) |
| `TEMPLATE_MODEL_COOLDOWN` | `60` | Пауза пары ключ/модель после краткой квоты |
| `TEMPLATE_QUOTA_BLOCK_SECONDS` | `900` | Временная блокировка пары ключ/модель при дневной квоте |
| `TEMPLATE_CONTRAST_SUGGEST` | `1` | Добавлять конкретный цвет-коррекцию в предупреждение о контрасте |
| `TEMPLATE_RESULT_TTL_SECONDS` | `3600` | Хранение готового результата |
| `TEMPLATE_MAX_PENDING` | `3` | Максимум активных заданий |
| `TEMPLATE_DAILY_CAP` | `100` | Максимум принятых заданий в UTC-день |
| `TEMPLATE_DAILY_TOKEN_CAP` | `1500000` | Суточный предел токенов |
| `TEMPLATE_BREAKER_FAILS` | `5` | Число последовательных ошибок до паузы |
| `TEMPLATE_BREAKER_PAUSE_SECONDS` | `120` | Длительность паузы breaker |
| `TEMPLATE_MAX_HTML_BYTES` | `300000` | Максимальный размер HTML |
| `TEMPLATE_HTML_ERROR_BYTES` | `98000` | Размер, выше которого результат отклоняется для доставки |
| `TEMPLATE_MAX_OUTPUT_TOKENS` | `16384` | Максимальный ответ модели |
| `TEMPLATE_TEMPERATURE` | `1.0` | Температура первой генерации |
| `TEMPLATE_REPAIR_TEMPERATURE` | `0.4` | Температура исправления |
| `TEMPLATE_THINKING_BUDGET` | `0` | Бюджет thinking tokens; `0` отключает |
| `TEMPLATE_HISTORY` | `12` | История для разнообразия брифов |
| `TEMPLATE_STORE_MAX` | `100` | Максимум сохранённых заданий |
| `TEMPLATE_VALIDATE_RATE_PER_MIN` | `30` | Лимит валидации на ключ в минуту |
| `TEMPLATE_SUBMIT_RATE_PER_MIN` | `20` | Лимит submit на ключ в минуту |
| `TEMPLATE_DISABLED_RULES` | пусто | Отключение warning-правил по ID |

Локальный пример `.env` (не добавляйте файл с реальными ключами в git):

```env
TEMPLATE_ENABLED=0
TEMPLATE_API_KEYS=replace-with-private-client-key
TEMPLATE_MODELS=gemini-2.5-flash
GEMINI_API_KEY_1=replace-with-secret
TEMPLATE_MAX_CONCURRENCY=1
TEMPLATE_RESERVE_KEYS_FOR_OCR=1
TEMPLATE_YIELD_TO_OCR=1
TEMPLATE_MAX_ATTEMPTS=3
TEMPLATE_JOB_DEADLINE_SECONDS=420
TEMPLATE_QUEUE_MAX_WAIT_SECONDS=300
TEMPLATE_DAILY_CAP=100
TEMPLATE_DAILY_TOKEN_CAP=1500000
```

Ключи API и модельные ключи берутся из Replit Secrets/Render/Infisical. Не помещайте их в `.env.example`, логи или клиентский код. Свободный текстовый `prompt` не принимается: сервер выбирает бриф и параметры только из белых списков.

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

3. После установки `TEMPLATE_API_KEYS` и `TEMPLATE_MODELS` включите флаг и перезапустите; проверьте `/template/rules`.
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

Пример eval:

```bash
python scripts/eval_templates.py --base-url https://<service> --api-key '<client-key>' --n 20 --out eval_out
python scripts/isolation_check.py --base-url https://<service> --api-key '<client-key>' --image sample.png
```
