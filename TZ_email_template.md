# ЗАДАНИЕ ДЛЯ АГЕНТА-РАЗРАБОТЧИКА: тип задачи `email_template` в сервисе bls-ocr ( редакция 3, единая)

> **Как использовать.** Положи этот файл и `email_prompt.txt` (исходное ТЗ письма, дословно, без правок) в корень репозитория `blsspainrussia-image`. Скажи агенту (Claude Code / Codex / Cursor): «Выполни задание из TZ_email_template.md». Всё ниже адресовано агенту.
>
> **Что это.** Единая редакция, собранная из трёх черновиков. Все расхождения между ними уже решены (раздел 15). Спорить с решениями не нужно: если решение противоречит коду, действуй по коду и отметь это в отчёте.

---

## 0. Роль и режим работы

Ты старший Python/FastAPI-инженер. Расширяешь **существующий** сервис (тот же репозиторий, тот же деплой на Render, один воркер uvicorn). Новый функционал: генерация HTML-шаблонов транзакционных писем через Gemini, автоматическая проверка валидатором, выдача по схеме **POST (поставить задачу) → GET (забрать результат)**.

Порядок работы:
1. Прочитай файлы из раздела 3 целиком. Сверь каждый факт таблицы с кодом. Если факт не подтвердился, отметь в отчёте и действуй по коду.
2. Выдай короткий план (до 40 строк) с найденными расхождениями. Вопросов не задавай: по каждой развилке в разделе 13 указан дефолт. Единственное допустимое «стоп»: отсутствует `email_prompt.txt`.
3. Реализуй строго по этапам раздела 12. После каждого этапа запускай `pytest -q`. Следующий этап начинай только при зелёных тестах. Каждый этап = отдельный коммит (откат = `git revert` этапа).
4. В конце выдай отчёт по разделу 14.

Жёсткие ограничения:
- Поведение существующих эндпоинтов (`/ocr`, `/ocr/batch`, `/ocr/submit`, `/ocr/result/{job_id}`, `/ocr/captcha6`, `/health`, `/status`, `/status.json`) не меняется. Допустимы только **аддитивные** поля в ответах.
- Новых runtime-зависимостей нет: только stdlib и то, что в `requirements.txt` (`fastapi`, `pydantic`, `httpx`, `requests`, …). Тестовые пакеты в новом `requirements-dev.txt`.
- Python 3.9+: без `match`, без `X | Y` в аннотациях (`typing.Optional/Union`). `asyncio.to_thread` допустим.
- Стиль репозитория: русские докстринги и комментарии, JSON-логи через `_job_log`, секреты через `_config_secret` (Infisical → Render).
- В логи нельзя писать: HTML письма, текст промпта, ответ модели целиком, ключи, полные URL прокси. Для HTML допустимы только `bytes` и `sha1`.
- Не рефакторить `mailru_captcha.py`, `gemini_sheet.py`, `contact_sheet.py`, `dddd_ocr.py`, OCR-промпты. `status.html`: разрешён один необязательный блок (9.3).
- Фича выключена по умолчанию: `TEMPLATE_ENABLED=0` ⇒ сервис ведёт себя как v4.6.

---

## 1. Цель

Добавить второй полноценный тип задачи очереди `email_template`:

```
POST /template/submit          →  {job_id, brief, ...}
GET  /template/result/{job_id} →  {status, stage, attempt, template:{html, valid, report, ...}}
```

Задача выполняет промпт из `email_prompt.txt` на Gemini, достаёт HTML, прогоняет через детерминированный валидатор правил этого промпта, при нарушениях делает до N ремонтных попыток и возвращает HTML вместе с отчётом.

Принципиальные требования:
- **OCR не страдает**: ни по задержкам, ни по ключам, ни по статистике (инварианты раздела 7).
- Валидатор отделён от Gemini: его можно вызвать на любом HTML (`POST /template/validate`).
- Новый код лежит в подпакете `email_template/`; общие модули меняются минимально и обратно совместимо.

Вне объёма (v2): хранение в БД (Supabase), массовая генерация, UI, отправка писем, метрики открываемости, перевод mail.ru/BLS на общий раннер, стриминг ответа Gemini.

---

## 2. Что нужно понимать про исходную постановку

- В репозитории **нет «типа задачи» в очереди**. BLS contact-sheet идёт через `JOBS`; mail.ru идёт синхронным `POST /ocr/captcha6` со своим циклом ретраев; метка `captcha_type="mailru"` есть только в логах. Тип задачи нужно **ввести** (реестр, раздел 6). Старый OCR становится типом `ocr_sheet` без изменения поведения.
- Gemini справляется с таким ТЗ (HTML 6–12 тыс. токенов), но не со 100% точностью. Типичные нарушения: лишний плейсхолдер, `class`/`<style>`, контраст < 4.5:1, запрещённое слово (даже в отрицании), >2 «!», забытый паддинг прехедера. «Готово к рассылке без доработок» обеспечивает связка **валидатор + безопасный autofix + ремонтный цикл**, а не один промпт.
- «Выбери случайно» LLM выполняет плохо. Случайность выбирает **сервер** (8.2) и передаёт в промпт (в исходном промпте уже есть строка «Способ оформления знака: №N»).
- Для этой задачи нужна модель класса Flash/Pro; lite-модель OCR (`gemini-3.1-flash-lite`) для писем не использовать.
- Группы правил валидатора: **V** переменные, **T** вёрстка, **S** содержание/спам-триггеры, **L** порядок блоков, **A** доступность/контраст, **P** палитра (только предупреждения), **X** разбор ответа модели.

---

## 3. Факты о текущем коде и их последствия

Сверь каждую строку с кодом. Следствие обязательно.

| # | Где | Факт | Следствие |
|---|-----|------|-----------|
| 1 | `jobs.py` | `_run_job_inner` жёстко вызывает `process_images_gemini_sheet`; один `JOB_MAX_SECONDS` (60 с) и для `asyncio.wait_for`, и для сторожа (`_watchdog_tick`); общий `_JOB_SEMAPHORE` = `len(GEMINI_KEYS)`; `_job_queue_metrics` считает pending/running по **всем** `JOBS` | Реестр типов; дедлайн, TTL, конкурентность и метрики очереди — свойства типа |
| 2 | `routers/ocr.py`, `mailru_captcha.py` | mail.ru не в очереди: синхронный `/ocr/captcha6`, дедлайн 25 с, свой цикл acquire→proxy→call→release | mail.ru не трогаем. Его цикл — образец для `gemini_runner.py` |
| 3 | `gemini_client.py` | `_call_gemini_rest` умеет только PNG (`inline_data`); дефолт `maxOutputTokens=10`, `temperature=0`; read-timeout `GEMINI_HTTP_TIMEOUT=15` с; `GEMINI_HARD_TIMEOUT=45` с; любой `httpx.RequestError` (в т.ч. `ReadTimeout`) → `ProxyUnavailable` | Генерацию (20–120 с) оборвут, а рабочий прокси ошибочно уйдёт в cooldown (`mark_failed`). Нужен профиль клиента `long` |
| 4 | `key_pool.py` | `KEY_LEASE_TTL=60`; сторож каждые 10 с вызывает `reap_stale()`, снимающий любую аренду старше 60 с, даже живую | `lease_ttl` на каждый `acquire` |
| 5 | `key_pool.release` | 429 переводит **весь ключ** в cooldown ≥ 30 с независимо от модели; все ключи заблокированы → `blackout_until` | 429 на «дорогой» модели писем заморозит ключ для OCR. Нужен cooldown по паре (ключ, модель) без blackout |
| 6 | `key_pool.pick_model`, `config.py` | Цепочка `GEMINI_MODELS` одна на всех | Своя цепочка `TEMPLATE_MODELS` |
| 7 | `telemetry.ATTEMPTS`, `status_data.py` | `ATTEMPTS` — кортеж из 8 полей, распаковывается в двух циклах `build_status` (~стр. 50 и ~88) | **`ATTEMPTS` не расширяем, события писем туда не пишем** (3.11): исключает `ValueError` и искажение p50/p95 прокси OCR |
| 8 | `schemas.JobResultResponse` | Фиксированная форма с `results: List[OCRResultItem]` | Отдельная модель ответа для писем; в общую ничего не добавлять |
| 9 | `routers/*` | `/ocr/*` без авторизации; `/status*` под HTTP Basic | `/template/*` закрыты ключом. Открытость `/ocr/*` не трогаем, указать в отчёте |
| 10 | окружение | Нет bs4/lxml/pytest, тестов нет; `JOBS` в памяти (один воркер) | Только stdlib (`html.parser`, `re`, `html`). Результаты писем живут в памяти с TTL |
| 11 | `telemetry._job_log` | `component` по умолчанию `"ocr_job"`, но `payload.update(fields)` его перекрывает. События `gemini_attempt`/`gemini_api_error`/`gemini_proxy_error` автоматически попадают в `ATTEMPTS`, `KEY_LAST`, `LAST_EVENT`. Каждый лог дублируется в Better Stack | Для писем: `component="email_template"` и события с префиксом **`tpl_`**. Они не попадают в OCR-статистику; в `telemetry.py` добавляется только `TEMPLATE_RUNS` (9.1) |
| 12 | `routers/ocr.py`, `routers/health.py`, `status_data.py` | `ocr_result` отдаёт **любое** задание из `JOBS`; `health` (`ocr_jobs_*`) и `status_data` (~стр. 156–230) считают все `JOBS` | `routers/ocr.py` в списке изменяемых (404 для чужого `kind`); везде фильтровать `job.get("kind","ocr_sheet")=="ocr_sheet"`. Записи без `kind` = `ocr_sheet` |
| 13 | `gemini_client._call_gemini_rest` | Берёт `parts[0].text`; `finishReason` только в metadata | Для текста: склеивать все `parts[*].text`, пропуская `thought:true`; `finishReason=MAX_TOKENS` → X02, `SAFETY/PROHIBITED_CONTENT/BLOCKLIST` → X03 |
| 14 | `telemetry.JOB_STATS` | Общие счётчики `accepted/done/error/started/watchdog_killed…` выводятся в `/health` и `/status.json` | Счётчики писем с префиксом `tpl_`; общие не трогать |
| 15 | `jobs._finish_job_error`, `_run_job` | Используют `JOB_MAX_SECONDS` и `JOB_RESULT_TTL_SECONDS` напрямую; ошибка сторожа увеличивает `watchdog_killed` | TTL и текст ошибки берутся из реестра по `job["kind"]` |
| 16 | `jobs.py`, `key_pool.py` | `_JOB_SEMAPHORE` создаётся при импорте; `GEMINI_KEYS = gemini_pool.keys` фиксируется при импорте | Семафор типа создавать лениво внутри корутины (на 3.9 семафор, созданный до loop, может привязаться к чужому loop); число ключей брать из `gemini_pool.keys` в момент вызова |
| 17 | инфраструктура | Render может перезапустить процесс → `JOBS` теряется | Клиент трактует `404` на `GET /template/result` как «задание потеряно, отправь заново» (README) |
| 18 | `jobs._watchdog_tick` | Возраст задания считается от `created_at`, т.е. включая ожидание в очереди | Для писем дедлайн считается от старта выполнения (`started_at`), ожидание в очереди ограничивается отдельно (6.3 п. 4) |

