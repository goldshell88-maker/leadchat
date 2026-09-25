# 06 — Статистика и отчёты

Документ детализирует раздел 3.4 DESIGN.md (`/stats`) и часть этапа 3 плана разработки
(«Роли и статистика»). Схема БД — строго из DESIGN.md 4.4 (`users`, `avito_accounts`,
`clients`, `conversations`, `messages`, `bots`, `templates`, `audit_log`), партиции
`messages` по месяцам. Ничего в ядре схемы не меняем; всё, что нужно статистике сверх
ядра, — это (а) соглашения о событиях в `audit_log`, (б) дополнительные индексы,
(в) одна materialized view и (г) одна SQL-функция. Всё это оформляется одной
Alembic-миграцией этапа 3 (`versions/xxxx_stats_indexes_and_mv.py`, DDL — через
`op.execute`).

Связанные документы: DESIGN.md 3.4 (макет экрана), 5.1 (матрица прав), 8.3 (воркер
входящих — источник событий reopen).

---

## 0. Соглашения, без которых метрики не считаются

### 0.1. Таймзона

- В БД всё хранится в `timestamptz` (UTC) — как и во всём проекте.
- **Все бизнес-определения — в `Europe/Moscow`**: «день», «час», «сегодня», рабочие
  часы 10:00–20:00 (взято из сценария бота, DESIGN.md 4.2).
- Параметры периода API принимают **даты по Москве** (`date_from`, `date_to`,
  обе включительно). Бэкенд конвертирует их в полуинтервал UTC
  `[ts_from, ts_to)`: `ts_from = date_from 00:00 MSK → UTC`,
  `ts_to = (date_to + 1 день) 00:00 MSK → UTC`. Во всех SQL ниже фигурируют уже
  готовые `:ts_from` / `:ts_to` — это гарантирует partition pruning по
  `messages.created_at`.

### 0.2. Правило партиций

Любой запрос к `messages` обязан содержать либо ограничение по `created_at`
(диапазон → pruning партиций), либо `conversation_id = ...` (точечный проход по
индексу `(conversation_id, created_at)` во всех партициях). Запросы «по всей таблице
без диапазона» в статистике запрещены — ревьюим на PR.

### 0.3. Контракт событий `audit_log`

DESIGN.md уже требует писать все значимые действия в `audit_log` (раздел 5.2).
Статистика опирается на конкретные action'ы — фиксируем их как контракт, обязательный
для кода чатов и движка ботов (без этих событий метрики «закрыто», «принято»,
«телефоны», «повторные» не существуют):

| `action` | `entity` / `entity_id` | `user_id` | `details` (JSONB) | Кто пишет |
|---|---|---|---|---|
| `conversation.status_changed` | `conversation` / `conv.id::text` | оператор; `NULL`, если бот/система | `{"from":"in_progress","to":"closed","by":"operator\|bot\|system","assignee_id":"<uuid\|null>"}` | endpoint смены статуса; шаг бота `close`; воркер reopen |
| `conversation.assigned` | `conversation` / `conv.id::text` | кто назначил (`NULL` при автораспределении) | `{"assignee_id":"<uuid>","prev_assignee_id":"<uuid\|null>","by":"self\|transfer\|head\|auto"}` | «взял в работу», «Передать коллеге», переназначение руководителем |
| `conversation.reopened` | `conversation` / `conv.id::text` | `NULL` | `{"client_id":"<uuid>"}` | inbound-воркер при `closed → new` (DESIGN.md 8.3) |
| `client.phone_captured` | `client` / `client.id::text` | менеджер или `NULL` | `{"conversation_id":"<uuid>","source":"regex\|bot\|manual"}` | `maybe_extract_phone`, шаг бота `ask phone`, ручное сохранение в карточке |
| `bot.handoff` | `conversation` / `conv.id::text` | `NULL` | `{"reason":"client_request\|ai_low_confidence\|negative\|offscript\|scenario\|ask_timeout\|ask_invalid\|loop_protection\|ai_unavailable\|scenario_changed"}` — реестр 02-BOT-ENGINE §4 | движок бота (резерв под метрики фазы 2) |

Правила:

- `details.by` в `status_changed` — **кто фактически сменил статус** (бот, оператор,
  система). `details.assignee_id` — ответственный на момент события (снимок; нужен,
  потому что `conversations.assignee_id` мутабелен, а отчёт за прошлый месяц не должен
  «переезжать» при переназначениях).
- Reopen пишет **два** события: `conversation.status_changed`
  (`{"from":"closed","to":"new","by":"system"}`) и `conversation.reopened`.
  История статусов остаётся полной, а счётчик повторных — простым.
- `client.phone_captured` пишется **один раз при первом заполнении** `clients.phone`
  (было `NULL`/пусто → стало значение). Перезапись уже существующего телефона событие
  не создаёт.
- Словарь `reason` в `bot.handoff` — строго реестр 02-BOT-ENGINE §4. Вмешательство
  оператора — **не** handoff: движок пишет `bot.muted` без записи `bot.handoff` (02 §2.6),
  в этот словарь оно не входит.
- `entity_id` в схеме — `text`; во всех JOIN'ах ниже он кастуется `::uuid`.

### 0.4. Биндинг параметров

SQL ниже написан под `sqlalchemy.text(...)` + asyncpg. Опциональные фильтры —
паттерн `(CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)`;
`None` передаётся явно типизированным `bindparam("account_id", type_=Uuid)`
(иначе asyncpg не выведет тип NULL). Мультивыбор менеджеров на `/stats`
(фильтр «менеджер(ы)» из DESIGN.md 3.4) реализуется тем же паттерном с массивом:
`(:manager_ids::uuid[] IS NULL OR c.assignee_id = ANY(:manager_ids))` — в примерах
ниже для краткости единичный `:manager_id`.

---

## 1. Словарь метрик

Единый глоссарий. Формулировки ниже — нормативные: код, тесты и подсказки в UI
(тултипы у карточек) обязаны совпадать с ними дословно.

### 1.1. Время первого ответа (FRT)

**База отсчёта** — `first_client_at`: время первого сообщения клиента в диалоге
(`messages.direction='in' AND sender_type='client'`, минимум `created_at` по диалогу).

Считаем **два независимых** FRT, оба от `first_client_at`:

- **FRT оператора** — до первого сообщения с `direction='out' AND
  sender_type='operator' AND delivery_status <> 'failed'`, отправленного не раньше
  `first_client_at`.
- **FRT бота** — то же, но `sender_type='bot'`.

Они не «или», а два отдельных показателя: диалог может иметь оба (бот ответил за
3 сек, оператор подключился через 40 мин), только один или ни одного.

**Два варианта времени** для FRT оператора:

- *астрономический* — простая разница `first_operator_at − first_client_at`;
- *по рабочим часам* — из интервала вырезано всё вне 10:00–20:00 МСК
  (функция `business_seconds_between`, раздел 2.1). Клиент написал в 23:00, оператор
  ответил в 10:05 — астрономический FRT 11 ч 5 мин, рабочий — 5 мин. Ответ до начала
  рабочего дня даёт рабочий FRT = 0 — это осознанная семантика («клиент не ждал в
  рабочее время нисколько»). SLA и мотивация менеджеров оцениваются по рабочему
  варианту; астрономический показываем рядом как «глазами клиента».
