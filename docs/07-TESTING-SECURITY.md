# 07 — Тестирование и безопасность

> Документ детализирует раздел 9 DESIGN.md и пункт «Нагрузочная проверка» этапа 6.
> Стек тестов: **pytest + pytest-asyncio** (бэкенд), **testcontainers** (интеграция),
> **Playwright** (e2e), **k6** (нагрузка). Всё гоняется в GitHub Actions (см. 7.7).

Ориентировочная структура каталогов, на которую ссылается документ:

```
app/               # код бэкенда — в корне репозитория (08 §1.1)
tests/             # рядом с app/, в корне (08 §1.1)
  unit/            # без внешних сервисов; Redis — fakeredis, HTTP — respx
  integration/     # testcontainers: postgres:16 + redis:7
  smoke/           # прогон против живого стенда/прода (раздел 6)
  fixtures/
    avito_webhooks/   # сырые payload'ы вебхуков (JSON)
    scenarios/        # сценарии ботов (JSONB из bots.scenario)
frontend/
  e2e/               # Playwright
tools/
  fake-avito/        # мок Авито (раздел 2)
load/
  k6/                # нагрузочные сценарии (раздел 3)
```

---

## 1. Пирамида тестов

Целевое соотношение: **много unit → десятки integration → ~10 e2e**. Правило: логика,
которую можно проверить без Postgres/Redis, проверяется в unit; всё, что касается
транзакций, идемпотентности и очередей — в integration; e2e подтверждает только
критические пользовательские пути.

### 1.1. Unit (pytest + pytest-asyncio)

Запуск: `pytest tests/unit -q`. Внешних сервисов нет: Redis — `fakeredis.aioredis`,
HTTP к Авито — `respx` (мокирование httpx), время — `time_machine`.

`tests/unit/conftest.py` (общая база):

```python
import fakeredis.aioredis
import pytest

@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()

@pytest.fixture
def account(db_stub):
    """avito_accounts-стаб: id, avito_user_id, зашифрованные токены, webhook_secret."""
    return make_account(avito_user_id=111222333, status="active")
```

#### 1.1.1. Движок ботов на сценариях-фикстурах

Сценарии — те же JSONB, что лежат в `bots.scenario` (типы шагов из раздела 4.1 DESIGN.md).
Каждый сценарий — файл в `tests/fixtures/scenarios/`, движок исполняется в памяти,
`ai_answer` замокан детерминированной заглушкой.

Обязательный набор кейсов:

| Кейс | Фикстура | Проверяем |
|---|---|---|
| Первичный приём, дневной путь | `primary_intake.json` | `send` → `ask(problem)` → `ai_answer(conf=0.9)` → `condition(рабочее время=ДА)` → `handoff`; `conversations.bot_active=false`, причина handoff записана |
| Первичный приём, ночь | `primary_intake.json` + `time_machine("2026-08-04 22:30 MSK")` | ветка НЕТ: `ask(phone)` с валидацией «похоже на телефон», `tag`, handoff в очередь |
| Клиент просит человека | любой сценарий | «позовите оператора» на любом шаге → немедленный handoff (правило 4.3-1) |
| Низкая уверенность AI | `ai_answer` мок возвращает `{confidence: 0.2, needs_operator: true}` | handoff без отправки ответа клиенту (4.3-2) |
| Негатив | классификатор-мок → `negative` | handoff + тег «негатив» (4.3-3) |
| 2+ сообщения мимо сценария | `menu` c вариантами, клиент дважды отвечает не по ним | handoff (4.3-4) |
| Вмешательство оператора | во время `ask` приходит исходящее `sender_type=operator` | бот замолкает: следующее входящее не порождает `bot_step` (4.3-6) |
| Расписание | `schedule={"always": false, "timezone": "Europe/Moscow", "intervals": [{"days": ["mon","tue","wed","thu","fri","sat","sun"], "start": "20:00", "end": "10:00"}]}` (формат 02 §2.5) | в 15:00 бот не стартует, в 21:00 стартует |
| Переменные | `send` c `{client_name}`, `{item_title}` (словарь 02 §1.2) | подстановка из `clients.name` / `conversations.item_title` |
| Недоступность AI | `ai_answer` мок кидает timeout | бот не падает и не молчит — handoff (раздел 4.5 DESIGN.md) |

```python
# tests/unit/test_bot_engine.py
import json, pathlib, pytest
from app.bots.engine import BotEngine

SCEN = pathlib.Path(__file__).parent.parent / "fixtures" / "scenarios"

@pytest.mark.asyncio
async def test_primary_intake_night_collects_phone(time_machine, conv, fake_ai):
    time_machine.move_to("2026-08-04T22:30:00+03:00")
    engine = BotEngine(scenario=json.loads((SCEN / "primary_intake.json").read_text()),
                       ai=fake_ai(confidence=0.9))
    out = await engine.feed(conv, "Разбил экран iPhone 13")
    assert "Оставьте телефон" in out.replies[-1]
    out = await engine.feed(conv, "+7 916 123-45-67")
    assert conv.bot_vars["phone"] == "+79161234567"
    assert out.handoff and out.handoff.reason == "scenario"   # реестр reason — 02 §4
```

#### 1.1.2. Парсер вебхуков (`AvitoAdapter.parse_webhook`)

Фикстуры — реальные сырые payload'ы из лог-таблицы (риск №2 DESIGN.md: формат меняется,
поэтому копим фикстуры из прода). Минимальный набор в `tests/fixtures/avito_webhooks/`:

- `text_message.json` — обычный текст → корректные `chat_id / message_id / author_id / text / item_info / created_at`;
- `image_message.json` — вложение → `attachments` заполнен, `text` может быть пустым;
- `own_echo.json` — `author_id == avito_user_id` аккаунта → воркер должен распознать эхо (сам парсер отдаёт событие, фильтр — в воркере; тестируем оба уровня);
- `system_event.json` — системное событие Авито (не сообщение) → `InboundEvent(kind="system")`, не падает;
- `unknown_schema.json` — неизвестный/усечённый формат → контролируемый `WebhookParseError`, а не `KeyError` (воркер отправит в лог-таблицу и ack'нет);
- `no_item.json` — чат без объявления → `item_info is None`, диалог создаётся без `item_*`.

```python
@pytest.mark.parametrize("fname", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_parser_never_raises_uncontrolled(fname):
    raw = json.loads(fname.read_text())
    try:
        AvitoAdapter().parse_webhook(raw)
    except WebhookParseError:
        pass          # допустимый контролируемый исход
```

#### 1.1.3. RBAC-матрица: каждый endpoint × каждая роль

Один параметризованный тест — исполняемая копия таблицы 5.1 DESIGN.md. Приложение
поднимается через `httpx.AsyncClient(app=...)` с подменёнными зависимостями БД;
для каждой роли выпускается тестовый JWT. **Любой новый endpoint обязан быть добавлен
в матрицу — это проверяет тест-страж** (см. ниже).

```python
# tests/unit/test_rbac_matrix.py
A, H, M, O = "admin", "head", "manager", "observer"
ALLOW, DENY = 200, 403      # для наблюдателя на write-ручках ожидаем ровно 403, не 401

MATRIX = [
    # (method, path — фактические пути 01 §13,            {роль: ожидание})
    ("GET",   "/api/v1/conversations",                    {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    ("GET",   "/api/v1/conversations/{conv}",             {A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}),
    ("POST",  "/api/v1/conversations/{conv}/messages",    {A: ALLOW, H: DENY,  M: ALLOW, O: DENY}),   # head — только чтение
    ("POST",  "/api/v1/conversations/{conv}/notes",       {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
    ("PATCH", "/api/v1/conversations/{conv}/status",      {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
    ("POST",  "/api/v1/conversations/{conv}/assign",      {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
    ("GET",   "/api/v1/stats/managers",                   {A: ALLOW, H: ALLOW, M: DENY,  O: DENY}),   # /stats/* менеджеру — 403 (01 §9)
    ("GET",   "/api/v1/stats/my/today",                   {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),
    ("POST",  "/api/v1/templates",                        {A: ALLOW, H: ALLOW, M: ALLOW, O: DENY}),   # личный; M — только owner_id=self, общий — отдельный тест (ниже)
    ("POST",  "/api/v1/bots",                             {A: ALLOW, H: DENY,  M: DENY,  O: DENY}),
    ("GET",   "/api/v1/avito/connect-url",                {A: ALLOW, H: DENY,  M: DENY,  O: DENY}),
    ("GET",   "/api/v1/avito-accounts",                   {A: ALLOW, H: ALLOW, M: DENY,  O: DENY}),
    ("POST",  "/api/v1/users",                            {A: ALLOW, H: DENY,  M: DENY,  O: DENY}),
    ("GET",   "/api/v1/audit-log",                        {A: ALLOW, H: ALLOW, M: DENY,  O: DENY}),
]

CASES = [(m, p, role, exp) for m, p, perms in MATRIX for role, exp in perms.items()]

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,role,expected", CASES,
                         ids=lambda v: str(v))
async def test_rbac(client, tokens, seed, method, path, role, expected):
    r = await client.request(method, path.format(conv=seed.conv_id),
                             headers={"Authorization": f"Bearer {tokens[role]}"},
                             json=SAMPLE_BODY.get((method, path)))
    if expected == ALLOW:
        assert r.status_code < 400, r.text
    else:
        assert r.status_code == 403, r.text

def test_every_endpoint_is_in_matrix(app):
    """Страж: endpoint без строки в MATRIX = красный CI."""
    covered = {(m, p) for m, p, _ in MATRIX}
    exempt = {("GET", "/api/health"), ("POST", "/api/v1/auth/login"),
              ("POST", "/api/v1/auth/refresh"), ("POST", "/api/hooks/avito/{account_id}"),
              ("GET", "/api/v1/avito/callback")}
    for route in app.routes:
        for m in route.methods - {"HEAD", "OPTIONS"}:
            assert (m, route.path) in covered | exempt, \
                f"{m} {route.path} не покрыт RBAC-матрицей"
```

Дополнительно (не параметризацией, отдельными тестами): общий шаблон — это
`POST /api/v1/templates` с `shared: true` в теле (право `templates:shared`, 01 §7.3):
head/admin — 2xx, менеджер — 403 (различие в теле, а не в пути, поэтому не в MATRIX);
менеджер не может редактировать
чужой личный шаблон (`owner_id != self` → 403); наблюдателю по DESIGN.md (5.2 «без заметок»)
сообщения `direction='note'` не отдаются вовсе — сериализатор диалога обязан фильтровать их
для роли observer, и это фиксируется отдельным тестом (иначе поведение молча «уедет» при
рефакторинге сериализаторов).

#### 1.1.4. Refresh-lock токенов Авито

Критичный инвариант (раздел 1.5 DESIGN.md): refresh_token одноразовый, конкурентный
двойной refresh сжигает аккаунт. Тестируем `refresh_tokens()` из 8.1:

```python
# tests/unit/test_token_refresh_lock.py
import asyncio, respx, httpx

@pytest.mark.asyncio
@respx.mock
async def test_concurrent_refresh_hits_token_endpoint_once(account, db, redis):
    route = respx.post("https://api.avito.ru/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "new-a", "refresh_token": "new-r", "expires_in": 86400}))
    await asyncio.gather(*(refresh_tokens(account, db, redis) for _ in range(5)))
    assert route.call_count == 1                      # лок сработал
    assert decrypt(account.refresh_token_enc) == "new-r"   # новый refresh сохранён

@pytest.mark.asyncio
@respx.mock
async def test_refresh_400_marks_needs_reauth(account, db, redis):
    respx.post("https://api.avito.ru/token").mock(return_value=httpx.Response(400))
    await refresh_tokens(account, db, redis)
    assert account.status == "needs_reauth"

@pytest.mark.asyncio
async def test_lock_released_on_exception(account, db, redis):
    """httpx упал -> лок не завис (иначе аккаунт застрянет на 30 сек+)."""
    with respx.mock:
        respx.post("https://api.avito.ru/token").mock(side_effect=httpx.ConnectError)
        with pytest.raises(httpx.ConnectError):
            await refresh_tokens(account, db, redis)
    assert await redis.get(f"lock:token:{account.id}") is None
```

Туда же: `send_message` при 401 делает ровно одну попытку авто-рефреша (respx: первый
вызов 401, второй 200 → итог успех; оба 401 → исключение, не бесконечный цикл).

### 1.2. Integration (testcontainers: postgres + redis)

Запуск: `pytest tests/integration -q`. Контейнеры поднимаются один раз на сессию,
между тестами — `TRUNCATE ... CASCADE` + `FLUSHDB`. Миграции — реальный `alembic upgrade head`
(это заодно тестирует сами миграции).

```python
# tests/integration/conftest.py
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16-alpine") as pg:
        run_alembic(pg.get_connection_url())
        yield pg.get_connection_url()

@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}"
```

Обязательные сценарии:

**INT-1. Полный путь вебхук → очередь → воркер → БД → Pub/Sub.**
`POST /api/hooks/avito/{account_id}?secret=...` с фикстурой `text_message.json` →
ответ `{"ok": true}` мгновенно; запись появилась в стриме `webhooks:avito`;
прогоняем consumer (`process_webhook_entry`) → в БД созданы `clients`, `conversations`
(status=`new`), `messages` (direction=`in`, `external_message_id` заполнен);
подписчик на канале `events` получил `{"type": "message:new", ...}`; entry ack'нут
(XPENDING == 0). Замер: время от POST до publish < 1 с.

**INT-2. Идемпотентность дублей вебхука.**
Тот же payload POST'ится 3 раза (ретраи Авито) → в `messages` ровно одна строка
(частичный уникальный индекс `(conversation_id, external_message_id, created_at)`),
Pub/Sub событие опубликовано ровно один раз, воркер не падает на
`IntegrityError`, все три entry ack'нуты.

**INT-3. Reconciliation.**
В fake-adapter'е `fetch_history` отдаёт 5 сообщений, 2 из которых уже в БД →
после прогона reconciliation-джобы в БД 5 (добавлено ровно 3), `last_message_at`
обновлён, для добавленных опубликованы события. Повторный прогон — 0 новых строк.

**INT-4. Возврат клиента в закрытый диалог.**
`conversations.status='closed'`, приходит вебхук от клиента → статус `new`,
`assignee_id=NULL` (логика из 8.3 DESIGN.md).

**INT-5. Эхо собственного исходящего.**
Вебхук с `author_id == account.avito_user_id` → сообщений не добавлено, entry ack'нут.

**INT-6. Доставка исходящего с ретраями.**
`create_outbound_message` (status=`pending`) → ARQ-джоба `deliver_message`, adapter
замокан: 2 раза 500, затем 200 → `delivery_status='delivered'`; adapter всегда 500 →
после 5 попыток `failed` + событие в Pub/Sub (для кнопки «повторить»).

**INT-7. Извлечение телефона.**
Вебхук с текстом «звоните 8 916 123 45 67» → `clients.phone` заполнен нормализованным
номером; повторное сообщение с другим номером не затирает подтверждённый (правило зафиксировать).

**INT-8. Consumer group переживает падение воркера.**
Entry прочитан, воркер «умер» до ack (эмулируем исключением) → после `XAUTOCLAIM`
вторым консюмером сообщение обработано, в БД — без дублей (совместно с INT-2 это
даёт at-least-once + идемпотентность = exactly-once по факту).

**INT-9. FTS.** Вставка сообщения на русском → `SELECT ... WHERE search @@ plainto_tsquery('russian', 'экран')` находит его через API поиска.

**INT-10. Партиции.** Вставка сообщения с `created_at` в следующем месяце не падает
(партиции создаёт scheduler-job за 7 дней до начала месяца — 05-DEPLOY-OPS §5.3, правило 5).

### 1.3. E2E (Playwright)

Каталог `frontend/e2e/`, TypeScript, `@playwright/test`. Стенд — `docker compose
-f docker-compose.test.yml up`: полный бэкенд + **fake-avito** (раздел 2) вместо
реального API. Сиды: admin / head / manager / observer + один подключённый аккаунт.

| # | Сценарий | Ключевые ассерты |
|---|---|---|
| E2E-1 | Логин | неверный пароль → ошибка без утечки «пользователь существует»; верный → `/chats`; refresh-cookie `HttpOnly; Secure; SameSite=Lax` |
| E2E-2 | Ответ в чате | fake-avito пушит входящее → строка появляется в списке **без перезагрузки** (WebSocket), бейдж непрочитанных; менеджер открывает, пишет ответ, Enter → ✓ доставлено; в fake-avito `GET /_control/outbox` содержит текст |
| E2E-3 | Передача диалога | менеджер А: «Передать коллеге» → выбор Б + комментарий → у Б диалог с ⚑ и комментарием, у А ушёл из «Мои»; `audit_log` содержит действие |
| E2E-4 | Шаблоны | создать личный шаблон с `{имя}`; в диалоге «⚡» → поиск → подстановка имени клиента в поле ввода; head редактирует общий шаблон, менеджер видит изменение |
| E2E-5 | Роли (UI-срез) | под observer нет поля ввода и кнопок статуса; под head вместо поля ввода плашка «Режим просмотра» |
| E2E-6 | Ошибка отправки | fake-avito в режиме 500 → сообщение помечается ✗, кнопка «повторить»; режим ok → повтор успешен |

Playwright-конфиг: `retries: 1`, trace `on-first-retry`, артефакты в CI.
Realtime-ассерты — только через `await expect(...).toVisible()` с таймаутом, никаких `sleep`.

**Smoke на Tauri** (минимальный, отдельный job на Windows runner, nightly + перед релизом,
через `tauri-driver` + WebdriverIO):
1) приложение запускается, окно открылось, версия в about совпадает с тегом сборки;
2) логин против тестового стенда проходит, список чатов отрисован;
3) закрытие окна сворачивает в трей (процесс жив), клик по трею разворачивает;
4) входящее из fake-avito → нативное уведомление появилось (проверка через Tauri-мок событий);
5) автообновление: фид updater'а с той же версией → «обновлений нет», без падения.
Остальное десктоп-специфичное (офлайн-очередь, DPAPI-шифрование кэша) — ручная приёмка в UAT (раздел 5).