---

## 4. Целевая архитектура

```
client ─POST /template/submit─► routers/template.py ──► director.pick_brief()  (случайный бриф без повторов)
                                   │ JOBS[job_id] = {kind:"email_template", ...}
                                   ▼
                          jobs.spawn_task(job_id, kind, payload)      (spawn_job остаётся алиасом для ocr_sheet)
                                   ▼   семафор типа, дедлайн типа, сторож
                      email_template.generate.run_template_job()
                        ├─ prompt.build_*()              system_instruction + бриф
                        ├─ gemini_runner.generate()      пул ключей/прокси, профиль long, резерв и приоритет OCR
                        ├─ extract.parse()               концепция + HTML
                        ├─ autofix.apply()               только механические правки (с откатом, если стало хуже)
                        ├─ validator.validate()          правила V/T/S/L/A/P/X
                        └─ repair-цикл ≤ N попыток       (ошибки → модель → новый HTML)
client ◄─GET /template/result/{id}─ JOBS[id] → {stage, attempt, template:{html, valid, report}}
```

Раскладка файлов (относительно `ocr_service/`):

```
ocr_service/
  task_kinds.py                НОВЫЙ  реестр типов; тип ocr_sheet (обёртка существующего конвейера)
  gemini_runner.py             НОВЫЙ  acquire→proxy→call→release с классификацией ошибок (по образцу mailru_captcha.py, без его правок)
  jobs.py                      ИЗМЕН. универсальный раннер по реестру
  gemini_client.py             ИЗМЕН. _post_generate/_parse_generate_response, профили клиента, call_gemini_generate()
  key_pool.py                  ИЗМЕН. lease_ttl, reserve, cooldown по (ключ, модель), цепочка моделей параметром
  config.py                    ИЗМЕН. TEMPLATE_* (раздел 10)
  telemetry.py                 ИЗМЕН. только TEMPLATE_RUNS, TEMPLATE_STATS и хелперы; ATTEMPTS не трогать
  status_data.py               ИЗМЕН. фильтр по kind, queue_by_kind, templates (аддитивно)
  schemas.py                   ИЗМЕН. модели /template/*; JobResultResponse не менять
  routers/ocr.py               ИЗМЕН. ocr_result: 404 для kind != ocr_sheet; больше ничего
  routers/health.py            ИЗМЕН. аддитивно (9.2); ocr_jobs_* только по ocr_sheet
  routers/template.py          НОВЫЙ  /template/submit, /result, /preview, /validate, /rules
  email_template/
    __init__.py
    prompt_base.txt            НОВЫЙ  дословная копия email_prompt.txt (не редактировать)
    prompt.py                  НОВЫЙ  PROMPT_VERSION, сборка сообщений
    director.py                НОВЫЙ  DesignBrief, пулы вариантов, история
    generate.py                НОВЫЙ  run_template_job()
    extract.py                 НОВЫЙ  разбор ответа модели
    autofix.py                 НОВЫЙ  безопасные механические правки
    validator.py               НОВЫЙ  Issue, Report, validate(), реестр правил
    contrast.py                НОВЫЙ  разбор цвета и WCAG-контраст
    preview.py                 НОВЫЙ  подстановка демо-данных
tests/                         НОВЫЙ  pytest (раздел 11)
scripts/eval_templates.py      НОВЫЙ  замер качества на живом сервисе
scripts/isolation_check.py     НОВЫЙ  проверка, что OCR не деградирует под нагрузкой писем
requirements-dev.txt           НОВЫЙ  pytest, pytest-asyncio
pytest.ini                     НОВЫЙ  asyncio_mode = auto
main.py                        ИЗМЕН. версия 4.7; include_router(template) и регистрация типа только при TEMPLATE_ENABLED=1
```

---

## 5. Контракт API

Общее:
- Авторизация: заголовок `X-API-Key`, значения из `TEMPLATE_API_KEYS` (через запятую, `_config_secret`). Сравнение `secrets.compare_digest`. Переменная пуста → все `/template/*` (кроме `/preview`) отвечают `503` с пояснением (как `/status`). Неверный или отсутствующий ключ → `401`.
- `TEMPLATE_ENABLED=0` → все `/template/*` отвечают `404`.
- `/template/result/{job_id}/preview` закрыт HTTP Basic (`require_status_auth`): браузер не умеет слать `X-API-Key` по ссылке.
- Один `job_id` принадлежит одному типу: `GET /ocr/result/{id}` для письма и `GET /template/result/{id}` для OCR → `404`.
- Тело `submit` ≤ 4 КБ.

### 5.1 `POST /template/submit`

Тело (все поля необязательны):

```json
{"account_id": "", "client_request_id": "", "seed": null, "currency_method": null,
 "brief_overrides": {"palette": null, "fonts": null, "header": null, "indicator": null},
 "model": null, "max_attempts": null}
```

- `currency_method`: 1..20, иначе `400`. `brief_overrides.*` и `model` — только из белых списков (пулы директора и `TEMPLATE_MODELS`), иначе `400`. Свободный текст клиента в промпт **не попадает** (защита от инъекций).
- `max_attempts`: 1..`TEMPLATE_MAX_ATTEMPTS`.
- Идемпотентность: если `client_request_id` не пуст и для пары (`account_id`, `client_request_id`) есть живое задание этого типа — вернуть его `job_id` с `"reused": true`. «Живое» = `pending`, либо `done/error` с ещё не наступившим `expires_at`. Индекс чистится вместе с TTL.
- Лимиты (порядок проверки): `TEMPLATE_MODELS` пусто или нет ключей Gemini → `503`; breaker (7.3) → `503` с `Retry-After`; очередь типа ≥ `TEMPLATE_MAX_PENDING` → `429`; суточный `TEMPLATE_DAILY_CAP` (UTC) → `429`; суточный `TEMPLATE_DAILY_TOKEN_CAP` → `429` с `reason:"daily_token_cap"`.

Ответ `200`:

```json
{"job_id": "…", "status": "pending", "kind": "email_template", "reused": false,
 "brief": {"seed": 1234567, "palette_family": "…", "fonts": "Georgia + Verdana",
           "header": "…", "indicator": "…", "currency_method": 14},
 "prompt_version": "1.0"}
```

### 5.2 `GET /template/result/{job_id}`

`?html=0` убирает `html` из ответа (лёгкий опрос). Клиент опрашивает раз в 3–5 с.

```json
{
  "status": "pending | done | error",
  "kind": "email_template",
  "stage": "queued | generating | validating | repairing | finished",
  "attempt": 1, "max_attempts": 3,
  "queue_position": null, "queued_jobs": 0, "running_jobs": 1, "max_concurrency": 1,
  "job_age_ms": 0, "queue_wait_ms": 0, "processing_ms": 0,
  "template": {
    "valid": true, "html": "<!DOCTYPE html>…", "bytes": 23412,
    "concept": "Палитра …; шрифты …", "brief": {}, "prompt_version": "1.0",
    "prompt_sha1": "abcdef012345", "model": "…",
    "attempts": [
      {"n": 1, "model": "…", "latency_ms": 41230, "finish_reason": "STOP",
       "tokens": {"in": 5200, "out": 9100, "thoughts": 0},
       "raw_errors": ["T01", "L01"], "autofixes": ["AF05"], "errors": [], "warnings": ["T13"]}
    ],
    "report": {"ok": true, "errors": [],
      "warnings": [{"rule": "T13", "message": "…", "where": "…"}],
      "stats": {"bytes": 23412, "tables": 31, "contrast_checked": 18, "contrast_unverified": 2}}
  },
  "error": null
}
```

Семантика:
- `status="done"` = «задание отработало»; смотреть `template.valid`. Если после всех попыток ошибки остались — возвращается **лучшая** попытка (меньше ошибок, затем меньше предупреждений) с `valid=false` и полным отчётом.
- `status="error"` — только инфраструктура. Поле `error` — объект/строка с кодом причины (`no_keys`, `no_proxy`, `quota`, `deadline`, `queue_timeout`, `ocr_priority_timeout`, `cancelled`) без секретов (`_safe_gemini_message`). Дедлайн, но есть хотя бы одна готовая попытка → `done` с лучшей.
- Результат живёт `TEMPLATE_RESULT_TTL_SECONDS`; максимум `TEMPLATE_STORE_MAX` готовых заданий (вытесняются самые старые готовые; `pending` не трогать).
- В `JOBS` хранится одна копия лучшего HTML (≤ `TEMPLATE_MAX_HTML_BYTES`) и список попыток **без** HTML.

