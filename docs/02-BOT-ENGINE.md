# 02 — Движок чат-ботов

**Статус:** детальный дизайн к реализации (этап 5 плана).
**Основание:** [DESIGN.md](../DESIGN.md), разделы 4.1–4.5. Этот документ не меняет принятых там решений — только детализирует их до уровня «завтра пишем код».

Ключевые опорные точки из DESIGN.md, на которых всё построено:

- Бот = запись в таблице `bots` (`name`, `is_enabled`, `schedule jsonb`, `scenario jsonb`, `knowledge_base text`), привязка к аккаунтам — через `avito_accounts.bot_id`.
- Состояние диалога — в `conversations.bot_active boolean` и `conversations.bot_vars jsonb`.
- Запуск: воркер входящих (`app/workers/inbound.py`, DESIGN 8.3) после записи сообщения вызывает `should_run_bot()` и ставит задачу `arq.enqueue_job("bot_step", conv.id, event.text)`.
- AI: `claude-sonnet-5` для `ai_answer`, `claude-haiku-4-5` для классификации/извлечения, таймаут 10 сек (дефолты; конфигурируются env `AI_MODEL_ANSWER` / `AI_MODEL_CLASSIFY` / `AI_TIMEOUT_SECONDS` — 05 §4, 08 §1.3); недоступность AI никогда не блокирует доставку — бот делает handoff (DESIGN 4.5).
- Сообщения бота: `messages.sender_type='bot'`, `direction='out'`, доставка через общий `deliver_message` (DESIGN 8.2). Заметки — `direction='note'`.

Хранение тегов для шага `tag` из DESIGN 4.1 — колонка `conversations.tags text[] NOT NULL DEFAULT '{}'` с GIN-индексом: она есть в схеме-источнике истины (DESIGN §4.4), отдельная миграция поверх DESIGN не нужна.

---

## 1. Формат сценария (`bots.scenario`)

### 1.1. Общая структура

Сценарий — ориентированный граф шагов, сериализованный в JSONB. Шаги лежат **массивом** (а не словарём) — порядок массива = порядок карточек в редакторе-списке (MVP UI, раздел 5). Переходы — по строковым `id` шагов.

```jsonc
{
  "version": 1,              // версия ФОРМАТА (мажорная, для будущих миграций)
  "revision": 3,             // инкремент при каждом сохранении (для дебага «на какой ревизии шёл диалог»)
  "entry": "greet",          // id стартового шага
  "settings": {              // необязательные пере-определения лимитов (см. 2.7)
    "max_steps_total": 100,
    "max_steps_per_tick": 20,
    "max_bot_messages_row": 5,
    "max_offscript_messages": 2
  },
  "steps": [ /* массив шагов, см. 1.3 */ ]
}
```

Правила графа (проверяются валидатором, раздел 5.2):

- `id` шага: `^[a-z0-9_]{1,64}$`, уникален в сценарии.
- Все ссылки (`next`, `on_timeout`, `on_invalid`, `on_low_confidence`, `options[].next`, `conditions[].next`, `else`) указывают на существующие `id`.
- Терминальные шаги — `handoff` и `close`: у них нет `next`, после них движок останавливается и `bot_active=false`.
- Каждый путь из `entry` обязан достигать терминального шага (иначе сценарий «повисает» — валидатор не даст сохранить).

### 1.2. Подстановка переменных

В любых текстах шагов (`text`, `comment`, `retry_text`) допустимы плейсхолдеры вида `{client_name}`. Пространство имён единое:

| Плейсхолдер | Источник |
|---|---|
| `{client_name}` | `clients.name` (пусто → подставляется «здравствуйте» без обращения — шаблонизатор вырезает `, {client_name}` целиком, см. `render_text`) |
| `{item_title}`, `{item_price}` | `conversations.item_title / item_price` |
| `{account_title}` | `avito_accounts.title` |
| `{имя_переменной}` | `bot_vars.vars` — всё, что собрали шаги `ask` и извлечение сущностей (напр. `{problem}`, `{phone}`, `{device_model}`) |

Неизвестный плейсхолдер при выполнении заменяется пустой строкой + warning в лог (валидатор ловит это ещё при сохранении — раздел 5.2).

> Словарь выше — **латиница**, и он действует только в сценариях ботов. У быстрых ответов
> операторов (templates, 01 §7) — свой, русский словарь `{имя}` / `{менеджер}` / `{объявление}`,
> подстановку которого делает фронт. Это две разные подсистемы — словари не смешивать.

### 1.3. Типы шагов и их параметры

Общие поля любого шага: `id`, `type`, `params` (объект, состав зависит от типа), `next` (кроме терминальных и `menu`/`condition`, где переходы внутри `params`/на верхнем уровне — см. ниже).

#### `send` — отправить сообщение

```jsonc
{ "id": "greet", "type": "send",
  "params": { "text": "Здравствуйте, {client_name}! …" },   // 1..1000 символов
  "next": "ask_problem" }
```

Движок: рендер текста → `create_outbound_message(sender_type='bot')` → `arq deliver_message` → инкремент счётчика `bot_msgs_row` → переход `next` (не ждёт фактической доставки в Авито — доставка асинхронна и ретраится сама, DESIGN 8.2).

#### `ask` — задать вопрос и ждать ответ

```jsonc
{ "id": "ask_phone", "type": "ask",
  "params": {
    "text": "Оставьте телефон — перезвоним первыми ✔",  // опционален: если null, вопрос уже задан предыдущим send
    "var": "phone",                 // имя переменной в bot_vars.vars, ^[a-z][a-z0-9_]{0,31}$
    "validate": "phone",            // "any" | "phone" | "number" | {"regex": "..."}
    "retry_text": "Не похоже на номер. Напишите в формате +7 900 000-00-00",
    "max_attempts": 2,              // попыток валидации; по исчерпании -> on_invalid
    "timeout": "24h"                // "30m" | "2h" | "24h" | секунды числом; null = ждать бесконечно (не рекомендуется)
  },
  "next": "tag_contact",            // ответ прошёл валидацию
  "on_timeout": "close_silent",     // дедлайн истёк; null => дефолт: handoff(reason="ask_timeout")
  "on_invalid": "handoff_night"     // попытки исчерпаны; null => дефолт: handoff(reason="ask_invalid")
}
```

Движок: отправляет `text` (если задан), пишет в `bot_vars.waiting` состояние ожидания (см. 2.1), ставит отложенную ARQ-задачу `bot_ask_timeout` (см. 2.4) и **останавливает тик**. Ответ клиента продолжит сценарий через `bot_step`.

Встроенные валидаторы:

```python
VALIDATORS = {
    "any":    lambda t: len(t.strip()) > 0,
    "phone":  lambda t: PHONE_RE.search(t) is not None,   # PHONE_RE см. 3.5
    "number": lambda t: re.search(r"\d+([.,]\d+)?", t) is not None,
}
# {"regex": "..."} -> re.search(params["validate"]["regex"], text, re.I)
```

При успехе в `vars[var]` пишется: для `phone` — нормализованный номер (`+7XXXXXXXXXX`), иначе — исходный текст ответа (обрезанный до 2000 символов).

#### `menu` — вопрос с вариантами

Авито не даёт нативных кнопок в API-переписке, поэтому меню — текст с нумерованными вариантами; матчинг ответа: номер варианта ИЛИ ключевые слова.

```jsonc
{ "id": "what_device", "type": "menu",
  "params": {
    "text": "Какая техника?\n1. Телефон/планшет\n2. Ноутбук\n3. Крупная бытовая",
    "var": "device_kind",            // опционально: сохранить id выбранного варианта
    "options": [
      { "id": "mobile", "label": "Телефон/планшет", "match": ["1", "телефон", "айфон", "iphone", "планшет", "смартфон"], "next": "flow_mobile" },
      { "id": "laptop", "label": "Ноутбук",          "match": ["2", "ноутбук", "макбук", "macbook"],                     "next": "flow_laptop" },
      { "id": "big",    "label": "Крупная бытовая",  "match": ["3", "стирал", "холодильник", "посудомо"],                "next": "flow_big" }
    ],
    "retry_text": "Пожалуйста, ответьте цифрой 1–3 🙂",
    "max_attempts": 2,
    "timeout": "24h"
  },
  "on_no_match": null,               // после max_attempts; null => дефолт handoff(reason="offscript")
  "on_timeout": null                 // null => дефолт handoff(reason="ask_timeout")
}
```

Матчинг: текст ответа приводится к нижнему регистру; вариант выбирается, если ответ **равен** одному из `match` или **содержит** элемент `match` длиной ≥3 как подстроку. Проверяется в порядке следования вариантов, первый совпавший побеждает. Несовпадение = «мимо сценария» → инкремент `offscript_msgs` (условие handoff №4, раздел 4) + `retry_text`.

#### `condition` — ветвление без вопроса

```jsonc
{ "id": "check_hours", "type": "condition",
  "params": {
    "conditions": [                      // проверяются по порядку, первый истинный побеждает
      { "if": { "kind": "work_hours", "from": "10:00", "to": "20:00", "timezone": "Europe/Moscow" }, "next": "handoff_day" },
      { "if": { "kind": "var_exists", "var": "phone" }, "next": "handoff_day" }
    ],
    "else": "night_msg"                  // обязателен
  }
}
```

Виды условий (`if.kind`):

| kind | Параметры | Истинно, когда |
|---|---|---|
| `work_hours` | `from`, `to` ("HH:MM"), `timezone` (default `Europe/Moscow`), опц. `days` | текущее время в интервале (интервал через полночь: `from > to` — например 20:00–10:00) |
| `var_exists` | `var` | `bot_vars.vars[var]` непусто |
| `var_equals` | `var`, `value` | точное совпадение (без регистра) |
| `text_contains` | `keywords: []` | последнее входящее сообщение содержит любое из слов (подстрока, lower) |
| `text_matches` | `regex` | `re.search` по последнему входящему |

