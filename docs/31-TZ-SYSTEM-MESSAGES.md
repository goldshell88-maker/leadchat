# Системные сообщения Авито (шаг 8)

> ⚠ **ВНИМАНИЕ: СОДЕРЖИМОЕ ЭТОГО ФАЙЛА ПЕРЕПУТАНО С `docs/30-TZ-CROSS-CLIENT.md`.**
>
> Всё, кроме заключительной «Встречной критики», описывает **шаг 7 — сквозной
> клиент**, а не шаг 8. Раздел «Задание» так и открывается словами «ШАГ 7.
> СКВОЗНОЙ КЛИЕНТ». Разбор по разделам:
>
> | Раздел | О чём на самом деле |
> |---|---|
> | «Что есть сейчас» | таблица `clients`, глобальный `external_id`, извлечение телефона — **шаг 7** |
> | «Чего мы не знаем» | глобален ли `author_id` между аккаунтами, хэш профиля — **шаг 7** |
> | «Задание» | `app/services/identity.py`, склейка людей — **шаг 7** |
> | «Встречная критика» | «Разбор ТЗ „system-msgs“» — **шаг 8, на своём месте** |
>
> Задание про НАСТОЯЩИЙ шаг 8 (служебные сообщения, `classify_avito_message`)
> лежит в `docs/30-TZ-CROSS-CLIENT.md`, раздел «Задание».
>
> Найдено 23.08.2026 картой знаний. Текст НЕ переставляли намеренно — см. ту же
> оговорку в `docs/30-TZ-CROSS-CLIENT.md`.


Техническое задание, написанное по брифу аудита и разобранное встречной критикой.

**Оценка:** 84 часов.

## Как устроено сейчас

## Что есть сейчас (проверено по коду, август 2026, схема на 0023)

### Клиент — одна плоская строка, ключ у неё чужой
`app/models/client.py` — таблица `clients`: `id`, `channel` (default `'avito'`), `external_id` (avito user id клиента), `name`, `phone`, `avito_rating`, плюс три поля чёрного списка (`blocked_at/blocked_by_id/blocked_reason`, миграция 0013). Единственное ограничение — `UniqueConstraint("channel", "external_id")` (DDL — `app/db/migrations/versions/0001_init.py:102-117`).

**Главное следствие, которое сегодня никто не объявлял вслух: ключ клиента — глобальный, без привязки к аккаунту.** То есть система УЖЕ считает, что `author_id` Авито одинаков во всех наших аккаунтах, и на этом допущении молча склеивает людей. Допущение нигде не проверено (см. unknowns 1). Если оно верно — цель шага 7 частично уже достигнута; если неверно — у нас есть готовый механизм склейки посторонних, и он включён.

Нет ни `client_identities`, ни истории слияний, ни `merged_into_id`, ни тегов и заметок на уровне клиента, ни `display_name`, ни `created_at`. Файла `app/services/clients.py` не существует вовсе: `app/api/routes/clients.py` — это только чёрный список (три ручки `block`/`unblock`/`blocked`, право `conversations:manage`).

### Где клиент создаётся
`app/services/inbound.py:121` `_upsert_client` — `SELECT ... WHERE channel='avito' AND external_id=str(event.author_id)`, при отсутствии `INSERT ... ON CONFLICT DO NOTHING` и повторное чтение (проигравший гонку читает строку победителя). Имя вебхук v3 не несёт, поэтому при вставке без имени ставится ARQ-задача `enrich_client` (`app/services/client_enrich.py`), которая тянет имя из `GET /messenger/v2/accounts/{uid}/chats/{chat_id}` с запасным проходом по списку чатов.

`conversations.client_id` — `app/models/conversation.py:44`, NOT NULL, FK на `clients.id`. Индекс `idx_conversations_client (client_id)` уже есть — создан в `0004_stats_indexes_and_mv.py` ради метрики «повторные клиенты».

### Извлечение телефона — два писателя, оба на входящих
`app/services/inbound.py:73`:
```python
_PHONE_CANDIDATE = re.compile(r"\+?\d(?:[\s\-().]*\d){9,10}")

def extract_phone(text): ...  # первый номер, 11 цифр с 7/8 → +7XXXXXXXXXX; 10 цифр с 9 → +7…
```
`_maybe_extract_phone` (`inbound.py:222`) пишет `clients.phone` **только если поле пусто** (правило INT-7, перезаписи нет) и кладёт аудит `client.phone_captured` с `details.source='regex'`. Второй писатель — `app/bots/engine.py:709` `capture_phone`, `source='bot'`, та же функция извлечения (`app/bots/steps.py:34` реэкспортирует `extract_phone`, `steps.py:249` `mask_phones` прячет номера перед отправкой в модель).

Номер оператора сегодня не привязывается — но **по случайности, а не по контракту**: `_maybe_extract_phone` зовётся из `apply_inbound_event`, куда попадают только входящие. В сигнатуре нет ни направления, ни типа отправителя; первый же вызов из другого места привяжет телефон компании к клиенту.

Разбивка карточки «Собрано телефонов» обещает три источника (`stats.py:523-556`), но `source='manual'` **не пишет никто** — ручного ввода телефона в системе нет, столбец «из них вручную» всегда ноль.

### Поиск
`app/services/conversations.py:292` `normalize_phone_query` — снимает разделители, требует ≥5 цифр, отрезает префикс 7/8; `_search_condition` (`:309`) ищет `Client.name ILIKE` OR `Client.phone LIKE '%хвост%'` OR полнотекст по `messages`. Ищется одна колонка `clients.phone` — второго номера у клиента быть не может.

### История клиента
`app/services/conversations.py:697` `client_history` → `GET /conversations/{id}/client-history` (`app/api/routes/conversations.py:582`): прошлые диалоги **по `conv.client_id`**, без пагинации, с числом сообщений одним GROUP BY. То есть блок уже «по клиенту», а не «по чату» — но клиент сегодня равен одному `author_id`.

Фронт: `frontend/src/features/chats/components/card/ClientCardPane.tsx:461` — секция уже называется **«История клиента»**, а «Первое обращение клиента» (`:471`) это её ПУСТОЕ состояние. Менять надо не заголовок, а то, что за ним стоит, и достроить шапку блока.

### Списки и признак повторности
`conversation_out` (`app/services/conversations.py:165-262`) отдаёт `client{id,name,phone,avito_rating,blocked,blocked_reason}` — признака повторности нет. Строка очереди (`app/services/inbox.py:356` `inbox_item`) собирается из того же `conversation_out`. Строка списка (`frontend/.../list/ConversationListItem.tsx`) имеет правило «один чип на строку» — слот занят срочностью (не отправлено / никто не берёт / негатив).

`/dialogs` (`app/services/conversation_table.py:391`) джойнит `Client.name`/`Client.phone`, `client_id` наружу не отдаёт; в CSV (`:277`) телефон есть, идентификатора клиента и признака повторности нет.

### Статистика «Повторные» — считает не то, что показывает
`app/services/stats.py:562` `_REOPENED_SQL` — `count(*)` событий `conversation.reopened` из `audit_log`.
`app/services/stats.py:574` `_REPEAT_CLIENTS_SQL` — `count(DISTINCT s.client_id)` из `mv_conversation_stats` c `EXISTS (другой диалог того же клиента с last_message_at < first_client_at)`.

В карточке (`frontend/src/features/stats/components/SummaryCards.tsx:125`) **значением стоит `reopened`**, а `repeat_clients` спрятан в подсказку. Отсюда и «Повторные: 1 ↩ вернулись · было 0»: показано число переоткрытий ОДНОГО И ТОГО ЖЕ чата. У Авито вернувшийся клиент почти всегда пишет по ДРУГОМУ объявлению — это новый `chat_id`, новый диалог, ноль переоткрытий. Метрика системно занижает возвраты, и шаг 7 её не чинит сам по себе — её надо менять руками (см. proposal §6). Дополнительно `_REPEAT_CLIENTS_SQL` сравнивает с `c2.last_message_at`, а его поднимает любая правка диалога, включая заметку.

MV `mv_conversation_stats` (`0004_stats_indexes_and_mv.py:107`) везёт `c.client_id` и обновляется раз в час (`app/scheduler/jobs/stats.py`, `REFRESH ... CONCURRENTLY`).

### Права и журнал
`app/core/rbac.py`: роли `admin/head/manager/observer`, 16 прав; отдельного права на персональные данные нет. **`observer` имеет `conversations:read` и получает `client.phone` в каждой строке списка** — телефоны видит роль, которой не положено видеть даже заметки.

`app/services/audit.py` — реестр `AUDIT_ACTIONS` (`tests/unit/test_audit.py` следит, что мимо реестра в журнал ничего не попадает); из клиентского там `client.phone_captured`, `client.blocked`, `client.unblocked`.

Телефон уезжает в URL: поиск идёт `GET /conversations?q=…`, а `q` не входит в `SECRET_KEYS` (`app/core/logging.py:18-29`) и не затирается ни в логах, ни в `scrub_secrets` (`app/core/observability.py:62`).

### Что даёт каталог Авито (docs/26 + разобранная спецификация, 238 путей)
* вебхук (`WebhookMessage`) несёт ровно `author_id`, `user_id`, `chat_id`, `item_id`, `content`, `chat_type ∈ {u2i,u2u,a2u}` — **ни имени, ни телефона, ни профиля**;
* `Chat.users[].id` описан как «Уникальный ID пользователя. **Обратите внимание на хэширование**» — предупреждение без пояснения, гарантии глобальности НЕТ;
* там же есть `users[].public_user_profile.url` вида `https://avito.ru/user/0a1b2c3d4e5f60718293a4b5c6d7e8f9/profile` — хэш профиля, который мы никогда не читали;
* `UserInfoSelf.phone/phones` (`/core/v1/accounts/self`) — это НАШ номер, не клиента;
* единственное место, где Авито сам отдаёт телефон клиента, — раздел ЦД/CPA: `POST /cpa/v1/phonesInfoFromChats` → `{id (ID ЦД), date, phone_number "Номер телефона из сообщения", pricePenny, group, url}`, 5 запросов в минуту; `chat_id` в ответе нет, связь с нашим чатом возможна только через `/cpa/v1/chatsByTime` → `OpenApiChat{actionId, channelId: "u2i-…", contactType, message}`. Ни одна из этих ручек нами никогда не вызывалась и требует платного тарифа ЦД.