- FRT бота — только астрономический (бот работает 24/7, рабочие часы к нему
  неприменимы).

**Агрегаты**: среднее и медиана (медиана — главная цифра карточки: среднее
разваливается на одном «ночном» хвосте). Плюс счётчики `answered` / `unanswered`.

**Краевые случаи (нормативно):**

1. Диалог, где ответа не было вообще, в среднее/медиану **не входит**; он попадает в
   счётчик `unanswered` (отдельно от карточки FRT показываем «без ответа: N»).
2. Ответ, отправленный **мимо LeadChat** (менеджер ответил из приложения Авито), в БД
   не попадает — воркер отбрасывает эхо собственных исходящих (DESIGN.md 8.3). Такой
   диалог выглядит как «без ответа». Фиксируем как осознанное ограничение и элемент
   дисциплины «отвечаем только через LeadChat»; в онбординг-инструкции для
   сотрудников (этап 6) — прямым текстом.
3. Первое сообщение диалога — исходящее (возможно после догрузки истории): FRT не
   определён, диалог исключается из всех FRT-агрегатов (`first_client_at IS NULL`).
4. `direction IN ('note','system')` ответом **не считается** никогда.
5. Сообщение с `delivery_status='failed'` ответом не считается (клиент его не
   получил); `pending` считается (оптимистично — доставка в фоне, DESIGN.md 8.2).
6. **Переоткрытый диалог** (клиент вернулся после `closed`, воркер перевёл в `new`):
   FRT считается **только по первому эпизоду** диалога. Возврат клиента учитывается
   метрикой «повторные обращения» (1.6), второй FRT не порождает. Границы эпизодов в
   схеме не хранятся — это осознанный размен точности на простоту; если понадобится
   FRT по эпизодам, он строится по парам (`conversation.reopened`, следующий ответ)
   из `audit_log`, не трогая схему.
7. Если оператор ответил, когда `assignee_id` ещё не был установлен, FRT диалога
   атрибутируется **автору первого ответа** (`sender_user_id` первого операторского
   сообщения), а не текущему `assignee_id`. Это правило действует и в таблице по
   менеджерам — метрика не «переезжает» при передачах диалога.

### 1.2. Диалогов новых / в работе / закрыто

- **Новых за период** — диалогов, у которых `first_client_at ∈ [ts_from, ts_to)`.
  Не по статусу `new` (статус мутабелен), а по факту появления первого входящего.
- **Закрыто за период** — уникальных диалогов, по которым в периоде есть событие
  `conversation.status_changed` с `details->>'to'='closed'`. Диалог, закрытый дважды
  за период (закрыли → клиент вернулся → закрыли), считается **один раз**;
  атрибуты (кем закрыт) берутся из последнего закрытия в периоде.
- **В работе** — snapshot-метрика **на текущий момент** (`status='in_progress'`),
  к периоду не привязана; на карточке подпись «сейчас». Рядом тот же snapshot по
  `new` («ждут ответа сейчас»).

Причина такого разделения: у `conversations` нет `created_at`/`closed_at` — и это
нормально: «создан» надёжнее выводить из первого сообщения (история догружается
задним числом при подключении аккаунта, DESIGN.md 1.2), а «закрыт» — из `audit_log`,
который хранит и повторные закрытия.

### 1.3. % закрытых ботом без оператора

Доля от «закрыто за период» (1.2), где по последнему закрытию диалога в периоде:

- `details->>'by' = 'bot'` (закрыл шаг сценария `close`), **и**
- в диалоге нет **ни одного** сообщения `direction='out' AND sender_type='operator'`
  за всю его историю.

Краевые случаи:

- Внутренняя заметка оператора (`direction='note'`) участием **не считается** —
  клиент её не видел; бот закрыл диалог сам.
- Переназначение/смена статуса оператором без сообщений — тоже не участие, но если
  диалог в итоге закрыл оператор (`by='operator'`), в числитель он не попадает по
  первому условию.
- Диалог закрыт ботом, переоткрыт клиентом и закрыт оператором в том же периоде →
  последнее закрытие оператора, в числитель не входит.

### 1.4. Собрано телефонов

Число событий `client.phone_captured` за период (см. 0.3 — событие только при первом
заполнении телефона у клиента). Разрез по `details->>'source'`: `bot` (шаг `ask
phone`), `regex` (автоизвлечение из текста, DESIGN.md 3.3), `manual` (менеджер внёс в
карточку). Один клиент даёт максимум одно событие за всю жизнь — метрика не
накручивается повторами. Фильтр по аккаунту — через `details->>'conversation_id'` →
`conversations.account_id`.

### 1.5. Сообщений отправлено (по менеджеру)

`count(*)` по `messages` за период: `direction='out' AND sender_type='operator'
AND delivery_status <> 'failed'`, группировка по `sender_user_id`.

- Заметки (`direction='note'`) не входят (не сообщение клиенту); при желании
  показываются отдельной колонкой «заметок».
- Сообщения бота не входят (у них `sender_type='bot'` и `sender_user_id IS NULL`).
- `failed` не входят; повторная успешная отправка после «повторить» считается
  один раз (это новая строка `messages`, старая остаётся `failed`).

### 1.6. Повторные обращения

Две связанные метрики:

- **Переоткрытий за период** — число событий `conversation.reopened`: клиент написал
  в диалог, который был `closed` (воркер перевёл его в `new`, DESIGN.md 8.3). Один
  диалог может дать несколько переоткрытий за период — считаем события: каждое —
  реальный возврат клиента.
- **Повторных клиентов за период** — клиентов, начавших в периоде диалог
  (по `first_client_at`), у которых уже существовал более ранний диалог (другое
  объявление / другой аккаунт — `clients` един на канал, `UNIQUE (channel,
  external_id)`).

Краевой случай: один и тот же человек с двух аккаунтов Авито — это два разных
`clients` (разные `external_id`), повторность между ними не видна. Ограничение
канала, не наше; склейку по телефону при желании делаем в фазе 2 — телефон уже
сохраняется в `clients.phone`.

---

## 2. SQL под каждую метрику

Все запросы — рабочие, под схему DESIGN.md 4.4 и индексы из раздела 3.1 этого
документа. Параметры: `:ts_from`, `:ts_to` (`timestamptz`, см. 0.1), `:account_id`
(`uuid | NULL`), `:manager_id` (`uuid | NULL`).

### 2.1. Функция рабочих часов (миграция этапа 3)