---

## 2. Мок Авито: сервис `fake-avito`

Отдельное FastAPI-приложение `tools/fake-avito/` (один файл + state в памяти), контейнер
в `docker-compose.test.yml` и в dev-профиле. Бэкенду в тестовом окружении подменяется
базовый URL: `AVITO_API_BASE=http://fake-avito:9000` (и `AVITO_AUTH_URL` — на страницу-заглушку,
которая сразу редиректит на callback с кодом). **Никакой логики «if test» в боевом коде —
только конфиг базового URL.**

### 2.1. Эмулируемые endpoints (контракт = раздел 8 DESIGN.md)

| Endpoint | Поведение |
|---|---|
| `POST /token` | `authorization_code` → выдаёт пару токенов; `refresh_token` → **одноразовость как у Авито**: повторный запрос со сгоревшим refresh → `400` (ловим гонки double-refresh на живом стенде); access TTL конфигурируем (по умолчанию 60 с в тестах — чтобы refresh-путь реально исполнялся) |
| `GET /core/v1/accounts/self` | профиль аккаунта (`avito_user_id`, имя) по Bearer-токену; просроченный/чужой токен → 401 |
| `POST /messenger/v3/webhook` | запоминает URL вебхука для аккаунта; кривой URL → 400 |
| `POST /messenger/v1/accounts/{uid}/chats/{chat_id}/messages` | валидирует Bearer, кладёт сообщение в outbox, возвращает `{"id": "..."}` |
| `GET /messenger/v2/accounts/{uid}/chats` (+ `?unread_only=true`) | список чатов из state — для backfill и reconciliation |
| `GET /messenger/v2/accounts/{uid}/chats/{chat_id}/messages/` | история чата |