#### `ai_answer` — ответ через Claude API

```jsonc
{ "id": "ai_draft", "type": "ai_answer",
  "params": {
    "confidence_threshold": 0.6,   // ниже порога -> on_low_confidence
    "max_reply_len": 800,          // обрезка ответа
    "context_messages": 10         // сколько последних сообщений диалога отдать модели
  },
  "next": "check_hours",
  "on_low_confidence": null        // null => дефолт handoff(reason="ai_low_confidence")
}
```

Движок: вызывает `ai_answer()` (раздел 3.2) с базой знаний бота и историей диалога. Если `needs_operator=false` и `confidence >= threshold` — отправляет `reply` как сообщение бота и идёт в `next`. Иначе — переход `on_low_confidence` (или дефолтный handoff). Таймаут/ошибка API → handoff `reason="ai_unavailable"` (раздел 3.3), **никогда** не ретраим синхронно.

#### `handoff` — передать оператору (терминальный)

```jsonc
{ "id": "handoff_day", "type": "handoff",
  "params": {
    "reason": "scenario",                                  // фиксируется в bot_vars и audit_log
    "comment": "Клиент описал проблему, бот дал предварительный ответ",  // попадёт заметкой в диалог
    "tags": ["первичный-приём"]                            // опционально, добавятся в conversations.tags
  }
}
```

Что делает `perform_handoff()` — раздел 4 (общая процедура для всех шести условий).

#### `close` — закрыть диалог (терминальный)

```jsonc
{ "id": "close_silent", "type": "close",
  "params": {
    "text": null,          // опциональное прощальное сообщение
    "silent": true         // true: закрыть без сообщения
  }
}
```

Движок: (опц.) отправляет `text`; `conversations.status='closed'`; `bot_active=false`; заметка `direction='note'` «🤖 Диалог закрыт ботом (шаг close_silent)»; событие в Pub/Sub. Если клиент напишет снова — воркер входящих переоткроет диалог (`status='new'`, DESIGN 8.3), и `should_run_bot` может запустить бота заново **с чистого листа** (см. 2.2, кроме случая `muted`).

#### `tag` — повесить теги

```jsonc
{ "id": "tag_contact", "type": "tag",
  "params": { "tags": ["контакт собран"] },
  "next": "note_phone" }
```

`UPDATE conversations SET tags = array(SELECT DISTINCT unnest(tags || %(new)s))` + Pub/Sub `conversation:updated`. Без сообщений клиенту.

#### `note` — внутренняя заметка

```jsonc
{ "id": "note_phone", "type": "note",
  "params": { "text": "Бот собрал контакт: {phone}. Проблема: {problem}" },
  "next": "handoff_night" }
```

Вставка в `messages`: `direction='note'`, `sender_type='bot'`, `body=render_text(...)`. Видна только сотрудникам (жёлтая вставка в ленте, DESIGN 3.3).

### 1.4. JSON Schema сценария

Схема (draft 2020-12) — используется и на бэке (проверка при `PUT /api/v1/bots/{id}`), и на фронте (генерация типов + первичная валидация). Файл `app/bots/scenario.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://chat.partner-lead-centre.ru/schemas/bot-scenario-v1.json",
  "title": "LeadChat bot scenario v1",
  "type": "object",
  "required": ["version", "entry", "steps"],
  "additionalProperties": false,
  "properties": {
    "version": { "const": 1 },
    "revision": { "type": "integer", "minimum": 0 },
    "entry": { "$ref": "#/$defs/stepId" },
    "settings": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "max_steps_total":       { "type": "integer", "minimum": 10, "maximum": 500 },
        "max_steps_per_tick":    { "type": "integer", "minimum": 5,  "maximum": 50 },
        "max_bot_messages_row":  { "type": "integer", "minimum": 2,  "maximum": 10 },
        "max_offscript_messages":{ "type": "integer", "minimum": 1,  "maximum": 5 }
      }
    },
    "steps": {
      "type": "array",
      "minItems": 1,
      "maxItems": 200,
      "items": { "$ref": "#/$defs/step" }
    }
  },
  "$defs": {
    "stepId":  { "type": "string", "pattern": "^[a-z0-9_]{1,64}$" },
    "stepRef": { "oneOf": [ { "$ref": "#/$defs/stepId" }, { "type": "null" } ] },
    "text":    { "type": "string", "minLength": 1, "maxLength": 1000 },
    "timeout": {
      "oneOf": [
        { "type": "string", "pattern": "^\\d+(m|h|d)$" },
        { "type": "integer", "minimum": 60, "maximum": 604800 },
        { "type": "null" }
      ]
    },
    "validate": {
      "oneOf": [
        { "enum": ["any", "phone", "number"] },
        { "type": "object", "required": ["regex"], "additionalProperties": false,
          "properties": { "regex": { "type": "string", "maxLength": 200 } } }
      ]
    },
    "condition": {
      "type": "object",
      "required": ["kind"],
      "oneOf": [
        { "properties": {
            "kind": { "const": "work_hours" },
            "from": { "type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$" },
            "to":   { "type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$" },
            "timezone": { "type": "string", "default": "Europe/Moscow" },
            "days": { "type": "array", "items": { "enum": ["mon","tue","wed","thu","fri","sat","sun"] } }
          },
          "required": ["kind", "from", "to"], "additionalProperties": false },
        { "properties": { "kind": { "const": "var_exists" }, "var": { "type": "string" } },
          "required": ["kind", "var"], "additionalProperties": false },
        { "properties": { "kind": { "const": "var_equals" }, "var": { "type": "string" }, "value": { "type": "string" } },
          "required": ["kind", "var", "value"], "additionalProperties": false },
        { "properties": { "kind": { "const": "text_contains" },
            "keywords": { "type": "array", "minItems": 1, "items": { "type": "string" } } },
          "required": ["kind", "keywords"], "additionalProperties": false },
        { "properties": { "kind": { "const": "text_matches" }, "regex": { "type": "string", "maxLength": 200 } },
          "required": ["kind", "regex"], "additionalProperties": false }
      ]
    },
    "step": {
      "type": "object",
      "required": ["id", "type", "params"],
      "properties": {
        "id":   { "$ref": "#/$defs/stepId" },
        "type": { "enum": ["send", "ask", "menu", "condition", "ai_answer", "handoff", "close", "tag", "note"] }
      },
      "allOf": [
        { "if": { "properties": { "type": { "const": "send" } } },
          "then": {
            "required": ["next"],
            "properties": {
              "next": { "$ref": "#/$defs/stepId" },
              "params": { "type": "object", "required": ["text"], "additionalProperties": false,
                "properties": { "text": { "$ref": "#/$defs/text" } } }
            } } },
        { "if": { "properties": { "type": { "const": "ask" } } },
          "then": {
            "required": ["next"],
            "properties": {
              "next": { "$ref": "#/$defs/stepId" },
              "on_timeout": { "$ref": "#/$defs/stepRef" },
              "on_invalid": { "$ref": "#/$defs/stepRef" },
              "params": { "type": "object", "required": ["var"], "additionalProperties": false,
                "properties": {
                  "text": { "oneOf": [ { "$ref": "#/$defs/text" }, { "type": "null" } ] },
                  "var":  { "type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$" },
                  "validate": { "$ref": "#/$defs/validate", "default": "any" },
                  "retry_text": { "oneOf": [ { "$ref": "#/$defs/text" }, { "type": "null" } ] },
                  "max_attempts": { "type": "integer", "minimum": 1, "maximum": 5, "default": 2 },
                  "timeout": { "$ref": "#/$defs/timeout", "default": "24h" }
                } }
            } } },
        { "if": { "properties": { "type": { "const": "menu" } } },
          "then": {
            "properties": {
              "on_no_match": { "$ref": "#/$defs/stepRef" },
              "on_timeout":  { "$ref": "#/$defs/stepRef" },
              "params": { "type": "object", "required": ["text", "options"], "additionalProperties": false,
                "properties": {
                  "text": { "$ref": "#/$defs/text" },
                  "var":  { "type": "string" },
                  "options": {
                    "type": "array", "minItems": 1, "maxItems": 10,
                    "items": { "type": "object", "required": ["id", "label", "match", "next"],
                      "additionalProperties": false,
                      "properties": {
                        "id": { "$ref": "#/$defs/stepId" },
                        "label": { "type": "string", "maxLength": 100 },
                        "match": { "type": "array", "minItems": 1, "items": { "type": "string" } },
                        "next": { "$ref": "#/$defs/stepId" }
                      } } },
                  "retry_text": { "oneOf": [ { "$ref": "#/$defs/text" }, { "type": "null" } ] },
                  "max_attempts": { "type": "integer", "minimum": 1, "maximum": 5, "default": 2 },
                  "timeout": { "$ref": "#/$defs/timeout", "default": "24h" }
                } }
            } } },
        { "if": { "properties": { "type": { "const": "condition" } } },
          "then": {
            "properties": {
              "params": { "type": "object", "required": ["conditions", "else"], "additionalProperties": false,
                "properties": {
                  "conditions": { "type": "array", "minItems": 1, "maxItems": 10,
                    "items": { "type": "object", "required": ["if", "next"], "additionalProperties": false,
                      "properties": { "if": { "$ref": "#/$defs/condition" }, "next": { "$ref": "#/$defs/stepId" } } } },
                  "else": { "$ref": "#/$defs/stepId" }
                } }
            } } },
        { "if": { "properties": { "type": { "const": "ai_answer" } } },
          "then": {
            "required": ["next"],
            "properties": {
              "next": { "$ref": "#/$defs/stepId" },
              "on_low_confidence": { "$ref": "#/$defs/stepRef" },
              "params": { "type": "object", "additionalProperties": false,
                "properties": {
                  "confidence_threshold": { "type": "number", "minimum": 0, "maximum": 1, "default": 0.6 },
                  "max_reply_len": { "type": "integer", "minimum": 100, "maximum": 1000, "default": 800 },
                  "context_messages": { "type": "integer", "minimum": 2, "maximum": 30, "default": 10 }
                } }
            } } },
        { "if": { "properties": { "type": { "const": "handoff" } } },
          "then": {
            "properties": {
              "params": { "type": "object", "additionalProperties": false,
                "properties": {
                  "reason": { "type": "string", "default": "scenario" },
                  "comment": { "oneOf": [ { "$ref": "#/$defs/text" }, { "type": "null" } ] },
                  "tags": { "type": "array", "items": { "type": "string", "maxLength": 50 } }
                } }
            },
            "not": { "required": ["next"] } } },
        { "if": { "properties": { "type": { "const": "close" } } },
          "then": {
            "properties": {
              "params": { "type": "object", "additionalProperties": false,
                "properties": {
                  "text": { "oneOf": [ { "$ref": "#/$defs/text" }, { "type": "null" } ] },
                  "silent": { "type": "boolean", "default": false }
                } }
            },
            "not": { "required": ["next"] } } },
        { "if": { "properties": { "type": { "const": "tag" } } },
          "then": {
            "required": ["next"],
            "properties": {
              "next": { "$ref": "#/$defs/stepId" },
              "params": { "type": "object", "required": ["tags"], "additionalProperties": false,
                "properties": { "tags": { "type": "array", "minItems": 1, "items": { "type": "string", "maxLength": 50 } } } }
            } } },
        { "if": { "properties": { "type": { "const": "note" } } },
          "then": {
            "required": ["next"],
            "properties": {
              "next": { "$ref": "#/$defs/stepId" },
              "params": { "type": "object", "required": ["text"], "additionalProperties": false,
                "properties": { "text": { "$ref": "#/$defs/text" } } }
            } } }
      ]
    }
  }
}
```