```sql
-- Сколько секунд интервала [t0, t1] пришлось на рабочие часы 10:00–20:00 МСК.
-- 7 дней в неделю (у Lead Partner нет выходных в сценарии бота, DESIGN.md 4.2);
-- если появятся выходные/праздники — добавить таблицу-календарь и JOIN к ней,
-- сигнатура не изменится.
CREATE OR REPLACE FUNCTION business_seconds_between(
    t0 timestamptz,
    t1 timestamptz,
    work_start interval DEFAULT interval '10 hours',   -- 10:00 МСК
    work_end   interval DEFAULT interval '20 hours'    -- 20:00 МСК
) RETURNS bigint
LANGUAGE sql STABLE STRICT AS $$
    SELECT COALESCE(sum(GREATEST(0, EXTRACT(epoch FROM
               LEAST   (t1 AT TIME ZONE 'Europe/Moscow', d + work_end)
             - GREATEST(t0 AT TIME ZONE 'Europe/Moscow', d + work_start)
           )))::bigint, 0)
    FROM generate_series(
           date_trunc('day', t0 AT TIME ZONE 'Europe/Moscow'),
           date_trunc('day', t1 AT TIME ZONE 'Europe/Moscow'),
           interval '1 day') AS d
$$;
```

`STRICT` — при `NULL` на входе (диалог без ответа) вернёт `NULL`, и агрегаты его
проигнорируют, а не посчитают нулём.

### 2.2. FRT: карточки «среднее/медианное время первого ответа»

Оператор и бот отдельно, астрономический + рабочий варианты, счётчики.

```sql
WITH conv_started AS (            -- диалоги, начавшиеся в периоде
    SELECT c.id, c.account_id, c.assignee_id, f.first_client_at
    FROM conversations c
    CROSS JOIN LATERAL (
        SELECT min(m.created_at) AS first_client_at
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.direction = 'in' AND m.sender_type = 'client'
    ) f
    WHERE c.last_message_at >= :ts_from          -- дешёвый отсев: если первое
                                                 -- сообщение в периоде, то последнее
                                                 -- заведомо не раньше ts_from
      AND f.first_client_at >= :ts_from
      AND f.first_client_at <  :ts_to
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id  = :account_id)
      AND (CAST(:manager_id AS uuid) IS NULL OR c.assignee_id = :manager_id)
),
replies AS (
    SELECT cs.id,
           cs.first_client_at,
           op.first_operator_at,
           bt.first_bot_at
    FROM conv_started cs
    LEFT JOIN LATERAL (
        SELECT min(m.created_at) AS first_operator_at
        FROM messages m
        WHERE m.conversation_id = cs.id
          AND m.created_at >= cs.first_client_at
          AND m.direction = 'out' AND m.sender_type = 'operator'
          AND m.delivery_status <> 'failed'
    ) op ON true
    LEFT JOIN LATERAL (
        SELECT min(m.created_at) AS first_bot_at
        FROM messages m
        WHERE m.conversation_id = cs.id
          AND m.created_at >= cs.first_client_at
          AND m.direction = 'out' AND m.sender_type = 'bot'
          AND m.delivery_status <> 'failed'
    ) bt ON true
)
SELECT
    count(*)                                         AS conversations_started,
    count(first_operator_at)                         AS answered_by_operator,
    count(first_bot_at)                              AS answered_by_bot,
    count(*) FILTER (WHERE first_operator_at IS NULL
                       AND first_bot_at IS NULL)     AS unanswered,

    round(avg(EXTRACT(epoch FROM first_operator_at - first_client_at)))::int
                                                     AS frt_operator_avg_sec,
    round((percentile_cont(0.5) WITHIN GROUP (ORDER BY
        EXTRACT(epoch FROM first_operator_at - first_client_at)))::numeric)::int
                                                     AS frt_operator_median_sec,
    round(avg(business_seconds_between(first_client_at, first_operator_at)))::int
                                                     AS frt_operator_avg_biz_sec,
    round((percentile_cont(0.5) WITHIN GROUP (ORDER BY
        business_seconds_between(first_client_at, first_operator_at)))::numeric)::int
                                                     AS frt_operator_median_biz_sec,

    round(avg(EXTRACT(epoch FROM first_bot_at - first_client_at)))::int
                                                     AS frt_bot_avg_sec,
    round((percentile_cont(0.5) WITHIN GROUP (ORDER BY
        EXTRACT(epoch FROM first_bot_at - first_client_at)))::numeric)::int
                                                     AS frt_bot_median_sec
FROM replies;
```

`avg` и `percentile_cont` игнорируют `NULL` — диалоги без ответа автоматически
выпадают из времени, оставаясь в счётчиках. Этот же SELECT (без фильтра периода) —
тело materialized view в 3.2: боевой дашборд читает MV, а запрос выше — «эталон»
для тестов и live-пересчёта узких срезов.

### 2.3. Диалогов новых / закрыто / в работе

```sql
-- Новых за период: тот же conv_started, что в 2.2
WITH conv_started AS ( /* ... из 2.2 ... */ )
SELECT count(*) AS conversations_new FROM conv_started;
```

```sql
-- Snapshot «сейчас» (в работе / ждут ответа): по индексу (account_id, status)
SELECT
    count(*) FILTER (WHERE c.status = 'new')          AS new_now,
    count(*) FILTER (WHERE c.status = 'in_progress')  AS in_progress_now
FROM conversations c
WHERE (CAST(:account_id AS uuid) IS NULL OR c.account_id  = :account_id)
  AND (CAST(:manager_id AS uuid) IS NULL OR c.assignee_id = :manager_id);
```

```sql
-- Закрыто за период: уникальные диалоги по событиям audit_log
SELECT count(DISTINCT a.entity_id) AS conversations_closed
FROM audit_log a
JOIN conversations c ON c.id = a.entity_id::uuid
WHERE a.action = 'conversation.status_changed'
  AND a.details->>'to' = 'closed'
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
  AND (CAST(:manager_id AS uuid) IS NULL
       OR (a.details->>'assignee_id')::uuid = :manager_id);
```

Фильтр по менеджеру — по снимку `details->>'assignee_id'` (кто вёл диалог на момент
закрытия), а не по мутабельному `conversations.assignee_id` — отчёты за прошлое не
меняются при переназначениях.

### 2.4. % закрытых ботом без оператора

```sql
WITH last_close AS (               -- последнее закрытие каждого диалога в периоде
    SELECT DISTINCT ON (a.entity_id)
           a.entity_id::uuid   AS conversation_id,
           a.details->>'by'    AS closed_by
    FROM audit_log a
    WHERE a.action = 'conversation.status_changed'
      AND a.details->>'to' = 'closed'
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
    ORDER BY a.entity_id, a.created_at DESC
),
flagged AS (
    SELECT lc.conversation_id,
           (lc.closed_by = 'bot'
            AND NOT EXISTS (          -- ни одного операторского сообщения за всю историю
                SELECT 1 FROM messages m
                WHERE m.conversation_id = lc.conversation_id
                  AND m.direction = 'out' AND m.sender_type = 'operator'
            )) AS bot_only
    FROM last_close lc
    JOIN conversations c ON c.id = lc.conversation_id
    WHERE (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
)
SELECT
    count(*)                                   AS closed_total,
    count(*) FILTER (WHERE bot_only)           AS closed_by_bot_no_operator,
    round(100.0 * count(*) FILTER (WHERE bot_only)
          / NULLIF(count(*), 0), 1)            AS bot_closed_pct
FROM flagged;
```

`NOT EXISTS` без диапазона дат легален — идёт по `(conversation_id, created_at)`
точечно (правило 0.2, вторая ветка).

### 2.5. Собрано телефонов