Тестов: 73 юнит-файла, 14 интеграционных (PG). Юниты идут на SQLite из `Base.metadata` — новые таблицы обязаны быть портируемыми (никаких `text[]`-специфичных выражений в ORM-слое, как уже сделано для `tags` через `TextArray`).

## Чего мы не знаем

* ГЛОБАЛЕН ЛИ avito user_id МЕЖДУ АККАУНТАМИ. В docs/26 такого утверждения нет. В разобранной спецификации у Chat.users[].id стоит дословно «Уникальный ID пользователя. Обратите внимание на хэширование» — предупреждение без объяснения, что именно хэшируется и от чего зависит; у webhook author_id пояснения нет вовсе. Вывод: документация глобальность НЕ гарантирует. По коду это тоже не выводится — у нас на всю систему один аккаунт с историей. Проверяется только живьём: написать с одного личного аккаунта Авито в объявления «Сергея» (100000001) и «Олега» (100000002) и сравнить author_id в webhook_raw_log. 15 минут работы владельца. Имитатор для этой проверки бесполезен по определению (docs/26: имитатор дважды подтвердил наши же ошибки).
* ОТДАЁТ ЛИ АВИТО ХЭШ ПРОФИЛЯ В НАШИХ ОТВЕТАХ. В спецификации у Chat.users[] есть public_user_profile.url вида https://avito.ru/user/<32 hex>/profile. Мы этот блок никогда не читали (app/integrations/avito/adapter.py:parse_chat берёт только id и name). Неизвестно: приходит ли он на боевых аккаунтах, приходит ли он для собеседника (а не только для владельца объявления), и — главное — одинаков ли хэш у одного человека, увиденного с двух РАЗНЫХ наших аккаунтов. Если одинаков, это и есть настоящий глобальный ключ, лучше user_id. Проверяется тем же живым опытом: снять GET /messenger/v2/accounts/{uid}/chats/{chat_id} на обоих аккаунтах и сравнить строки.
* ДОСТУПЕН ЛИ РАЗДЕЛ ЦД/CPA НА АККАУНТАХ ЗАКАЗЧИКА. POST /cpa/v1/phonesInfoFromChats — единственное место в каталоге, где Авито сам отдаёт «номер телефона из сообщения». Неизвестно: есть ли у заказчика тариф ЦД (в описании соседних методов иерархии прямо сказано «необходимо приобрести тариф»), отвечает ли метод 200 или 403 на его ключах, и что реально лежит в phone_number (полный номер, маска, null).
* СВЯЗЫВАЕТСЯ ЛИ ОТВЕТ ЦД С НАШИМ ЧАТОМ. phonesInfoFromChats возвращает id = «ID ЦД» и НЕ возвращает chat_id. Связь с нашим external_chat_id возможна только через /cpa/v1/chatsByTime → OpenApiChat.channelId (пример «u2i-4HzWMRUoAAVnH5i2CC8meg» — формат совпадает с нашим). Совпадают ли actionId между двумя ручками и совпадает ли channelId с нашим external_chat_id посимвольно — по документам не выводится, только опытом.
* УСТОЙЧИВ ЛИ author_id ВО ВРЕМЕНИ. Меняется ли идентификатор клиента, если он перерегистрирует профиль, меняет номер телефона в Авито или его аккаунт объединяют/разделяют на стороне Авито. Ни в каталоге, ни в спецификации об этом ничего. Это определяет, можно ли строить на avito_user_id вообще что-то долгоживущее.
* ЧТО ПРИХОДИТ В author_id ДЛЯ ЧАТОВ ТИПА a2u И u2u. В спецификации chat_type ∈ {u2i, u2u, a2u}, где a2u — «чат по профилю пользователя с Авито». Неизвестно, заводим ли мы при таких сообщениях «клиента Авито» — служебного отправителя, которого нельзя ни склеивать, ни показывать в очереди. По нашим данным (33 входящих) таких ещё не было.
* ЕСТЬ ЛИ У ЗАКАЗЧИКА ИЕРАРХИЯ АККАУНТОВ АВИТО. В спецификации есть раздел «Иерархия Аккаунтов» с перезакреплением объявлений между сотрудниками. Если девять аккаунтов заказчика — это иерархия одной компании, поведение идентификаторов может отличаться от девяти независимых аккаунтов. Вопрос владельцу, по коду не определяется.
* СКОЛЬКО В ИСТОРИИ КЛИЕНТОВ С ТЕЛЕФОНОМ. От этого зависит и польза сквозного клиента, и время бэкофилла. У нас нет копии базы заказчика (в проекте фигурирует цифра 454 тыс. диалогов из разбора Jivo, но это Jivo, а не наша база). Меряется одним SELECT на проде до начала работ; до этого оценка бэкофилла — с запасом.
* СРОК ХРАНЕНИЯ ТЕЛЕФОНОВ И ОСНОВАНИЕ ОБРАБОТКИ. Решение владельца, не инженера: сколько лет держим номера, удаляем ли по требованию клиента, кто в компании отвечает за такие запросы. Механизм я закладываю (настройка + задача-уборщик), число ставит владелец — ровно как с рабочими часами в #41.
* ЕСТЬ ЛИ СРЕДИ КЛИЕНТОВ ЗАВЕДОМО ОБЩИЕ НОМЕРА. Ремонт техники: один номер на семью, номер управляющей компании, номер соседа-посредника. Сколько таких у заказчика — знает только он. От оценки зависит, окажется ли экран «Возможные дубли» пустым или в нём каждый день будет по десятку пар.

## Задание

## ШАГ 7. СКВОЗНОЙ КЛИЕНТ — техническое задание

### §0. Главное архитектурное решение: неизвестное живёт в одной функции

Единственная непроверяемая по документам величина — **область действия идентификатора Авито**. Она изолируется одной функцией и одной настройкой; весь остальной код о ней не знает.

```python
# app/services/identity.py
GLOBAL = "global"

async def scope_for(db, type_: str, account: AvitoAccount | None) -> str:
    """Область, в которой значение идентификатора уникально.

    Телефон и email уникальны во вселенной — область 'global'.
    Идентификатор Авито — область АККАУНТА, пока не доказано обратное:
    в каталоге (docs/26) утверждения о глобальности нет, а в спецификации
    у users[].id стоит «Обратите внимание на хэширование» без пояснений.
    Доказали живым опытом — включается настройка, и функция начинает
    возвращать GLOBAL. Больше не меняется НИЧЕГО.
    """
    if type_ in ("phone", "email", "telegram"):
        return GLOBAL
    if type_ == "avito_user_id":
        if await app_settings.get(db, app_settings.IDENTITY_AVITO_ID_GLOBAL):
            return GLOBAL
        return str(account.id)
    raise ValueError(type_)
```

Настройка `identity.avito_user_id_global`, тип bool, **по умолчанию false**. Асимметрия рисков считается так: если идентификаторы на самом деле глобальны, а мы осторожничаем — вернувшийся на другой канал клиент получает баннер «похоже, тот же клиент» вместо готовой истории, цена ошибки один щелчок. Если они не глобальны, а мы поверили — посторонние люди склеены, оператор видит чужую переписку и чужой номер, цена ошибки — утечка персональных данных и ручной разбор. Поэтому по умолчанию false, хотя это и строже сегодняшнего поведения (сегодня `UNIQUE(channel, external_id)` склеивает по всем аккаунтам молча и всегда).

---

### §1. Схема БД (миграция 0024)