JSON Schema не умеет проверять ссылочную целостность (`next` указывает на существующий шаг) и достижимость — это делает второй проход валидатора на Python (раздел 5.2).

### 1.5. Пример: сценарий «Первичный приём» (валидный JSON)

Дословная реализация DESIGN 4.2. Это сценарий по умолчанию — создаётся сид-миграцией при первом запуске.

```json
{
  "version": 1,
  "revision": 1,
  "entry": "greet",
  "settings": { "max_bot_messages_row": 5, "max_offscript_messages": 2 },
  "steps": [
    {
      "id": "greet",
      "type": "send",
      "params": { "text": "Здравствуйте, {client_name}! Это сервис Lead Partner 👋\nПодскажите, что случилось с техникой — модель и проблему?" },
      "next": "ask_problem"
    },
    {
      "id": "ask_problem",
      "type": "ask",
      "params": { "text": null, "var": "problem", "validate": "any", "retry_text": null, "max_attempts": 1, "timeout": "24h" },
      "next": "ai_draft",
      "on_timeout": "handoff_no_reply",
      "on_invalid": null
    },
    {
      "id": "ai_draft",
      "type": "ai_answer",
      "params": { "confidence_threshold": 0.6, "max_reply_len": 800, "context_messages": 10 },
      "next": "check_hours",
      "on_low_confidence": null
    },
    {
      "id": "check_hours",
      "type": "condition",
      "params": {
        "conditions": [
          { "if": { "kind": "work_hours", "from": "10:00", "to": "20:00", "timezone": "Europe/Moscow" }, "next": "handoff_day" }
        ],
        "else": "night_msg"
      }
    },
    {
      "id": "handoff_day",
      "type": "handoff",
      "params": {
        "reason": "scenario",
        "comment": "Клиент описал проблему, бот дал предварительный ответ",
        "tags": ["первичный-приём"]
      }
    },
    {
      "id": "night_msg",
      "type": "send",
      "params": { "text": "Мастер ответит утром. Оставьте телефон — перезвоним первыми ✔" },
      "next": "ask_phone"
    },
    {
      "id": "ask_phone",
      "type": "ask",
      "params": {
        "text": null,
        "var": "phone",
        "validate": "phone",
        "retry_text": "Кажется, это не номер телефона 🙂 Напишите в формате +7 900 000-00-00",
        "max_attempts": 2,
        "timeout": "12h"
      },
      "next": "tag_contact",
      "on_timeout": "handoff_night",
      "on_invalid": "handoff_night"
    },
    {
      "id": "tag_contact",
      "type": "tag",
      "params": { "tags": ["контакт собран"] },
      "next": "note_contact"
    },
    {
      "id": "note_contact",
      "type": "note",
      "params": { "text": "🤖 Бот собрал контакт: {phone}\nПроблема со слов клиента: {problem}" },
      "next": "handoff_night"
    },
    {
      "id": "handoff_night",
      "type": "handoff",
      "params": {
        "reason": "scenario",
        "comment": "Ночной диалог: проблема зафиксирована, перезвонить утром первыми",
        "tags": ["ночной-лид"]
      }
    },
    {
      "id": "handoff_no_reply",
      "type": "handoff",
      "params": {
        "reason": "scenario",
        "comment": "Клиент не ответил на первый вопрос — диалог возвращён в общую очередь",
        "tags": []
      }
    }
  ]
}
```

Триггер (DESIGN 4.2): первое входящее сообщение в новом диалоге, менеджер ещё не отвечал — это и проверяет `should_run_bot()` (2.2). Ветка «confidence < порога → сразу handoff» покрыта дефолтом `on_low_confidence: null` → `handoff(reason="ai_low_confidence")`.

> **Молчание клиента ведёт на handoff, а не на `close`** — решение владельца: автоматического закрытия диалогов по таймауту в продукте нет нигде. Шаг `close` остаётся в языке сценариев (админ вправе построить ветку, которая закрывает диалог осознанно), но дефолтный сценарий им не пользуется. Ровно этот граф вшит в сид-миграцию `0005_bots_engine` и в `app/bots/scenarios` — примеры и код обязаны совпадать, иначе следующий читатель воспроизведёт закрытие.

---

## 2. Runtime: машина состояний и воркеры

### 2.1. Состояние диалога

Всё состояние бота в диалоге живёт в двух полях `conversations` (DESIGN 4.4), ничего больше не добавляем:

- `bot_active boolean` — бот «владеет» диалогом прямо сейчас. Единственный флаг, который читают чужие подсистемы (UI показывает 🤖, воркер входящих решает, звать ли `bot_step`).
- `bot_vars jsonb` — приватное состояние движка:

```jsonc
{
  "bot_id": "6f0e…",              // какой бот вёл/ведёт диалог
  "scenario_revision": 3,         // ревизия сценария на момент старта (сценарий читается из bots при каждом тике,
                                  // но при несовпадении ревизии и отсутствии текущего шага в новом сценарии -> handoff(reason="scenario_changed"))
  "step": "ask_phone",            // текущий шаг; для ask/menu — тот, чей ответ ждём
  "waiting": {                    // null, если ничего не ждём
    "kind": "ask",                // "ask" | "menu"
    "var": "phone",
    "deadline": "2026-08-05T09:12:00+00:00",
    "token": "d81ad0c2",          // случайный токен: отложенная задача таймаута сверяет его (см. 2.4)
    "attempts": 0                 // сколько невалидных ответов уже было
  },
  "vars": { "problem": "iPhone 13, разбит экран", "phone": "+79001234567" },
  "counters": {
    "steps_total": 7,             // выполнено шагов за всю жизнь диалога
    "bot_msgs_row": 1,            // сообщений бота ПОДРЯД без ответа клиента
    "offscript_msgs": 0,          // входящих «мимо сценария» подряд (условие handoff №4)
    "ai_calls": 1
  },
  "muted": false,                 // true после вмешательства оператора — НАВСЕГДА (см. 2.6)
  "handoff": {                    // заполняется при handoff
    "reason": "scenario",
    "at": "2026-08-05T06:40:11+00:00",
    "step": "handoff_night"
  },
  "started_at": "2026-08-04T21:03:00+00:00",
  "last_step_at": "2026-08-05T06:40:11+00:00"
}
```

Машина состояний диалога с точки зрения бота:

```
                    should_run_bot() == true
   IDLE ──────────────────────────────────────────▶ RUNNING (внутри тика bot_step)
 (bot_active=false,                                    │
  bot_vars={} или muted)                               ├─ шаг ask/menu ──▶ WAITING (bot_active=true, waiting≠null)
        ▲                                              │                      │ входящее сообщение ──▶ RUNNING
        │                                              │                      │ таймаут (ARQ) ────────▶ RUNNING
        │                                              ├─ handoff ──▶ IDLE (bot_active=false, handoff зафиксирован)
        │                                              └─ close ────▶ IDLE (status='closed')
        │
        └── оператор написал в любой момент ⇒ MUTED (bot_active=false, muted=true, навсегда)
```

`RUNNING` — транзиентное состояние внутри одного вызова воркера; в БД между тиками бот всегда либо WAITING, либо IDLE/MUTED.

### 2.2. Запуск: `should_run_bot`

Вызывается воркером входящих (DESIGN 8.3) после записи сообщения клиента, синхронно, без запросов к внешним API:

```python
# app/bots/runtime.py
def should_run_bot(conv: Conversation, account: AvitoAccount, bot: Bot | None) -> bool:
    if conv.bot_active:
        return True                      # бот ждёт ответа (ask/menu) — продолжаем сценарий
    if conv.bot_vars.get("muted"):
        return False                     # оператор вмешивался — бот молчит навсегда (условие №6)
    if bot is None or not bot.is_enabled:
        return False                     # к аккаунту не привязан бот / бот выключен
    if conv.bot_vars.get("handoff"):
        return False                     # бот уже отдал диалог оператору — второй раз не входим
    if has_operator_messages(conv):      # EXISTS(messages WHERE conversation_id=... AND sender_type='operator')
        return False                     # «менеджер ещё не отвечал» из триггера DESIGN 4.2
    if not is_bot_scheduled_now(bot):    # расписание, см. 2.5
        return False
    return conv.status == "new"          # только свежий диалог
```

`bot` берётся по `account.bot_id` (join уже сделан воркером входящих). Обратите внимание: расписание проверяется при **старте** сценария; уже начатый диалог (`bot_active=true`) бот доводит до конца и вне расписания — иначе клиент, ответивший в 10:01 на ночной вопрос, останется без реакции.

### 2.3. Воркер `bot_step`

ARQ-задача, сигнатура совпадает с вызовом из DESIGN 8.3: `bot_step(ctx, conversation_id, incoming_text)`. Для срабатывания по таймауту `incoming_text=None`.

```python
# app/bots/runtime.py
import asyncio, secrets, structlog
from datetime import datetime, timedelta, timezone

log = structlog.get_logger()

MAX_STEPS_PER_TICK_DEFAULT = 20

async def bot_step(ctx, conversation_id: UUID, incoming_text: str | None) -> None:
    redis, db = ctx["redis"], ctx["db_session_factory"]()
    # 1. Пер-диалоговый лок: клиент может прислать 3 сообщения за секунду,
    #    consumer group отдаст их разным воркерам — сериализуемся.
    lock_key = f"lock:bot:{conversation_id}"
    if not await redis.set(lock_key, "1", nx=True, ex=30):
        await ctx["arq"].enqueue_job("bot_step", conversation_id, incoming_text,
                                     _defer_by=timedelta(seconds=1))
        return
    try:
        async with db.begin():
            conv = await get_conversation_for_update(db, conversation_id)   # SELECT ... FOR UPDATE
            bot = await get_bot_for_conversation(db, conv)                  # по avito_accounts.bot_id
            # 2. Повторные guard'ы: между enqueue и выполнением всё могло измениться
            if conv is None or bot is None or not bot.is_enabled:
                return
            if conv.bot_vars.get("muted") or has_operator_messages_cached(conv):
                await mute_bot(db, conv)                                    # 2.6
                return

            state = BotState.from_conv(conv)                                # парсинг bot_vars
            engine = ScenarioEngine(bot=bot, conv=conv, state=state, db=db, redis=redis, arq=ctx["arq"])

            if incoming_text is not None:
                await engine.on_incoming(incoming_text)   # пред-обработка входящего: детекторы handoff (раздел 4),
                                                          # валидация ask/menu, извлечение телефона
            else:
                await engine.on_timeout()                 # 2.4: истёк дедлайн ask/menu

            await engine.run()                            # 3. основной цикл — до ask/терминала/лимита
            conv.bot_vars = state.dump()                  # 4. атомарная фиксация состояния вместе с сообщениями
    finally:
        await redis.delete(lock_key)
```

Основной цикл движка:

```python
# app/bots/engine.py
class ScenarioEngine:
    async def on_incoming(self, text: str) -> None:
        s = self.state
        s.counters["bot_msgs_row"] = 0                       # клиент ответил — серия бота прервана

        # --- детекторы, работающие на КАЖДОМ входящем (раздел 4) ---
        if detect_human_request(text):                       # условие №1: «позовите оператора»
            await self.do_handoff(reason="client_request"); return
        asyncio.ensure_future(self.classify_and_react(text)) # условие №3: негатив (haiku, не блокирует тик — 3.4)
        maybe_extract_phone_to_vars(self.conv, s, text)      # регулярка, как в DESIGN 3.3/8.3

        # --- продолжение сценария ---
        if s.bot_active_first_run():                         # bot_vars пуст: старт сценария
            s.init(bot=self.bot)
            self.conv.bot_active = True
            s.next_step = self.scenario.entry
            return

        w = s.waiting
        if w is None:
            # бот ничего не спрашивал, а клиент пишет: «мимо сценария» (условие №4)
            s.counters["offscript_msgs"] += 1
            if s.counters["offscript_msgs"] >= self.limits.max_offscript_messages:
                await self.do_handoff(reason="offscript")
            return

        step = self.scenario.step(s.step)
        if w["kind"] == "ask":
            ok, value = validate_answer(step.params, text)
            if ok:
                s.vars[step.params["var"]] = value
                s.stop_waiting(); s.counters["offscript_msgs"] = 0
                s.next_step = step.next
            else:
                await self.handle_invalid_answer(step, w)    # retry_text / on_invalid
        elif w["kind"] == "menu":
            option = match_menu_option(step.params["options"], text)
            if option:
                if step.params.get("var"): s.vars[step.params["var"]] = option["id"]
                s.stop_waiting(); s.counters["offscript_msgs"] = 0
                s.next_step = option["next"]
            else:
                s.counters["offscript_msgs"] += 1            # несовпадение с меню — тоже «мимо сценария»
                await self.handle_invalid_answer(step, w)

    async def run(self) -> None:
        s = self.state
        executed = 0
        while s.next_step is not None:
            if s.counters["steps_total"] >= self.limits.max_steps_total \
               or executed >= self.limits.max_steps_per_tick \
               or s.counters["bot_msgs_row"] >= self.limits.max_bot_messages_row:
                await self.do_handoff(reason="loop_protection")   # 2.7
                return
            step = self.scenario.step(s.next_step)
            if step is None:                                       # сценарий отредактировали под ногами
                await self.do_handoff(reason="scenario_changed"); return
            s.step, s.next_step = step.id, None
            s.counters["steps_total"] += 1; executed += 1
            s.last_step_at = utcnow_iso()
            await self.execute(step)          # диспетчер по step.type; ask/menu выставляют waiting
                                              # и НЕ выставляют next_step -> цикл останавливается;
                                              # handoff/close переводят в IDLE; остальные пишут next_step

    async def execute(self, step) -> None:
        handler = {
            "send": self.exec_send, "ask": self.exec_ask, "menu": self.exec_menu,
            "condition": self.exec_condition, "ai_answer": self.exec_ai_answer,
            "handoff": self.exec_handoff, "close": self.exec_close,
            "tag": self.exec_tag, "note": self.exec_note,
        }[step.type]
        await handler(step)
```

`exec_ask` (показывает работу с ожиданием и таймером):

```python
    async def exec_ask(self, step) -> None:
        p = step.params
        if p.get("text"):
            await self.send_bot_message(render_text(p["text"], self.ctx_vars()))
        token = secrets.token_hex(4)
        timeout = parse_timeout(p.get("timeout"))            # "24h" -> timedelta | None
        self.state.waiting = {
            "kind": "ask", "var": p["var"], "token": token, "attempts": 0,
            "deadline": (utcnow() + timeout).isoformat() if timeout else None,
        }
        if timeout:
            await self.arq.enqueue_job("bot_ask_timeout", self.conv.id, token,
                                       _defer_by=timeout)
        # next_step не выставляем -> run() останавливается, бот в состоянии WAITING
```

`send_bot_message` — единая точка исходящих бота:

```python
    async def send_bot_message(self, text: str) -> None:
        msg = await create_outbound_message(self.db, self.conv, user=None,
                                            text=text, sender_type="bot")   # delivery_status='pending'
        await self.arq.enqueue_job("deliver_message", msg.id)               # общий контур доставки, DESIGN 8.2
        await publish_event(self.redis, "message:new", self.conv, msg)      # -> WebSocket Hub
        self.state.counters["bot_msgs_row"] += 1
```

### 2.4. Таймаут `ask`/`menu` (ARQ deferred job)

При установке ожидания ставится **отложенная** задача (`_defer_by=timeout`, штатный механизм ARQ). Отменять её при ответе клиента не нужно — она самоаннулируется по токену:

```python
# app/bots/runtime.py
async def bot_ask_timeout(ctx, conversation_id: UUID, token: str) -> None:
    """Срабатывает через timeout после exec_ask. No-op, если ответ уже пришёл."""
    db = ctx["db_session_factory"]()
    async with db.begin():
        conv = await get_conversation_for_update(db, conversation_id)
        w = (conv.bot_vars or {}).get("waiting")
        if not conv.bot_active or not w or w.get("token") != token:
            return          # клиент уже ответил (waiting сброшен/пересоздан с другим токеном)
    # токен совпал — дедлайн реально истёк: продолжаем сценарий веткой on_timeout
    await ctx["arq"].enqueue_job("bot_step", conversation_id, None)
```

Внутри `engine.on_timeout()`:

```python
    async def on_timeout(self) -> None:
        s = self.state
        step = self.scenario.step(s.step)
        s.stop_waiting()
        target = step.on_timeout if step and step.on_timeout else None
        if target:
            s.next_step = target                     # напр. close_silent или handoff_night
        else:
            await self.do_handoff(reason="ask_timeout")   # дефолт: молча передать в очередь
```

Каждый повторный `exec_ask` (в т.ч. retry после невалидного ответа) генерирует **новый** токен и новую отложенную задачу — старая при срабатывании увидит чужой токен и умрёт. Это дешевле, чем `Job.abort()`, и устойчиво к рестартам воркеров: состояние истинности — только в `bot_vars.waiting`.

Ретрай невалидного ответа:

```python
    async def handle_invalid_answer(self, step, waiting) -> None:
        waiting["attempts"] += 1
        if waiting["attempts"] >= step.params.get("max_attempts", 2):
            self.state.stop_waiting()
            target = step.on_invalid or step.on_no_match  # ask | menu
            if target: self.state.next_step = target
            else: await self.do_handoff(reason="ask_invalid")
            return
        retry_text = step.params.get("retry_text")
        if retry_text:
            await self.send_bot_message(render_text(retry_text, self.ctx_vars()))
        # waiting остаётся, дедлайн и токен НЕ пересоздаём — общий дедлайн шага один
```

### 2.5. Расписание активности (`bots.schedule`)

Формат JSONB (дефолт из DDL — `{"always": true}`):

```jsonc
// круглосуточно (значение по умолчанию)
{ "always": true }

// «только вне рабочих часов 20:00–10:00 МСК» (пример из DESIGN 4.1)
{
  "always": false,
  "timezone": "Europe/Moscow",
  "intervals": [
    { "days": ["mon","tue","wed","thu","fri","sat","sun"], "start": "20:00", "end": "10:00" }
  ]
}

// «только в выходные днём»
{
  "always": false,
  "timezone": "Europe/Moscow",
  "intervals": [ { "days": ["sat","sun"], "start": "10:00", "end": "20:00" } ]
}
```

Правила: интервалы объединяются по ИЛИ; `start > end` означает интервал через полночь (20:00–10:00 = 20:00–24:00 текущего дня + 00:00–10:00 следующего; `days` относится к дню **начала** интервала). Проверка:

```python
# app/bots/schedule.py
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def is_bot_scheduled_now(bot: Bot, now: datetime | None = None) -> bool:
    sch = bot.schedule or {"always": True}
    if sch.get("always", False):
        return True
    tz = ZoneInfo(sch.get("timezone", "Europe/Moscow"))
    local = (now or datetime.now(tz=tz)).astimezone(tz)
    for iv in sch.get("intervals", []):
        if _hits(iv, local):
            return True
    return False

def _hits(iv: dict, local: datetime) -> bool:
    start, end = _hm(iv["start"]), _hm(iv["end"])
    t = (local.hour, local.minute)
    today = DAYS[local.weekday()]
    days = iv.get("days", DAYS)
    if start <= end:                                   # обычный интервал в пределах суток
        return today in days and start <= t < end
    # интервал через полночь: либо «хвост» сегодняшнего дня, либо «голова» после дня начала
    yesterday = DAYS[(local.weekday() - 1) % 7]
    return (today in days and t >= start) or (yesterday in days and t < end)

def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":"); return int(h), int(m)
```

Таймзона по умолчанию всюду — `Europe/Moscow` (`zoneinfo`, никакого `pytz`); в контейнерах `TZ` не важен — все вычисления явные. Тот же модуль используют условие `work_hours` шага `condition` и песочница (раздел 5.3, где `now` подменяется).

Семантика: расписание — фильтр на **вход** бота в диалог (`should_run_bot`). Начатый диалог доводится независимо от расписания; `bot_ask_timeout` тоже срабатывает всегда.

### 2.6. «Оператор написал → бот замолкает навсегда»

Условие №6 из DESIGN 4.3. Реализуется не в движке, а в точке создания исходящего сообщения оператора — так правило работает, даже если бот в этот момент ждал ответа или тик был в полёте:

```python
# app/api/routes/messages.py — endpoint POST /conversations/{id}/messages (DESIGN 8.2)
async def send_message(...):
    conv = await get_conversation_or_404(db, conv_id)
    msg = await create_outbound_message(db, conv, user, payload.text)   # sender_type='operator'
    if conv.bot_active or not conv.bot_vars.get("muted"):
        await mute_bot(db, conv, by_user=user)
    await arq.enqueue_job("deliver_message", msg.id)
    ...

# app/bots/runtime.py
async def mute_bot(db, conv, by_user=None) -> None:
    conv.bot_active = False
    bv = dict(conv.bot_vars or {})
    bv["muted"] = True
    bv["waiting"] = None            # ожидающий bot_ask_timeout самоаннулируется по токену
    conv.bot_vars = bv
    await write_audit(db, user_id=by_user.id if by_user else None,
                      action="bot.muted", entity="conversation", entity_id=str(conv.id))
```

Гарантии:

- `muted` живёт в `bot_vars` и **никогда не сбрасывается** — даже когда клиент возвращается в закрытый диалог и `status` снова становится `new` (DESIGN 8.3), `should_run_bot` вернёт `False`.
- Гонка «тик бота уже выполняется, оператор написал параллельно»: `bot_step` берёт `SELECT ... FOR UPDATE` на диалог и перепроверяет `muted`/`has_operator_messages` в начале транзакции; endpoint оператора обновляет ту же строку — один из двух дождётся другого, и максимум, что успеет бот, — дослать сообщение текущего шага. Следующего тика не будет.
- Сообщения самого бота (`sender_type='bot'`) и системные (`system`) правило не триггерят — только `operator`.
- Заметки (`direction='note'`) бота не глушат: менеджер может оставить внутренний комментарий, не отбирая диалог у бота. Глушит только реальный исходящий текст клиенту.

### 2.7. Защита от зацикливания

Три независимых предохранителя (лимиты из `scenario.settings`, дефолты в скобках):

1. **`max_steps_per_tick` (20)** — шагов за один вызов `bot_step`. Ловит циклы из «бесплатных» шагов (`condition` → `tag` → `condition` → …), которые не порождают сообщений и иначе крутились бы вечно внутри одного тика.
2. **`max_steps_total` (100)** — шагов за всю жизнь диалога (счётчик в `bot_vars.counters.steps_total`). Ловит медленные циклы через ask/ответ («задай вопрос — получи ответ — вернись к вопросу»).
3. **`max_bot_messages_row` (5)** — сообщений бота подряд без единого входящего (`counters.bot_msgs_row`, сбрасывается в `on_incoming`). Ловит любые конфигурации, при которых бот заваливает клиента текстом; это и анти-спам-предохранитель по отношению к Авито.

Срабатывание любого лимита ⇒ `do_handoff(reason="loop_protection")` + событие в Sentry с `bot_id`, `step`, счётчиками — это всегда ошибка конфигурации сценария, её должен увидеть админ. Валидатор при сохранении дополнительно ищет статические циклы без `ask`/`menu` на пути (раздел 5.2) — рантайм-лимиты остаются последней линией обороны (циклы через условия от данных статически не поймать).

---

## 3. Шаг `ai_answer` и AI-подсистема

Все вызовы Claude API — только из воркера (`app/bots/ai.py`), через `AsyncAnthropic`, официальный SDK `anthropic`. Ключ — `ANTHROPIC_API_KEY` из env. Никогда из HTTP-запроса пользователя.

```python
# app/bots/ai.py
from anthropic import AsyncAnthropic
client = AsyncAnthropic(timeout=9.0, max_retries=0)   # жёсткий бюджет: ретраи запрещены,
                                                      # общий лимит контролирует asyncio.wait_for(10.0)
```

### 3.1. System-prompt для `ai_answer` (точный текст)

Собирается из двух частей: статический каркас (ниже, в коде — константа `AI_ANSWER_SYSTEM`) + `bots.knowledge_base` (цены, сроки, регламенты — правит админ в редакторе бота). Каркас стабилен → помечаем `cache_control` для prompt-кэша, база знаний — вторым блоком.

```text
Ты — ассистент сервисного центра Lead Partner (ремонт техники: смартфоны, планшеты,
ноутбуки, бытовая техника). Ты отвечаешь клиентам в чате Авито от имени сервиса,
пока мастер недоступен. Твоя задача — дать первичную консультацию по типовым
вопросам (ориентировочные цены, сроки, порядок работы) и собрать контекст для мастера.

ПРАВИЛА (нарушать их нельзя ни при каких условиях):
1. Отвечай ТОЛЬКО на основе базы знаний, приведённой ниже. Если ответа в базе нет —
   не отвечай по существу: верни needs_operator=true и низкую confidence.
2. НИКОГДА не обещай точную стоимость или срок ремонта. Любая цена — «ориентировочно,
   от N ₽, точная стоимость после бесплатной диагностики».
3. НИКОГДА не выдумывай услуги, акции, скидки, адреса или гарантийные условия,
   которых нет в базе знаний.
4. Не проси предоплату, не давай реквизитов, не отправляй ссылок.
5. Не выдавай себя за живого мастера, но и не подчёркивай, что ты бот, если не спросили.
   Если спросили прямо — честно скажи, что ты автоответчик сервиса, и предложи позвать мастера.
6. Пиши по-русски, дружелюбно и коротко: 1–3 предложения, без списков и заголовков,
   без эмодзи-спама (максимум один эмодзи). Обращайся на «вы».
7. Если клиент раздражён, ругается, говорит о жалобе/возврате/споре — не спорь,
   верни needs_operator=true.
8. Если вопрос не про ремонт техники (спам, реклама, другая тема) — needs_operator=true,
   confidence не выше 0.2.
9. Сомневаешься — needs_operator=true. Передать мастеру — всегда лучше, чем ошибиться.

Ответ верни ТОЛЬКО вызовом инструмента submit_answer. Поля:
- reply: текст ответа клиенту (даже при needs_operator=true — вежливая фраза-мост,
  например «Передаю ваш вопрос мастеру, он ответит в ближайшее время»);
- confidence: число 0..1 — насколько ответ покрыт базой знаний
  (1.0 — прямой ответ из базы; 0.5 — частично; ниже 0.4 — базы не хватает);
- needs_operator: true, если нужен живой сотрудник (нет ответа в базе, негатив,
  нетиповой случай, просьба позвать человека).

=== БАЗА ЗНАНИЙ LEAD PARTNER ===
{knowledge_base}
=== КОНЕЦ БАЗЫ ЗНАНИЙ ===
```