```sql
SELECT
    count(*)                                              AS phones_collected,
    count(*) FILTER (WHERE a.details->>'source' = 'bot')    AS by_bot,
    count(*) FILTER (WHERE a.details->>'source' = 'regex')  AS by_regex,
    count(*) FILTER (WHERE a.details->>'source' = 'manual') AS by_manual
FROM audit_log a
LEFT JOIN conversations c ON c.id = (a.details->>'conversation_id')::uuid
WHERE a.action = 'client.phone_captured'
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id);
```

### 2.6. Сообщений отправлено по менеджеру

```sql
SELECT u.id AS manager_id, u.full_name,
       count(*) AS messages_sent
FROM messages m
JOIN users u ON u.id = m.sender_user_id
WHERE m.created_at >= :ts_from AND m.created_at < :ts_to
  AND m.direction = 'out' AND m.sender_type = 'operator'
  AND m.delivery_status <> 'failed'
  AND (CAST(:account_id AS uuid) IS NULL OR EXISTS (
        SELECT 1 FROM conversations c
        WHERE c.id = m.conversation_id AND c.account_id = :account_id))
GROUP BY u.id, u.full_name
ORDER BY messages_sent DESC;
```

Идёт целиком по частичному индексу `idx_messages_operator_out` (3.1) в партициях
периода.

### 2.7. Повторные обращения

```sql
-- (а) переоткрытия
SELECT count(*) AS reopened
FROM audit_log a
JOIN conversations c ON c.id = a.entity_id::uuid
WHERE a.action = 'conversation.reopened'
  AND a.created_at >= :ts_from AND a.created_at < :ts_to
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id);
```

```sql
-- (б) повторные клиенты: начали диалог в периоде, уже имея более ранний диалог
WITH conv_started AS ( /* ... из 2.2 ... */ )
SELECT count(DISTINCT c.client_id) AS repeat_clients
FROM conv_started s
JOIN conversations c ON c.id = s.id
WHERE EXISTS (
    SELECT 1 FROM conversations c2
    WHERE c2.client_id = c.client_id
      AND c2.id <> c.id
      AND c2.last_message_at < s.first_client_at   -- прежний диалог был раньше
);
```

### 2.8. Таблица по менеджерам (боевой запрос endpoint'а `/stats/managers`)

FRT-часть берётся из `mv_conversation_stats` (3.2) с атрибуцией автору первого
ответа (правило 1.1.7); «принято»/«закрыто» — live из `audit_log`; «отправлено» —
live из `messages` (обе части append-only и дёшевы по индексам, свежесть — секунды).

```sql
WITH frt AS (
    SELECT s.first_operator_user_id AS user_id,
           count(*)                                 AS answered,
           round(avg(s.frt_operator_sec))::int      AS frt_avg_sec,
           round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY s.frt_operator_sec))::numeric)::int      AS frt_median_sec,
           round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY s.frt_operator_biz_sec))::numeric)::int  AS frt_median_biz_sec
    FROM mv_conversation_stats s
    WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_to
      AND s.first_operator_user_id IS NOT NULL
      AND (CAST(:account_id AS uuid) IS NULL OR s.account_id = :account_id)
    GROUP BY 1
),
taken AS (
    SELECT (a.details->>'assignee_id')::uuid AS user_id,
           count(*) AS taken
    FROM audit_log a
    JOIN conversations c ON c.id = a.entity_id::uuid
    WHERE a.action = 'conversation.assigned'
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
    GROUP BY 1
),
closed AS (
    SELECT a.user_id,
           count(DISTINCT a.entity_id) AS closed
    FROM audit_log a
    JOIN conversations c ON c.id = a.entity_id::uuid
    WHERE a.action = 'conversation.status_changed'
      AND a.details->>'to' = 'closed'
      AND a.user_id IS NOT NULL                      -- закрытия ботом не приписываем
      AND a.created_at >= :ts_from AND a.created_at < :ts_to
      AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
    GROUP BY 1
),
sent AS (
    SELECT m.sender_user_id AS user_id,
           count(*) AS messages_sent
    FROM messages m
    WHERE m.created_at >= :ts_from AND m.created_at < :ts_to
      AND m.direction = 'out' AND m.sender_type = 'operator'
      AND m.delivery_status <> 'failed'
      AND (CAST(:account_id AS uuid) IS NULL OR EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.id = m.conversation_id AND c.account_id = :account_id))
    GROUP BY 1
)
SELECT u.id AS manager_id, u.full_name, u.is_active,
       COALESCE(t.taken, 0)          AS taken,
       COALESCE(f.answered, 0)       AS answered,
       COALESCE(cl.closed, 0)        AS closed,
       f.frt_avg_sec, f.frt_median_sec, f.frt_median_biz_sec,
       COALESCE(s.messages_sent, 0)  AS messages_sent
FROM users u
LEFT JOIN frt    f  ON f.user_id  = u.id
LEFT JOIN taken  t  ON t.user_id  = u.id
LEFT JOIN closed cl ON cl.user_id = u.id
LEFT JOIN sent   s  ON s.user_id  = u.id
WHERE u.role IN ('admin', 'manager')       -- head/observer не пишут (DESIGN.md 5.1)
  AND (u.is_active
       OR COALESCE(t.taken, f.answered, cl.closed, s.messages_sent) IS NOT NULL)
ORDER BY messages_sent DESC;
```

Отключённые сотрудники показываются, только если у них есть активность в периоде —
отчёт за прошлый месяц не теряет уволенных.

---

## 3. Производительность

### 3.1. Дополнительные индексы (Alembic-миграция этапа 3)