### 2.2. Управляющая плоскость `/_control/*` (используют тесты)

| Endpoint | Назначение |
|---|---|
| `POST /_control/incoming` `{account_id, chat_id?, author_name, text, item?}` | создаёт входящее в state **и сам POST'ит вебхук** на зарегистрированный URL (payload — в точном формате фикстур 1.1.2); `chat_id` не задан → новый чат |
| `POST /_control/mode` `{mode, scope?}` | режим ошибок: `ok` \| `401` \| `429` \| `500` \| `slow`; `scope` — какие endpoints затронуты (`messages`, `token`, `all`) |
| `GET /_control/outbox` | всё, что бэкенд «отправил в Авито» — главный ассерт e2e |
| `POST /_control/drop_webhooks` `{count}` | следующие N вебхуков не доставлять (тест reconciliation на живом стенде) |
| `POST /_control/reset` | сброс state между тестами |

### 2.3. Сценарии ошибок (контракт поведения)

- **401** — на `messages`: бэкенд обязан сделать ровно один авто-рефреш и повторить (см. 1.1.4); на `token`: аккаунт → `needs_reauth`.
- **429** — ответ с заголовком `Retry-After: 5`; бэкенд обязан отложить доставку минимум на Retry-After, не долбить и не терять сообщение (остаётся `pending`).
- **500** — бэкенд ретраит экспоненциально до 5 раз, затем `failed`.
- **slow** — задержка ответа 20 с (> клиентского таймаута 15 с из 8.1/8.2): проверяем, что таймаут срабатывает, воркер не зависает, доставка ретраится.

Дополнительно fake-avito пишет в лог каждый принятый запрос — при разборе флаки-тестов
это первый источник истины.

---

## 3. Нагрузочная проверка перед релизом (k6)

Профиль из допущений DESIGN.md с запасом: **пик 50 входящих сообщений/мин + 30 операторов
онлайн** (10 000/сутки ≈ 7/мин в среднем, пиковый коэффициент ×7). Стенд — прод-VPS
до открытия для сотрудников (или клон с теми же ресурсами), бэкенд смотрит в fake-avito.

### 3.1. Сценарий `tests/load/release.js`

> Реализация: `tests/load/release.js` (k6 — операторы, чтение лент, ответы,
> вебхуки), `tests/load/inject.py` (вброс через управляющую плоскость
> fake-avito изнутри сети compose + серверные метрики §3.2),
> `tests/load/watch.sh` (`docker stats` и счётчик перезапусков на хосте).
> Отступления реализации от листинга ниже перечислены в шапке `release.js`.