### 5.3 Остальные эндпоинты

| Метод и путь | Авторизация | Назначение |
|---|---|---|
| `GET /template/result/{job_id}/preview?raw=0\|1` | HTTP Basic | HTML с демо-данными вместо 7 переменных (`raw=1`: как есть). Заголовки: `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `X-Template-Valid: 0\|1`. Значения подстановки экранируются `html.escape`. HTML невалидного письма отдаётся без изменений |
| `POST /template/validate` | `X-API-Key` | Тело `{"html": "…", "currency_method": null}`. Проверка `Content-Length` до чтения тела; > `TEMPLATE_MAX_HTML_BYTES` → `413`. Лимит `TEMPLATE_VALIDATE_RATE_PER_MIN` на ключ (скользящее окно в памяти, словарь ≤ 100 ключей) → `429`. Синхронно возвращает `report`. Выполнять через `asyncio.to_thread`. Gemini и его ключи не нужны |
| `GET /template/rules` | `X-API-Key` | Список правил: id, severity, описание, `enabled` |

---

## 6. Реестр типов задач (`task_kinds.py`)

### 6.1 Структура

```python
@dataclass(frozen=True)
class TaskKind:
    name: str                                        # "ocr_sheet" | "email_template"
    runner: Callable[[str, dict], Awaitable[None]]   # (job_id, payload) -> заполняет JOBS[job_id]
    deadline_s: Callable[[], float]                  # читается из конфига в момент вызова
    deadline_from: str                               # "created" | "started"
    queue_max_wait_s: float                          # 0 = не ограничено
    watchdog_grace_s: float                          # запас сторожа поверх deadline
    result_ttl_s: Callable[[], int]
    max_concurrency: Callable[[], int]
    store_max: Callable[[], int]                     # 0 = без лимита
    stats_prefix: str                                # "" | "tpl_"

KINDS: Dict[str, TaskKind]; register(kind); get(name)   # неизвестный kind → внутренняя ошибка, не 500 клиенту
```

### 6.2 Значения

| Поле | `ocr_sheet` (как сейчас) | `email_template` |
|---|---|---|
| `runner` | существующее тело `_run_job_inner`, вынесенное дословно | `email_template.generate.run_template_job` |
| `deadline_s` / `deadline_from` | `JOB_MAX_SECONDS` (60) / `created` | `TEMPLATE_JOB_DEADLINE_SECONDS` (420) / `started` |
| `queue_max_wait_s` | 0 | `TEMPLATE_QUEUE_MAX_WAIT_SECONDS` (300) |
| `watchdog_grace_s` | 0 | 15 |
| `result_ttl_s` | `JOB_RESULT_TTL_SECONDS` | `TEMPLATE_RESULT_TTL_SECONDS` (3600) |
| `max_concurrency` | `max(len(gemini_pool.keys),1)` | `TEMPLATE_MAX_CONCURRENCY` (1) |
| `store_max` | 0 | `TEMPLATE_STORE_MAX` (100) |
| `stats_prefix` | `""` | `"tpl_"` |

### 6.3 Требования к `jobs.py`

1. `spawn_task(job_id, kind, payload)` — общий запуск; `spawn_job(job_id, images)` остаётся и вызывает `spawn_task(..., "ocr_sheet", {"images": images})` (роутер OCR не меняется).
2. Семафоры — словарь `{kind: Semaphore}`, создаются лениво в корутине (факт 16). Для `ocr_sheet` размер и поведение прежние.
3. Любая ошибка раннера ловится в `_run_task`, пишется `status="error"` и `_finish_job_error`; ключ освобождается в `finally` через `ensure_released`.
4. Сторож использует `deadline_s + watchdog_grace_s` **своего** типа. Для `deadline_from="started"` возраст считается от `started_at`; задание, не стартовавшее за `queue_max_wait_s`, отменяется с ошибкой `queue_timeout`. Раннер писем сам ограничивает себя `deadline_s - 5 с` (`asyncio.wait_for`), чтобы успеть вернуть лучшую попытку; сторож — последний рубеж.
5. `_job_queue_metrics`: pending/running/`max_concurrency` считаются только среди заданий того же `kind`. Для `ocr_sheet` вывод **идентичен прежнему** (golden-тест).
6. `_finish_job_error` и `_run_job` берут TTL и счётчики из реестра; для `ocr_sheet` текст ошибки и счётчики не меняются.
7. `_cleanup_expired_jobs`: `expires_at` по `result_ttl_s` типа; `_enforce_store_max(kind)` вытесняет самые старые готовые.
8. Запись в `JOBS` без `kind` = `ocr_sheet`.
9. `JOB_STATS` не менять; счётчики писем — отдельные (9.1).

---

## 7. Изоляция нагрузки: инварианты (обязательны и тестируются)

| # | Инвариант | Как обеспечить |
|---|-----------|----------------|
| I1 | При `TEMPLATE_ENABLED=0` поведение = v4.6 | Роутер не подключается, тип не регистрируется, семафор не создаётся, `/health` без блока `template` |
| I2 | OCR-пути не меняются | Snapshot-тест: тело запроса `_call_gemini_rest` (parts, `temperature:0.0`, `maxOutputTokens:10`) идентично до/после; golden `JobResultResponse` и `_job_queue_metrics` |
| I3 | Отдельная конкурентность | Письма не берут семафор OCR и наоборот |
| I4 | Резерв ключей для OCR | `gemini_pool.acquire(reserve=N, lease_ttl=T)` возвращает `(None, None)`, если после взятия число свободных (не reserved, не dead, не blocked, без hold) ключей стало бы `< N`. Для OCR `reserve=0`. Для письма `N = effective_reserve` (8.7) |
| I5 | 429 письма не замораживает ключ для OCR | `release(idx, error, model=...)`: при rate-limit/quota и переданном `model` блокируется пара `(idx, model)` (`key_model_blocked`), ключ и `blackout_until` не трогаются. `pick_model`/`model_allowed` учитывают словарь. Вызовы OCR (без `model`) как раньше. Ошибки ключа (невалидный, биллинг, проект) блокируют ключ целиком, как сейчас |
| I6 | Генерация не штрафует каналы | Профиль `long`: `ReadTimeout` и hard-timeout → новое исключение `GenerationTimeout` (не `ProxyUnavailable`): `mark_failed` не вызывается, ключ освобождается без cooldown. `ConnectError/ConnectTimeout/ProxyError/ProtocolError` → `ProxyUnavailable`; гео-блок → `ProxyGeoBlocked`, как раньше. Профиль `short` — как раньше |
| I7 | Аренда не теряется | `lease_ttl[idx]` на ключ; `reap_stale()` без аргумента использует `lease_ttl[idx]` (для OCR = `KEY_LEASE_TTL`). Письмо: `TEMPLATE_LEASE_TTL` > `TEMPLATE_HARD_TIMEOUT` |
| I8 | Статистика OCR чистая | События `tpl_*` не попадают в `ATTEMPTS/KEY_LAST/QUALITY/ACCOUNTS`; `_account_touch` для писем не вызывается; тест: после прогона письма `ATTEMPTS` не изменился |
| I9 | Память ограничена | `TEMPLATE_STORE_MAX`, лимит HTML, TTL, чистка индекса идемпотентности; 200 submit при мок-Gemini не поднимают `len(JOBS)` выше лимитов |
| I10 | Любой исход возвращает ключ и финальный статус | `try/finally gemini_pool.ensure_released(idx)`; `CancelledError` и дедлайн → задание не остаётся `pending/running` |
| I11 | Секреты и контент не в логах | Тест: перехват stdout, поиск подстрок HTML/промпта/ключей/URL прокси |
| I12 | Расход токенов под контролем | `TEMPLATE_MAX_ATTEMPTS`, `TEMPLATE_MAX_PENDING`, `TEMPLATE_DAILY_CAP`, `TEMPLATE_DAILY_TOKEN_CAP`, breaker, `TEMPLATE_MIN_ATTEMPT_SECONDS`. Токены считаются сразу после каждого ответа; при превышении суточного лимита идущие задания дорабатывают текущую попытку и новую не начинают |
| I13 | Event loop не блокируется | Валидатор — линейные регулярки и один проход `html.parser`, без вложенных квантификаторов и `.*` в середине шаблона по всему документу; 300 КБ патологического HTML < 1 с; в `/validate` и в `generate` валидация через `to_thread`, если `len(html) > 40 000`; бюджет ≤ 50 мс на 30 КБ (в CI мягко ≤ 200 мс) |

### 7.3 Breaker
Если подряд `TEMPLATE_BREAKER_FAILS` (5) заданий закончились `status="error"` по инфраструктурной причине, новые `submit` отвечают `503` на `TEMPLATE_BREAKER_PAUSE_SECONDS` (120). Успешное задание сбрасывает счётчик. Состояние в памяти, виден в `/health`.

---

## 8. Подсистема `email_template`

### 8.1 Промпт (`prompt.py`)
- `PROMPT_VERSION = "1.0"`; любое изменение текста или логики сборки = рост версии. В результат пишется также `prompt_sha1` (первые 12 символов sha1 от `prompt_base.txt`).
- `prompt_base.txt` — дословная копия `email_prompt.txt`, идёт как `systemInstruction`. Нет `email_prompt.txt` → стоп.
- Первое сообщение пользователя строится только из полей `DesignBrief` и шаблона:
  ```
  ПАРАМЕТРЫ ЗАПУСКА (обязательны к исполнению, выбирать самому не нужно):
  Способ оформления знака: №{currency_method}
  Семейство палитры: {palette_family}
  Пара шрифтов: {fonts}
  Композиция шапки: {header}
  Тип статус-индикатора: {indicator}
  Не повторяй палитры недавних писем: {avoid_palettes или «нет»}
  Номер запуска (для разнообразия формулировок): {seed}
  Выполни задание из системной инструкции. Верни ответ в формате из раздела «ФОРМАТ ОТВЕТА»;
  файл template.html допустимо вернуть одним блоком ```html.
  ```