```sql
-- ============================ clients: карточка вместо строки

ALTER TABLE clients ADD COLUMN display_name    text;
ALTER TABLE clients ADD COLUMN created_at      timestamptz NOT NULL DEFAULT now();
ALTER TABLE clients ADD COLUMN merged_into_id  uuid REFERENCES clients(id) ON DELETE SET NULL;
ALTER TABLE clients ADD COLUMN tags            text[] NOT NULL DEFAULT '{}';

-- Ключ клиента переезжает в client_identities. Ограничение снимается,
-- иначе тот же числовой author_id на втором аккаунте не сможет завести
-- вторую строку — то есть область действия идентификатора будет намертво
-- зашита в схему ровно там, где мы не знаем ответа.
ALTER TABLE clients DROP CONSTRAINT uq_clients_channel_external_id;
CREATE INDEX ix_clients_channel_external ON clients (channel, external_id);
CREATE INDEX ix_clients_merged_into ON clients (merged_into_id)
    WHERE merged_into_id IS NOT NULL;
CREATE INDEX ix_clients_tags ON clients USING gin (tags);

-- clients.phone НЕ УДАЛЯЕТСЯ. Он становится производной колонкой —
-- «главный номер», который пишет только identity-служба. Его читают
-- статистика (stats.py:1462), таблица разбора (conversation_table.py:391),
-- выгрузки и боты; выкорчёвывать его значило бы переписать пять модулей
-- ради нормализации, которой никто не увидит.

-- ============================ идентичности

CREATE TABLE client_identities (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id          uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    type               text NOT NULL,
    -- 'global' либо uuid аккаунта строкой — см. §0.
    scope              text NOT NULL,
    value_normalized   text NOT NULL,
    -- как это выглядело в жизни: «8 (900) 111-22-41 доб. 12».
    -- Показывается в карточке рядом с нормализованным.
    value_raw          text,
    source             text NOT NULL,
    confidence         text NOT NULL,
    -- Погашенная идентичность: значение за клиентом видели, но связь
    -- отключена решением человека (см. «Разделить», §4.5). Нужна, чтобы
    -- следующее сообщение с тем же номером не воскресило слияние и не
    -- упёрлось в уникальный индекс.
    active             boolean NOT NULL DEFAULT true,
    first_seen         timestamptz NOT NULL DEFAULT now(),
    last_seen          timestamptz NOT NULL DEFAULT now(),
    verified_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    verified_at        timestamptz,
    -- откуда взяли: карточка показывает «из сообщения от 12 августа»
    conversation_id    uuid REFERENCES conversations(id) ON DELETE SET NULL,
    message_id         uuid,
    CONSTRAINT ck_client_identities_type
        CHECK (type IN ('phone','avito_user_id','email','telegram')),
    CONSTRAINT ck_client_identities_source
        CHECK (source IN ('avito_field','message_in','bot','operator','import')),
    CONSTRAINT ck_client_identities_confidence
        CHECK (confidence IN ('high','medium','low'))
);

-- Сердце сопоставления: одно значение в своей области принадлежит РОВНО
-- одному клиенту. Второй претендент упирается в этот индекс — и именно
-- конфликт по нему, а не отдельный «алгоритм поиска дублей», запускает
-- слияние. Частичный: погашенные строки в счёт не идут.
CREATE UNIQUE INDEX uq_client_identities_key
    ON client_identities (type, scope, value_normalized) WHERE active;

CREATE INDEX ix_client_identities_client ON client_identities (client_id);
-- Кандидаты (§2, уровень medium) считаются самосоединением по этому индексу,
-- без отдельной таблицы: таблица кандидатов протухала бы после каждого
-- слияния и требовала бы своего пересчёта.
CREATE INDEX ix_client_identities_value ON client_identities (type, value_normalized);

-- ============================ аудит слияний с откатом

CREATE TABLE client_merges (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_client_id    uuid NOT NULL REFERENCES clients(id),
    target_client_id    uuid NOT NULL REFERENCES clients(id),
    reason              text NOT NULL,   -- phone | avito_user_id | manual | backfill
    matched_value       text,            -- что совпало, в нормализованном виде
    performed_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,  -- NULL = система
    performed_at        timestamptz NOT NULL DEFAULT now(),
    undone_at           timestamptz,
    undone_by_user_id   uuid REFERENCES users(id) ON DELETE SET NULL,
    -- Снимок ДЛЯ ОТКАТА, а не для отчёта. Хранит ровно то, что откат
    -- обязан вернуть: какие диалоги переехали, какие идентичности
    -- переехали, какие поля выжившего были дополнены.
    -- {"conversations": [uuid…], "identities": [uuid…],
    --  "target_before": {"display_name":…, "phone":…, "tags":[…]},
    --  "source_before": {"blocked_at":…, …}}
    payload             jsonb NOT NULL,
    CONSTRAINT ck_client_merges_not_self CHECK (source_client_id <> target_client_id)
);
CREATE INDEX ix_client_merges_target ON client_merges (target_client_id, performed_at DESC);
CREATE INDEX ix_client_merges_source ON client_merges (source_client_id);

-- ============================ запрет повторной склейки

CREATE TABLE client_merge_blocks (
    client_a_id      uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    client_b_id      uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    reason           text,
    PRIMARY KEY (client_a_id, client_b_id),
    -- упорядоченная пара: (A,B) и (B,A) — один и тот же запрет
    CONSTRAINT ck_client_merge_blocks_order CHECK (client_a_id < client_b_id)
);

-- ============================ заметки на уровне клиента

CREATE TABLE client_notes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    author_id   uuid REFERENCES users(id) ON DELETE SET NULL,
    body        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_client_notes_client ON client_notes (client_id, created_at DESC);
-- Отдельная таблица, а НЕ messages(direction='note'): заметки-сообщения
-- привязаны к диалогу и уезжают в ленту конкретного чата. «Всегда звонить
-- после 18:00» — свойство человека, а не одного обращения по холодильнику.

-- ============================ история клиента дешевеет

CREATE INDEX ix_conversations_client_last
    ON conversations (client_id, last_message_at DESC);
DROP INDEX idx_conversations_client;   -- префикс нового, дубликат
```

Портируемость на SQLite юнит-тестов: `tags` — уже имеющийся тип `TextArray` (`app/models/types.py`), `payload` — `JSONB` с вариантом, частичные уникальные индексы SQLite поддерживает (тот же приём, что у `uq_messages_conversation_external_created`).

---

### §2. Правила сопоставления

**Приоритет ключей:** телефон > avito_user_id > email > имя.

| Ключ | Область | Совпадение значит | Уровень |
|---|---|---|---|
| `phone` | global | тот же человек | **high — автослияние** |
| `avito_user_id`, один аккаунт | account | тот же человек | **high — автослияние** |
| `avito_user_id`, разные аккаунты | — | **вывод: документация глобальность НЕ гарантирует** (см. unknowns 1) | **medium — баннер** |
| `email` | global | вероятно тот же | medium (писателя пока нет) |
| имя | — | **ничего** | не считается вовсе |

**Вывод по каталогу, прямым текстом.** В `docs/26-AVITO-API-CATALOG.md` нет ни одного утверждения о том, что идентификатор пользователя одинаков в разных аккаунтах. В исходной спецификации, по которой каталог собран, у `Chat.users[].id` стоит «Уникальный ID пользователя. Обратите внимание на хэширование» — предупреждение без объяснения; у `author_id` в вебхуке нет и его. **Значит межаккаунтное слияние по avito_user_id запрещено**, и по умолчанию совпадение числовых идентификаторов на разных аккаунтах даёт кандидата уровня medium, а не слияние.

Почему medium, а не «не показывать»: даже в худшем предположении (идентификатор — 32-битный хэш, свой на каждый аккаунт) случайное совпадение на 400 тысяч клиентов и девять каналов ожидается примерно у полутора десятков пар за всю историю — редко для мусора, но слишком часто, чтобы сливать молча. Человек в экране «Возможные дубли» разберёт полтора десятка пар за год.

**Уровни:** `high` → сливаем сами, `medium` → баннер в диалоге и строка в «Возможных дублях», `low` → не показываем и не храним (иначе экран дублей превратится в свалку, и его перестанут открывать — а вместе с ним перестанут разбирать и настоящие пары).

**Кандидаты считаются запросом, без своей таблицы:**

```sql
SELECT a.client_id AS a_id, b.client_id AS b_id, a.type, a.value_normalized
FROM client_identities a
JOIN client_identities b
  ON  b.type = a.type
 AND  b.value_normalized = a.value_normalized
 AND  b.client_id > a.client_id
WHERE a.active AND b.active
  AND a.scope <> b.scope            -- разные аккаунты: слить нельзя, показать нужно
  AND NOT EXISTS (SELECT 1 FROM client_merge_blocks k
                   WHERE k.client_a_id = a.client_id AND k.client_b_id = b.client_id)
```

**Псевдокод привязки идентичности — единственная точка входа:**

```
attach_identity(db, client, type, value_raw, *, account, source, conf,
                conversation=None, message=None) -> AttachResult:
    value = normalize(type, value_raw)              # §3
    if value is None: return SKIPPED
    scope = scope_for(type, account)

    INSERT client_identities(...) ON CONFLICT (type, scope, value_normalized)
           WHERE active DO NOTHING
    owner = SELECT client_id FROM client_identities
             WHERE type=? AND scope=? AND value_normalized=? AND active

    if owner == client.id:
        UPDATE last_seen = now()                     # и value_raw, если был NULL
        return ATTACHED

    if blocked_pair(owner, client.id):
        INSERT client_identities(..., active=false)  # тень: доказательство есть,
        return BLOCKED                               # связь отключена человеком

    if conf == 'high' and mergeable(owner, client):
        return MERGE_REQUIRED(owner, client)         # см. ниже — сливает воркер
    return CANDIDATE

mergeable(a, b):
    # Блокированный и неблокированный не сливаются автоматически НИКОГДА.
    # Слияние спамера с настоящим клиентом либо оглушит живого человека,
    # либо вернёт спамера в очередь тринадцати операторам; и то и другое
    # должен решать человек, глядя на обе карточки.
    return (a.blocked_at is None) == (b.blocked_at is None)
```

**Слияние не выполняется на горячем пути вебхука.** `apply_inbound_event` пишет идентичность (одна строка) и, если вернулось `MERGE_REQUIRED`, ставит после коммита ARQ-задачу `merge_clients(a, b, reason)` — ровно тем же приёмом, что `enqueue_enrich_client` сегодня. Причина: слияние переписывает `client_id` у всех диалогов клиента, и держать эту транзакцию внутри доставки сообщения значит поставить приём обращений в зависимость от размера чужой истории. Задержка признака «Повторный» — сотни миллисекунд, и она видна только в первую секунду жизни диалога.

**Слияние (задача `merge_clients`):**

```
merge(a_id, b_id, reason, by_user=None):
    a_id, b_id = ordered by id           # детерминированный порядок блокировок
    SELECT ... FROM clients WHERE id IN (a_id,b_id) ORDER BY id FOR UPDATE
    a, b = resolve_root(a), resolve_root(b)     # идём по merged_into_id
    if a.id == b.id: return                     # уже слиты — идемпотентно
    if blocked_pair(a,b): return
    if not mergeable(a,b): record_candidate; return

    target, source = older_by_created_at(a, b)  # выживает тот, кто раньше
                                                # пришёл: у него длиннее история
    snapshot = {
      "conversations": [ids диалогов source],
      "identities":    [ids идентичностей source],
      "target_before": {display_name, phone, tags, name, avito_rating},
      "source_before": {merged_into_id, blocked_at, blocked_by_id, blocked_reason},
    }
    UPDATE conversations SET client_id = target.id WHERE client_id = source.id
    UPDATE client_identities SET client_id = target.id WHERE client_id = source.id
    UPDATE client_notes      SET client_id = target.id WHERE client_id = source.id
    merge_fields(target, source)                # §4.7 — ничего не затирается
    UPDATE clients SET merged_into_id = target.id WHERE id = source.id
    INSERT client_merges(...)                   # с payload = snapshot
    write_audit('client.merged', entity='client', entity_id=target.id,
                details={source, reason, matched_value, conversations: N})
    publish WS 'client:merged' {source_id, target_id}
```

`resolve_root` идёт по `merged_into_id` максимум 16 шагов и валит задачу при цикле — цикл невозможен по построению (сливаемый всегда получает `merged_into_id`, а выживший обязан иметь его NULL), но молчаливый бесконечный цикл в воркере хуже упавшей задачи.

Строка `clients` **никогда не удаляется**: старые ссылки (журнал, выгрузки, чужие вкладки) должны разрешаться, а не отдавать 404. Все чтения проходят через `resolve_root`.

---

### §3. Извлечение телефона