```javascript
import http from "k6/http";
import ws from "k6/ws";
import { check, sleep } from "k6";
import { Trend, Counter } from "k6/metrics";

const e2eDelivery = new Trend("e2e_delivery_ms", true);   // вебхук -> WS у оператора
const wsMessages = new Counter("ws_messages_received");
const BASE = __ENV.BASE_URL;          // https://staging-хост
const HOOK = `${BASE}/api/hooks/avito/${__ENV.ACCOUNT_ID}?secret=${__ENV.HOOK_SECRET}`;

export const options = {
  scenarios: {
    incoming: {                        // 50 вебхуков/мин, 30 минут + всплеск
      executor: "ramping-arrival-rate",
      timeUnit: "1m", preAllocatedVUs: 20,
      stages: [
        { target: 50, duration: "5m" },   // разгон
        { target: 50, duration: "20m" },  // плато
        { target: 250, duration: "2m" },  // spike ×5: очередь должна расти и разбираться
        { target: 50, duration: "3m" },
      ],
      exec: "incomingWebhook",
    },
    operators: {                       // 30 операторов онлайн
      executor: "constant-vus", vus: 30, duration: "30m",
      exec: "operator",
    },
  },
  thresholds: {
    "http_req_duration{scenario:incoming}": ["p(95)<200"], // ответ вебхука
    "http_req_duration{scenario:operators}": ["p(95)<500"],
    e2e_delivery_ms: ["p(95)<2000"],
    http_req_failed: ["rate<0.001"],
  },
};

export function incomingWebhook() {
  // sent_at внутри payload -> оператор посчитает e2e-лаг при получении по WS
  const payload = makeAvitoPayload({ sentAt: Date.now(), uniqueMsgId: true });
  const r = http.post(HOOK, JSON.stringify(payload),
                      { headers: { "Content-Type": "application/json" } });
  check(r, { "hook 200": (r) => r.status === 200 });
}

export function operator() {
  const token = login(__VU);           // 30 сидовых менеджеров op01..op30
  // WS-авторизация — только одноразовым тикетом (01 §11.1): токен в query запрещён
  const ticket = http.post(`${BASE}/api/v1/ws/ticket`, null, auth(token)).json("ticket");
  ws.connect(`${BASE.replace("http", "ws")}/api/ws?ticket=${ticket}`, {}, (sock) => {
    sock.on("message", (raw) => {
      const ev = JSON.parse(raw);
      if (ev.type === "message:new" && ev.data.message.meta?.sent_at) {
        e2eDelivery.add(Date.now() - ev.data.message.meta.sent_at);
        wsMessages.add(1);
      }
    });
    sock.setInterval(() => {           // фоновое поведение оператора
      http.get(`${BASE}/api/v1/conversations?tab=mine`, auth(token));   // раз в 15 с
    }, 15000);
    sock.setInterval(() => {           // ответ клиенту ~раз в минуту на оператора
      const conv = pickRandomConversation(token);
      if (conv) http.post(`${BASE}/api/v1/conversations/${conv}/messages`,
                          JSON.stringify({ text: "Добрый день! Уточняю по вашему вопросу.",
                                           client_message_id: uuidv4() }),   // 01 §1.6
                          auth(token));
    }, 60000 + Math.random() * 30000);
    sock.setTimeout(() => sock.close(), 30 * 60 * 1000);
  });
}
```

Итого нагрузка: ~50–250 вебхуков/мин + 30 постоянных WS + ~120 GET/мин + ~30 отправок/мин
(отправки уходят через ARQ в fake-avito — воркерный тракт тоже под нагрузкой).

### 3.2. Что смотрим во время прогона (серверная сторона)

| Метрика | Как снять | Порог |
|---|---|---|
| Лаг очереди вебхуков | `XINFO GROUPS webhooks:avito` (поле `lag`) + `XPENDING webhooks:avito workers` каждые 10 с (`tests/load/inject.py`, он же вбрасывает нагрузку). **XLEN лагом не является**: стрим не очищается по мере разбора, его длина растёт всегда | на плато ≈ 0; после spike возвращается к 0 за < 3 мин |
| E2E-доставка (вебхук→WS) | тренд `e2e_delivery_ms` в k6 | p95 < 2 c, p99 < 5 c |
| Ответ webhook-endpoint | k6 `http_req_duration{scenario:incoming}` | p95 < 200 мс |
| REST API операторов | k6 `http_req_duration{scenario:operators}` | p95 < 500 мс |
| Доставка исходящих | SQL: `SELECT delivery_status, count(*) FROM messages WHERE direction='out' AND created_at > now()-interval '40 min' GROUP BY 1` | 100% `delivered`, 0 `failed` |
| Потери сообщений | `_control/incoming` счётчик fake-avito vs `SELECT count(*) FROM messages WHERE direction='in' ...` | расхождение = 0 (с учётом reconciliation) |
| CPU / RAM контейнеров | `docker stats` в файл | CPU < 70% на плато (запас ×2 до роста), без OOM |
| Соединения PG | `SELECT count(*) FROM pg_stat_activity` | < размера пула, без роста-утечки |
| WS-стабильность | k6: `ws_sessions` без реконнект-шторма; логи api без `ConnectionClosed`-спама | 0 массовых разрывов |
| Ошибки приложения | Sentry за окно прогона | 0 новых issue |

### 3.3. Критерии прохождения (gate релиза)

1. Все k6-thresholds зелёные (они и есть машинный вердикт: p95 вебхука < 200 мс, API < 500 мс, e2e-доставка < 2 с, ошибок < 0.1%).
2. Ни одно входящее не потеряно: счётчики fake-avito == БД.
3. Spike ×5 пережит: очередь разобрана < 3 мин после спада, ни один контейнер не перезапустился.
4. Refresh токенов, случившийся во время прогона (TTL access в fake-avito 10 мин), не вызвал ошибок доставки.
5. После прогона: в `messages` нет строк `pending` старше 5 минут; память api/worker вернулась к базовой (нет утечки).

Прогон повторяется после любого фикса. Результат (k6 summary + watch.sh лог) прикладывается
к релизному тикету.

---

## 4. Чек-лист безопасности к релизу

Детализация раздела 9 DESIGN.md. Формат: пункт → **метод проверки** → ожидаемый результат.
Чек-лист проходится на проде (или идентичном стейджинге) перед открытием доступа; колонку
«✔/дата/кто» вести в релизном тикете. Автоматизируемые пункты помечены `[auto]` — они
входят в CI или в smoke.