### 3.2. Структурированный ответ через tool-use

Модель — `settings.ai_model_answer` (env `AI_MODEL_ANSWER`, по умолчанию `claude-sonnet-5` — DESIGN 2/4.5, 05 §4, 08 §1.3). Структура гарантируется принудительным вызовом инструмента со `strict: true` — распарсенный `input` обязан соответствовать схеме, никакого «JSON в свободном тексте».

```python
# app/bots/ai.py
import asyncio
from anthropic import APIError, APITimeoutError, APIConnectionError

from app.core.config import settings   # AI_MODEL_* / AI_TIMEOUT_SECONDS (05 §4, 08 §1.3)

SUBMIT_ANSWER_TOOL = {
    "name": "submit_answer",
    "description": "Вернуть структурированный ответ для клиента сервиса Lead Partner.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "reply":          {"type": "string", "description": "Ответ клиенту, 1-3 предложения, по-русски"},
            "confidence":     {"type": "number", "description": "0..1, покрытие ответа базой знаний"},
            "needs_operator": {"type": "boolean", "description": "true, если нужен живой сотрудник"},
        },
        "required": ["reply", "confidence", "needs_operator"],
        "additionalProperties": False,
    },
}

class AIAnswer(TypedDict):
    reply: str
    confidence: float
    needs_operator: bool

AI_TIMEOUT_SECONDS = float(settings.ai_timeout_seconds)   # DESIGN 4.5: таймаут всех AI-вызовов (env, дефолт 10 сек)

async def ai_answer(bot: Bot, dialog: list[dict], item_title: str | None) -> AIAnswer | None:
    """None => AI недоступен/не уложился: вызывающий обязан сделать handoff (3.3)."""
    system = [
        {"type": "text", "text": AI_ANSWER_SYSTEM_STATIC,
         "cache_control": {"type": "ephemeral"}},                     # кэшируемый каркас
        {"type": "text", "text": AI_ANSWER_KB_TEMPLATE.format(
            knowledge_base=bot.knowledge_base or "(база знаний пуста)")},
    ]
    messages = []
    if item_title:
        messages.append({"role": "user",
                         "content": f"[Контекст: клиент пишет по объявлению «{item_title}»]"})
    messages += dialog     # последние context_messages сообщений: client->user, bot/operator->assistant
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=settings.ai_model_answer,
                max_tokens=1024,
                system=system,
                messages=messages,
                tools=[SUBMIT_ANSWER_TOOL],
                tool_choice={"type": "tool", "name": "submit_answer",
                             "disable_parallel_tool_use": True},
            ),
            timeout=AI_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, APITimeoutError, APIConnectionError, APIError) as e:
        log.warning("ai_answer_failed", bot_id=str(bot.id), error=type(e).__name__)
        return None

    block = next((b for b in resp.content if b.type == "tool_use"
                  and b.name == "submit_answer"), None)
    if block is None:
        return None                       # аномалия (refusal и т.п.) — трактуем как недоступность
    data = block.input                    # уже валидирован API благодаря strict
    return AIAnswer(reply=data["reply"][:1000],
                    confidence=max(0.0, min(1.0, float(data["confidence"]))),
                    needs_operator=bool(data["needs_operator"]))
```

Сборка `dialog`: последние `params.context_messages` сообщений диалога с `direction in ('in','out')` (заметки и системные исключаем), маппинг ролей: `sender_type='client'` → `user`, `bot`/`operator` → `assistant`; соседние одноимённые роли API склеивает сам.

Использование в движке:

```python
    async def exec_ai_answer(self, step) -> None:
        p = step.params
        self.state.counters["ai_calls"] += 1
        result = await ai_answer(self.bot,
                                 await load_dialog_for_ai(self.db, self.conv, p["context_messages"]),
                                 self.conv.item_title)
        if result is None:                                    # 3.3: таймаут/недоступность
            await self.do_handoff(reason="ai_unavailable"); return
        self.state.vars["_ai_confidence"] = result["confidence"]
        if result["needs_operator"] or result["confidence"] < p["confidence_threshold"]:
            if result["reply"] and result["needs_operator"]:
                await self.send_bot_message(result["reply"])  # фраза-мост «передаю мастеру»
            target = step.on_low_confidence
            if target: self.state.next_step = target
            else: await self.do_handoff(reason="ai_low_confidence")
            return
        await self.send_bot_message(result["reply"][: p["max_reply_len"]])
        self.state.next_step = step.next
```

### 3.3. Таймаут 10с и недоступность API → handoff

Политика (DESIGN 4.5: «недоступность AI никогда не блокирует доставку сообщений»):

- Бюджет одного вызова — жёсткие **10 секунд** стены: `asyncio.wait_for(10.0)` поверх клиентского `timeout=9.0`, `max_retries=0` (авторетраи SDK запрещены — они умножают время).
- Любая ошибка (`TimeoutError`, connection, 429, 5xx, 400, refusal) ⇒ `ai_answer()` возвращает `None` ⇒ движок делает `handoff(reason="ai_unavailable")` и **не ретраит**: клиент попадает к живому оператору быстрее, чем мы дождались бы восстановления API.
- Ошибка логируется в Sentry с типом исключения; в диалоге появляется системная заметка «🤖 AI недоступен, передал оператору», чтобы менеджер понимал контекст.
- Circuit breaker (дёшево и достаточно): счётчик `INCR ai:fail` c `EX 300` в Redis; при `>= 10` отказов за 5 минут `exec_ai_answer` не вызывает API вовсе, сразу handoff. Сбрасывается первым успешным вызовом (`DEL ai:fail`). Это спасает от расстрела rate limit'а и очередей в момент инцидента у провайдера.

### 3.4. Классификатор негатива (`settings.ai_model_classify`, по умолчанию `claude-haiku-4-5`)

Дёшево и быстро (DESIGN 4.5), запускается на **каждое** входящее сообщение клиента, пока `bot_active=true` — но не блокирует тик: `asyncio.ensure_future` в `on_incoming`, реакция прилетает отдельно.

```python
CLASSIFY_TOOL = {
    "name": "classify",
    "description": "Классифицировать сообщение клиента сервиса ремонта техники.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "sentiment":   {"type": "string", "enum": ["positive", "neutral", "negative"],
                            "description": "negative = злость, жалоба, угроза отзывом/спором, мат, обвинения"},
            "wants_human": {"type": "boolean",
                            "description": "true, если клиент просит живого человека/мастера/менеджера (в т.ч. перефразированно)"},
            "reason":      {"type": "string", "description": "краткое объяснение по-русски, до 15 слов"},
        },
        "required": ["sentiment", "wants_human", "reason"],
        "additionalProperties": False,
    },
}

CLASSIFY_SYSTEM = (
    "Ты — классификатор сообщений клиентов сервиса ремонта техники Lead Partner.\n"
    "Тебе дают последние сообщения клиента из чата Авито. Определи:\n"
    "1) sentiment: negative — если клиент зол, ругается, жалуется на сервис, грозит "
    "отзывом, спором на Авито, возвратом денег, юристом; neutral — обычный вопрос; "
    "positive — благодарность, согласие.\n"
    "2) wants_human: просит ли клиент живого человека (примеры: «позовите оператора», "
    "«есть тут кто живой?», «хватит мне писать ботом», «дайте мастера», «соедините с менеджером»).\n"
    "Иронию и вежливое недовольство («ну отлично, конечно…») тоже считай негативом.\n"
    "Ответь только вызовом инструмента classify."
)

async def classify_message(texts: list[str]) -> dict | None:
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=settings.ai_model_classify,
                max_tokens=200,
                system=CLASSIFY_SYSTEM,
                messages=[{"role": "user", "content": "\n---\n".join(texts[-3:])}],
                tools=[CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "classify",
                             "disable_parallel_tool_use": True},
            ),
            timeout=AI_TIMEOUT_SECONDS,
        )
    except Exception as e:
        log.warning("classify_failed", error=type(e).__name__)
        return None      # классификатор — best effort: его отказ ничего не ломает
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    return block.input if block else None
```

Реакция (`ScenarioEngine.classify_and_react`, своя короткая транзакция — тик к этому моменту уже закоммичен):

- `sentiment == "negative"` ⇒ `do_handoff(reason="negative")` + тег `негатив` + Pub/Sub событие `conversation:updated` — фронтенд поднимает диалоги с тегом `негатив` в топ списка (DESIGN 4.3: «диалог поднимается в топ»);
- `wants_human == true` ⇒ `do_handoff(reason="client_request")` — второй эшелон условия №1 для перефразировок, которые не поймала ключевая эвристика (4.1);
- отказ классификатора (`None`) — игнорируем: это вспомогательный контур.

Если к моменту реакции бот уже не активен (handoff случился по другой причине) — тегируем негатив, handoff не повторяем (`do_handoff` идемпотентен: проверяет `bot_vars.handoff`).

### 3.5. Извлечение телефона и модели техники

Двухэшелонная схема — дёшево там, где можно, AI там, где нужно:

**Эшелон 1 — регулярка на каждом входящем** (та же, что в карточке клиента, DESIGN 3.3/8.3; нулевая цена):

```python
PHONE_RE = re.compile(r"(?:\+7|8|7)[\s\-\(\)]*(\d[\s\-\(\)]*){10}")

def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return "+" + digits

def maybe_extract_phone_to_vars(conv, state, text) -> None:
    m = PHONE_RE.search(text)
    if not m: return
    phone = normalize_phone(m.group(0))
    state.vars.setdefault("phone", phone)
    # и в карточку клиента (clients.phone), если там пусто — как в DESIGN 8.3
```

**Эшелон 2 — `claude-haiku-4-5` один раз перед handoff** (когда собран весь текст клиента; результат идёт в заметку менеджеру и в `bot_vars.vars`):