**Из полей Авито — источника нет.** В каталоге нет ни одного метода, отдающего телефон собеседника; `/core/v1/accounts/self` отдаёт НАШ номер. Единственный кандидат — платный раздел ЦД (`/cpa/v1/phonesInfoFromChats` + `/cpa/v1/chatsByTime`), и он вынесен в unknowns 3–4. **В базовый объём он не входит**; под него заводится одна функция-заглушка с честной подписью, чтобы включение было правкой одного файла:

```python
async def fetch_phone_from_avito(account, conversation) -> PhoneHit | None:
    """Телефон клиента из полей Авито. Сейчас всегда None.

    Ручки, которая отдаёт телефон собеседника по chat_id, в каталоге НЕТ.
    Ближайшее — раздел ЦД: phonesInfoFromChats возвращает phone_number, но
    без chat_id, а связь с нашим чатом идёт через chatsByTime.channelId.
    Ни одна из ручек нами не вызывалась, тариф ЦД не подтверждён, форма
    ответа не проверена живьём. Пока это не проверено — функция возвращает
    None, и ни одна строка кода выше о ЦД не знает.
    """
    return None
```

**Из текста входящих — расширение существующей регулярки.** `extract_phone` превращается в `extract_phones(text) -> list[str]` (все номера, а не первый: «мой 89001112241, жены 89001112242» — это два), `extract_phone` остаётся обёрткой `first or None`, чтобы боты (`app/bots/steps.py:34,244,303,328`) и `mask_phones` не разъехались с ядром.

```
normalize_phone(raw) -> "+7XXXXXXXXXX" | None:
    отрезать добавочный: всё после (?:доб|доб\.|ext|extension|#|\bx\b)\s*\d{1,6}$
    digits = только цифры
    11 цифр и начинается с 8 → "+7" + digits[1:]
    11 цифр и начинается с 7 → "+7" + digits[1:]
    10 цифр и начинается с 9 → "+7" + digits
    иначе → None            # иностранные номера не выдумываем

отбрасывать до нормализации:
    номер стоит вплотную к «₽», «руб», «р.»          — это цена
    подряд 12+ цифр                                   — счёт, IMEI, карта
    номер целиком внутри URL                          — идентификатор объявления
    рядом слова «заказ», «артикул», «серийный», «S/N» — номер не телефонный
```

**Номер из сообщения оператора не привязывается — по контракту, а не по случайности.** Сегодня это верно лишь потому, что `_maybe_extract_phone` зовётся только из входящего пути; новая функция принимает сообщение целиком и отказывается работать иначе:

```python
async def capture_phones_from_message(db, conv, client, msg, account) -> None:
    if (msg.direction, msg.sender_type) != ("in", "client"):
        return   # исходящее оператора — это номер КОМПАНИИ; заметка — не
                 # слова клиента; сообщение бота — наш же текст
```

Заметки (`direction='note'`) источником не являются: именно туда оператор вставляет номер диспетчерской. Оператор, знающий номер клиента, вносит его кнопкой — `POST /clients/{id}/identities`, `source='operator'`, `confidence='high'`, `verified_by_user_id`. Заодно у карточки «Собрано телефонов» впервые появится непустой столбец «вручную» (сегодня `source='manual'` не пишет никто).

**Источник хранится и показывается всегда** — `source` + `conversation_id`/`message_id`, в карточке строкой «из сообщения 12 авг · открыть».

---

### §4. Крайние случаи

**4.1 Нет телефона.** Клиент живёт на одном `avito_user_id` в области своего аккаунта. Всё работает как сегодня; «Повторный» считается в пределах канала. Никаких заглушек и пустых строк идентичностей.

**4.2 Один номер у двух людей** (семья, посредник, номер сервиса). Автослияние сработает — это неизбежная цена правила «телефон = главный ключ». Разбирается кнопкой «Разделить» (4.5); после неё пара попадает в `client_merge_blocks`, а второй экземпляр идентичности пишется погашенным (`active=false`), и карточка честно говорит: «номер общий с карточкой Петров И. — разделены вручную 12 августа».

**4.3 Клиент сменил номер.** Старая идентичность не удаляется и не гасится — у неё просто перестаёт двигаться `last_seen`. Оба номера ищутся, оба видны в карточке, главным (`clients.phone`) становится тот, у кого свежее `last_seen` при равной уверенности. Обратная опасность — оператор связи отдал старый номер другому человеку через год — снимается тем же «Разделить»; автоматики «забыть номер через N месяцев» не будет: она молча теряла бы историю постоянных клиентов, которые обращаются раз в два года.

**4.4 Один человек с двух аккаунтов.** Есть телефон → сливается автоматически (high). Телефона нет → пара «одинаковый avito_user_id, разные аккаунты» даёт medium: баннер в диалоге и строка в «Возможных дублях». Как только владелец проверит глобальность идентификатора живьём и включит `identity.avito_user_id_global`, такие пары начнут сливаться сами, а разовая команда `clients-backfill --merge-candidates` добьёт накопленные.

**4.5 Ошибочное слияние и «Разделить».** Кнопка в карточке и в журнале слияний. `POST /clients/{id}/split {merge_id}`:

```
split(merge_id, by_user):
    m = SELECT ... FOR UPDATE; if m.undone_at: 409 «уже разделено»
    # слияния откатываются только в обратном порядке: если поверх этого
    # легло второе, сначала откатывается оно
    if EXISTS позднее слияние с target_client_id = m.target_client_id
       и undone_at IS NULL: 409 «сначала откатите более позднее»
    UPDATE conversations     SET client_id = m.source_client_id
        WHERE id = ANY(payload.conversations)
    UPDATE client_identities SET client_id = m.source_client_id
        WHERE id = ANY(payload.identities)
    UPDATE client_notes      SET client_id = m.source_client_id WHERE …
    восстановить target из payload.target_before (только поля, которые
        слияние ДОПОЛНИЛО; правки человека после слияния не трогаем —
        если display_name менялся после merge, оставляем как есть и
        пишем это в details аудита)
    UPDATE clients SET merged_into_id = NULL WHERE id = m.source_client_id
    INSERT client_merge_blocks (упорядоченная пара, by_user, reason)
    погасить у обоих идентичности, которые теперь конфликтуют:
        одна остаётся active у того, кому её оставил человек, вторая
        пишется active=false
    UPDATE client_merges SET undone_at=now(), undone_by_user_id=by_user
    write_audit('client.split', …)
```

**4.6 Одновременный приход на два аккаунта.** Два воркера пишут идентичности; `uq_client_identities_key` пропускает одного, второй читает победителя. Клиентов может получиться два (разные области) — тогда каждый ставит задачу `merge_clients` с одной и той же упорядоченной парой и одним `_job_id=f"merge:{min}:{max}"`, так что выполнится она один раз. Внутри — `FOR UPDATE` по возрастанию id, значит взаимной блокировки нет. Повторный запуск уже выполненного слияния — no-op по `resolve_root`.

**4.7 Конфликт полей при слиянии.** Ничто не затирается:

| Поле | Правило |
|---|---|
| `display_name` | человеческое (`display_name`) выигрывает у автоматического (`name`); два человеческих — берётся у выжившего, второе уходит в `payload` и показывается в карточке строкой «также известен как» |
| `name` | у выжившего; пустое дополняется |
| `phone` | пересчитывается из идентичностей (самая свежая high) |
| `tags` | объединение множеств |
| `avito_rating` | у выжившего; NULL дополняется |
| `blocked_*` | **конфликт запрещает автослияние вовсе** (`mergeable`), при ручном — человек в диалоге подтверждения выбирает явно |
| `created_at` | минимальный из двух |
| заметки, идентичности, диалоги | переезжают все |

**4.8 Слияние клиента, у которого открытый диалог у другого оператора.** Диалоги не меняют ни ответственного, ни статуса, ни очереди — слияние трогает только `client_id`. Отдельно публикуется WS-кадр `client:merged`, чтобы открытые карточки перерисовались, а не показывали историю удалённого.

---

### §5. Интерфейс

**5.1 Карточка справа — блок «История клиента»** (секция и заголовок уже есть, `ClientCardPane.tsx:461`; меняется содержимое и появляется шапка):

```
┌ История клиента ────────────────────────────┐
│ 5 обращений · с 3 марта · каналы: Парт-7, Олег │
│                                              │
│ Холодильник Bosch  · Закрыт · 12 авг · Пётр  │
│ Стиральная Indesit · Закрыт · 3 мая  · Анна  │
│ …ещё 2                                       │
├ Телефоны ────────────────────────────────────┤
│ +7 900 111-22-41   из сообщения 12 авг  →    │
│ +7 900 111-22-42   внёс Пётр 3 мая      ✕    │
│ + добавить номер                             │
├ Теги клиента ────────────────────────────────┤
│ [постоянный] [юрлицо]  + тег                 │
├ Заметки о клиенте ───────────────────────────┤
│ Анна, 3 мая: звонить после 18:00              │
│ + заметка                                     │
└──────────────────────────────────────────────┘
```
Пустое состояние «Первое обращение клиента» остаётся — но теперь оно означает то, что написано.

Если у клиента есть кандидат уровня medium, над блоком встаёт баннер:
```
⚠ Похоже, это тот же клиент, что «Иван» (канал Олег):
  тот же идентификатор Авито, разные каналы.
  [Объединить]   [Это разные люди]
```

**5.2 Бейдж «Повторный» — до открытия диалога.** В строке списка чатов и в `/dialogs`. В список он приходит полем `client.repeat: bool` + `client.conversations_count: int`, посчитанным одним запросом на страницу (в `_load_related`, рядом с батч-загрузкой клиентов):

```sql
SELECT client_id, count(*) AS n, min(created_at) AS since
FROM conversations WHERE client_id = ANY(:ids) GROUP BY client_id
```
`repeat = n > 1`. Индекс `ix_conversations_client_last` покрывает.

Рисуется **не в слоте чипа** — он занят срочностью, и правило «один чип на строку» (`ConversationListItem.tsx`, docs/17 §Т8) ломать нельзя: иначе «не отправлено» будет исчезать под «повторный». Бейдж — маленький значок «↩» слева от имени клиента с подсказкой «5-е обращение, впервые 3 марта». В `/dialogs` — отдельная узкая колонка «Повт.» со значением `↩5`.