### 4.1. Auth и сессии

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| A1 | Access JWT живёт 15 мин | `[auto]` unit: декодировать выданный токен, `exp-iat==900`; вручную: подождать/подделать `exp` → 401 | просроченный токен отвергается |
| A2 | Refresh-cookie: HttpOnly, Secure, SameSite=Lax, TTL 14 дней | `curl -si POST /api/v1/auth/login` → разбор `Set-Cookie` | все три флага + Max-Age≈1209600 |
| A3 | Ротация refresh | login → refresh → повторить refresh со **старым** cookie | старый отвергнут (401), в audit_log событие |
| A4 | Logout отзывает refresh | login → logout → refresh со старым cookie | 401; ключ в Redis-denylist существует с TTL |
| A5 | Пароли — argon2id | `SELECT left(password_hash,10) FROM users LIMIT 3` | все начинаются с `$argon2id$` |
| A6 | Брутфорс-блокировка: 10 неудач по IP и по учётке | скрипт: 11 попыток с неверным паролем | с 11-й — **403 `account_locked`** (прикладной лимит 01 §1.7/§2.1); 429 возможен только раньше, от nginx-зоны login (05 §3.1) при явном флуде; в audit_log событие; через окно блокировки вход возможен |
| A7 | Заведение сотрудника только одноразовой ссылкой | открыть ссылку дважды | второй раз — «ссылка использована»; ссылка имеет TTL |
| A8 | Ошибки логина не раскрывают существование email | login с несуществующим email и с существующим+неверным паролем | тексты ошибок идентичны |
| A9 | Отключённый сотрудник (`is_active=false`) теряет доступ немедленно | отключить пользователя с живой сессией → любой его запрос | 401/403 не позже истечения access (15 мин), WS разорван |

### 4.2. RBAC

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| R1 | Матрица прав на каждом endpoint | `[auto]` тест 1.1.3 зелёный + тест-страж покрытия | новый endpoint не может попасть в прод мимо матрицы |
| R2 | UI-скрытие ≠ безопасность | под observer выполнить `curl POST /api/v1/conversations/{id}/messages` с его токеном | 403 от бэкенда, не только скрытая кнопка |
| R3 | Head не может писать клиентам | `curl POST .../messages` под head | 403; заметка (`/notes`) — 200 |
| R4 | Менеджер не видит чужую статистику | `GET /api/v1/stats/managers` под manager | 403 (как и весь `/stats/*`, 01 §9); `/api/v1/stats/my/today` — только свои цифры |
| R5 | Личные шаблоны изолированы | manager A читает/правит шаблон manager B по id | 403/404 |
| R6 | audit_log пишется | выполнить передачу диалога, смену роли, вход | `SELECT * FROM audit_log ORDER BY id DESC LIMIT 10` — все три события с user_id и details |

### 4.3. Токены Авито

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| T1 | Токены в БД только шифртекстом | `SELECT access_token_enc FROM avito_accounts LIMIT 1` — попытаться `convert_from(...,'UTF8')`; поиск по дампу: `pg_dump ... \| grep -cE 'Bearer\|[A-Za-z0-9_-]{40,}'` вручную по образцу известного токена | ни одного plaintext-токена в дампе |
| T2 | Ключ шифрования вне БД и вне git | `git log -p --all -S TOKEN_ENC_KEY` (имя переменной — 05 §4, `.env.example`); `[auto]` gitleaks в CI | ключ только в env/systemd credentials на сервере |
| T3 | Refresh под локом | `[auto]` тест 1.1.4 | двойного использования refresh нет; на проде за неделю нет ни одного 400 от /token в логах |
| T4 | `needs_reauth` работает | на стейджинге отозвать доступ (fake-avito mode 400 на token) | аккаунт 🔴 в /settings/accounts, уведомление админам, доставка по остальным аккаунтам не затронута |
| T5 | Токены не попадают в логи | см. 4.6-S1 | — |

### 4.4. Webhook endpoint

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| W1 | Секрет обязателен, сравнение constant-time | `curl POST /api/hooks/avito/{id}` без secret / с неверным / с верным, кроме того код-ревью на `compare_digest` | 403 / 403 / 200; тайминги неразличимы (compare_digest) |
| W2 | Тело > 1 МБ отклоняется | `curl --data-binary @2mb.json` | 413 до чтения в память (лимит на nginx `client_max_body_size` + проверка в приложении) |
| W3 | Rate-limit на endpoint | залп 1000 rps скриптом | 429 после порога, воркеры живы, легитимный трафик после паузы проходит |
| W4 | Невалидный JSON/схема не роняет обработку | `curl` с мусором (при верном секрете) | 200 (ack), payload в лог-таблице сырых вебхуков, воркер жив |
| W5 | Endpoint не отдаёт информацию | `GET /api/hooks/avito/{id}` | 405; ответ без стектрейсов и версий |

### 4.5. Rate limits и сетевой периметр

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| N1 | HTTPS-only + HSTS | `curl -sI http://chat.partner-lead-centre.ru` → 301 на https; `curl -sI https://...` → `Strict-Transport-Security` | редирект + HSTS max-age ≥ 15552000 |
| N2 | TLS-конфиг | `testssl.sh chat.partner-lead-centre.ru` | оценка A/A+, нет TLS<1.2 |
| N3 | Rate-limit на /api/v1/auth/login | см. A6 | 429 от nginx-зоны при флуде (05 §3.1) |
| N4 | CORS только свой домен | `curl -H "Origin: https://evil.example" -I .../api/v1/conversations` | нет `Access-Control-Allow-Origin` для чужого Origin |
| N5 | Наружу торчат только 80/443 | `nmap -p- <IP>` с внешнего хоста | Postgres/Redis (5432/6379) недоступны извне, привязаны к docker-сети |
| N6 | Media без подписанной ссылки недоступны | прямой `curl https://.../media/<путь>` без подписи и с истёкшей подписью | 403; валидная подпись — 200 |
| N7 | Swagger/openapi недоступны анониму | `GET /api/docs`, `/api/openapi.json` без токена | 401/404 (внутренний инструмент) |