```python
EXTRACT_TOOL = {
    "name": "extract",
    "description": "Извлечь структурированные данные из переписки с клиентом сервиса ремонта.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "phone":        {"type": ["string", "null"], "description": "телефон в формате +7XXXXXXXXXX или null"},
            "device_brand": {"type": ["string", "null"], "description": "производитель: Apple, Samsung, HP, LG, Bosch... или null"},
            "device_model": {"type": ["string", "null"], "description": "модель как можно точнее: iPhone 13, MacBook Air M1, WD-80... или null"},
            "problem":      {"type": ["string", "null"], "description": "суть неисправности одним предложением по-русски или null"},
        },
        "required": ["phone", "device_brand", "device_model", "problem"],
        "additionalProperties": False,
    },
}

EXTRACT_SYSTEM = (
    "Извлеки данные из сообщений клиента сервиса ремонта техники. "
    "Не выдумывай: если чего-то нет в тексте — верни null. "
    "Модель техники нормализуй (айфон 13 про -> iPhone 13 Pro). "
    "Ответь только вызовом инструмента extract."
)
```

Вызов — из `perform_handoff` (best effort, под тем же 10с-таймаутом): вход — конкатенация всех входящих сообщений клиента этого диалога (до 4000 символов). Результат: непустые поля дописываются в `bot_vars.vars` (не перетирая собранное `ask`'ами), `phone` — в `clients.phone` (если пусто), и всё вместе — в заметку-сводку менеджеру.

---

## 4. Handoff: шесть условий и их детекция

`do_handoff(reason)` — единая идемпотентная процедура (повторный вызов — no-op по `bot_vars.handoff`):

```python
    async def do_handoff(self, reason: str, comment: str | None = None, tags: list[str] = ()) -> None:
        s = self.state
        if s.handoff_done(): return
        self.conv.bot_active = False
        self.conv.status = "new"                     # DESIGN 4.3: диалог попадает в общий список,
        # assignee_id остаётся NULL                  # MVP: общая очередь «кто взял — тот и ведёт»
        s.set_handoff(reason=reason, step=s.step)
        s.waiting = None
        entities = await extract_entities_best_effort(self.db, self.conv)     # 3.5, эшелон 2
        await add_conversation_tags(self.db, self.conv, [*tags, *reason_tags(reason)])
        await insert_note(self.db, self.conv, body=handoff_summary(reason, comment, s.vars, entities))
        # системное сообщение в ленту: «🤖 Бот передал диалог оператору (причина: ...)»
        await insert_system_message(self.db, self.conv, handoff_system_text(reason))
        await write_audit(self.db, user_id=None, action="bot.handoff",
                          entity="conversation", entity_id=str(self.conv.id),
                          details={"reason": reason, "step": s.step, "bot_id": str(self.bot.id)})
        # события conversation:handoff в каталоге WS нет (01 §11.3) — публикуем
        # conversation:updated с патчем; звук/⚑ у менеджеров даёт message:new
        # системного сообщения, вставленного выше
        await publish_event(self.redis, "conversation:updated", self.conv,
                            patch={"status": "new", "bot_active": False})
        s.next_step = None                           # остановить run()
```

Менеджер после handoff видит всю переписку бота и собранные переменные (заметка-сводка в карточке) — как требует DESIGN 4.3. `reason_tags`: `negative → ["негатив"]`, остальные — без автотегов.

Как технически детектируется каждое из шести условий DESIGN 4.3:

| № | Условие | Где и как детектируется |
|---|---|---|
| **1** | Клиент явно просит человека | Два эшелона. **(а)** Синхронно в `on_incoming`, до любого шага сценария — словарная эвристика по нормализованному тексту (lower, ё→е): `HUMAN_WORDS = ["оператор", "менеджер", "человек", "живой", "мастера позов", "позовите", "соедините", "хватит бот", "не бот"]`; совпадение подстроки длиной ≥2 слов контекста не требует — `detect_human_request()` возвращает `True` ⇒ мгновенный `handoff(reason="client_request")`, сценарий не продолжается. **(б)** Асинхронно — `wants_human=true` от классификатора на Haiku (3.4) ловит перефразировки («есть тут кто живой?»). Эвристика даёт мгновенность, классификатор — полноту. |
| **2** | `ai_answer` вернул низкую уверенность / вопрос вне базы знаний | В `exec_ai_answer` (3.2): `needs_operator == true` **или** `confidence < params.confidence_threshold` (дефолт 0.6) ⇒ переход `on_low_confidence`, а при его отсутствии — `handoff(reason="ai_low_confidence")`. Порог хранится в шаге сценария — настраивается админом per-бот без релиза. |
| **3** | Негатив/жалоба | Классификатор `claude-haiku-4-5` (3.4) на каждом входящем при активном боте: `sentiment == "negative"` ⇒ `handoff(reason="negative")` + тег `негатив` + Pub/Sub-событие; фронтенд сортирует диалоги с этим тегом в топ списка. Вызов асинхронный — задержка реакции ≤ пары секунд, тик бота не тормозится. |
| **4** | Клиент прислал 2+ сообщения после ответа бота, не попав в сценарий | Счётчик `bot_vars.counters.offscript_msgs`. Инкремент в `on_incoming` в двух случаях: (а) бот ничего не ждёт (`waiting == null`), а клиент пишет; (б) ответ не сматчился с вариантами `menu`. Сбрасывается при любом «попадании» (валидный ответ `ask`, выбор варианта `menu`). При достижении `max_offscript_messages` (дефолт 2) ⇒ `handoff(reason="offscript")`. |
| **5** | Сценарий дошёл до шага `handoff` | `exec_handoff` вызывает `do_handoff(reason=params.reason or "scenario", comment=params.comment, tags=params.tags)`. Плюс все **дефолтные** ветки: `on_timeout`/`on_invalid`/`on_no_match`/`on_low_confidence` = `null`, лимиты зацикливания (2.7), недоступность AI (3.3) — сводятся к тому же `do_handoff` со своими reason'ами (`ask_timeout`, `ask_invalid`, `offscript`, `loop_protection`, `ai_unavailable`). |
| **6** | Оператор сам вмешался (написал в диалог) | Вне движка — в endpoint'е отправки сообщений (2.6): `sender_type='operator'` ⇒ `mute_bot()`: `bot_active=false`, `bot_vars.muted=true` навсегда, ожидающие таймауты аннулируются токеном. Это не `do_handoff` (диалог уже в руках человека — статус не трогаем, заметок не пишем), только `audit_log: bot.muted`. Гонки закрыты `FOR UPDATE`-перепроверкой в начале каждого тика. |

Полный реестр `reason` (фиксируется в `bot_vars.handoff.reason`, `audit_log.details` и виден в статистике «% закрыто ботом / причины передач»):
`client_request`, `ai_low_confidence`, `negative`, `offscript`, `scenario`, `ask_timeout`, `ask_invalid`, `loop_protection`, `ai_unavailable`, `scenario_changed`.

---

## 5. Редактор сценариев в `/settings/bots` (MVP)

Доступ — только `admin` (матрица прав DESIGN 5.1; на бэке `require_permission("bots:manage")`, все мутации — в `audit_log`). Стек UI — React 18 + Mantine + TanStack Query (DESIGN 2). MVP — «список шагов с формами», визуальный конструктор — фаза 2.0 (DESIGN 7): формат сценария уже графовый, конструктору ничего мигрировать не придётся.

### 5.1. Экраны

**Список ботов** (`/settings/bots`) — таблица: имя, вкл/выкл (Switch → `POST /api/v1/bots/{id}/enable` | `/disable` — PATCH у ботов не даём, 01 §8.3–8.4), привязанные аккаунты (бейджи `avito_accounts.title`), расписание одной строкой («24/7» / «пн–вс 20:00–10:00 МСК»), диалогов за 7 дней. Кнопки «Создать бота» (создаёт копию дефолтного «Первичного приёма») и «Дублировать».

**Редактор бота** (`/settings/bots/:id`) — одна страница, три зоны:

1. **Шапка**: имя; переключатель `is_enabled`; мультиселект аккаунтов (пишет `avito_accounts.bot_id`; аккаунт может быть привязан только к одному боту — при перепривязке предупреждение); редактор расписания (radio «Круглосуточно» / «По расписанию» + строки-интервалы: чекбоксы дней, два TimeInput; подпись «Время московское»; интервал через полночь валиден и подсвечивается подсказкой «через полночь»); textarea «База знаний» (`bots.knowledge_base`, monospace, счётчик символов, лимит 20 000) с подсказкой «Используется шагом ai_answer. Пишите фактами: услуга — цена от — срок».

2. **Список шагов** — вертикальный список карточек (`Card` + `Accordion`), порядок = порядок массива `steps`, перестановка стрелками ↑↓ (dnd не обязателен в MVP). Карточка: иконка типа (💬 send, ❓ ask, 📋 menu, 🔀 condition, ✨ ai_answer, 👤 handoff, ✅ close, 🏷 tag, 🗒 note), `id` (моноширинно, редактируется; при переименовании все ссылки на шаг обновляются автоматически на клиенте), сводка («send: „Здравствуйте…“ → ask_problem»). Разворот — форма шага:
   - общие контролы: тип (Select; смена типа сбрасывает `params` с подтверждением), переходы — **Select со списком `id` всех шагов** (+ пункт «— дефолт: передать оператору —» для nullable-ссылок `on_timeout`/`on_invalid`/`on_no_match`/`on_low_confidence`);
   - текстовые поля — textarea с тулбаром-подстановкой переменных (кнопка `{…}` → выпадашка: системные `{client_name}`, `{item_title}`… + все `var`, объявленные `ask`-шагами выше по списку);
   - `ask`: имя переменной, Select валидатора (`любой ответ` / `телефон` / `число` / `regex…`), retry-текст, попытки (NumberInput 1–5), таймаут (Select: 30м/1ч/2ч/12ч/24ч/3д/без лимита);
   - `menu`: редактируемая таблица вариантов (label, ключевые слова через запятую, переход), кнопка «+ вариант»;
   - `condition`: список строк «если [вид условия + его поля] → [шаг]», «+ условие», внизу обязательное «иначе → [шаг]»;
   - `ai_answer`: слайдер порога уверенности 0–1 (шаг 0.05), длина ответа, глубина контекста;
   - `handoff`: комментарий менеджеру, теги (TagsInput);
   - `tag`/`note`/`close`/`send` — тривиальные формы по параметрам из 1.3.
   Кнопка «+ Добавить шаг» — внизу и между карточками.

3. **Панель действий** (sticky снизу): «Сохранить» (disabled, пока есть ошибки валидации), «Протестировать» (5.3), «Отменить изменения». Ошибки валидации — список под панелью, клик по ошибке скроллит к шагу и раскрывает его; карточки с ошибками — красная рамка.

Черновик редактора живёт в Zustand-сторе (стейт формы) и не отправляется на сервер до «Сохранить»; «Протестировать» работает с черновиком (5.3).

### 5.2. Валидация при сохранении

Двухслойная, одинаковые правила на клиенте (мгновенный фидбек) и на сервере (истина; `PUT /api/v1/bots/{id}` → `422` со списком ошибок `{step_id, field, code, message}`).

**Слой 1 — JSON Schema** (1.4): типы, обязательные поля, лимиты длин, форматы времени/regex.

**Слой 2 — семантика графа** (`app/bots/validator.py`, чистая функция `validate_scenario(scenario) -> list[Issue]`):

| Код | Уровень | Проверка |
|---|---|---|
| `duplicate_id` | error | `id` шагов уникальны |
| `entry_missing` | error | `entry` существует в `steps` |
| `broken_ref` | error | каждая ссылка (`next`, `on_*`, `options[].next`, `conditions[].next`, `else`) указывает на существующий шаг |
| `unreachable_step` | warning | шаг недостижим из `entry` (BFS по всем рёбрам); сохранить можно — бывает полезно при черновиках |
| `no_terminal_path` | error | из какого-то достижимого шага не существует пути в `handoff`/`close` (обход конденсации графа); «повисший» сценарий запрещён |
| `static_loop` | error | цикл, не содержащий ни одного `ask`/`menu`-ребра (только send/condition/tag/note/ai_answer) — гарантированное зацикливание без участия клиента |
| `var_undefined` | warning | `{переменная}` в тексте не объявлена ни одним `ask`/`menu` на каком-либо пути от `entry` до этого шага и не системная |
| `var_shadowed` | warning | два `ask` пишут в одну переменную |
| `bad_regex` | error | `validate.regex`/`text_matches.regex` не компилируется (`re.compile`) или длиннее 200 символов |
| `menu_dup_match` | warning | одно ключевое слово в нескольких вариантах одного меню |
| `ai_without_kb` | warning | есть шаг `ai_answer`, а `knowledge_base` пуста |
| `ask_no_timeout` | warning | `timeout: null` — бот может ждать вечно |
| `first_step_asks` | warning | `entry` — это `ask` без `text`: бот стартует молчаливым ожиданием |

`error` блокирует сохранение; `warning` показывается, но сохранить можно (кнопка «Сохранить с предупреждениями»). При сохранении: `revision += 1`, `audit_log(action="bot.updated", details={"revision": ..., "diff_steps": [...]})` — действие называется именно `bot.updated`, как в 01 §8.3 и в контракте 06 §0.3; словарь подписей журнала во фронте знает только это имя. Активные диалоги на старой ревизии дорабатывают по правилу из 2.1: если их текущий `step` исчез из нового сценария — аккуратный `handoff(reason="scenario_changed")` на следующем тике.

### 5.3. Кнопка «Протестировать» — песочница

Правая выезжающая панель (`Drawer`) с эмуляцией диалога. Работает с **черновиком** сценария из редактора (сохранять не нужно), реальному клиенту ничего не уходит, в `conversations`/`messages` ничего не пишется.

**Бэкенд** — тот же `ScenarioEngine` с подменённым контекстом (`SandboxContext`): исходящие складываются в список вместо `deliver_message`, состояние держится в Redis (`sandbox:{admin_id}:{uuid}`, TTL 1 час), время и расписание подменяемы. Эндпоинты (все — `require_permission("bots:manage")`):

```
POST /api/v1/bots/sandbox/start
  body: { scenario: {...черновик...}, knowledge_base: "...", schedule: {...},
          client_name: "Иван", item_title: "Ремонт iPhone",
          now_override: "2026-08-04T22:30:00+03:00" | null,   # эмуляция «сейчас ночь»
          ai_mode: "real" | "stub" }                          # real = живые вызовы Claude
  -> 422 c ошибками валидации ИЛИ
  -> { session_id, events: [...] }        # события первого тика (обычно пусто: бот ждёт первого сообщения)

POST /api/v1/bots/sandbox/{session_id}/message
  body: { text: "Здравствуйте, разбил экран айфона" }
  -> { events: [
        { "kind": "bot_message", "text": "Здравствуйте, Иван! ..." },
        { "kind": "step", "id": "ask_problem", "type": "ask" },          # трассировка
        { "kind": "waiting", "var": "problem", "deadline": "…" },
        { "kind": "ai_call", "confidence": 0.82, "needs_operator": false },
        { "kind": "handoff", "reason": "scenario", "comment": "…" },
        { "kind": "note", "text": "…" }, { "kind": "tags", "tags": ["…"] },
        { "kind": "close" }
      ],
      state: { step, waiting, vars, counters, bot_active, status, tags, handoff },
      trace: ["greet", "ask_problem", ...] }        # плоский список id выполненных шагов

POST /api/v1/bots/sandbox/{session_id}/fire-timeout    # кнопка «⏩ Промотать таймаут»
  -> те же events (эмулирует bot_ask_timeout без ожидания 24ч)

DELETE /api/v1/bots/sandbox/{session_id}
```

**Реестр `kind` (полный).** Пример выше показывает не все события — движок пишет в трассировку больше, и фронт с движком обязаны сходиться по этому списку. Встречаются: `step`, `waiting`, `bot_message`, `note`, `ai_call`, `tags`, `handoff`, `close`, `answer`, `offscript`, `retry`, `timeout`, `timeout_ignored`, `condition`, `detector`, а также `skipped` (`reason`: `not_scheduled` | `not_waiting`) от самой песочницы. Незнакомый `kind` панель рисует общей серой строкой — добавление нового события не ломает фронт, но дописывать его сюда обязательно.

`ai_mode`: `real` — настоящие вызовы `claude-sonnet-5`/`claude-haiku-4-5` (админ проверяет базу знаний и пороги на живой модели; в UI дисклеймер «расходует токены»); `stub` — детерминированные заглушки (`confidence=0.9`, `reply="[AI-ответ по базе знаний]"`, негатив по слову «ужасно») для отладки логики графа офлайн.

**Фронтенд-панель**: сверху тумблеры контекста — «Время: сейчас / задать…» (влияет на `work_hours` и расписание), «AI: настоящий / заглушка», поля имени клиента и объявления. Середина — чат: сообщения «клиента» (ввод внизу), ответы бота, серые технические строки трассировки («→ шаг check_hours: рабочее время? нет», «✨ ai_answer: confidence 0.42 < 0.6 → handoff»), жёлтые — заметки, красная плашка — handoff с причиной. Справа мини-панель состояния: текущий шаг, `vars` (живая таблица), счётчики, кнопка «⏩ Промотать таймаут» (активна, когда бот в WAITING). Кнопка «Сбросить» начинает сессию заново. Типовой чек админа перед включением бота: дневной сценарий → «сейчас ночь» → невалидный телефон дважды → «позовите оператора» → «ужасный сервис!» — все шесть условий handoff проверяются руками за минуту.

---

## 6. Файлы и задачи (сводно)

```
app/bots/
  scenario.schema.json   # JSON Schema (1.4)
  models.py              # Pydantic-модели Scenario/Step/BotState (парсинг bot_vars)
  validator.py           # семантическая валидация графа (5.2)
  engine.py              # ScenarioEngine: on_incoming / on_timeout / run / exec_* (2.3)
  runtime.py             # ARQ-задачи bot_step, bot_ask_timeout; should_run_bot; mute_bot (2.2–2.6)
  schedule.py            # is_bot_scheduled_now, work_hours (2.5)
  handoff.py             # do_handoff, reason-реестр, сводка менеджеру (4)
  ai.py                  # ai_answer, classify_message, extract, circuit breaker (3)
  sandbox.py             # SandboxContext + Redis-состояние (5.3)
app/api/routes/bots.py   # CRUD /api/v1/bots + /api/v1/bots/sandbox/* (5)
```

ARQ-задачи: `bot_step(conv_id, text|None)`, `bot_ask_timeout(conv_id, token)` — обе идемпотентны и безопасны к повторной доставке (лок + токен + `FOR UPDATE`). Env: `ANTHROPIC_API_KEY`, `AI_MODEL_ANSWER`, `AI_MODEL_CLASSIFY`, `AI_TIMEOUT_SECONDS` (05 §4; читаются через `settings` — 08 §1.3). Порядок реализации внутри этапа 5 (соответствует DESIGN 7): движок + send/ask/menu/condition/handoff/close/tag/note → расписания и mute → редактор с валидацией → песочница → `ai_answer` + классификатор + извлечение.