**5.3 Экран «Возможные дубли»** — `/settings/duplicates`, право `clients:merge` (admin, head):

```
Возможные дубли                                   [12 пар]

Иван Петров        ↔  Иван
5 обращений        ·    1 обращение
Парт-7             ·    Олег
совпало: идентификатор Авито 987654 на разных каналах
последнее: 12 авг  ·  3 авг
                          [Объединить]  [Это разные люди]  [Открыть обе]
```
«Это разные люди» пишет `client_merge_blocks` — пара исчезает навсегда, включая после будущего включения глобальности идентификаторов.

**5.4 Поиск по номеру находит клиента.** `_search_condition` перестаёт смотреть в `Client.phone` и уходит в идентичности:
```sql
EXISTS (SELECT 1 FROM client_identities ci
         WHERE ci.client_id = conversations.client_id AND ci.active
           AND ci.type='phone' AND ci.value_normalized LIKE '%'||:tail)
```
Плюс новая ручка `POST /api/v1/clients/search {query}` — телефон уходит **в теле**, а не в query-string (§7).

**5.5 Ручки** (все под `resolve_root`, ответ всегда про выжившего):

| Метод | Право |
|---|---|
| `GET /api/v1/clients/{id}` — карточка, счётчики, теги | `conversations:read` |
| `GET /api/v1/clients/{id}/conversations` — сквозная история, пагинация | `conversations:read` |
| `GET /api/v1/clients/{id}/identities` — полные значения | `clients:pii` |
| `PATCH /api/v1/clients/{id}` — `display_name`, `tags` | `conversations:manage` |
| `POST/DELETE /api/v1/clients/{id}/identities` | `conversations:manage` |
| `GET/POST /api/v1/clients/{id}/notes` | `notes:read` / `notes:write` |
| `GET /api/v1/clients/{id}/candidates` — для баннера | `conversations:read` |
| `GET /api/v1/clients/duplicates` | `clients:merge` |
| `POST /api/v1/clients/{id}/merge {other_id}` | `clients:merge` |
| `POST /api/v1/clients/{id}/split {merge_id}` | `clients:merge` |
| `POST /api/v1/clients/duplicates/dismiss {a,b}` | `clients:merge` |
| `POST /api/v1/clients/search {query}` | `conversations:read` |

Старая `GET /conversations/{id}/client-history` остаётся псевдонимом новой — на неё завязан фронт и внешних потребителей у неё нет, но ломать её в одном релизе с миграцией незачем.

---

### §6. Влияние на остальную систему

**6.1 Метрика «Повторные» чинится вместе с шагом 7 и независимо от него.** Сегодня значением карточки стоит `reopened` — переоткрытия ОДНОГО чата, а вернувшийся клиент Авито почти всегда пишет по новому объявлению и даёт новый чат и ноль переоткрытий. Отсюда «Повторные: 1 · было 0» при живом потоке. Меняем:

```sql
-- значение карточки: клиенты, у которых В ПЕРИОД началось обращение,
-- а раньше уже было хотя бы одно
SELECT count(DISTINCT s.client_id)
FROM mv_conversation_stats s
WHERE s.first_client_at >= :ts_from AND s.first_client_at < :ts_to
  {фильтры}
  AND EXISTS (SELECT 1 FROM mv_conversation_stats s2
               WHERE s2.client_id = s.client_id
                 AND s2.first_client_at < s.first_client_at)
```
Сравнение по `first_client_at` вместо `last_message_at` — прежнее сравнивалось с колонкой, которую поднимает любая заметка. `reopened` уезжает в подсказку, местами меняясь с `repeat_clients`.

**Честно про задержку:** MV обновляется раз в час, `client_id` в нём — снимок. Слитые час назад клиенты до обновления считаются двумя. Прятать это нельзя — в ответе `/stats` уже есть метка `refreshed_at`, к карточке добавляется подсказка «данные на 14:00».

**6.2 Распределение — «повторное обращение прежнему оператору».** Настройка `distribution.sticky_repeat`, bool, **по умолчанию false** (включение меняет то, как люди работают, — та же логика, что у самой автораздачи). В `pick_assignee` перед выбором наименее загруженного:

```
if sticky_repeat and client.repeat:
    prev = ответственный последнего ЗАКРЫТОГО диалога этого клиента
           (по conversations, ORDER BY last_message_at DESC LIMIT 1)
    if prev is not None and prev in доступные_кандидаты:   # онлайн, оператор
        return prev, reason='sticky_repeat'                # канала, не сверх предела
# иначе — обычный выбор наименее загруженного, очередь как страховка
```
Аудит `conversation.auto_assigned` получает `details.rule='sticky_repeat'`.

**6.3 Экспорт.** В CSV `/dialogs` (`conversation_table.py:277`) и в лист «Диалоги» выгрузки статистики (`stats.py:1455`) добавляются две колонки: `client_id` (UUID выжившего) и «Повторное» (да/нет). `client_id` в выгрузке — единственный способ сгруппировать строки по человеку в чужом Excel; без него сквозной клиент заканчивается на границе системы. Выгрузка по-прежнему пишет аудит `stats.exported`.

---

### §7. Персональные данные

* **Новое право `clients:pii`** в `app/core/rbac.py`: `admin`, `head`, `manager` — да; `observer` — нет. Сегодня `conversation_out` отдаёт `client.phone` всем, у кого есть `conversations:read`, то есть наблюдателю, которому не положены даже заметки. Это чинится здесь же.
* **Маскирование.** Без `clients:pii` в списках и карточке приходит `+7 900 ***-**-41`. Полное значение — только `GET /clients/{id}/identities`.
* **Журнал просмотров.** Ручка полных значений пишет аудит `client.pii_viewed` (`entity='client'`, `details={count}`). Дедупликация в Redis: одна запись на пару «сотрудник × клиент» в час — иначе журнал зальёт прокрутка списка, и в нём перестанут искать.
* **Срок хранения.** Настройка `pii.retention_days`, значение по умолчанию — **«не удалять»** (`None`). Задача планировщика написана и покрыта тестом, гасит `phone`-идентичности клиентов без обращений дольше срока, пишет аудит. Число ставит владелец, увидев отчёты своими глазами, — тот же порядок, что с рабочими часами в #41.
* **Никаких телефонов в URL.** Поиск по номеру переезжает на `POST /clients/search` (запрос в теле). Плюс — независимо — в `SECRET_KEYS` (`app/core/logging.py:18`) добавляется `q`, чтобы уже существующий `GET /conversations?q=` перестал утекать в логи и в Sentry, где сегодня он не затирается.

## Крайние случаи

| Случай | Поведение |
|---|---|
| У клиента нет телефона (написал «сколько стоит ремонт», ушёл) | Живёт на одной идентичности avito_user_id в области своего аккаунта. Всё как сегодня; «Повторный» считается в пределах канала. Заглушек и пустых строк не заводится. |
| Один номер у двух людей: муж и жена, посредник, номер управляющей компании | Автослияние сработает — это цена правила «телефон главный ключ». Человек жмёт «Разделить»: диалоги и идентичности возвращаются по снимку, пара уходит в client_merge_blocks, у проигравшего идентичность записывается погашенной (active=false), карточка показывает «номер общий с карточкой N — разделены вручную 12 августа». Повторно они не склеятся никогда. |
| Клиент сменил номер телефона | Старая идентичность не удаляется и не гасится, у неё перестаёт двигаться last_seen. Оба номера ищутся и видны в карточке; главным (clients.phone) становится свежайший из high. Автоматического «забывания» старых номеров нет: оно молча теряло бы историю клиентов, которые обращаются раз в два года. |
| Оператор связи отдал брошенный номер другому человеку | Автослияние сведёт двух посторонних. Ловится тем же «Разделить» + запрет пары. Автоматики по давности last_seen не вводим: она сработала бы против постоянных клиентов чаще, чем против перевыданных номеров. |
| Один человек пишет с двух наших аккаунтов, телефон известен | Совпадение телефона — уровень high, автослияние. Один клиент, одна карточка, история из обоих каналов, «Повторный» до открытия диалога. |
| Один человек пишет с двух наших аккаунтов, телефона нет, avito_user_id совпадает | Слияния НЕТ: документация глобальность идентификатора не гарантирует (в спецификации «Обратите внимание на хэширование» без пояснений). Пара показывается уровнем medium — баннер «похоже, тот же клиент» в диалоге и строка в «Возможных дублях». После живой проверки владелец включает настройку identity.avito_user_id_global, и такие пары начинают сливаться сами; накопленные добивает clients-backfill --merge-candidates. |
| Ошибочное слияние — надо разделить | client_merges.payload хранит снимок ровно того, что откат обязан вернуть: списки переехавших диалогов, идентичностей и заметок плюс поля выжившего до слияния. Split возвращает всё по снимку, ставит merged_into_id = NULL, заводит запрет пары и пишет аудит client.split. Правки человека, сделанные ПОСЛЕ слияния, не перетираются — они остаются и упоминаются в деталях события. |
| Разделить нужно слияние, поверх которого легло второе | 409 «сначала откатите более позднее». Откат вне порядка склеил бы снимки и вернул диалоги не туда — тихо и необратимо. |
| Конфликт полей при слиянии (два имени, два тега, два рейтинга) | Не затирается ничего. Человеческое display_name выигрывает у автоматического, второе показывается строкой «также известен как»; теги объединяются; NULL дополняются; created_at берётся минимальный; телефон пересчитывается из идентичностей. Всё исходное лежит в payload и возвращается откатом. |
| Один из двух клиентов в чёрном списке | Автослияния НЕТ вовсе (предикат mergeable). Иначе либо спамер вернётся в очередь тринадцати операторам, либо настоящий клиент оглохнет. Пара уходит в «Возможные дубли», и человек, глядя на обе карточки, выбирает состояние пометки явно. |
| Сообщения от одного клиента приходят на два аккаунта одновременно | Оба воркера пишут идентичность, уникальный частичный индекс пропускает одного, второй читает победителя. Если клиентов получилось два, оба ставят задачу merge_clients с одинаковым _job_id = merge:{min_id}:{max_id} — выполнится она однажды. Внутри блокировки FOR UPDATE берутся в порядке возрастания id, взаимной блокировки нет; повторный запуск слияния — no-op через resolve_root. |
| Слияние в момент, когда диалог клиента открыт у другого оператора | Диалоги не меняют ни ответственного, ни статуса, ни места в очереди — трогается только client_id. Публикуется WS-кадр client:merged, по которому открытые карточки перерисовываются, а не показывают историю исчезнувшего клиента. |
| Клиент пишет цену, номер заказа, IMEI или ссылку с длинным числом | Кандидат отбрасывается до нормализации: соседство с ₽/руб, 12+ цифр подряд, вхождение в URL, слова «заказ/артикул/серийный» рядом. Ложный телефон опаснее пропущенного: он склеит двух посторонних. |
| Оператор пишет клиенту номер компании; или вставляет чей-то номер в заметку | Не привязывается никогда. Захват принимает сообщение целиком и работает только при (direction, sender_type) == ('in','client'); заметки и сообщения бота отсекаются тем же условием. Свой номер оператор вносит явной кнопкой — source='operator', с именем внёсшего. |
| Метрика «Повторные» сразу после слияния | mv_conversation_stats обновляется раз в час и до обновления считает слитых двумя клиентами. Не прячем: у ответа /stats уже есть refreshed_at, к карточке добавляется подсказка «данные на 14:00». |
| Старая ссылка на клиента, который был слит (закладка, журнал, чужая вкладка) | Строка clients не удаляется никогда, у неё лишь появляется merged_into_id. Любое чтение проходит через resolve_root и отдаёт карточку выжившего, а не 404. |