### 4.6. Секреты в логах

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| S1 | Фильтр логгера режет секреты | `[auto]` unit: caplog при refresh/логине/отправке — в записях нет подстрок токена/пароля; вручную на стейдже: прогнать OAuth-подключение и `docker compose logs \| grep -E '(access_token\|refresh_token\|password\|Bearer )'` | 0 совпадений (кроме замаскированных `***`) |
| S2 | Sentry не получает секретов | вызвать тестовую ошибку в тракте отправки, открыть event | `before_send`-скраббер вычистил headers/env; cookie и Authorization отсутствуют |
| S3 | Сырые payload-логи вебхуков не содержат наших токенов | просмотреть лог-таблицу | там только данные Авито-стороны (это ок), ретеншн 30 дней настроен |
| S4 | .env не в образе и не в git | `docker run --rm <image> ls -la /app`; `git ls-files \| grep -E '\.env$'` | .env отсутствует |

### 4.7. Аудит зависимостей `[auto]`

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| D1 | Python-зависимости | job в CI: `pip-audit --strict` по зависимостям из `uv.lock` (через `uv export --format requirements-txt`) на каждый PR + еженедельный cron-workflow | 0 известных уязвимостей high/critical; фиксы или задокументированные исключения с тикетом |
| D2 | npm-зависимости | `npm audit --omit=dev --audit-level=high` в том же job | аналогично |
| D3 | Rust/Tauri | `cargo audit` в job сборки MSI | аналогично |
| D4 | Lock-файлы закоммичены | ревью: `pyproject.toml`/`uv.lock`, `package-lock.json`, `Cargo.lock` в git | сборка воспроизводима |
| D5 | Базовые Docker-образы | `docker scout cves` (или trivy) на итоговые образы, еженедельно | нет critical; образы на актуальных digest |

### 4.8. Бэкапы восстановимы

| # | Проверка | Метод | Ожидаемо |
|---|---|---|---|
| B1 | Ежедневный pg_dump выполняется | cron/скрипт на сервере; проверить свежесть: `ls -la /var/backups/leadchat/` | файл за сегодня, размер > 0 и монотонно правдоподобен |
| B2 | Офсайт-копия уходит | лог синка (restic/rclone), проверить наличие вчерашнего дампа на офсайте | есть; доступ к офсайту — отдельные креды, недоступные с VPS на удаление истории (append-only/versioning) |
| B3 | **Восстановление реально работает** | раз в релиз (и далее ежеквартально): поднять `postgres:16` в чистом контейнере, `pg_restore` вчерашнего дампа, запустить API против него, залогиниться, открыть диалог | восстановление < 30 мин, приложение работает; результат и время зафиксированы в тикете |
| B4 | Media бэкапится | `/var/leadchat/media` включён в офсайт-синк; выборочно восстановить один файл | вложение открывается |
| B5 | Секреты для восстановления доступны не только на VPS | ключ AES токенов + env задокументированы в менеджере секретов компании | восстановление возможно при полной потере VPS (токены Авито расшифруются) |

---

## 5. Приёмочные сценарии релиза 1.0 (UAT)

Проходятся вручную на проде перед открытием доступа команде; каждая роль — под своей
реальной учёткой. Отметки — в релизном тикете. Клиентскую сторону эмулируем реальным
тестовым аккаунтом Авито (покупатель пишет с телефона).

**Админ**

1. Вход по email+паролю; «запомнить меня» переживает закрытие браузера.
2. Подключение аккаунта Авито: «Подключить аккаунт» → согласие на avito.ru → возврат в `/settings/accounts`, карточка 🟢, webhook «активен».
3. Догрузка истории: в течение нескольких минут старые диалоги подключённого аккаунта появились с полной перепиской.
4. Заведение сотрудника: «Пригласить» → одноразовая ссылка → сотрудник ставит пароль и входит; повторное открытие ссылки — отказ.
5. Смена роли сотрудника и отключение (`is_active=false`) — эффект немедленный (сессия сотрудника умирает).
6. Создание бота из сценария «Первичный приём», привязка к аккаунту, расписание «вне 10:00–20:00».
7. Тестовое входящее с Авито ночью (или с временно инвертированным расписанием): бот здоровается, задаёт вопрос, собирает телефон, диалог падает в очередь с тегом.
8. Общий шаблон: создать в `/settings/templates` — виден менеджерам.
9. Журнал аудита: видны события входов, передач, изменений настроек за сегодня.
10. Страница `/download` отдаёт свежий NSIS-инсталлятор `LeadChat-Setup.exe` (04 §1.3/§6.1; MSI — только для GPO/Intune, ставится per-machine); установка на чистой Windows без прав администратора (per-user); вход из десктопа.
11. `Ctrl+Shift+L` разворачивает окно; закрытие окна оставляет иконку в трее; бейдж непрочитанных на иконке растёт при новом входящем.

**Руководитель (head)**

12. Видит все диалоги, фильтр «по менеджеру» работает.
13. В открытом диалоге вместо поля ввода — плашка «Режим просмотра»; отправить сообщение невозможно (и через DevTools/API — 403, проверяет техлид).
14. Переназначение: снять диалог с менеджера А на менеджера Б — у Б появился с ⚑.
15. Внутренняя заметка в чужом диалоге — сохраняется, менеджер её видит, клиент — нет (проверить в приложении Авито клиента!).
16. `/stats`: цифры по всем менеджерам за период сходятся с ручным пересчётом по 2–3 диалогам; экспорт CSV открывается в Excel без кракозябр (UTF-8 BOM).
17. Общие шаблоны редактируются; раздел «Боты»/«Аккаунты»/«Сотрудники» недоступен.

**Менеджер**