- Ремонтная попытка — многоходовой диалог: `[user: первое сообщение] → [model: прошлый ответ целиком] → [user: ремонт]`. Текст ремонта:
  ```
  Проверка нашла нарушения. Исправь ТОЛЬКО их; дизайн, палитру, тексты и выбранные параметры не меняй.
  Верни ответ в том же формате: концепция, файл template.html целиком, строка с переменными.
  Нарушения:
  - [T01] Найден <style> — где: <style type="text/css">…
  - [A01] Контраст 3.2:1 (нужно 4.5:1) — цвет #8a8a8a на #ffffff, текст «Подготовим…»
  ```
  Не более 12 нарушений (первыми T/S/V, затем L, затем A); каждое `where` ≤ 120 символов; ничего от клиента в сообщение не попадает. Для `X02` (обрыв) добавляется просьба сократить тексты и уложиться в лимит. Для `X03` (блокировка) — ремонт с тем же брифом, но без многоходового контекста (прошлого ответа нет).
- Ремонт идёт с тем же брифом и `seed`.
- Параметры генерации: `temperature=TEMPLATE_TEMPERATURE` (первая), `TEMPLATE_REPAIR_TEMPERATURE` (ремонт); `maxOutputTokens=TEMPLATE_MAX_OUTPUT_TOKENS`; `thinkingConfig` только если задан `TEMPLATE_THINKING_BUDGET`; `responseMimeType` и схема не используются.

### 8.2 Директор (`director.py`)
`DesignBrief(seed, palette_family, fonts, header, indicator, currency_method, avoid_palettes)`.

Случайность: `random.Random(seed)`, `seed = secrets.randbelow(2**31)`, если не задан. Один `seed` + те же пулы и история ⇒ тот же бриф (HTML не воспроизводится: температура > 0).

Пулы (белые списки; расширять только с ростом `PROMPT_VERSION`; сужать ниже указанного нельзя):

**palette_family** (12). Названия не содержат корней `красн|оранж|терракот|беж|крем|коричн|сер` и не воспроизводят запреты промпта (тест регуляркой). «Малиновый/коралловый» допустимы только как декоративный акцент (разрешено промптом).

| Код | Значение |
|---|---|
| P1 | хвойный зелёный текст, лимонно-жёлтый акцент, светло-мятный фон блока |
| P2 | бирюзовый текст, сиреневый акцент, холодный светло-голубой фон блока |
| P3 | сливовый текст, небесно-голубой акцент, светло-лавандовый фон блока |
| P4 | морская синь текст, лаймовый акцент, светло-аквамариновый фон блока |
| P5 | оливковый текст, малиновый декоративный акцент, светло-фисташковый фон блока |
| P6 | изумрудный текст, золотисто-жёлтый акцент, светлый шалфейный фон блока |
| P7 | графитово-зелёный текст, бирюзовый акцент, светлый мятно-ледяной фон блока |
| P8 | черничный текст, мятный акцент, светло-голубой фон блока |
| P9 | глубокий лазурный текст, подсолнечно-жёлтый акцент, светло-небесный фон блока |
| P10 | тёмно-бирюзовый текст, коралловый декоративный акцент, светло-мятный фон блока |
| P11 | пыльно-фиолетовый текст, салатовый акцент, светлый пудрово-лиловый фон блока |
| P12 | хвойно-синий текст, пурпурно-малиновый декоративный акцент, светлый льдистый фон блока |

- **fonts** (≥8, только из белого списка Arial/Georgia/Trebuchet MS/Verdana/Tahoma/Helvetica): `Georgia + Verdana`; `Trebuchet MS + Georgia`; `Arial + Georgia`; `Tahoma + Georgia`; `Verdana + Trebuchet MS`; `Helvetica + Georgia`; `Georgia + Tahoma`; `Trebuchet MS + Verdana`.
- **header** (≥8): смещённая вправо шапка с цветной полоской слева; шапка-плашка со скруглением и точкой-акцентом; шапка с многоцветной полоской из 3–4 сегментов сверху; шапка по центру с тонкой рамкой; шапка с крупной начальной буквой бренда в круге; шапка-ступенька (две плашки со сдвигом); шапка с вертикальной чертой между словами; шапка с цветной подложкой-маркером под названием.
- **indicator** (≥8): три сегмента-плашки с подписями внутри; точки с соединяющими линиями; прогресс-бар из трёх секций; три карточки со ступенчатым сдвигом; круги с цифрами и соединением; вертикальная линия с тремя точками; три кружка с галочкой/точкой/пустым кружком и подписями под ними; лента из трёх скруглённых шагов с разной заливкой.
- **currency_method**: 1..20.

История (глобальная, в памяти, `deque(maxlen=TEMPLATE_HISTORY)`; записи `(palette, header, indicator, currency_method)`):
- Из пулов исключаются значения последних `min(5, len(пула)-1)` записей (для `currency_method` — последние 8); если пул опустел, исключения снимаются. `avoid_palettes` = последние 5 палитр.
- Запись в историю — в момент приёма задания, атомарно (без `await` между выбором и записью). Явный override подменяет значение, история всё равно обновляется.

### 8.3 Разбор ответа (`extract.py`)
`parse(text, finish_reason, prompt_block_reason) -> Parsed(concept, html, tail, ...)`:
- Нормализация: удалить BOM, `\r\n` → `\n`.
- HTML = блок ```` ```html … ``` ````; если нет — срез от первого `<!DOCTYPE html` (регистр не важен) до первого `</html>` после него включительно. Концепция = текст до `<!DOCTYPE`, без ограждений, ≤ 600 символов. «Хвост» (строка с переменными и «способ оформления знака: №N, название») = текст после `</html>`, ≤ 400 символов.
- Нет `<!DOCTYPE` → `X01`; нет `</html>` либо `finishReason=MAX_TOKENS` → `X02` (HTML неполный, итогом не считается); `finish_reason` в {`SAFETY`,`PROHIBITED_CONTENT`,`BLOCKLIST`} или есть `prompt_block_reason` → `X03`; больше одного документа → берётся первый, `X04` (warning).
- Все `X*` (кроме `X04`) — ошибки попытки и запускают ремонт.

### 8.4 Валидатор (`validator.py`)
Контракт: `validate(html: str, currency_method: Optional[int] = None) -> Report`. Чистая функция: без сети, состояния и asyncio. `Issue(rule, severity, message, where)` (`where` ≤ 120 символов, без переводов строк); `Report(ok, errors, warnings, stats)`, `ok = not errors`. Реестр `RULES: Dict[str, RuleSpec(id, severity, title, enabled)]`. Отключать через `TEMPLATE_DISABLED_RULES` можно только warning-правила (попытка отключить error — игнор с записью в лог).

Разбор: один проход `html.parser.HTMLParser` (стек тегов, лёгкое дерево `Node(tag, attrs, children, start_pos, end_pos)`, позиции символов для правил порядка) + регулярки только на коротких строках (значения атрибутов, текстовые узлы). Нормализация текста перед языковыми правилами: `casefold`, `ё→е`, `html.unescape`, неразрывные и нулевой ширины пробелы → пробел, `{{…}}` вынуты из «видимого текста». Размер > `TEMPLATE_MAX_HTML_BYTES` → единственная ошибка `T23`, остальное не запускается.

Термины: «маркер» = комментарий `<!-- Здесь вставляется тело письма -->`; «подвал» = внешняя таблица/строка, содержащая `mailto:`; «блок сообщения» = от маркера до подвала.

Переменные (`ALLOWED_VARS`): `brandName, name, productName, transactionalId, price, supportEmail, email`.

**V — переменные**

| ID | Sev | Проверка |
|---|---|---|
| V01 | E | Все 7 переменных присутствуют как `{{x}}` |
| V02 | E | Нет других плейсхолдеров: `{{ x }}` с пробелами, `{{{`, `{%`, `${`, `[[`, одиночные `{{`/`}}`, фильтры `\|`, `&#123;`, `{{` с иными именами |
| V03 | E | `{{productName}}` внутри `<title>` |
| V04 | E | `{{productName}}` в теле письма до маркера (блок деталей) |
| V05 | E | `{{brandName}}` в шапке: до первого `{{name}}` |
| V06 | E | `{{name}}` до `{{transactionalId}}`; `{{transactionalId}}` и `{{price}}` до маркера |
| V07 | E | Ровно одна ссылка `<a`, её `href` ровно `mailto:{{supportEmail}}`; `{{supportEmail}}` не встречается вне этой ссылки; `{{email}}` как текст (не в атрибуте) после неё |
| V08 | E | Знак `$` (текстом, не в атрибуте) в окне ±300 символов вокруг `{{price}}` в одной строке таблицы (`<tr>`); для способа 15 допустима подпись `USD` |
| V09 | W | Параметры в `mailto:` (`?subject=` и т.п.) |
| V10 | W | Если передан `currency_method`: «хвост» ответа указывает `№N`, совпадающий с заказанным (проверяется в `generate`, не в чистом `validate`) |
| V11 | W | Размер текста с `{{price}}` вне 24–30 px |

**T — вёрстка**