## Миграция

## Миграция и бэкофилл

### Что делает миграция 0024
Только схема (§1): четыре новые таблицы, четыре колонки в `clients`, снятие `uq_clients_channel_external_id`, индексы. Данных она не трогает вовсе — ни одной строки. `DROP CONSTRAINT` и `CREATE INDEX` на `clients` (сегодня это десятки тысяч строк, не миллионы) занимают секунды; тяжёлые индексы (`ix_conversations_client_last` на 454 тыс. диалогов) создаются `CONCURRENTLY` отдельным шагом вне транзакции — приём в проекте уже принят в `0004`.

Порядок выкатки обязателен: **схема → код → бэкофилл**. После миграции и до бэкофилла система работает по-старому, потому что `_upsert_client` продолжает находить клиента по `(channel, external_id)` — новый резолвер включается тем же релизом кода, но идентичностей у старых клиентов ещё нет, и он честно заводит их на лету при первом сообщении. То есть **простоя нет и «дырки» между шагами нет**: даже если бэкофилл не запустить вовсе, система будет накапливать идентичности сама, просто история склеится не сразу.

### Бэкофилл: команда, а не автозапуск

```
leadchat clients-backfill [--dry-run] [--limit N] [--since ГГГГ-ММ-ДД]
                          [--rescan-messages] [--merge-candidates]
                          [--resume] [--undo-run RUN_ID]
```

Фазы, каждая идемпотентна сама по себе:

1. **avito_user_id.** Для каждой пары (клиент, аккаунт), встречающейся в `conversations`, пишется идентичность `type='avito_user_id'`, `scope=<uuid аккаунта>`, `value=clients.external_id`, `source='import'`, `confidence='high'`, `first_seen=min(created_at диалогов)`. `INSERT ... ON CONFLICT DO NOTHING` — повторный прогон не делает ничего.
2. **Телефоны из карточек.** Каждый непустой `clients.phone` → идентичность `type='phone'`, `scope='global'`, `source='import'`, `confidence='high'`. Конфликт по уникальному индексу означает «у двух клиентов один номер» — в dry-run это строка отчёта, в боевом прогоне — задача слияния.
3. **Слияния.** Выполняются той же функцией `merge()`, что и в бою (не отдельным «миграционным» кодом — второй реализацией одного правила мы бы гарантировали расхождение). Каждое пишет `client_merges` с `reason='backfill'` и общим `run_id` в `payload`.
4. **`--rescan-messages` (по требованию, не по умолчанию).** Перечитывает тексты входящих за период и достаёт номера, которых нет в карточках. Это единственная дорогая фаза: проход по партициям `messages` с `direction='in' AND sender_type='client'`, пачками по 5 000, курсор в `app_settings['identity.backfill_cursor']`, `--resume` продолжает с места остановки. Обязательно с `--since`: тащить регексом весь архив за годы ради номеров, которые уже никому не нужны, смысла нет.
5. **`--merge-candidates`.** Отдельная команда для дня, когда владелец проверит глобальность идентификаторов и включит `identity.avito_user_id_global`: превращает накопленные пары medium в слияния.

### Dry-run — не «то же самое, но без записи»
`--dry-run` открывает транзакцию, выполняет **все те же операции** и делает `ROLLBACK` в конце (плюс страховка: сессия помечена флагом, который запрещает `commit` на уровне службы слияния). Отчёт печатается в stdout и кладётся файлом:

```
Бэкофилл сквозного клиента — ПРОБНЫЙ ПРОГОН (записи не будет)

Клиентов всего                       12 480
  из них с телефоном                  3 902 (31%)
Идентичностей будет создано          16 382
  avito_user_id                       12 511
  phone                                3 871

Слияний по телефону                      64
  затронуто диалогов                    311
  из них клиенты на РАЗНЫХ каналах       12   ← ради этого всё и делается

Кандидатов medium (тот же avito_user_id на разных каналах)   9
  из них у 9 совпадает ещё и телефон  ← косвенное подтверждение
                                         глобальности идентификатора
Пар с конфликтом чёрного списка (сливать не будем)           1
Клиентов без единого идентификатора                          0

Пробный прогон окончен, изменений в базе нет.
```

Строка «из них у N совпадает ещё и телефон» — бесплатная эмпирическая проверка unknowns 1: если у всех пар с одинаковым `avito_user_id` на разных каналах совпал ещё и телефон, глобальность подтверждается данными, а не догадкой. Обратное (совпал id, телефоны разные) — прямое доказательство, что идентификаторы НЕ глобальны, и настройку включать нельзя.

### Обратимость
* Схема: `downgrade()` пишется (dev-only, по правилу 08 §1.4 в проде не применяется), но он и не нужен — новые таблицы пустые не мешают старому коду, а снятое `uq_clients_channel_external_id` восстанавливается только после отката слияний.
* Данные: **каждое** слияние (и боевое, и из бэкофилла) лежит в `client_merges` со снимком. `clients-backfill --undo-run RUN_ID` откатывает весь прогон в обратном хронологическом порядке той же функцией `split()`, что и кнопка в интерфейсе. Ни одна строка `clients` при слиянии не удаляется, поэтому откат всегда возможен физически.
* Худший случай — «идентификаторы оказались не глобальны, а мы уже включили настройку»: выключаем настройку, `--undo-run` по прогонам с `reason='avito_user_id'`, пары уходят в `client_merge_blocks`. Ни одного сообщения, диалога или телефона при этом не теряется.

### Порядок работ на проде
1. Ночью: миграция 0024 (`CONCURRENTLY`-индексы отдельным шагом).
2. Релиз кода. Настройки: `identity.avito_user_id_global=false`, `distribution.sticky_repeat=false`, `pii.retention_days=None` — то есть новое поведение включено только там, где оно безопасно.
3. `clients-backfill --dry-run` → отчёт читает владелец.
4. Боевой прогон фаз 1–3 (минуты на нынешнем объёме).
5. Через сутки — решение по `--rescan-messages` и по глобальности идентификаторов, исходя из строки-подсказки в отчёте и живой проверки владельца.
6. `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_conversation_stats` вне очереди, чтобы «Повторные» показали правду сразу, а не через час.

Проверять что-либо из этого на встроенном имитаторе запрещено: он написан по нашим догадкам и уже дважды подтверждал наши же ошибки (`docs/26`). Всё, что касается формы данных Авито, проверяется только на боевых аккаунтах.

## Встречная критика

# Разбор ТЗ «system-msgs»

Проверил по коду. Ниже: что принять, что переделать, что выбросить, и честная оценка.

Общий вердикт: **архитектурная идея верная и подтверждается кодом, но реализация в текущем виде сломает прод на трёх местах, а два «подтверждённых» признака Авито на самом деле угаданы.** Оценка в 40 часов занижена в 2,5–3 раза для описанного объёма.

---

## 1. ПРИНЯТЬ КАК ЕСТЬ (проверено по коду, работает)

**1.1. Ключевое решение «direction='system' + sender_type='avito' → метрики отваливаются сами» — верно.** Перепроверил все двенадцать потребителей, ни один не увидит такое сообщение:

| Место | Предикат | Отвалится? |
|---|---|---|
| MV `mv_conversation_stats` (`0004_stats_indexes_and_mv.py:118-121`) | `direction='in' AND sender_type='client'` | да |
| `_FRT_LIVE_SQL` (`app/services/stats.py:352-356`) | то же | да |
| `conversation_table.py:326-333` `first_client` | **только** `direction='in'` | да (по direction) |
| `conversation_table.py:346-356` `messages_count` | `direction in ('in','out')` | да |
| `restore_awaiting` (`app/services/messages.py:612-620`) | `direction='in'` | да |
| `read_markers.py:183` | `direction='in'` | да |
| превью списка `conversations.py:596` | `direction IN ('in','out')` | да |
| `notify.ts:68` звук | `direction==='in' && sender_type==='client'` | да |
| `MessageBubble.tsx:9-14` `kindOf` | `direction==='system'` → серый чип | **рисуется правильно без единой правки фронта** |

Автор проверил это добросовестно. Единственная поправка — в §7 (см. 2.10).

**1.2. Шаг 2 «наблюдение» и перепись по `webhook_raw_log` — лучшая часть документа.** Проверил формат хранения: `app/workers/inbound.py:230` кладёт конверт как есть, значит `payload->'payload'->'value'->>'type'` в SQL корректен. Это единственная часть ТЗ, которая честно добывает контракт вместо угадывания.

**1.3. `avito_source_type` как наблюдательный журнал** — принять. Дёшево, обратимо, отвечает на вопрос, на который сейчас никто ответить не может.