18. Видит вкладки Мои/Все/Новые/Закрытые; новый диалог появился со звуком и счётчиком в заголовке вкладки.
19. Взял новый диалог (ответил) → диалог стал «в работе» и попал в «Мои».
20. Ответ клиенту доходит до реального приложения Авито; ответ клиента возвращается в LeadChat < 2 с.
21. Быстрый ответ через «⚡»: поиск, подстановка `{имя}` реальным именем.
22. Вложение (фото) в обе стороны: клиент → менеджер и менеджер → клиент.
23. Телефон, написанный клиентом текстом, появился в карточке клиента.
24. Передача коллеге с комментарием; у коллеги — ⚑ и комментарий.
25. Заметка «видно только сотрудникам» — жёлтая вставка, в Авито не ушла.
26. Закрытие диалога; клиент написал снова → диалог снова «новый» без ответственного.
27. Виджет «моя статистика за сегодня» соответствует действиям; раздел `/stats` недоступен.
28. Десктоп: тост Windows о новом сообщении, «Ответить» из тоста отправляет текст; офлайн-режим — выключить Wi-Fi, прочитать кэш диалогов, набрать ответ (⏳), включить сеть → ушёл.

**Наблюдатель**

29. Видит все диалоги и поиск; поля ввода нет, заметок нет, статусы менять нельзя.
30. Прямой API-запрос на отправку под его токеном → 403 (проверяет техлид).
31. Разделы статистики и настроек отсутствуют в навигации и недоступны по прямым URL.

**Кросс-ролевые**

32. Одновременная работа: два менеджера в одном диалоге видят сообщения друг друга в реальном времени, «кто-то печатает»/двойной ответ не ломают ленту.
33. Оператор вмешался в диалог, который вёл бот, → бот замолчал в этом диалоге навсегда.
34. Отвал вебхука (попросить поддержку/эмулировать блокировкой исходящего IP на 10 мин): сообщения всё равно появились ≤ 5 мин (reconciliation) без дублей.

Критерий приёмки: пункты 1–34 пройдены, блокеров нет; некритичные замечания — в бэклог 1.1.

---

## 6. Регрессионный smoke после каждого деплоя

Цель: за ≤ 5 минут автоматически убедиться, что деплой не сломал критический тракт.
Реализация — pytest-набор `tests/smoke/` (маркер `@pytest.mark.smoke`), запускается
шагом деплой-workflow **после** `docker compose up -d --build` против боевого URL; красный
smoke = откат образа на предыдущий тег.

Ключевое ограничение: **прод не должен слать ничего в реальный Авито**. Поэтому smoke
работает только с внутренними трактами + спец-сущностями: сидовый пользователь
`smoke@leadchat.local` (role=manager, скрыт из UI-списков) и служебный диалог
`SMOKE-CONV` (без привязки к реальному аккаунту Авито; сообщения в него создаются
с `direction='note'` — наружу не уходят по определению).

| # | Шаг | Проверка | Бюджет |
|---|---|---|---|
| SM-1 | `GET /api/health` | 200; в теле (контракт 01 §1.1 / 05 §7.2): `status == "ok"`, `db == true` (SELECT 1), `redis == true` (PING), `version == деплоенному тегу` | 5 с |
| SM-2 | Логин smoke-пользователя | 200, access-JWT валиден, refresh-cookie с нужными флагами | 5 с |
| SM-3 | `GET /api/v1/conversations?limit=1` | 200, схема ответа валидна (Pydantic-модель из клиентского контракта) | 5 с |
| SM-4 | WebSocket | тикет через `POST /api/v1/ws/ticket` → подключение `/api/ws?ticket=` (01 §11.1), ping/pong, подписка на события | 10 с |
| SM-5 | Тракт «запись → realtime» | POST заметки в SMOKE-CONV → строка в БД и событие пришло в открытый WS из SM-4 < 2 с (проверяет API→БД→Pub/Sub→WS Hub целиком) | 15 с |
| SM-6 | Очередь и воркер | поставить ARQ-джобу `smoke_noop` → джоба исполнена, результат в Redis < 10 с (worker-контейнер жив и разбирает очередь) | 15 с |
| SM-7 | Планировщик | heartbeat-ключ `scheduler:alive` в Redis обновлялся < 2 мин назад (scheduler-процесс жив — иначе умрёт refresh токенов) | 2 с |
| SM-8 | Webhook-endpoint | POST без секрета → 403; с секретом smoke-аккаунта-заглушки → 200 и entry в стриме (не обрабатывается дальше: аккаунт disabled) | 5 с |
| SM-9 | Статика фронтенда | `GET /` → 200, HTML содержит хэш свежего бандла (сверка с манифестом сборки) | 5 с |
| SM-10 | Токен-статусы | `GET /api/v1/avito-accounts` под admin-smoke-токеном: ни один боевой аккаунт не в `needs_reauth` (регресс деплоя не убил refresh) | 5 с |

```yaml
# фрагмент .github/workflows/deploy.yml
  smoke:
    needs: deploy
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { version: "0.5.x" }
      - run: uv sync --frozen                  # как в ci.yml (05 §5.1)
      - run: uv run pytest tests/smoke -m smoke -q
        env:
          SMOKE_BASE_URL: https://chat.partner-lead-centre.ru
          SMOKE_PASSWORD: ${{ secrets.SMOKE_USER_PASSWORD }}
          EXPECTED_VERSION: ${{ github.sha }}
      - if: failure()
        run: ./deploy/rollback.sh ${{ github.event.inputs.previous_tag }}   # + алерт в чат команды
```

Тот же набор можно дёргать вручную: `make smoke` — первый шаг диагностики любого
инцидента («что вообще живо?»).

---

## 7. Сводка: что и когда гоняется в CI

| Триггер | Набор | Время | Блокирует |
|---|---|---|---|
| Каждый PR | ruff + mypy + eslint + tsc, unit (1.1), RBAC-матрица, integration (1.2), pip-audit/npm audit (4.7) | ~8 мин | merge |
| Merge в main | всё выше + e2e Playwright (1.3) на compose-стенде с fake-avito | ~15 мин | деплой |
| Деплой | smoke (раздел 6) | ≤ 5 мин | остаётся ли деплой |
| Nightly | e2e + Tauri-smoke (Windows runner) + docker scout/trivy | — | алерт |
| Перед релизом (вручную) | k6-нагрузка (раздел 3) + чек-лист безопасности (раздел 4) + UAT (раздел 5) + B3-восстановление бэкапа | ~1 день | релиз |