| ID | Sev | Проверка |
|---|---|---|
| T01 | E | Нет `<style` |
| T02 | E | Нет `class=` |
| T03 | E | Нет тегов `img, svg, picture, video, audio, canvas, iframe, object, embed, form, input, select, textarea, script, link, base, noscript, button` |
| T04 | E | Нет `display:flex/inline-flex/grid/inline-grid`, `position:absolute/fixed/sticky`, `@media`, `@import`, `@font-face`, `url(`, `expression(`, `var(`; `float:` — W |
| T05 | E | Нет внешних адресов `http://`, `https://`, `//host`, `javascript:`, `data:` (кроме значения в `mailto:`) |
| T06 | E | `<!DOCTYPE html>` в начале; `<html … lang="ru"`; `<meta charset="UTF-8">` (регистр не важен); `viewport` с `width=device-width`; `X-UA-Compatible`; ровно по одному `<html>/<head>/<body>` |
| T07 | E | Основная таблица: `width` 520..600 и в стиле `width:100%` + `max-width:<то же число>px` |
| T08 | E | Каждый `<table` имеет `role="presentation" cellpadding="0" cellspacing="0" border="0"` |
| T09 | E | `bgcolor` и `background-color` одновременно на `<body>` (или внешней таблице) и на основной таблице |
| T10 | E | Прехедер: первый элемент в `<body>` (`div`/`span`) со всеми декларациями `display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all`; видимый текст 50..110 символов (без `&nbsp;&zwnj;`; `{{transactionalId}}` считать 10 символами); в конце ≥3 пар `&nbsp;&zwnj;` (допустимы `&#160;&#8204;`) |
| T11 | E | `border-radius` на `<table>`/`<td>` требует `border-collapse:separate` на ближайшей таблице |
| T12 | E | `font-size` < 12px где-либо (кроме прехедера) |
| T13 | W | `font-size` 12..14.99px вне подвала |
| T14 | W | `line-height` вне 1.4..1.6 (безразмерное, либо px/`font-size` при обоих в одном `style`) |
| T15 | E | Каждое `font-family`: все семейства из белого списка (Arial, Georgia, Trebuchet MS, Verdana, Tahoma, Helvetica, serif, sans-serif; кавычки/`&quot;` нормализуются), первое — из списка, в конце `serif` или `sans-serif` |
| T16 | E | Теги `h1–h3, p, ul, li, hr, table, td` внутри блока сообщения имеют `style`; `strong/em/i/b` без стиля — W |
| T17 | E | Блок сообщения: `h2`; ≥1 `p` (больше 2 → W); `strong`; `ul` ровно с тремя `li`; `hr`; курсив (`em`/`i`/`font-style:italic`) |
| T18 | E | Маркер встречается ровно один раз и посимвольно как в промпте; сразу перед блоком сообщения |
| T19 | E | Других HTML-комментариев нет (включая `<!--[if mso]>`: промпт запрещает комментарии кроме маркера) |
| T20 | E | `<title>` непустой |
| T21 | E | Теги `table/tr/td/ul/li/p/h1-h3/a` и `html/head/body` сбалансированы |
| T22 | E | `<a>`: задан `color` и `text-decoration:underline` |
| T23 | E/W | Размер: > `TEMPLATE_MAX_HTML_BYTES` → E (дальше не проверять); > `TEMPLATE_HTML_ERROR_BYTES` (98000; Gmail клиппирует ~102 КБ, нужен запас на подстановку) → E; > 80000 → W |
| T24 | E | Нет `<ol` (маркированный список, не нумерованный) |
| T25 | W | `font-size` не в `px`; таблиц больше 60 |

**S — содержание и спам-триггеры** (по видимому тексту, `<title>`, прехедеру и значениям `alt/title`)

| ID | Sev | Проверка |
|---|---|---|
| S01 | E | Запрещённые слова и фразы (RU+EN) **даже в отрицании**. Список — константа `BANNED`: urgent, action required, act now, act fast, limited time, don't delay, dont delay, verify now, verify immediately, get paid now, unlock funds, funds on hold, payment hold, delays may apply, within 24 hours, last chance, срочн*, немедленн*, прямо сейчас, не откладыва*, успейте, успей, быстрее, последний шанс, иначе потеря*, в течение 24 час*, средства под блокировк*, заблокир*, не упустите, торопитесь, поторопитесь. Границы слов, регистр не важен; `sold` и `hot` — только отдельными словами |
| S02 | E/W | Число `!` в видимом тексте, `<title>` и прехедере: E при > 2, W при = 2 |
| S03 | E | Эмодзи в `<title>`, прехедере, `h1–h3`; 🚨⚡⚠️❗‼ где угодно; суммарно в остальном тексте > 1. Диапазоны: U+1F000–1FAFF, U+2600–26FF, U+2700–27BF (кроме U+2713), U+2B00–2BFF, U+FE0F. `&#10003;` и ✓ разрешены |
| S04 | E/W | Капс: слово ≥3 букв целиком заглавными в `h1–h3` (кроме USD, ID, PDF, SMS, OK, FAQ) — E; ≥4 букв в остальном тексте и `text-transform:uppercase` — W→E для `uppercase` в стилях |
| S05 | E | Кнопки: нет `<button`, нет `role="button"`, нет `<a` со стилем `background*` или `padding` (вид кнопки) |
| S06 | E | Заезженные фразы: «ваш заказ оформлен», «спасибо за покупку», «мы рады сообщить», «уважаемый клиент» |
| S07 | E | Обещания сроков: `в течение`, `через \d`, `за \d+ (мин\|час\|дн\|сут\|недел)`, `\d+ (рабоч\w+ )?(дн\|час\|сут)`, `до \d+ (дн\|час)`, `в ближайш`, «завтра», «сегодня до» |
| S08 | W | Заголовок «Статус заказа» / «Ваш заказ» как есть |
| S09 | W | Нет `h1` и нет текста ≥22px в приветствии |
| S10 | E | Обращение на «ты»: `ты, тебе, тебя, твой, твоя, твоё, твои, тобой` (границы слов) |
| S11 | W | Призывы «нажмите», «кликните», «жмите», «click here» |
| S12 | W | Сэйл-лексика: акци*, скидк*, распродаж*, бейдж-слова (проверять без ложных срабатываний; не расширять без теста) |

**L — порядок блоков**

| ID | Sev | Проверка |
|---|---|---|
| L01 | E | Порядок первых вхождений: `brandName` < `name` < `productName`(тело) < `transactionalId` < `price` < маркер < `<h2` < `<ul` < `<hr` < курсив < `supportEmail` < `email` |
| L02 | E | Между блоком деталей и маркером есть индикатор: ≥3 разных коротких текстовых подписи этапов (текстовые узлы ≤30 символов) |

**A — доступность**

| ID | Sev | Проверка |
|---|---|---|
| A01 | E | Контраст текст/фон ≥ 4.5 (округление до 2 знаков), любой текстовый узел с определёнными цветами (включая знак валюты и галочку) |
| A02 | E | Контраст знака валюты ≥ 4.5 (отдельный id для отчёта: блок вокруг `$` перед `{{price}}`) |
| A03 | W | Индикатор читается без цвета: есть `&#10003;`/✓ или слово-маркер завершённого этапа |
| A04 | W | Доля непроверенных пар > 30% |

**P — палитра (только warning)**

| ID | Sev | Проверка |
|---|---|---|
| P01 | W | Красный/оранжевый: hue 345°–360° или 0°–45° при S ≥ 0.6 и светлоте 0.25–0.75 (малиновый/коралловый допустимы как декор — решает человек) |
| P02 | W | Различных hex-цветов < 4 или > 10 |

**X — разбор ответа** — см. 8.3.

Не проверяется автоматически (указать в отчёте): отсутствие «типовой» палитры из запретного списка, живость текстов, тон, рендер в Outlook/Gmail dark mode.

`stats`: `bytes, tables, contrast_checked, contrast_unverified, placeholders, colors, preheader_chars, exclamations, emoji`.

### 8.5 Контраст (`contrast.py`)
- Парсер цвета: `#rgb`, `#rrggbb`, `rgb(r,g,b)`, имена (`white, black, silver, gray, navy, teal, green, maroon, purple, olive, blue, red, yellow, lime, aqua, fuchsia, orange`). `rgba` с α<1, `transparent`, `inherit`, `currentColor`, `initial` → «неопределённо».
- Цвет текста: ближайший предок с `color` (в `style`); нет → `#000000`. Фон: ближайший предок с `background-color`, `background:` (первый цветовой токен) или `bgcolor`; для `linear-gradient(...)` проверять все остановки и брать худшую; фон не найден → `#ffffff`. Пара с «неопределённым» компонентом → `contrast_unverified += 1`, правило не срабатывает.
- Пропускать: прехедер, `<title>`, узлы из пробелов/`&nbsp;`, `display:none`. Пары дедуплицируются; одна ошибка A01 на уникальную плохую пару с примером текста ≤ 40 символов.
- WCAG 2.x: линеаризация sRGB (`c/12.92` либо `((c+0.055)/1.055)^2.4`), `L = 0.2126R + 0.7152G + 0.0722B`, `ratio = (L1+0.05)/(L2+0.05)`. Порог 4.5 для всего текста. Тёмную тему клиентов не моделируем.
- Золотые значения для теста: чёрный/белый = 21:1; `#767676` на белом ≈ 4.54 (pass); `#777777` на белом ≈ 4.48 (fail).

### 8.6 Autofix (`autofix.py`)
Только механика, без изменения смысла, цветов, текстов. `apply(html) -> (html, [ids])`, идемпотентна.