Индексы объявляются на партиционированном родителе `messages` — PostgreSQL 16 сам
создаёт их в каждой существующей и каждой новой партиции (в т.ч. создаваемой
ежемесячным job'ом планировщика).

```sql
-- 1. Лента диалога и все точечные lookback'и статистики (LATERAL'ы в 2.2/2.4, MV).
--    Нужен и самому экрану чатов — самый важный индекс проекта.
CREATE INDEX idx_messages_conv_created
    ON messages (conversation_id, created_at);

-- 2. «Сообщений отправлено по менеджеру» (2.6, 2.8, виджет 6).
--    Частичный: исходящие операторов — ~10–20% строк.
CREATE INDEX idx_messages_operator_out
    ON messages (sender_user_id, created_at)
    WHERE direction = 'out' AND sender_type = 'operator';

-- 3. Входящие клиентов: heatmap (3.4) и таймсерия messages_in.
CREATE INDEX idx_messages_client_in
    ON messages (created_at)
    WHERE direction = 'in' AND sender_type = 'client';

-- conversations: snapshot-счётчики, фильтры, отсев по last_message_at (2.2)
CREATE INDEX idx_conversations_last_message ON conversations (last_message_at DESC);
CREATE INDEX idx_conversations_account_status ON conversations (account_id, status);
CREATE INDEX idx_conversations_assignee_status ON conversations (assignee_id, status);
CREATE INDEX idx_conversations_client ON conversations (client_id);

-- audit_log: все событийные метрики (2.3–2.5, 2.7, 2.8)
CREATE INDEX idx_audit_action_created ON audit_log (action, created_at);
CREATE INDEX idx_audit_entity ON audit_log (entity, entity_id, created_at);
```

Замечания:

- На партиционированном родителе `CREATE INDEX CONCURRENTLY` невозможен. На нашем
  масштабе (внутренний инструмент, ≤10 000 сообщений/сутки) обычный `CREATE INDEX`
  на свежей БД мгновенен; если индексы добавляются на живую систему с историей —
  строить по партициям (`CREATE INDEX CONCURRENTLY ... ON messages_y2026m07 ...` —
  схема имён `messages_y{YYYY}m{MM}`, 08 §6.3) и
  прикреплять `ALTER INDEX ... ATTACH PARTITION` — без остановки.
- GIN по `search` и уникальный частичный по `external_message_id` уже есть в ядре
  (DESIGN.md 4.4) — не дублируем.

### 3.2. Materialized view `mv_conversation_stats`

Одна MV с **пофактовой** (по диалогу) гранулярностью — а не с предагрегатами. Все
карточки, графики и таблица менеджеров сводятся из неё простыми `GROUP BY` за
миллисекунды, при этом MV не привязана к конкретным разрезам: новый фильтр в UI не
требует новой MV.

```sql
CREATE MATERIALIZED VIEW mv_conversation_stats AS
WITH base AS (
    SELECT c.id, c.account_id, c.client_id, c.assignee_id, c.status,
           f.first_client_at
    FROM conversations c
    CROSS JOIN LATERAL (
        SELECT min(m.created_at) AS first_client_at
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.direction = 'in' AND m.sender_type = 'client'
    ) f
    WHERE f.first_client_at IS NOT NULL          -- краевой случай 1.1.3
)
SELECT b.id                                       AS conversation_id,
       b.account_id, b.client_id, b.assignee_id, b.status,
       b.first_client_at,
       op.first_operator_at,
       op.first_operator_user_id,                 -- атрибуция: автор первого ответа
       bt.first_bot_at,
       EXTRACT(epoch FROM op.first_operator_at - b.first_client_at)::int
                                                  AS frt_operator_sec,
       business_seconds_between(b.first_client_at, op.first_operator_at)
                                                  AS frt_operator_biz_sec,
       EXTRACT(epoch FROM bt.first_bot_at - b.first_client_at)::int
                                                  AS frt_bot_sec,
       msg.msgs_in, msg.msgs_out_operator, msg.msgs_out_bot,
       (msg.msgs_out_operator > 0)                AS has_operator_reply,
       cl.closed_at, cl.closed_by
FROM base b
LEFT JOIN LATERAL (
    SELECT m.created_at    AS first_operator_at,
           m.sender_user_id AS first_operator_user_id
    FROM messages m
    WHERE m.conversation_id = b.id
      AND m.created_at >= b.first_client_at
      AND m.direction = 'out' AND m.sender_type = 'operator'
      AND m.delivery_status <> 'failed'
    ORDER BY m.created_at
    LIMIT 1
) op ON true
LEFT JOIN LATERAL (
    SELECT min(m.created_at) AS first_bot_at
    FROM messages m
    WHERE m.conversation_id = b.id
      AND m.created_at >= b.first_client_at
      AND m.direction = 'out' AND m.sender_type = 'bot'
      AND m.delivery_status <> 'failed'
) bt ON true
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE m.direction = 'in'
                              AND m.sender_type = 'client')   AS msgs_in,
           count(*) FILTER (WHERE m.direction = 'out'
                              AND m.sender_type = 'operator'
                              AND m.delivery_status <> 'failed') AS msgs_out_operator,
           count(*) FILTER (WHERE m.direction = 'out'
                              AND m.sender_type = 'bot'
                              AND m.delivery_status <> 'failed') AS msgs_out_bot
    FROM messages m
    WHERE m.conversation_id = b.id
) msg ON true
LEFT JOIN LATERAL (
    SELECT a.created_at     AS closed_at,
           a.details->>'by' AS closed_by
    FROM audit_log a
    WHERE a.entity = 'conversation'
      AND a.entity_id = b.id::text
      AND a.action = 'conversation.status_changed'
      AND a.details->>'to' = 'closed'
    ORDER BY a.created_at DESC
    LIMIT 1
) cl ON true;

-- уникальный индекс обязателен для REFRESH ... CONCURRENTLY
CREATE UNIQUE INDEX mv_conversation_stats_pk
    ON mv_conversation_stats (conversation_id);
CREATE INDEX ON mv_conversation_stats (first_client_at);
CREATE INDEX ON mv_conversation_stats (account_id, first_client_at);
CREATE INDEX ON mv_conversation_stats (first_operator_user_id, first_client_at);
CREATE INDEX ON mv_conversation_stats (closed_at);
```

Карточки FRT из MV — тот же агрегат, что в 2.2, только `FROM mv_conversation_stats
WHERE first_client_at >= :ts_from AND first_client_at < :ts_to` и фильтры по
колонкам MV.

**Refresh — ежечасно через APScheduler** (тот же scheduler-процесс, что и refresh
токенов Авито, DESIGN.md 1.5):

```python
# app/scheduler/jobs/stats.py
from datetime import datetime, timezone
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.core.db import engine
from app.core.redis import redis

def register(scheduler):
    scheduler.add_job(
        refresh_stats_mv,
        CronTrigger(minute=5),          # каждый час в HH:05
        id="stats_mv_refresh",
        coalesce=True, max_instances=1, misfire_grace_time=600,
    )

async def refresh_stats_mv():
    async with engine.connect() as conn:
        await conn.execute(text("SET statement_timeout = '120s'"))
        # CONCURRENTLY: дашборд не блокируется на время refresh
        await conn.execute(
            text("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_conversation_stats"))
        await conn.commit()
    await redis.set("stats:refreshed_at",
                    datetime.now(timezone.utc).isoformat())
```

`stats:refreshed_at` отдаётся в каждом ответе `/stats/*` — UI показывает
«Данные обновлены в 14:05». Тайминг: refresh пересчитывает MV целиком; при целевой
нагрузке (~3–4 млн сообщений/год, десятки тысяч диалогов) это секунды. Порог
деградации — refresh дольше 2–3 минут по логам scheduler'а; тогда MV заменяется
инкрементальной таблицей `conversation_stats`, которую inbound-воркер обновляет
UPSERT'ом при каждом сообщении (схема колонок та же, API не меняется). До порога —
не усложняем.

### 3.3. Что читает MV, что считается live

| Потребитель | Источник | Свежесть | Почему |
|---|---|---|---|
| Карточки FRT, «новых за период», % бота, таймсерии по диалогам, FRT-колонки таблицы менеджеров | `mv_conversation_stats` | ≤ 1 ч | Тяжёлые lookback'и по `messages` посчитаны заранее; отчёту руководителя часовая свежесть не мешает — в UI подпись «обновлено в HH:05» |
| «Закрыто», «принято», «телефоны», «переоткрытия» | live `audit_log` | секунды | Append-only, узкие выборки по `idx_audit_action_created` — дешевле, чем тащить в MV |
| «Сообщений отправлено», таймсерия `messages_*` | live `messages` | секунды | Частичные индексы + pruning партиций; `count(*)` за период — index-only scan |
| Snapshot «в работе / ждут сейчас» | live `conversations` | секунды | По определению «сейчас» (1.2); по индексам status |
| Тепловая карта | live `messages` + кэш Redis 600 с | ≤ 10 мин | См. 3.4 |
| Виджет менеджера «сегодня» | live, узкий скоуп + кэш 30 с | ≤ 30 с | Раздел 6 |

Итог: MV — только там, где live-запрос упирался бы в lookback по всей истории
`messages`; всё событийное — live, и «сегодняшние» цифры в этих метриках не отстают.

### 3.4. Тепловая карта «день недели × час»

Показатель — входящие сообщения клиентов (`direction='in' AND
sender_type='client'`): именно поток входящих отвечает на вопрос «когда нужен
дежурный» (DESIGN.md 3.4).

```sql
SELECT extract(isodow FROM (m.created_at AT TIME ZONE 'Europe/Moscow'))::int AS dow,
       extract(hour   FROM (m.created_at AT TIME ZONE 'Europe/Moscow'))::int AS hour,
       count(*) AS messages_in
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
WHERE m.created_at >= :ts_from AND m.created_at < :ts_to
  AND m.direction = 'in' AND m.sender_type = 'client'
  AND (CAST(:account_id AS uuid) IS NULL OR c.account_id = :account_id)
GROUP BY 1, 2
ORDER BY 1, 2;
```

Решение — **live, не MV**: за типичный период (месяц) это ~300 тыс. строк по
частичному индексу `idx_messages_client_in` в 1–2 партициях — десятки миллисекунд.
Результат кэшируется в Redis (`stats:heatmap:{account_id|all}:{date_from}:{date_to}`,
TTL 600 с) — повторные открытия дашборда БД не трогают. API зануляет
недостающие ячейки до полной матрицы 7 × 24 (фронтенду не нужно догадываться);
рендер — на фронте (Mantine, цветовая шкала по квантилям выборки, а не по максимуму —
один пиковый час не «выжигает» карту).

---

## 4. API-контракт `/api/v1/stats/*`

Общее:

- Префикс `/api` на домене `chat.partner-lead-centre.ru` (DESIGN.md 6), версия — `v1`.
- **RBAC** (DESIGN.md 5.1, каталог прав — 01 §12): все endpoint'ы раздела, кроме
  `/stats/my/today`, — `require_permission("stats:all")` → роли `admin`, `head`. `manager` и
  `observer` получают `403`. `/stats/my/today` — `require_permission("stats:own")`
  → `admin`, `head`, `manager` (только свои цифры; `user_id` берётся из JWT,
  параметром не передаётся — чужую статистику через этот endpoint не увидеть).
- Общие query-параметры: `date_from`, `date_to` (ISO-даты, обе включительно, по
  Москве — см. 0.1), `account_id` (uuid), `manager_id` (uuid, повторяемый:
  `manager_id=a&manager_id=b`).
- Валидация: `date_from ≤ date_to`; период ≤ 366 дней (`400 period_too_long`);
  будущие даты обрезаются до сегодня.
- Все длительности в ответах — **секунды, целые**; форматирование («1 мин 35 с») —
  на фронте.
- Каждый ответ несёт `refreshed_at` (значение `stats:refreshed_at` из Redis) —
  честная метка свежести MV-части.

### 4.1. `GET /api/v1/stats/summary` — карточки-метрики

Для каждой карточки — значение за период и сравнение с предыдущим периодом той же
длины (бэкенд сам исполняет те же запросы со сдвинутыми границами).

```json
{
  "period":      {"date_from": "2026-08-01", "date_to": "2026-08-04", "tz": "Europe/Moscow"},
  "prev_period": {"date_from": "2026-07-28", "date_to": "2026-07-31"},
  "refreshed_at": "2026-08-04T11:05:12Z",
  "cards": {
    "conversations_new":     {"value": 312, "prev": 280, "delta_pct": 11.4},
    "conversations_closed":  {"value": 290, "prev": 301, "delta_pct": -3.7},
    "in_progress_now":       {"value": 47,  "prev": null, "delta_pct": null},
    "waiting_now":           {"value": 6,   "prev": null, "delta_pct": null},
    "frt_operator": {
      "median_sec": 95,  "avg_sec": 340,
      "median_biz_sec": 88, "avg_biz_sec": 210,
      "answered": 260, "unanswered": 52,
      "prev_median_sec": 120, "delta_pct": -20.8
    },
    "frt_bot": {"median_sec": 3, "avg_sec": 4, "answered": 295},
    "bot_closed": {
      "pct": 18.6, "closed_by_bot": 54, "closed_total": 290,
      "prev_pct": 15.2, "delta_pct": 3.4
    },
    "phones_collected": {
      "value": 78, "by_source": {"bot": 41, "regex": 30, "manual": 7},
      "prev": 65, "delta_pct": 20.0
    },
    "repeat_contacts": {"reopened": 25, "repeat_clients": 19, "prev_reopened": 21}
  }
}
```

Правила формы: карточка — всегда объект (не голое число), чтобы добавление полей не
ломало клиент; `delta_pct` = `(value - prev) / prev * 100`, `null` при
`prev IN (0, null)` и для snapshot-метрик («сейчас» не с чем сравнивать);
у процентных метрик `delta_pct` — разница в процентных пунктах.

### 4.2. `GET /api/v1/stats/timeseries` — график

Параметры сверх общих: `metric` (enum: `conversations_new | conversations_closed |
messages_in | messages_out | frt_operator_median | phones_collected`),
`group` (`day | hour`; `hour` разрешён при периоде ≤ 7 дней, иначе `400`).

```json
{
  "metric": "conversations_new",
  "group": "day",
  "refreshed_at": "2026-08-04T11:05:12Z",
  "points": [
    {"ts": "2026-08-01", "value": 41},
    {"ts": "2026-08-02", "value": 0},
    {"ts": "2026-08-03", "value": 55},
    {"ts": "2026-08-04", "value": 38}
  ]
}
```

Ряд **зануляется до сплошного** на бэкенде (для `frt_operator_median` пустой день —
`null`, не `0`: нулевая медиана и «нет данных» — разные вещи). `ts` — дата при
`group=day`, `"2026-08-04T13:00"` (московское время без смещения) при `group=hour`.
Источники: диалоговые метрики — MV (`date_trunc('day', first_client_at AT TIME ZONE
'Europe/Moscow')`), сообщения — live по частичным индексам, телефоны — live по
`audit_log`.

### 4.3. `GET /api/v1/stats/heatmap` — тепловая карта

Параметры: общие, кроме `manager_id` (входящие менеджеру не принадлежат).

```json
{
  "tz": "Europe/Moscow",
  "metric": "messages_in",
  "cells": [
    {"dow": 1, "hour": 0, "value": 2},
    {"dow": 1, "hour": 1, "value": 0},
    {"dow": 7, "hour": 23, "value": 11}
  ]
}
```

Всегда ровно 168 ячеек (7 × 24, `dow` — ISO: 1 = понедельник), нули включены.

### 4.4. `GET /api/v1/stats/managers` — таблица менеджеров

Параметры сверх общих: `sort` (`messages_sent | taken | answered | closed |
frt_median_sec | frt_median_biz_sec`, по умолчанию `messages_sent`),
`order` (`asc | desc`). Пагинации нет — менеджеров десятки (DESIGN.md, допущение 1).

```json
{
  "period": {"date_from": "2026-08-01", "date_to": "2026-08-04"},
  "refreshed_at": "2026-08-04T11:05:12Z",
  "rows": [
    {
      "manager_id": "6f1e...", "full_name": "Анна Смирнова", "is_active": true,
      "taken": 34, "answered": 31, "closed": 28,
      "frt_avg_sec": 210, "frt_median_sec": 74, "frt_median_biz_sec": 71,
      "messages_sent": 412
    }
  ],
  "totals": {
    "taken": 120, "answered": 260, "closed": 290,
    "frt_median_sec": 95, "frt_median_biz_sec": 88, "messages_sent": 1834
  }
}
```

Семантика колонок — из словаря: `taken` — события `conversation.assigned` на этого
менеджера; `answered` — диалогов, где он автор первого операторского ответа (1.1.7);
`closed` — уникальных диалогов, закрытых им; FRT — по диалогам из `answered`.
`totals` считается по всей выборке заново (медиана суммы ≠ сумма медиан).
Запрос — раздел 2.8.

### 4.5. `POST /api/v1/stats/export` + `GET /api/v1/stats/export/{job_id}`

Асинхронный экспорт (раздел 5). Тело запроса:

```json
{
  "format": "xlsx",
  "date_from": "2026-08-01",
  "date_to": "2026-08-04",
  "account_id": null,
  "manager_id": null,
  "sheets": ["summary", "managers", "conversations"]
}
```

Ответ `202`: `{"job_id": "b8c4..."}`. Ошибки: `400` — период > 366 дней;
`409 export_already_running` — у пользователя уже есть активный экспорт;
`429` — исчерпан дневной лимит (раздел 5.4).

`GET /api/v1/stats/export/{job_id}` (доступен только автору job'а):

```json
{
  "job_id": "b8c4...",
  "status": "done",
  "format": "xlsx",
  "rows": 4211,
  "url": "/api/v1/media/exports/leadchat-stats_2026-08-01_2026-08-04_b8c4.xlsx?sig=...&exp=...",
  "expires_at": "2026-08-05T12:00:00Z",
  "error": null
}
```

`status`: `pending | running | done | failed`. UI поллит раз в 2 с (WebSocket тут
не нужен — экспорт занимает секунды). `url` — подписанная nginx-ссылка, тот же
механизм `secure_link`, что и у media (DESIGN.md 1.4): `EXPORT_DIR` лежит под
`/var/leadchat/media/`, поэтому файл раздаёт существующий location `/api/v1/media/`
(параметры `sig`/`exp`, генерация — `signed_media_url`, 05 §3.2/3.3), TTL 24 ч.

### 4.6. `GET /api/v1/stats/my/today` — виджет менеджера

Раздел 6.

---

## 5. Экспорт CSV/XLSX

### 5.1. Почему в воркере

Экспорт — единственная операция статистики, которая может жить секундами и писать
файлы: в API-процессе ей не место. `POST /stats/export` кладёт job в ARQ
(`arq.enqueue_job("export_stats", job_id, params)`), статус живёт в Redis
(`stats:export:{job_id}`, hash, TTL 24 ч), файл — на диске рядом с media.

### 5.2. Формат файлов

**XLSX** (openpyxl, обязательно `write_only=True` — потоковая запись, память O(1)
от числа строк). Листы по параметру `sheets`:

- **«Сводка»** — пары «показатель / значение» (все карточки 4.1 + период + фильтры
  + время выгрузки). Длительности — в секундах и рядом в человекочитаемом виде.
- **«Менеджеры»** — таблица 4.4 построчно + строка «Итого».
- **«Диалоги»** — по строке на диалог из `mv_conversation_stats` за период
  (server-side cursor, `yield_per=1000`): дата первого сообщения (МСК), аккаунт
  (`avito_accounts.title`), клиент (`clients.name`, `clients.phone`), объявление
  (`conversations.item_title`), статус, ответственный, автор первого ответа,
  FRT оператора (сек, астр. и раб.), FRT бота (сек), сообщений вх/исх,
  закрыт кем и когда, `conversation_id` (для сверки).

**CSV** — те же данные листа «Диалоги» (плоский формат, один файл): кодировка
`utf-8-sig` (BOM — иначе русский Excel показывает кракозябры), разделитель `;`
(Excel с русской локалью), перевод строки `\r\n`.

Имя файла: `leadchat-stats_{date_from}_{date_to}_{job8}.{ext}`.

### 5.3. Код воркера (каркас)

```python
# app/workers/exports.py
import csv
from pathlib import Path

from openpyxl import Workbook

EXPORT_DIR = Path("/var/leadchat/media/exports")
MAX_CONV_ROWS = 100_000

async def export_stats(ctx, job_id: str, params: dict):
    redis = ctx["redis"]
    await redis.hset(f"stats:export:{job_id}", mapping={"status": "running"})
    try:
        path = EXPORT_DIR / filename(params, job_id)
        if params["format"] == "xlsx":
            rows = await write_xlsx(path, params)      # ниже
        else:
            rows = await write_csv(path, params)
        # nginx secure_link, как media: relpath от /var/leadchat/media (05 §3.3)
        url = signed_media_url(f"exports/{path.name}", ttl=86_400)
        await redis.hset(f"stats:export:{job_id}", mapping={
            "status": "done", "url": url, "rows": rows})
    except ExportTooLarge as e:
        await redis.hset(f"stats:export:{job_id}", mapping={
            "status": "failed", "error": str(e)})
    except Exception:
        await redis.hset(f"stats:export:{job_id}", mapping={
            "status": "failed", "error": "internal"})
        raise                                           # ретраи/Sentry — стандартно
    finally:
        await redis.expire(f"stats:export:{job_id}", 86_400)


async def write_xlsx(path: Path, params: dict) -> int:
    wb = Workbook(write_only=True)

    ws = wb.create_sheet("Сводка")
    for name, value in await fetch_summary_pairs(params):   # карточки 4.1
        ws.append([name, value])

    ws = wb.create_sheet("Менеджеры")
    ws.append(["Менеджер", "Принято", "Отвечено", "Закрыто",
               "FRT ср., с", "FRT мед., с", "FRT мед. (раб.), с", "Отправлено"])
    async for r in fetch_manager_rows(params):              # запрос 2.8
        ws.append([r.full_name, r.taken, r.answered, r.closed,
                   r.frt_avg_sec, r.frt_median_sec, r.frt_median_biz_sec,
                   r.messages_sent])

    ws = wb.create_sheet("Диалоги")
    ws.append(CONV_HEADERS)
    n = 0
    async for r in fetch_conversation_rows(params):         # MV, yield_per=1000
        n += 1
        if n > MAX_CONV_ROWS:
            raise ExportTooLarge(
                f"Больше {MAX_CONV_ROWS} диалогов — сузьте период или фильтры")
        ws.append(conv_row(r))

    wb.save(path)
    return n
```

CSV-вариант — `csv.writer(open(path, "w", encoding="utf-8-sig", newline=""),
delimiter=";")`, тот же генератор строк, тот же лимит.

### 5.4. Лимиты и уборка

| Ограничение | Значение | Где enforced |
|---|---|---|
| Период экспорта | ≤ 366 дней | API, `400` |
| Строк листа «Диалоги» | ≤ 100 000 | воркер, `failed` с подсказкой сузить период |
| Одновременных экспортов на пользователя | 1 | API, `409` (ключ `stats:export:active:{user_id}`) |
| Экспортов в сутки на пользователя | 20 | API, `429` (Redis-счётчик, TTL до полуночи МСК) |
| Хранение файлов | 7 дней | ежедневный APScheduler-job удаляет из `EXPORT_DIR` файлы старше 7 суток |
| TTL подписанной ссылки | 24 ч | nginx `secure_link` |

Доступ к файлу — только по подписанной ссылке из статуса job'а; листинг каталога в
nginx выключен. Экспорт логируется в `audit_log`
(`action='stats.exported'`, `details={"format", "date_from", "date_to", "rows"}`) —
выгрузка данных клиентов должна быть видима в журнале аудита.

---

## 6. Виджет «моя статистика за сегодня»

Отдельный лёгкий endpoint — грузится при каждом старте рабочего места менеджера
(DESIGN.md 5.2), поэтому: без MV (нужна live-свежесть), без тяжёлых lookback'ов,
узкий скоуп (один пользователь, одна суточная партиция), кэш в Redis
(`stats:my:{user_id}:{date}`, TTL 30 с).

### 6.1. `GET /api/v1/stats/my/today`

Роли: `manager` (а также `admin`, `head` — свои цифры). `user_id` — из JWT.
`:day_start`/`:day_end` — границы московского «сегодня» в UTC.

```json
{
  "date": "2026-08-04",
  "active_now": 7,
  "waiting_reply_now": 2,
  "taken_today": 5,
  "closed_today": 3,
  "messages_sent_today": 64,
  "frt_median_sec_today": 74,
  "answered_today": 6
}
```

`waiting_reply_now` — мои диалоги `in_progress`, где последнее видимое клиенту
сообщение — входящее (клиент ждёт ответа); это же число — источник для акцента в UI.

### 6.2. SQL (один запрос, CTE по строке на блок)

```sql
WITH sent AS (
    SELECT count(*) AS messages_sent_today
    FROM messages m
    WHERE m.created_at >= :day_start AND m.created_at < :day_end
      AND m.direction = 'out' AND m.sender_type = 'operator'
      AND m.sender_user_id = :user_id
      AND m.delivery_status <> 'failed'
),
taken AS (
    SELECT count(*) AS taken_today
    FROM audit_log a
    WHERE a.action = 'conversation.assigned'
      AND (a.details->>'assignee_id')::uuid = :user_id
      AND a.created_at >= :day_start AND a.created_at < :day_end
),
closed AS (
    SELECT count(DISTINCT a.entity_id) AS closed_today
    FROM audit_log a
    WHERE a.action = 'conversation.status_changed'
      AND a.details->>'to' = 'closed'
      AND a.user_id = :user_id
      AND a.created_at >= :day_start AND a.created_at < :day_end
),
active AS (
    SELECT count(*)                                        AS active_now,
           count(*) FILTER (WHERE lm.sender_type = 'client') AS waiting_reply_now
    FROM conversations c
    LEFT JOIN LATERAL (
        SELECT m.sender_type
        FROM messages m
        WHERE m.conversation_id = c.id
          AND m.direction IN ('in', 'out')       -- заметки/системные не считаются
        ORDER BY m.created_at DESC
        LIMIT 1
    ) lm ON true
    WHERE c.assignee_id = :user_id AND c.status = 'in_progress'
),
frt AS (
    -- диалоги, начавшиеся сегодня, где первый операторский ответ — мой
    SELECT count(*)                                        AS answered_today,
           round((percentile_cont(0.5) WITHIN GROUP
               (ORDER BY r.frt_sec))::numeric)::int        AS frt_median_sec_today
    FROM (
        SELECT EXTRACT(epoch FROM op.first_at - f.first_client_at) AS frt_sec
        FROM conversations c
        CROSS JOIN LATERAL (
            SELECT min(m.created_at) AS first_client_at
            FROM messages m
            WHERE m.conversation_id = c.id
              AND m.direction = 'in' AND m.sender_type = 'client'
        ) f
        CROSS JOIN LATERAL (
            SELECT m.created_at AS first_at, m.sender_user_id
            FROM messages m
            WHERE m.conversation_id = c.id
              AND m.created_at >= f.first_client_at
              AND m.direction = 'out' AND m.sender_type = 'operator'
              AND m.delivery_status <> 'failed'
            ORDER BY m.created_at
            LIMIT 1
        ) op
        WHERE c.last_message_at >= :day_start        -- отсев: сегодняшние диалоги
          AND f.first_client_at >= :day_start AND f.first_client_at < :day_end
          AND op.sender_user_id = :user_id
    ) r
)
SELECT * FROM sent, taken, closed, active, frt;   -- все CTE по одной строке
```

Стоимость: `sent` — index-only по `idx_messages_operator_out`; `taken`/`closed` —
по `idx_audit_action_created`; `active` — десятки диалогов менеджера ×
`LIMIT 1` по `idx_messages_conv_created`; `frt` — только диалоги с
`last_message_at` сегодня. Всё в сумме — единицы миллисекунд; с кэшем 30 с endpoint
безопасен хоть при поллинге виджета.

Определения — те же, что в словаре (раздел 1): виджет менеджера и `/stats`
руководителя обязаны сходиться цифра в цифру при одинаковых фильтрах — это
проверяется интеграционным тестом (один сценарий наливает фикстуры,
оба endpoint'а сверяются между собой и с эталонными SQL из раздела 2).

---

## 7. Чек-лист внедрения (этап 3 плана)

1. Миграция: функция `business_seconds_between`, индексы 3.1, MV 3.2 + её индексы.
2. Хелпер `audit(action, entity, entity_id, user_id, **details)` и его вызовы во
   всех точках из 0.3 (смена статуса, назначение, reopen в inbound-воркере,
   захват телефона, шаг бота `close`).
3. `app/scheduler/jobs/stats.py` — регистрация refresh-job.
4. Роутер `app/api/routes/stats.py` — endpoints 4.1–4.6, permissions
   `stats:all` / `stats:own` (каталог — 01 §12).
5. Воркер `app/workers/exports.py` + cleanup-job; файлы экспорта раздаются
   существующим nginx-location `/api/v1/media/` (05 §3.2) — отдельный location
   не нужен (`EXPORT_DIR` лежит под alias `/var/leadchat/media/`).
6. Тесты: юнит на `business_seconds_between` (границы 10:00/20:00, переход суток,
   ответ до начала дня); интеграционные на каждую метрику по фикстурам со всеми
   краевыми случаями раздела 1 (без ответа, note, failed, reopen, закрыл бот);
   сверка виджета 6 с `/stats` (см. выше).