**1.4. Реестр текущего состояния (§1–§6)** — сверил выборочно, факты верны. Три копии `add_system_message` подтверждаю: `app/services/conversations.py:762`, `app/bots/handoff.py:122`, руками в `app/services/queue_cleanup.py:194`. CHECK на `direction`/`sender_type` действительно нет (`0001_init.py:207-222`). `value["type"]` действительно не читается (`adapter.py:363-381`). Мелкая неточность: `flow_id` не «читается и выбрасывается», а явно исключён из вложений в `_extract_attachments` (`adapter.py:241-243`) — эффект тот же, формулировку поправить.

**1.5. `ADD CONSTRAINT ... NOT VALID` + `VALIDATE` на партиционированной таблице** — я это проверил запуском PG 16 (та же версия, что в `docker-compose.prod.yml:152`): работает, констрейнт садится и на родителя, и на партицию. Технически верно. (Но см. 3.1 — при текущем объёме это лишнее.)

**1.6. Сторож §7 как приём** — принять, идея правильная. Реализацию поправить (2.10).

---

## 2. ПЕРЕДЕЛАТЬ

### 2.1. ГЛАВНОЕ: два правила включены по умолчанию на угаданной семантике

Это ровно тот дефект, который ищут в первую очередь.

**`flow_id_is_system: true`.** `docs/26 §3` подтверждает только, что `flow_id` — строка в `MessageContent`, а не вложение, с описанием «сообщение написал чат-бот Авито». Это описание поля из зеркала OpenAPI — источник ровно того же класса, что дважды дал 4xx (v2 вместо v3, 403 вместо 401). Из «написал чат-бот» **не следует** «служебное и не содержит обращения клиента»: сценарий бота Авито может собирать заявку и передавать текст клиента. Правило, включённое по умолчанию, тихо унесёт такие обращения в свёрнутую группу без звука, без очереди, без ожидания — то есть ровно тот исход, который сам ТЗ в edge-case №3 называет «самым дорогим». И это прямо противоречит §миграция шага 2 («правила пишутся ПОСЛЕ данных»).

**`call_is_system: true` — хуже.** Каталог подтверждает форму `call.status`, но «звонок = служебное» — это управленческое решение, а не факт API, и для компании по ремонту техники оно, вероятно, неверное: звонок это самый сильный сигнал лида. Под предложением уведомление о звонке перестанет: растить `unread_count`, звенеть, ставить `awaiting_since`, попадать в очередь, считаться в `msgs_in`/`first_client_at`. Диалог, где звонок был единственным входящим, исчезнет из статистики целиком (edge-case №8 это признаёт и подаёт как «правильно» — не факт, что владелец согласится).

**Что сделать:**
- `flow_id_is_system` и `call_is_system` — **`false` по умолчанию**. Тогда релиз шага 2 действительно ничего не меняет, как и обещано.
- В перепись §миграции добавить колонку с текстом: `min(left(...->'content'->>'text', 120)) FILTER (WHERE content ? 'flow_id')` — чтобы владелец увидел, что именно пишет бот Авито, прежде чем включать.
- Звонок — **отдельный вид** (`event_call`), а не `system_avito`, и по умолчанию он ведёт себя как сегодня (звенит, встаёт в очередь). Решение «звонок тихий» вынести в отдельную задачу с отдельным согласием владельца.
- Шаблоны `text_rules` с выдуманным русским текстом (`^Объявление снято с публикации`, `^(Вам позвонил|Пропущенный звонок)`, `^Статус объявления`, `^Сообщение удалено`) убрать в пустые строки, как у остальных четырёх. Правило с правдоподобным шаблоном и `enabled: false` — это мина: в три часа ночи его включат, не проверив. Плюс `^` не спасает: клиент может написать «Объявление снято с публикации?» как вопрос.

### 2.2. `_upsert_client` пропустить нельзя — противоречие внутри ТЗ

`app/models/conversation.py:37`: `client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("clients.id"), nullable=False)`.

Edge-case №1 говорит «диалога нет → создаём closed». Edge-case №2 говорит «`_upsert_client` НЕ вызывается». Оба сразу невозможны: без клиента диалог не вставить.

**Что сделать — выбрать одно и записать явно:**
- (а) служебное сообщение в НЕИЗВЕСТНЫЙ чат — **не создаём ничего**, сырец уже лежит в `webhook_raw_log`, выходим. Это честнее и дешевле: фантомного клиента и фантомного диалога не появляется вовсе, а когда клиент напишет сам — сработает обычное создание.
- (б) создаём клиента как сегодня и мирится с «клиентом 0» в справочнике.

Рекомендую (а). Но выбор должен быть в ТЗ, а не два взаимоисключающих абзаца.

### 2.3. Новая вставка теряет идемпотентность вебхука

Псевдокод §4 говорит «ВСТАВИТЬ Message(...)». Существующий путь — `_insert_message_idempotent` (`app/services/inbound.py:191-219`) с `ON CONFLICT DO NOTHING` по `uq_messages_conversation_external_created`. Авито ретраит вебхуки, и `reconciliation` (`app/workers/reconciliation.py:186`) прогоняет историю через ту же функцию повторно. Без `ON CONFLICT` получим дубли служебных сообщений и повторный `conv.updated_at = now` на каждом ретрае.

**Что сделать:** ранний выход обязан идти через `_insert_message_idempotent` с параметрами вида, и возвращать `False` при конфликте.

### 2.4. `kind NOT NULL` ломает не три места вставки, а восемь

Message-строки конструируются в:

```
app/services/inbound.py:219            in/client
app/services/messages.py:393           out/operator
app/services/messages.py:528           note/operator
app/services/conversations.py:769      system/system
app/bots/handoff.py:79                 (_new_message: out|note/bot)
app/bots/sandbox.py:271                песочница бота
app/services/queue_cleanup.py:194      system/system
app/services/avito_accounts.py:636     _insert_history_message — in/client и out/operator
```

Последний — критичный: **это четвёртый путь записи входящих клиентских сообщений, и классификатор в него не заведён вовсе.** Импорт истории при подключении аккаунта (`_backfill_chat`) продолжит создавать `direction='in', sender_type='client'` для служебных сообщений Авито, и они честно попадут в `first_client_at` MV и в `messages_count`. ТЗ упоминает только, что `normalize_history_message` начнёт сохранять `value.type` — этого мало.

**Что сделать:** перечислить все восемь мест в ТЗ; завести классификатор и в `_insert_history_message`; либо (проще) дать колонке `server_default 'client'` и признать, что вид у исторических строк ставится бэкофиллом, а не вставкой.

### 2.5. `downgrade` НЕ честный после шага 3

ТЗ: «`downgrade`: DROP COLUMN kind, avito_source_type, kind_rule; DROP TABLE messages_kind_backup. Данные не теряются — вид выводится из direction/sender_type».

Это верно **только до** `reclassify --apply`. После него у переклассифицированных строк `direction='system', sender_type='avito'` — значения, которых нет ни в одном предикате системы. `downgrade` уронит и колонку `kind`, и таблицу с прежними значениями, и восстановить их будет **нечем**: сообщения клиента навсегда останутся невидимыми для статистики, ленты, превью и поиска непрочитанного. Это самая опасная строка во всём документе.

**Что сделать:** `downgrade` обязан либо сначала откатить переклассификацию из бэкапа, либо падать с внятной ошибкой, если в таблице есть хоть одна строка `sender_type='avito'`. Прописать явно.

### 2.6. Повторный прогон бэкфилла и откат: PK бэкапа даёт крах на второй прогон

- Бэкофилл миграции (`WHERE kind IS NULL`) идемпотентен — тут ТЗ прав.
- `reclassify --apply` дважды подряд идемпотентен (кандидаты `kind='client'`, уже переведённые не попадают) — тоже верно.
- А вот **apply → rollback → apply** падает: `PRIMARY KEY (message_id, created_at)` в `messages_kind_backup` не даёт записать то же сообщение во второй batch. Уникальная ошибка посреди прогона, половина обновлена, половина нет.

**Что сделать:** PK `(batch_id, message_id, created_at)`. Или (лучше) — выбросить таблицу целиком, см. 3.2.

### 2.7. Поиск сырца по `external_message_id` — сканирование на каждое сообщение

`app/models/webhook_raw.py:20-24`: индексы только `stream_id`, `received_at`, `(account_id, received_at)`. Найти сырец по id сообщения можно только полным сканом JSONB. В псевдокоде §3 это делается **на каждое сообщение** внутри цикла по страницам по 5000.

**Что сделать:** одним проходом собрать словарь `{value.id → payload}` (`SELECT payload->'payload'->'value'->>'id', payload FROM webhook_raw_log WHERE received_at >= :since`), либо завести выражение-индекс. Иначе прогон на боевом встанет намертво и займёт таблицу.

### 2.8. `restore_awaiting` не умеет того, что от неё требуют

ТЗ: «ПЕРЕСЧИТАТЬ `awaiting_since` (restore_awaiting — она уже умеет считать отметку из переписки)».

Не умеет. `app/services/messages.py:624-627`:
```python
if conv.awaiting_since is not None and conv.awaiting_since <= since:
    return False
```
Функция только **сдвигает отметку назад** и никогда её не снимает. После переклассификации у диалога, где единственное `in` было служебным, правильный результат — `awaiting_since = NULL`. `restore_awaiting` оставит протухшую отметку, и `check_awaiting` (`app/scheduler/jobs/awaiting.py:153`) будет вечно писать «клиент ждёт 3 дня» про диалог, где клиента не было.

**Что сделать:** отдельный пересчёт, который умеет ставить NULL, и отдельный тест на него.

### 2.9. `REFRESH MATERIALIZED VIEW CONCURRENTLY` вне транзакции

В псевдокоде §3 он стоит внутри общего потока. `CONCURRENTLY` нельзя выполнять в транзакционном блоке. Мелочь, но это ровно та мелочь, которая падает на боевом после часового прогона.

### 2.10. Мутационный тест §7 доказывает меньше, чем обещает

«Тест обязан падать, если убрать `sender_type='avito'`». Но `conversation_table.first_client`, `messages_count`, `restore_awaiting` и `read_markers` фильтруют **только по `direction`** — уберите `sender_type='avito'`, оставив `direction='system'`, и эти четыре проверки останутся зелёными.