| ID | Правка |
|---|---|
| AF01 | Обрезать всё вне `<!DOCTYPE…</html>`; если документ начинается с `<html` — добавить `<!DOCTYPE html>` |
| AF02 | `{{ name }}` → `{{name}}` для 7 разрешённых имён (только пробелы внутри скобок) |
| AF03 | Нормализовать пробелы в маркере к точной строке |
| AF04 | Добавить недостающие `role/cellpadding/cellspacing/border` в `<table` |
| AF05 | Дополнить прехедер парами `&nbsp;&zwnj;` до 6 пар |
| AF06 | Удалить `class="…"`, только если в документе нет `<style` |
| AF07 | Добавить `border-collapse:separate` таблицам с `border-radius`, если `border-collapse` не задан |
| AF08 | Добавить недостающие `lang="ru"` / `<meta charset="UTF-8">` / `X-UA-Compatible` / `viewport` по шаблону (если `<html>`/`<head>` есть) |
| AF09 | Синхронизировать `bgcolor` и `background-color` на `<body>` и основной таблице (если задан один из двух) |
| AF10 | Добавить `text-decoration:underline` в стиль ссылки (если `color` у неё есть) |

Никогда автоматически: тексты, цвета, шрифты, размеры, структуру блоков, `<style>`, слова, `!`, эмодзи, контраст. **Страховка:** валидировать до и после; если у исправленной версии ошибок **больше**, откатить автофикс целиком и записать `autofixes=[]`. `raw_errors` = ошибки до autofix, `errors` = после.

### 8.7 Цикл генерации и вызов Gemini

`generate.py::run_template_job`:
```
для n = 1..max_attempts:
    если уже есть попытка и до дедлайна осталось < TEMPLATE_MIN_ATTEMPT_SECONDS → стоп
    stage = generating (n==1) | repairing
    ответ = runner.generate(...)           # инфраструктурные ретраи внутри, не тратят n
    stage = validating
    parse → (нет html: X-ошибка, ремонт) → autofix → validate (to_thread для >40 КБ)
    попытка записывается; лучшая = min по (кол-во errors, кол-во warnings, n)
    если report.ok → стоп
    если число ошибок не уменьшилось две попытки подряд → стоп
итог: done (valid = best.report.ok); error — если ни одна попытка не завершилась
```
- Warnings ремонт не запускают. `stage`/`attempt` пишутся в `JOBS[job_id]` перед каждым шагом.
- Инфраструктурные сбои (нет ключа, прокси, 5xx, 429, `GenerationTimeout`) **не тратят** `n`: свой счётчик `TEMPLATE_INFRA_RETRIES` (4) на задание. Исчерпан или дедлайн без готовой попытки → `status="error"` с кодом.
- Логи: `tpl_accepted, tpl_started, tpl_gemini_attempt, tpl_gemini_error, tpl_validated, tpl_autofix, tpl_repair, tpl_done, tpl_error, tpl_expired, tpl_rejected`; поля — `component="email_template"`, `job_id`, `account_id`, `client_request_id`, `attempt`, `model`, `latency_ms`, `finish_reason`, `tokens`, `rule_ids`, `bytes`, `sha1`, `valid`. Без HTML и промпта.

`gemini_runner.generate()` (по образцу `mailru_captcha.py`):
1. **OCR-приоритет.** `effective_reserve = min(TEMPLATE_RESERVE_KEYS_FOR_OCR, max(live_keys - 1, 0))`, где `live_keys` — не dead и без operator hold. Если `TEMPLATE_YIELD_TO_OCR=1`: перед `acquire` письмо ждёт (опрос 2 с, время идёт в дедлайн), пока `ocr_busy = (ocr_queued > 0) or (live_keys == 1 and ocr_running > 0)`; счётчики — из `JOBS` по `kind="ocr_sheet"`. Дедлайн ожидания → `ocr_priority_timeout`. При единственном живом ключе письма идут только при простое OCR (сознательный компромисс). Mail.ru не виден в `JOBS`, но его аренды видны в пуле, поэтому правило резерва защищает и его.
2. `gemini_pool.acquire(lease_ttl=TEMPLATE_LEASE_TTL, reserve=effective_reserve)`; ожидание ключа — опрос `GEMINI_ACQUIRE_POLL`, не дольше остатка дедлайна. Модель — `pick_model(idx, chain=TEMPLATE_MODELS или [model запроса])`.
3. Прокси — `smart_proxy_pool.next()` (как в остальном коде), URL не логировать.
4. `call_gemini_generate(profile="long", ...)`.
5. Исходы: успех → `release(idx)`; `GenerationTimeout` → ключ без cooldown, прокси не штрафовать; `ProxyUnavailable` → `mark_failed` прокси как в существующем коде, ключ без cooldown; 404 модели → `mark_model_unavailable`; 500/503/504 → `mark_model_overloaded`, ключ не штрафовать; 429 → cooldown пары (ключ, модель) (не меньше `TEMPLATE_MODEL_COOLDOWN`, с учётом `retryDelay`); прочее → как `MODEL_OR_RESPONSE_ERROR`. `finally: ensure_released(idx)`.
6. `TEMPLATE_MODELS` пусто → submit `503`; все модели цепочки недоступны → задание `error`.

### 8.8 Клиент Gemini (`gemini_client.py`, аддитивно)
- Вынести общий `_post_generate(...)`/`_parse_generate_response(...)`; сигнатура и поведение `_call_gemini_rest` не меняются (E0-снимок).
- Профили httpx-клиента: `short` (как сейчас) и `long` (`connect=TEMPLATE_CONNECT_TIMEOUT`, `read=TEMPLATE_HTTP_READ_TIMEOUT`, `write=30`, `pool=10`). Кэш клиентов ключуется парой `(proxy_url, profile)`; `close_http_clients` закрывает все.
- `call_gemini_generate(contents, system_instruction, model, key, proxy, generation_config, profile="long", hard_timeout=TEMPLATE_HARD_TIMEOUT) -> GenResult(text, finish_reason, usage, latency_ms, prompt_block_reason)`. Тело: `system_instruction`, `contents` (только текстовые части, без `inline_data`), `generationConfig`. Текст = склейка `parts[*].text` без `thought:true`. `usage` из `usageMetadata` (`promptTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount`).
- Новое исключение `GenerationTimeout`: `ReadTimeout` и hard-timeout профиля `long`.

### 8.9 `key_pool.py` (дефолты = старое поведение)
- `acquire(lease_ttl=None, reserve=0, model_chain=None)`; `reap_stale()` использует `lease_ttl[idx]`.
- `release(idx, error=None, model=None)`: см. I5.
- `pick_model(idx, chain=None)`: цепочка параметром, учитывает `key_model_blocked`.
- `key_state`/`grouped_keys` показывают ключ в cooldown только при блокировке ключа целиком; блокировки пар — отдельным аддитивным полем.

### 8.10 Превью (`preview.py`)
Демо-значения: `brandName="Лавка Тестов"`, `name="Анна"`, `productName="Набор «Тест»"`, `transactionalId="A-10492"`, `price="49.90"`, `supportEmail="support@example.com"`, `email="anna@example.com"`. Замена точных токенов `{{x}}`, значения через `html.escape`.

---

## 9. Наблюдаемость

### 9.1 Телеметрия
- `TEMPLATE_RUNS = deque(maxlen=2000)`: `(ts, job_id[:8], valid, attempts, total_ms, tokens_in, tokens_out, model, bytes, error_code, rule_ids_tuple)`; HTML не хранится.
- `TEMPLATE_STATS` (в памяти, обнуляется рестартом; ориентир для защиты кошелька, а не бухгалтерия): `tpl_accepted, tpl_done_valid, tpl_done_invalid, tpl_error, tpl_reused, tpl_rejected_limit`, токены `in/out/thoughts` за сутки (UTC) с датой сброса.
- `ATTEMPTS`, `KEY_LAST`, `QUALITY`, `ACCOUNTS`, `JOB_STATS` не менять.

### 9.2 `/health` (аддитивно)
- `ocr_jobs_pending/running/in_memory` считать только по `ocr_sheet`; значения не меняются, пока писем нет. Условия «unhealthy/overloaded» не менять; письма в них не входят.
- При `TEMPLATE_ENABLED=1` блок `template`: `{enabled, models, pending, running, max_concurrency, ocr_key_reserve, daily_jobs, daily_cap, daily_tokens, breaker_open, prompt_version}`.

### 9.3 `/status.json` и `status.html`
- `build_status()` добавляет `queue_by_kind: {"ocr_sheet": {...}, "email_template": {...}}` и `templates: {runs_24h, valid_rate, first_attempt_valid_rate, avg_attempts, latency_p50/p95, tokens_in/out, top_rules:[{rule,count}], last_runs[20]}`. Существующие ключи и формат не меняются; список заданий и очередь OCR фильтруются по `ocr_sheet`.
- В `status.html` допускается один блок «Письма», рендерится только при наличии `data.templates`. Если правка рискованна — пропустить и указать в отчёте.

### 9.4 Безопасность
- Все `/template/*` кроме `/preview` — `X-API-Key`; ключи в логи не пишутся.
- Свободный текст клиента в промпт не попадает.
- Нет `eval/exec/pickle`; нет чтения файлов по клиентским путям.

---

## 10. Конфигурация (`config.py`)

Значения читаются через `os.getenv`; секрет `TEMPLATE_API_KEYS` — через `_config_secret`. Невалидные числа → дефолт.