**Что сделать:** две мутации, обе обязаны валить тест: (1) `sender_type` → `'client'`, (2) `direction` → `'in'`.

### 2.11. Блокировка `messages` при накате

`ALTER TABLE messages ADD COLUMN` берёт ACCESS EXCLUSIVE на родителя и все партиции. `app/scheduler/partitions.py:98` (`CREATE TABLE ... PARTITION OF messages`) берёт тяжёлый лок на того же родителя — в самом файле это описано. Если ALTER встанет в очередь за долгой транзакцией, **все INSERT в `messages` встанут за ним**, конвейер вебхуков заткнётся, стрим в Redis начнёт расти.

**Что сделать:** в начале миграции `SET lock_timeout = '3s'`, накат с остановленными ARQ-воркерами, порядок в `docs/RUNBOOK-DEPLOY.md`.

### 2.12. `system_avito` в списке диалогов уронит кэш у всех тринадцати

`applyWsEvent.ts:146-151`: если строка диалога не найдена в кэше (`!found`), делается `invalidateQueries` по всему корню conversations. Служебное сообщение в закрытый/старый диалог, которого ни у кого в списке нет, вызовет полный рефетч списков у всех операторов — на каждое такое сообщение.

**Что сделать:** либо не публиковать `message:new` для `system_avito` в диалог, который никем не открыт, либо пропускать ветку `invalidateQueries` при `msg.kind === 'system_avito'`. Заодно: правку `applyWsEvent.ts:141-142` (безусловная перезапись `last_message`) ТЗ нашёл верно — принять.

### 2.13. Пропущенные крайние случаи

1. **Клиент ответил боту Авито** — его реплика может нести `flow_id`. Самая дорогая ошибка классификации, и её в unknowns нет вовсе. Добавить.
2. **Пагинация ленты против свёртки.** `list_messages` отдаёт 50 строк на страницу (`app/services/conversations.py:1112+`). Если 45 из них служебные и свёрнуты, оператор открывает диалог и видит три пузыря и полоску «45 системных». Прокрутка вверх грузит страницу, которая может оказаться служебной целиком. Свёртка и курсорная пагинация в ТЗ не сведены.
3. **Служебное сообщение с вложением.** `_extract_attachments` отработает и на нём (уведомление о звонке = `content.call` → вложение «Звонок»). Макет плашки вложений не предусматривает — что рисуем?
4. **Правила поменяли посреди прогона `reclassify`.** Версию правил надо зафиксировать в `audit_log` вместе с batch.
5. **Полнотекстовый поиск.** Колонка `search` — GENERATED, служебные сообщения в неё попадают и будут находиться поиском по ленте. Не сломано, но должно быть решено явно.
6. **Телефон из уведомления о звонке.** ТЗ решает «не извлекать». Если перепись покажет настоящий номер — это потерянный лид, а не безопасность. Оставить как решение владельца, а не как умолчание кода.

---

## 3. ВЫБРОСИТЬ (смысл не теряется)

**3.1. Пляску `NOT VALID` + `VALIDATE` + `SET NOT NULL`.** Работает (проверил на PG 16), но таблице два дня от роду и два аккаунта. Один `ALTER TABLE messages ADD COLUMN kind text NOT NULL DEFAULT 'client'` + `DROP DEFAULT` решает то же за долю секунды. Экономия: три оператора DDL и один абзац объяснений. Условие «если больше пары миллионов строк — дробим» оставить как есть, но сначала выполнить `SELECT count(*)` — это одна команда, а не повод писать ветвление в ТЗ.

**3.2. Таблицу `messages_kind_backup` целиком.** Переклассификация ходит **только** в одну сторону: `client/in/client → system_avito/system/avito`. Прежние значения — константа, хранить их незачем. Замена: колонка `kind_batch_id uuid`, откат = один UPDATE:
```sql
UPDATE messages SET kind='client', direction='in', sender_type='client',
       kind_rule=NULL, kind_batch_id=NULL
WHERE kind_batch_id = :batch;
```
Уходит: таблица, PK, индекс, запись бэкапа в горячем цикле, баг из 2.6 и половина риска из 2.5. **Минус ~4 часа и один класс отказа.**

**3.3. `ix_messages_avito_source_type`.** Частичный по `avito_source_type IS NOT NULL` — это *каждое* сообщение из Авито. Индекс ради одного админского запроса раз в неделю по таблице в тысячи строк. Seq scan справится. Выбросить.

**3.4. `system_unseen: int` в списке диалогов.** Коррелированный подзапрос по `messages` на каждую из строк **горячей** ручки, которую тринадцать человек дёргают весь день, ради «тихой пометки без бейджа», которую никто не просил. Выбросить.

**3.5. `system_avito_count` и `has_system` в /dialogs + 12-я колонка CSV.** «Сколько служебных сообщений Авито в диалоге» — не управленческий вопрос. Плюс изменение `CSV_HEADER` ломает готовые таблицы у заказчика. Выбросить целиком.

**3.6. Query-параметр `kinds=` у ленты.** Сам ТЗ пишет «по умолчанию отдаются ВСЕ: свёртка — дело интерфейса». Значит потребителя у параметра нет. Мёртвая ручка. Выбросить.

**3.7. `kind_label` с сервера.** Подпись плашки — текст интерфейса. Сервер, отдающий строки для отрисовки, — это связка, которую потом не развязать. Фронт мапит `kind → подпись`. Выбросить.

**3.8. Свёртку групп + переключатель «☑ Системные» + хранение состояния — отложить во вторую задачу.** `MessageBubble.tsx:9-14` уже рисует `direction==='system'` серым чипом по центру: правильное отображение получается **без единой строки фронта**. Свёртка решает проблему «служебных много», которую никто ещё не измерил — а перепись из шага 2 как раз и скажет, много ли их. Это самое крупное сокращение объёма: уходит групповое состояние, стор, персистентность, кнопка в шапке и вся возня с пагинацией из 2.13.2. **Минус ~12 часов.**

**3.9. Экран «Настройки → Авито → Системные сообщения» с предпросмотром по каждому правилу.** Самая дорогая часть документа. Его откроет один человек два раза в жизни; тринадцать диспетчеров не увидят его никогда. Ручку `preview` (она дешёвая, переиспользует `classify`) — оставить. Экран — заменить на текстовое поле с JSON и кнопкой «Проверить», выводящей таблицу «правило → сколько поймает → 10 примеров». Требование «нельзя включить, не увидев цифру» перенести в валидацию PATCH (правило с `enabled: true` и без свежего preview-токена → 400) либо просто в дисциплину одного администратора. **Минус ~10 часов.**

**3.10. Дублирование `POST /api/v1/maintenance/reclassify` и CLI.** Выбрать одно. CLI (`app/cli.py` уже есть) — прогон длинный, HTTP-ручка для него всё равно потребует фоновой задачи и опроса. Заодно не придётся заводить новый роутер `maintenance` и решать, что такое право `admin` (в `app/core/rbac.py:33` это роль с полным набором прав, отдельного разрешения нет — ТЗ тут неточен). `GET /maintenance/avito-source-types` заменить на строку в `docs/` с готовым SQL.

---

## 4. ЧЕСТНОСТЬ ОЦЕНКИ

**40 часов — не хватает примерно в 2,5–3 раза для описанного объёма.** Что в оценку явно не заложено:

| Работа | Часы |
|---|---|
| Миграция + правка **восьми** мест вставки + ORM + CHECK в модели (иначе SQLite-юниты не ловят мусор) | 8 |
| `classify.py` + таблица примеров + тесты | 5 |
| `app_settings`: новый `kind="json"`, схема, компиляция регулярок, аудит-диф (сейчас там только `bool/int_or_null/days/hour`, `app_settings.py:79-86`) | 5 |
| Адаптер: два поля в `InboundEvent` (`slots=True` dataclass) + оба парсера + тесты | 4 |
| `inbound.py`: ранний выход через идемпотентную вставку + WS + тесты | 6 |
| Сведение трёх `add_system_message` в одну | 3 |
| Ручки ленты/списка + предикат превью | 5 |
| Ручки настроек + preview | 6 |
| **CLI `reclassify`: батчи по партициям, откат, пересчёт `awaiting_since`, MV, аудит** | 12 |
| **Отчёт «было → станет»: прогон `_FRT_LIVE_SQL` по теневой классификации без записи** — самая недооценённая часть, готового механизма нет | 10 |
| Сторож §7 (нужен PG: MV + live SQL + две мутации) | 6 |
| Фронт: чип, типы, `applyWsEvent`, свёртка, переключатель, стор, /dialogs, экран настроек | 20 |
| Прогон наблюдения, перепись, оформление, RUNBOOK | 6 |
| **Итого как написано** | **~96** |

**После сокращений из §3** (без бэкап-таблицы, без свёртки и переключателя, без экрана настроек, без /dialogs и CSV, без `system_unseen`, `kinds=`, `kind_label`, без HTTP-ручек maintenance):

- **Задача A — «наблюдение», выкатывать сразу: 8 часов.** Адаптер сохраняет `value.type`; миграция добавляет `kind` + `avito_source_type` + `kind_rule` с бэкофиллом по паре колонок; SQL-перепись в `docs/`. Ничего в поведении не меняется, откат — тривиальный `DROP COLUMN` (переклассификации ещё не было, значит проблема 2.5 не возникает).
- **Задача B — «классификация», писать ПОСЛЕ переписи: 34–40 часов.** Классификатор, настройка, ранний выход, `reclassify` с отчётом, сторож, чип на фронте.

То есть **40 часов — честная цифра для второй половины, если первую отделить, а лишнее выбросить.** Для документа как он написан — нет.

---

## 5. ГЛАВНОЕ, ЧТО НАДО ПОМЕНЯТЬ В СТРУКТУРЕ ЗАДАНИЯ

ТЗ само доказывает, что правила надо писать после данных (§миграция, шаг 2) — и само же включает два правила до данных (§3, `flow_id_is_system: true`, `call_is_system: true`) и приносит восемь готовых русских шаблонов. **Разбить на две задачи по границе шага 2** — и противоречие исчезает само, а первая задача уезжает на прод сегодня и начинает собирать настоящий словарь Авито вместо предположений о нём.