| Переменная | Дефолт | Назначение |
|---|---|---|
| `TEMPLATE_ENABLED` | `0` | Главный выключатель (I1) |
| `TEMPLATE_API_KEYS` | пусто | Ключи доступа к `/template/*`, через запятую; пусто → `503` |
| `TEMPLATE_MODELS` | пусто | Цепочка моделей писем. **Пусто → submit `503`** (lite-модель OCR не подставлять). Пример: `gemini-3.5-flash` (имя автором ТЗ не проверено: проверить смоук-тестом; 404 → следующая в цепочке) |
| `TEMPLATE_MAX_CONCURRENCY` | `1` | Параллельных генераций |
| `TEMPLATE_RESERVE_KEYS_FOR_OCR` | `1` | Сколько ключей письма не вправе занимать |
| `TEMPLATE_YIELD_TO_OCR` | `1` | Ждать, пока OCR в очереди (8.7) |
| `TEMPLATE_MAX_ATTEMPTS` | `3` | Верхний предел качественных попыток |
| `TEMPLATE_INFRA_RETRIES` | `4` | Инфраструктурных повторов на задание |
| `TEMPLATE_JOB_DEADLINE_SECONDS` | `420` | Дедлайн от старта выполнения; сторож добавляет 15 с |
| `TEMPLATE_QUEUE_MAX_WAIT_SECONDS` | `300` | Максимум ожидания в очереди |
| `TEMPLATE_MIN_ATTEMPT_SECONDS` | `90` | Не начинать новую попытку, если осталось меньше |
| `TEMPLATE_HTTP_READ_TIMEOUT` | `130` | Read-timeout профиля `long` |
| `TEMPLATE_CONNECT_TIMEOUT` | `8` | Connect-timeout профиля `long` |
| `TEMPLATE_HARD_TIMEOUT` | `150` | Потолок одного вызова |
| `TEMPLATE_LEASE_TTL` | `180` | Аренда ключа; **> `TEMPLATE_HARD_TIMEOUT`** (при старте: предупреждение в лог и автоподъём до `HARD + 30`) |
| `TEMPLATE_MAX_OUTPUT_TOKENS` | `16384` | Лимит ответа |
| `TEMPLATE_TEMPERATURE` / `_REPAIR_TEMPERATURE` | `1.0` / `0.4` | Температуры |
| `TEMPLATE_THINKING_BUDGET` | пусто | Если задан — в `thinkingConfig.thinkingBudget`; пусто → поле не отправляется |
| `TEMPLATE_MODEL_COOLDOWN` | `60` | Минимальная пауза пары (ключ, модель) после 429 |
| `TEMPLATE_MAX_PENDING` | `3` | Предел очереди типа (ожидают + выполняются) |
| `TEMPLATE_DAILY_CAP` | `100` | Заданий в сутки (UTC) |
| `TEMPLATE_DAILY_TOKEN_CAP` | `1500000` | Токенов (in+out+thoughts) в сутки |
| `TEMPLATE_RESULT_TTL_SECONDS` | `3600` | Жизнь результата |
| `TEMPLATE_STORE_MAX` | `100` | Максимум готовых заданий в памяти |
| `TEMPLATE_HISTORY` | `12` | Глубина истории директора |
| `TEMPLATE_BREAKER_FAILS` / `_PAUSE_SECONDS` | `5` / `120` | Breaker (7.3) |
| `TEMPLATE_MAX_HTML_BYTES` | `300000` | Предел HTML для validate и хранения |
| `TEMPLATE_HTML_ERROR_BYTES` | `98000` | Порог ошибки T23 |
| `TEMPLATE_VALIDATE_RATE_PER_MIN` | `30` | Запросов `/validate` в минуту на ключ |
| `TEMPLATE_DISABLED_RULES` | пусто | Отключаемые warning-правила |

Проверки согласованности при старте (логировать предупреждения, не падать): `LEASE_TTL > HARD_TIMEOUT`; `HARD_TIMEOUT < JOB_DEADLINE`; `TEMPLATE_MAX_CONCURRENCY ≤ max(len(keys) - reserve, 1)`.
Расчёт по умолчанию: 3 попытки × ≈40–90 с укладываются в 420 с. В README добавить таблицу переменных и пример `.env` без реальных значений.

---

## 11. Тесты (`tests/`, pytest + pytest-asyncio, без сети)

Сеть и Gemini мокаются (`monkeypatch` на `call_gemini_generate`, `process_images_gemini_sheet`, `_get_http_client`); время — патчем `time.monotonic`/`asyncio.sleep`. Фикстуры в `tests/fixtures/`; `good.html` — эталонное письмо, написанное вручную по правилам 8.4 и проходящее валидатор **без ошибок**; на каждое правило — минимальная мутация `good.html`, ломающая именно его.

### 11.1 Этап 0: характеризационные тесты и снимки (до любых правок, на нетронутом коде)
- `test_ocr_contract.py` (`TestClient`, `process_images_gemini_sheet` замокан): форма ответов `POST /ocr/submit` и `GET /ocr/result/{id}` в состояниях `pending/done/error`; `404` на неизвестный `job_id`; значения `queue_position/queued_jobs/running_jobs` для нескольких заданий; сторож отменяет задание старше лимита (лимит подменён малым) и освобождает ключ.
- `test_status_contract.py`: `GET /status.json` (Basic) отдаёт 200 при непустом `ATTEMPTS` (3 записи вручную) и без; `GET /health` — набор ключей и код.
- `test_key_pool.py`: round-robin; ключ в cooldown не выдаётся; `reap_stale` снимает аренду старше 60 с; `release(error)` блокирует ключ целиком; `ensure_released` идемпотентен.
- Снимки (`tests/golden/*.json`, времена нормализованы): payload `_call_gemini_rest`, `_job_queue_metrics`, `JobResultResponse`, `build_status`. После E3, E4, E7 обязаны совпасть.

### 11.2 Новый код

| Файл | Что проверяет |
|---|---|
| `test_validator.py` | эталон ⇒ `ok`; каждое правило срабатывает на своей мутации; запрещённые слова в отрицании (по каждому слову списка); `!`=2 → W, 3 → E; ✓ допустим; эмодзи в title; кнопка-ссылка; `ты`; `ol`; границы: ровно 3 пары паддинга ок, 98 000 байт ок / 98 001 E, 300 001 → только T23; устойчивость (пустая строка, не-HTML, без `<body>`, битые теги, вложенность 500 уровней, 300 КБ `<<<<`, 100 000 символов `a` в атрибуте — каждый < 1 с без исключений); производительность эталона ≤ 200 мс (цель 50 мс); `/template/rules` содержит все `RULES` |
| `test_contrast.py` | золотые пары (21:1; `#777777` fail; `#767676` pass); наследование цвета/фона; `rgba` → unverified; `linear-gradient` → худшая остановка |
| `test_extract.py` | с обёрткой ``` и без; обрыв (X02, `MAX_TOKENS`); два документа (X04); блокировка (X03); пустой ответ; хвост с №N |
| `test_autofix.py` | каждая правка отдельно, остальной HTML не меняется; идемпотентность; откат при ухудшении (`autofixes=[]`); AF06 не применяется при `<style` |
| `test_director.py` | детерминизм по seed; нет повторов из истории; пул не пустеет; белые списки; запретные корни в палитрах; override вне списка и `currency_method` вне 1..20 → ошибка; строка «Игнорируй инструкции» в любом поле запроса → `400` |
| `test_gemini_client.py` | **snapshot тела OCR-запроса (I2)**; профиль `long` создаёт отдельный клиент и не режет ответ за 20–100 с; `ReadTimeout` → `GenerationTimeout` (long) и `ProxyUnavailable` (short); `ConnectError` → `ProxyUnavailable`; `GenerationTimeout` не вызывает `mark_failed`; thought-части исключены; в теле нет `inline_data`; `finishReason`/`usage` разбираются |
| `test_key_pool_ext.py` | `reserve` (I4: при `reserve=1` и 2 ключах второй захват письма отклонён, OCR проходит); `lease_ttl` и `reap_stale` (200 с не снимается раньше, 60 с снимается); 429 пары (ключ, модель) не блокирует ключ и не взводит blackout (I5), для письма следующая модель цепочки доступна; `release` без `model` как раньше |
| `test_task_kinds.py`, `test_jobs.py` | **golden `_job_queue_metrics` для `ocr_sheet` не изменился**; метрики по `kind` не смешиваются; семафоры раздельны (письмо не блокирует OCR и наоборот); сторож убивает письмо по его дедлайну (от `started_at`), а OCR — по 60 с; ожидание в очереди > `queue_max_wait` → `queue_timeout`; отмена возвращает ключ (I10); запись без `kind` = OCR; `GET /ocr/result` письма → 404 и наоборот; ленивый семафор работает в loop `TestClient` |
| `test_generate.py` | фейковый runner: плохо→хорошо ⇒ `valid`, 2 попытки; всегда плохо ⇒ лучшая попытка, `valid=false`; автофикс спасает без второго вызова (`raw_errors` непусто, `errors` пусто); `MAX_TOKENS` → X02 → ремонт, в ремонтное сообщение попали id и `where`, HTML письма в логах нет; нет прогресса 2 раза ⇒ ранний стоп; дедлайн ⇒ лучшая попытка возвращена, а без попыток — `error`+`deadline`; инфраструктурные сбои не тратят `attempt`, после `INFRA_RETRIES` → `error` без секретов; `ocr_priority_timeout`; `ATTEMPTS` не менялся (I8) |
| `test_api.py` | 503 без `TEMPLATE_API_KEYS` и без `TEMPLATE_MODELS`; 401; 400 вне белого списка; идемпотентность (`reused`, второй вызов Gemini не делается); 429 на очередь, `DAILY_CAP`, `DAILY_TOKEN_CAP`; 404 между типами; `?html=0`; заголовки превью и Basic-авторизация, экранирование (подставить `<script>` в демо-данные патчем); `/validate`: 413 (по `Content-Length` и по факту), 429 по rate; breaker → 503; `TEMPLATE_ENABLED=0` ⇒ `/template/*` = 404 |
| `test_logs.py` | перехват stdout: нет подстрок HTML/промпта/ключей/URL прокси (I11) |
| `test_status.py` | `build_status()` и `/health` не падают при наличии `TEMPLATE_RUNS`; значения OCR-блоков не изменились |
| `test_isolation.py` | параллельно 1 письмо (мок «спит» 3 с) и 10 OCR-заданий (мок быстрый): все OCR завершаются без ожидания письма; ключ не остаётся в cooldown из-за письма; `/status.json` корректен; p50/p95 OCR не содержат латентности письма |

### 11.3 Скрипты для живого сервиса (не входят в pytest)
- `scripts/eval_templates.py --base-url … --api-key … --n 20 [--concurrency 1] [--model …] [--out eval_out/]`: `submit` → опрос раз в 4 с → HTML сохраняется локально (не в логи). Сводка и `summary.json`: доля `valid` с первой попытки и итоговая, распределение числа попыток, доля спасённых автофиксом, топ-10 нарушаемых правил (по `raw_errors`), p50/p95 длительности, токены, число заданий `error` по кодам; разнообразие: число различных палитр (три самых частых hex), способов знака, Jaccard по 5-граммам слов видимого текста и список пар со схожестью > 0.5.
- `scripts/isolation_check.py`: фоном 2 задания писем, одновременно OCR-запросы 1/с; сравнивает p95 OCR с базовым; следит, что в `gemini_keys_by_state` всегда ≥ `TEMPLATE_RESERVE_KEYS_FOR_OCR` свободных ключей.

---

## 12. Этапы (после каждого `pytest -q` зелёный)

| Этап | Содержание | Выход |
|---|---|---|
| E0 | Ветка `feature/email-template`; `requirements-dev.txt`, `pytest.ini`, каркас `tests/`; **снимки OCR до любых правок** (11.1) | Зелёные снимки на неизменённом коде. Если факт раздела 3 не подтвердился — зафиксировать в плане |
| E1 | `validator`, `contrast`, `extract`, `autofix`, `preview`, фикстура `good.html` и их тесты | Чистая подсистема, нет сети |
| E2 | `director`, `prompt`, `prompt_base.txt` | Тесты директора |
| E3 | `gemini_client` (`_post_generate`, профиль `long`, `GenerationTimeout`, `call_gemini_generate`), `key_pool` (`lease_ttl`, `reserve`, cooldown пары), `config` `TEMPLATE_*` с проверками согласованности | **E0-снимки зелёные** |
| E4 | `task_kinds`, `jobs` (kind-aware, ленивые семафоры, сторож, queue_max_wait), `routers/ocr.py` (404 чужого kind) | **E0-снимки зелёные** |
| E5 | `gemini_runner` (с OCR-приоритетом), `generate` | Тесты с фейковым runner |
| E6 | `routers/template.py`, `schemas`, store/идемпотентность/лимиты/breaker, `main.py` (флаг, версия 4.7) | API-тесты |
| E7 | `telemetry` (`TEMPLATE_RUNS/STATS`), `health`, `status_data`, (опц.) блок в `status.html` | Тесты статуса, E0-снимки зелёные |
| E8 | `scripts/*`, README (контракт POST→GET, env-таблица, пример `.env`, политика клиента: опрос 3–5 с, повтор POST на 404, смотреть `template.valid`; ограничения: память, рестарты, суточные счётчики; как включить), финальный отчёт | Отчёт |

---

## 13. Развилки и дефолты (не спрашивать)

| Вопрос | Решение |
|---|---|
| Включение по умолчанию | Нет: `TEMPLATE_ENABLED=0` |
| Расширять `ATTEMPTS` полем `task_kind` | Нет; события `tpl_*` и `TEMPLATE_RUNS` |
| Где хранить задания и результаты | В `JOBS` с `kind`, TTL через `expires_at`, лимит `TEMPLATE_STORE_MAX`; БД вне объёма |
| Модель по умолчанию | Нет; `TEMPLATE_MODELS` обязателен |
| Формат ответа модели | Текст с блоком ```` ```html ````; JSON/`responseSchema` не использовать |
| Стриминг | Нет |
| После всех попыток остались ошибки | `status="done"`, лучшая попытка, `valid=false`, полный отчёт |
| Ремонт по warnings | Нет |
| Один живой ключ | Письма идут только при простое OCR (yield); 503 на submit только при отсутствии ключей |
| Воркеры | Один; Redis не добавлять |
| История директора | Глобальная |
| Конфликт правила валидатора и `email_prompt.txt` | Промпт главнее; правило ослабить до W, добавить тест, отметить в отчёте; не удалять |
| Ложные срабатывания `BANNED` | Не расширять без теста; убирать по результату `eval_templates.py` |
| Факт из раздела 3 не подтвердился | Действовать по коду, отметить в плане |
| Нет `email_prompt.txt` | Остановиться и сообщить |
| Имя модели недоступно | Следующая в цепочке; пусто → `error` |
| `/ocr/*` без авторизации | Не менять, отметить в отчёте |
| mail.ru в реестр | Нет |
| `/preview` невалидного письма | Отдавать без изменений, статус в `X-Template-Valid` |
| Версия приложения | 4.7 |

---

## 14. Отчёт и критерии готовности

Отчёт агента (одним сообщением, без воды):
1. Расхождения с разделом 3 (номер, подтвердилось/нет, что сделал).
2. По этапам: коммит, файлы, результат тестов (passed/failed), `git diff --stat` общих файлов с пояснением, почему изменения обратно совместимы.
3. Итог `pytest -q` (число тестов, время) и отдельно доказательство I2/I8: E0-снимки зелёные после E3, E4, E7.
4. Реально добавленные переменные с дефолтами; минимум для Render/Infisical: `TEMPLATE_ENABLED=1`, `TEMPLATE_API_KEYS`, `TEMPLATE_MODELS`.
5. Что **не** проверено вживую (сеть, реальные модели, Render) и как проверить: `eval_templates.py`, `isolation_check.py`.
6. Что отложено и почему.
7. Наблюдения: открытость `/ocr/*`; потеря заданий при рестарте; ограничения суточных счётчиков; ограничения валидатора (конец 8.4).
8. Чек-лист выкатки с готовыми `curl` (ключи — плейсхолдеры): (1) деплой с `TEMPLATE_ENABLED=0`, проверить `/health`, `/status.json`, `/ocr/*`; (2) задать `TEMPLATE_API_KEYS`, `TEMPLATE_MODELS`, включить флаг; (3) `POST /template/validate` на эталоне (без траты токенов); (4) один `submit`, проверка результата и `/preview`; (5) `eval_templates.py --n 20`, затем повтор с параллельной OCR-нагрузкой; (6) откат: `TEMPLATE_ENABLED=0` без отката кода.

Definition of Done:
- [ ] Все тесты зелёные, инварианты I1–I13 покрыты тестами; у каждого правила валидатора есть тест-мутация.
- [ ] При `TEMPLATE_ENABLED=0` ответы всех старых эндпоинтов совпадают со снимками E0.
- [ ] Нет новых зависимостей в `requirements.txt`.
- [ ] Нет HTML/промпта/ключей/URL прокси в логах.
- [ ] README описывает контракт POST→GET и клиентскую политику.
- [ ] Живые ориентиры (проверяет человек, не блокируют сборку): на 20 генерациях итоговая `valid` ≥ 85–90%, медиана попыток ≤ 2, p95 длительности ≤ 200 с, ≥10 разных палитр, ≥12 способов знака, пар со схожестью > 0.5 не более 2; деградация p95 OCR под параллельной генерацией ≤ 10%, число ключей в cooldown по вине писем = 0.

---

## 15. Приложение: как разрешены расхождения между черновиками

| Тема | Варианты | Выбрано |
|---|---|---|
| `ATTEMPTS` | расширить до 9 полей / не трогать | Не трогать; события `tpl_*` (факты 7, 11) |
| Дедлайн письма | 420 с / 240 с от старта | 420 с от `started_at`, очередь ≤ 300 с |
| Один живой ключ | 503 на submit / письма при простое OCR | Письма при простое OCR (yield); 503 только без ключей |
| `TEMPLATE_MODELS` | дефолт `gemini-3.5-flash` / пусто→503 | Пусто→503 (lite для писем запрещён) |
| Исключение таймаута | `GenerationTimeout` / `GeminiCallTimeout` | `GenerationTimeout` |
| Имена env | `HTTP_TIMEOUT`/`HTTP_READ_TIMEOUT`, `LEASE_TTL`/`KEY_LEASE_TTL`, `THINKING`/`THINKING_BUDGET`, `VALIDATE_MAX_BYTES`/`MAX_HTML_BYTES` | `HTTP_READ_TIMEOUT`, `LEASE_TTL`, `THINKING_BUDGET`, `MAX_HTML_BYTES` |
| Хранилище | 200 × 3600 с / 50 × 1800 с | 100 × 3600 с |
| Повторы | `CALL_ROUTES` на попытку / `INFRA_RETRIES` на задание | `INFRA_RETRIES` (не тратят попытку) |
| Пулы директора | краткие названия / расписанные палитры | Расписанные 12 палитр, из «серых» убрана лексика с корнем «сер» |
| Окно истории | 5/6 / `HISTORY_SIZE`/8 | палитра/шапка/индикатор — последние `min(5, len-1)`, знак — 8, `avoid_palettes` — 5 |
| Комментарии в письме | Outlook-условные разрешить / запретить | Запретить (промпт: комментариев нет, кроме маркера) |
| Знак `$` | W / E | E (промпт требует «$» прямо перед `{{price}}`) |
| Размер HTML | 100 000 / 98 000 | 98 000 (E), 80 000 (W) |
| Паддинг прехедера в autofix | 6 / 10 пар | 6 пар |
| Коды правил | две нумерации | Нумерация редакции 2 (V/T/S/L/A/P/X), добавлены уникальные проверки из редакции 1: `ты`, `ol`, `role=button`, длина `h1`, доля unverified, сэйл-лексика |
| Защитные механизмы редакции 1 | — | Сохранены: откат autofix, `validate` rate limit, breaker, суточный лимит токенов, `TEMPLATE_DISABLED_RULES`, `isolation_check.py`, golden-снимки |