# LeadChat — проектная документация

**Внутренняя платформа управления общением с клиентами через чаты Авито**
Бренд: Lead Partner · Домен: chat.partner-lead-centre.ru · Статус: готово к реализации

> Допущения (можно скорректировать без переделки архитектуры):
> 1. Нагрузка внутреннего инструмента: до ~50 сотрудников, до ~30 подключённых аккаунтов Авито, до ~10 000 входящих сообщений/сутки. Архитектура выдержит рост на порядок без переписывания.
> 2. Основной язык бэкенда — Python (у команды уже есть Python-проекты).
> 3. Хостинг — один VPS/VDS в РФ (доступность API Авито) + Docker Compose; Kubernetes не нужен.

---

## 1. Архитектура системы

### 1.1. Общая схема

```
                                  ┌──────────────────────────────┐
                                  │           АВИТО              │
                                  │  Messenger API + OAuth 2.0   │
                                  └──────┬───────────────▲───────┘
                            webhooks     │               │  REST (отправка,
                            (входящие)   │               │  чтение, токены)
                                         ▼               │
┌────────────────────────────────────────────────────────┴────────────────────┐
│                              LEADCHAT BACKEND                               │
│                                                                             │
│  ┌───────────────┐   ┌──────────────────┐   ┌──────────────────────────┐    │
│  │ Webhook       │──▶│  Redis Streams   │──▶│  Worker-пул (обработка)  │    │
│  │ Gateway       │   │  (очередь)       │   │  · нормализация сообщений │   │
│  │ (принять и    │   └──────────────────┘   │  · запись в PostgreSQL    │   │
│  │  ответить 200)│                          │  · движок ботов           │   │
│  └───────────────┘                          │  · маршрутизация на       │   │
│                                             │    менеджера              │   │
│  ┌───────────────┐   ┌──────────────────┐   └───────────┬──────────────┘    │
│  │ REST API      │   │ Channel Adapter  │               │                   │
│  │ (FastAPI)     │◀─▶│ Layer:           │               ▼                   │
│  │ · чаты        │   │  · AvitoAdapter  │   ┌──────────────────────────┐    │
│  │ · сотрудники  │   │  · TelegramAd.*  │   │  Redis Pub/Sub           │    │
│  │ · боты        │   │  · WhatsAppAd.*  │   │  (события реального      │    │
│  │ · статистика  │   │  (* — будущее)   │   │   времени)               │    │
│  └───────┬───────┘   └──────────────────┘   └───────────┬──────────────┘    │
│          │                                              │                   │
│  ┌───────▼──────────────────────────────────────────────▼──────────────┐    │
│  │            WebSocket Hub (онлайн-доставка в интерфейсы)             │    │
│  └───────┬─────────────────────────────────────────────────┬──────────┘     │
│          │                                                 │                │
│  ┌───────▼────────┐  ┌──────────────┐  ┌──────────────┐    │                │
│  │  PostgreSQL 16 │  │  Redis 7     │  │ Token Vault  │    │                │
│  │  (чаты, users, │  │  (кэш, сессии│  │ (шифрован-   │    │                │
│  │   боты, стата) │  │   очереди)   │  │  ные токены) │    │                │
│  └────────────────┘  └──────────────┘  └──────────────┘    │                │
└────────────────────────────────────────────────────────────┼────────────────┘
                                                             │
                       ┌────────────────────┬────────────────┤
                       ▼                    ▼                ▼
              ┌────────────────┐   ┌────────────────┐  ┌──────────────┐
              │  Веб-кабинет   │   │  Десктоп (Win) │  │  Мобильный   │
              │  React SPA     │   │  Tauri 2 +     │  │  браузер     │
              │  chat.partner- │   │  тот же React  │  │  (адаптив)   │
              │  lead-centre.ru│   │                │  │              │
              └────────────────┘   └────────────────┘  └──────────────┘
```

### 1.2. Интеграция с Messenger API Авито

Каждый аккаунт Авито подключается через OAuth и живёт в системе как сущность `avito_account`:

- **Подключение**: админ в настройках нажимает «Подключить аккаунт Авито» → редирект на страницу согласия Авито (authorization_code flow) → callback → сохраняем `access_token` + `refresh_token` (шифрованно) + `avito_user_id`.
- **Приём сообщений**: после подключения регистрируем webhook (`POST /messenger/v3/webhook`) с URL вида `https://chat.partner-lead-centre.ru/api/hooks/avito/{account_id}?secret=...`.
- **Отправка**: `POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages` от имени соответствующего аккаунта.
- **Догрузка истории**: при подключении аккаунта фоновая задача выкачивает существующие чаты (`GET /messenger/v2/accounts/{user_id}/chats` + `.../messages/`) и складывает в БД, чтобы менеджеры видели контекст до внедрения LeadChat.

Вся работа с Авито инкапсулирована в `AvitoAdapter`, реализующий общий интерфейс `ChannelAdapter` (см. 1.6) — это и есть точка расширения на Telegram/WhatsApp.

### 1.3. Webhook vs Polling — обоснование

**Выбор: Webhook как основной механизм + периодический reconciliation-polling как страховка.**

| Критерий | Webhook | Polling |
|---|---|---|
| Задержка доставки | < 1 сек | = интервалу опроса (10–60 сек) |
| Нагрузка на лимиты API | Нулевая для приёма | Съедает rate limit пропорционально числу аккаунтов |
| Надёжность | Авито может не доставить (сеть, даунтайм нашей стороны) | Гарантированно догоняет всё |
| Сложность | Нужен публичный HTTPS-endpoint | Тривиален |

Для чат-платформы задержка — главный критерий (менеджер должен видеть сообщение мгновенно), поэтому webhooks. Их известная слабость (недоставка при даунтайме) закрывается фоновым reconciliation: раз в 5 минут воркер опрашивает `GET .../chats?unread_only=true` по каждому аккаунту и досоздаёт сообщения, которых нет в БД (идемпотентно, по `avito_message_id`). Так мы получаем скорость вебхуков и гарантии поллинга.

Правила обработки вебхука:
1. Проверить секрет в URL/заголовке.
2. Немедленно ответить `200 OK` (Авито отключает вебхуки, которые отвечают медленно/ошибками).
3. Положить сырой payload в Redis Stream — всю реальную работу делает воркер.

### 1.4. Хранение истории чатов (БД)

PostgreSQL 16. Ключевые таблицы (полная схема — раздел 4.4):

- `conversations` — диалог: канал, аккаунт-источник, клиент, ответственный менеджер, статус (`new / in_progress / closed`), связанное объявление Авито.
- `messages` — сообщения: партиционирование по месяцам (`PARTITION BY RANGE (created_at)`), `UNIQUE (channel, external_message_id)` для идемпотентности, `direction` (in/out/system/note), `sender_type` (client/operator/bot).
- Вложения храним на диске сервера (`/var/leadchat/media`, отдача через nginx c подписанными ссылками); в БД — только метаданные. Для внутреннего инструмента S3 не обязателен, но путь к нему — замена одного storage-класса.
- Полнотекстовый поиск по сообщениям: `tsvector` + GIN-индекс (русская конфигурация). Для наших объёмов Elasticsearch не нужен.

### 1.5. OAuth 2.0 Авито и жизненный цикл токенов

**Flow — authorization_code** (а не client_credentials), потому что подключаются *несколько разных* аккаунтов Авито, и действовать нужно от имени каждого.

```
Админ ──▶ GET /api/avito/connect  ──▶ 302 на avito.ru/oauth?response_type=code
                                        &client_id=...&scope=messenger:read,messenger:write
                                        &state=<CSRF-токен, привязан к сессии>
Авито ──▶ GET /api/avito/callback?code=...&state=...
Бэкенд ──▶ POST https://api.avito.ru/token  (grant_type=authorization_code)
        ◀── { access_token (TTL 24ч), refresh_token, expires_in }
Бэкенд ──▶ шифрует оба токена, пишет в avito_accounts, регистрирует webhook
```

**Обновление токенов** (access живёт 24 часа):
- Планировщик (APScheduler в отдельном процессе) каждые 30 минут выбирает аккаунты, у которых `expires_at < now() + 2h`, и делает `grant_type=refresh_token`. Обновляются *оба* токена (refresh у Авито одноразовый — сохранять новый обязательно).
- Дополнительно — реактивный путь: любой вызов API, получивший 401, запускает refresh через распределённый lock в Redis (`SET NX lock:token:{account_id}`), чтобы конкурирующие воркеры не сожгли одноразовый refresh_token двойным использованием.
- Если refresh невозможен (пользователь отозвал доступ) — аккаунт помечается `needs_reauth`, админам уходит уведомление в интерфейс и в трей десктопа.

**Хранение**: токены шифруются AES-256-GCM (библиотека `cryptography`, ключ — в env/systemd credentials, не в БД и не в git). В логи токены не попадают никогда (фильтр логгера).

### 1.6. Абстракция каналов (задел на Telegram/WhatsApp)

```python
class ChannelAdapter(Protocol):
    channel: str                                   # "avito" | "telegram" | ...
    async def send_message(self, conv, text, attachments) -> ExternalMessageRef: ...
    async def fetch_history(self, account, since) -> AsyncIterator[InboundMessage]: ...
    async def setup_webhook(self, account) -> None: ...
    def parse_webhook(self, raw: dict) -> InboundEvent: ...
    async def refresh_credentials(self, account) -> None: ...
```

Весь остальной код (воркеры, боты, UI, статистика) работает с нормализованными `Conversation`/`Message` и не знает про Авито. Добавление Telegram = один новый адаптер + строка в enum каналов.

### 1.7. Архитектура десктоп-приложения

**Выбор: Tauri 2** (обёртка над тем же React-фронтендом). Обоснование против альтернатив — в разделе 2.3.

Устройство:
- Frontend — тот же собранный React-бандл, что и веб (одна кодовая база; `if (isTauri())` включает нативные фичи).
- Rust-ядро Tauri даёт: системный трей, нативные уведомления Windows (Action Center), автозапуск, глобальный хоткей (Ctrl+Shift+L — развернуть окно), бейдж непрочитанных на иконке, автообновление (tauri-updater с подписью).
- Все данные — через тот же REST + WebSocket, что и веб. Локально (SQLite через tauri-plugin-sql) кэшируются последние 200 диалогов для мгновенного старта и офлайн-чтения; исходящие, набранные в офлайне, ставятся в локальную очередь и отправляются при восстановлении связи с пометкой времени.
- Инсталлятор — MSI/NSIS ~5–8 МБ, установка без прав администратора (per-user), автообновление в фоне. Это и есть «низкий порог входа»: скачал → два клика → работает.

---

## 2. Стек технологий

| Слой | Выбор | Почему именно это |
|---|---|---|
| Бэкенд | **Python 3.12 + FastAPI** | Нативный async (вебхуки, WebSocket, множество одновременных вызовов Авито — I/O-bound задача); Pydantic валидирует и наши API, и payload'ы Авито; у команды уже есть Python-экспертиза (лид-бот) — общий код и найм проще. NestJS сопоставим технически, но дробит стек компании на два языка; Laravel — худший async/WebSocket из трёх. |
| ORM / миграции | SQLAlchemy 2 (async) + Alembic | Стандарт де-факто, типизированный. |
| Очереди / realtime | **Redis 7**: Streams (очередь вебхуков с consumer group и ack), Pub/Sub (фан-аут событий в WebSocket), кэш, локи, rate-limit | Один инфраструктурный компонент закрывает четыре потребности. RabbitMQ избыточен для наших объёмов: Streams дают persistence и повторную доставку, а лишний сервис в проде — лишние отказы. |
| Фоновые задачи | **ARQ** (async, поверх того же Redis) + APScheduler для расписаний | Celery тяжелее и хуже дружит с asyncio. |
| БД | **PostgreSQL 16** | Чаты — реляционные данные насквозь (диалог→сообщения→менеджер→статистика): нужны транзакции, JOIN'ы, агрегаты для отчётов. JSONB покрывает «гибкие» payload'ы каналов, FTS закрывает поиск, партиционирование — рост истории. MongoDB потеряла бы транзакционность и удобство аналитики; MySQL просто слабее в JSONB/FTS/партициях. |
| Фронтенд | **React 18 + TypeScript + Vite** | Крупнейшая экосистема именно для чатов: TanStack Query (кэш серверного состояния), TanStack Virtual (виртуализация длинных списков сообщений — критично), Zustand (лёгкий стор). Тот же бандл идёт в Tauri без изменений. Svelte элегантен, но экосистема и найм слабее; Vue — нормальная альтернатива, решает имеющаяся экспертиза, при прочих равных React. |
| UI-кит | Mantine (или shadcn/ui) | Готовые списки, модалки, нотификации; быстро кастомизируется под бренд Lead Partner. |
| Десктоп | **Tauri 2** | Инсталлятор 5–8 МБ против 80–120 МБ у Electron, RAM ~×3 меньше (WebView2 предустановлен в Windows 10/11), автообновление и трей из коробки, подписанные апдейты. Тот же React-код — нулевая доп. разработка UI. Flutter означал бы второй, полностью отдельный UI-код — неприемлемо для внутреннего инструмента. Electron — запасной вариант, если встретится несовместимость WebView2 (риск низкий: UI без экзотики). |
| Веб-сервер | nginx (TLS, статика, media, проксирование WS) | |
| Деплой | Docker Compose: `api`, `worker`, `scheduler`, `postgres`, `redis`, `nginx` + `certbot` | Один VPS, `git pull && docker compose up -d --build`. CI — GitHub Actions (тесты, сборка образов и MSI). |
| Мониторинг | Sentry (ошибки) + Uptime-Kuma (доступность) + структурные JSON-логи | Достаточно для внутреннего инструмента. |
| AI для ботов | Claude API (`claude-sonnet-5` — рабочая лошадка, `claude-haiku-4-5` — классификация) | См. раздел 4.5. |

---

## 3. Дизайн интерфейсов

### 3.1. Карта страниц веб-кабинета

```
/login                      Вход (email+пароль, «запомнить меня»)
/chats                      Главный экран — рабочее место (3 колонки)
/chats/:id                  Тот же экран с открытым диалогом (deep-link)
/stats                      Статистика и отчёты            (admin, head)
/settings/accounts          Аккаунты Авито                 (admin)
/settings/team              Сотрудники и роли              (admin)
/settings/bots              Чат-боты и сценарии            (admin)
/settings/templates         Быстрые ответы (общие)         (admin, head)
/settings/profile           Профиль, смена пароля, свои шаблоны   (все)
```
Публичной регистрации нет — сотрудников заводит администратор (внутренний инструмент).

### 3.2. Вход

Стилистика страницы входа — как на partner-lead-centre.ru: светлый фон, фирменный зелёный `#3cc13b`, логотип Lead Partner, слоган «Мы ремонтируем — Вы зарабатываете», карточка по центру: email, пароль, «Войти». Ниже — ссылка «Скачать приложение для Windows». (Палитра снята с CSS сайта: primary `#3cc13b`, светло-зелёные акценты `#8ada89`/`#d8f3d8`, текст `#212529`, шрифт Proxima Nova → Roboto/Inter; детально — docs/10-UX-DESIGN-SYSTEM.md.)

### 3.3. Главный экран «Чаты» (ядро продукта)

```
┌──────┬─────────────────────┬──────────────────────────────────┬───────────────────┐
│  ≡   │ ЧАТЫ            120 │  Иван П. · «Ремонт iPhone 13»    │ КЛИЕНТ            │
│──────│ [🔍 Поиск…        ] │  аккаунт: LP-Москва   ● в работе │ Иван Петров       │
│ 💬   │ [Мои|Все|Новые|Закр]│──────────────────────────────────│ ★ 4.9 · на Авито  │
│ Чаты │─────────────────────│                                  │   с 2021          │
│      │ ● Иван Петров  12:40│        ┌────────────────────┐    │ 📞 +7 9xx xxx-xx  │
│ 📊   │ iPhone 13, экран    │        │ Здравствуйте! Экран│    │    (из диалога)   │
│ Стат.│ «А сколько будет…»  │        │ разбит, почём?     │    │───────────────────│
│      │─────────────────────│        └────────────────────┘    │ ОБЪЯВЛЕНИЕ        │
│ ⚙️   │   Мария С.     12:31│  ┌────────────────────────┐      │ Ремонт iPhone     │
│ Настр│ Ноутбук HP  ✓ Отвеч.│  │ Добрый день! Замена    │      │ от 1500 ₽  [фото] │
│      │─────────────────────│  │ экрана iPhone 13 —     │      │ ссылка на Авито ↗ │
│──────│ ⚑ Пётр К.      11:58│  │ от 8 900 ₽ …           │      │───────────────────│
│ LP   │ Стиральная машина   │  └────────────────────────┘      │ ДИАЛОГ            │
│ 🟢   │ передан от Марии    │   🤖 бот собрал контакт          │ Менеджер: Вы      │
│ Анна │─────────────────────│──────────────────────────────────│ Статус: [в работе▾]│
│      │ … (виртуальный      │ [⚡шаблоны] [📎] [🗒 заметка]     │ [→ Передать]      │
│      │    список)          │ ┌──────────────────────────────┐ │───────────────────│
│      │                     │ │ Написать сообщение…    [➤]  │ │ 🗒 ЗАМЕТКИ (внутр.)│
│      │                     │ └──────────────────────────────┘ │ «торгуется, дать  │
│      │                     │                                  │  скидку до 10%»   │
└──────┴─────────────────────┴──────────────────────────────────┴───────────────────┘
```

**Левая колонка — список диалогов.** Фильтры-вкладки (Мои / Все / Новые / Закрытые), поиск по имени/тексту/телефону, фильтр по аккаунту Авито и по менеджеру (для руководителя). Каждая строка: аватар, имя, название объявления, последнее сообщение, время, бейдж непрочитанных (●), значок ⚑ если диалог передан тебе коллегой, 🤖 если сейчас ведёт бот. Сортировка: сначала с непрочитанными, затем по времени. Новые сообщения приходят по WebSocket без перезагрузки; звук + заголовок вкладки «(3) LeadChat».

**Центр — переписка.** Шапка: клиент, объявление, аккаунт-получатель, статус. Лента с датами-разделителями; входящие слева, исходящие справа (с именем оператора, если менеджеров несколько), сообщения бота помечены 🤖, внутренние заметки — жёлтые вставки «видно только сотрудникам». Статусы доставки ✓/✗ (ошибка отправки — кнопка «повторить»). Панель ввода: текст (Enter — отправить, Shift+Enter — перенос), «⚡» — быстрые ответы (поиск + подстановка переменных `{имя}`, `{менеджер}`), 📎 вложения, «🗒» — переключение в режим заметки.

**Правая колонка — карточка.** Клиент (имя, рейтинг Авито, телефон — автоматически извлекается из текста диалога регуляркой и сохраняется в карточку), объявление (превью, цена, ссылка), управление диалогом (ответственный, статус, «Передать коллеге» — выбор сотрудника + комментарий), история прошлых диалогов с этим клиентом, внутренние заметки.

### 3.4. Статистика (`/stats`)

Сверху фильтры: период, менеджер(ы), аккаунт Авито. Карточки-метрики: диалогов всего / новых / закрыто; среднее и медианное время первого ответа; % диалогов, закрытых ботом без оператора; собрано телефонов. Ниже — график обращений по дням/часам (тепловая карта часов — видно, когда нужен дежурный) и таблица по менеджерам: принято, закрыто, ср. время ответа, сообщений отправлено. Экспорт CSV/XLSX.

### 3.5. Настройки

- **Аккаунты Авито**: список карточек (название, avito_user_id, статус токена 🟢/🔴 `needs_reauth`, статус webhook, дата подключения), кнопки «Подключить аккаунт» (OAuth-редирект), «Переподключить», «Отключить». Правило распределения: какой бот и какая группа менеджеров обслуживают этот аккаунт.
- **Сотрудники**: таблица (имя, email, роль, статус активен/отключён, онлайн-индикатор), «Пригласить» — форма + одноразовая ссылка установки пароля.
- **Боты**: см. раздел 4.
- **Шаблоны**: общие (правит админ/руководитель) и личные; папки, поиск, переменные.

### 3.6. Десктоп-приложение — отличия от веба

Тот же интерфейс, плюс:
- **Трей**: иконка с бейджем непрочитанных; клик — развернуть; правый клик — меню (статус «на месте/отошёл», «Новые чаты: 3», выход). Закрытие окна сворачивает в трей, не убивает приложение.
- **Нативные уведомления** Windows: новое сообщение → тост с текстом и кнопкой «Ответить» (быстрый ответ прямо из уведомления через Tauri-плагин), клик — открыть диалог.
- **Офлайн-режим**: последние диалоги читаются из локального SQLite-кэша; набранные сообщения встают в очередь и уходят при появлении сети (в UI помечены ⏳).
- Автозапуск с Windows (галка в настройках), глобальный хоткей Ctrl+Shift+L, автообновление в фоне.

### 3.7. Отображение ролей в UI

См. таблицу в разделе 5 — интерфейс один, элементы скрываются/дизейблятся по роли, и та же матрица прав дублируется на бэкенде (UI-скрытие — это UX, а не безопасность).

---

## 4. Чат-боты

### 4.1. Модель

Бот = именованный сценарий, привязанный к одному или нескольким аккаунтам Авито + расписание активности (например, «24/7» или «только вне рабочих часов 20:00–10:00»). Сценарий — граф шагов, хранится как JSONB, редактируется в UI как список шагов (MVP) с последующим апгрейдом до визуального конструктора.

Типы шагов:

| Шаг | Что делает |
|---|---|
| `send` | Отправить текст (с переменными: имя клиента, название объявления) |
| `ask` | Отправить вопрос и ждать ответ → сохранить в переменную (`phone`, `device`, …) |
| `menu` | Вопрос с вариантами; ветвление по выбору/ключевым словам |
| `condition` | Ветвление: рабочее время? есть телефон? упомянуто слово? |
| `ai_answer` | Ответить через Claude API в рамках базы знаний (см. 4.5) |
| `handoff` | Передать оператору (причина фиксируется) |
| `close` | Закрыть диалог |
| `tag` / `note` | Повесить тег, записать заметку в карточку |

### 4.2. Пример сценария «Первичный приём» (по умолчанию)

```
Триггер: первое входящее сообщение в новом диалоге, менеджер ещё не отвечал
│
├─ send: «Здравствуйте, {client_name}! Это сервис Lead Partner 👋
│         Подскажите, что случилось с техникой — модель и проблему?»
├─ ask → переменная `problem` (ждать до 24 ч)
├─ ai_answer: черновой ответ по базе знаний (типовые цены/сроки),
│             confidence < порога → сразу handoff
├─ condition: рабочее время (10:00–20:00 МСК)?
│    ├─ ДА  → handoff(оператору): «клиент описал проблему, бот дал
│    │        предварительный ответ» + close bot-сессии
│    └─ НЕТ → send: «Мастер ответит утром. Оставьте телефон —
│             перезвоним первыми ✔»
│             ask → `phone` (валидация: похоже на телефон)
│             tag: "контакт собран" → карточка клиента
│             handoff(в очередь на утро)
```

### 4.3. Условия переключения на оператора (handoff)

Срабатывает любое из:
1. Клиент явно просит человека («оператор», «человек», «менеджер», «позовите»).
2. `ai_answer` вернул низкую уверенность или вопрос вне базы знаний.
3. Негатив/жалоба (классификатор на Haiku: negative → немедленный handoff с тегом «негатив», диалог поднимается в топ списка).
4. Клиент прислал 2+ сообщения после ответа бота, не попав в сценарий.
5. Сценарий дошёл до шага `handoff`.
6. Оператор сам вмешался (написал в диалог) → бот мгновенно замолкает в этом диалоге.

При handoff: статус `new` → диалог попадает в общий список; менеджер видит всю переписку бота и собранные переменные в карточке. Правило распределения на аккаунт: «в общую очередь» (кто взял — тот и ведёт, MVP) или round-robin по онлайн-менеджерам (фаза 3).

### 4.4. Схема данных (ядро)

```sql
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext UNIQUE NOT NULL,
  password_hash text NOT NULL,              -- argon2id
  full_name text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin','head','manager','observer')),
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE avito_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,                      -- «LP-Москва»
  avito_user_id bigint UNIQUE NOT NULL,
  access_token_enc bytea NOT NULL,          -- AES-256-GCM
  refresh_token_enc bytea NOT NULL,
  token_expires_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'active',    -- active | needs_reauth | disabled
  webhook_secret text NOT NULL,
  bot_id uuid REFERENCES bots(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel text NOT NULL DEFAULT 'avito',
  external_id text NOT NULL,                -- avito user id клиента
  name text, phone text, avito_rating numeric,
  UNIQUE (channel, external_id)
);

CREATE TABLE conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel text NOT NULL DEFAULT 'avito',
  external_chat_id text NOT NULL,
  account_id uuid NOT NULL REFERENCES avito_accounts(id),
  client_id uuid NOT NULL REFERENCES clients(id),
  assignee_id uuid REFERENCES users(id),
  status text NOT NULL DEFAULT 'new',       -- new | in_progress | closed
  bot_active boolean NOT NULL DEFAULT false,
  bot_vars jsonb NOT NULL DEFAULT '{}',
  tags text[] NOT NULL DEFAULT '{}',        -- теги диалога (шаг tag бота, «негатив» и т.п.)
  item_title text, item_url text, item_price text,
  last_message_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),  -- любое изменение строки (ORM onupdate);
                                                  -- основа фильтра ?updated_since= в API
  UNIQUE (channel, external_chat_id)
);
CREATE INDEX ON conversations USING gin (tags);

CREATE TABLE messages (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  external_message_id text,
  direction text NOT NULL,                  -- in | out | note | system
  sender_type text NOT NULL,                -- client | operator | bot | system
  sender_user_id uuid REFERENCES users(id),
  body text,
  attachments jsonb NOT NULL DEFAULT '[]',
  delivery_status text NOT NULL DEFAULT 'delivered',  -- pending|delivered|failed
  created_at timestamptz NOT NULL DEFAULT now(),
  search tsvector GENERATED ALWAYS AS (to_tsvector('russian', coalesce(body,''))) STORED,
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE UNIQUE INDEX ON messages (conversation_id, external_message_id, created_at)
  WHERE external_message_id IS NOT NULL;    -- идемпотентность вебхуков
CREATE INDEX ON messages USING gin (search);

CREATE TABLE bots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  is_enabled boolean NOT NULL DEFAULT true,
  schedule jsonb NOT NULL DEFAULT '{"always": true}',
  scenario jsonb NOT NULL,                  -- граф шагов
  knowledge_base text                       -- база знаний для ai_answer
);

CREATE TABLE templates (          -- быстрые ответы
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id uuid REFERENCES users(id),       -- NULL = общий
  title text NOT NULL, body text NOT NULL, folder text
);

CREATE TABLE audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id uuid, action text NOT NULL, entity text, entity_id text,
  details jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
```

### 4.5. Интеграция с AI

- Шаг `ai_answer`: system-prompt = база знаний бота (цены, сроки, регламенты Lead Partner) + жёсткие правила («не обещай точную цену без диагностики», «не выдумывай услуги», «сомневаешься — верни handoff»). Модель — `claude-sonnet-5`; ответ структурированный: `{reply, confidence, needs_operator}`.
- Классификация негатива и извлечение телефона/модели техники — `claude-haiku-4-5` (дёшево, быстро).
- Подсказки оператору (фаза 3): кнопка «✨ предложить ответ» — черновик по истории диалога, менеджер правит и отправляет.
- Все AI-вызовы идут через воркер с таймаутом 10 сек; недоступность AI никогда не блокирует доставку сообщений (бот в этом случае просто делает handoff).

---

## 5. Ролевая модель

### 5.1. Матрица прав (enforced на бэкенде, permission-check в каждом endpoint)

| Право | Админ | Руководитель | Менеджер | Наблюдатель |
|---|:-:|:-:|:-:|:-:|
| Читать все диалоги | ✅ | ✅ | ✅ | ✅ |
| Отправлять сообщения | ✅ | ❌ (только чтение) | ✅ | ❌ |
| Менять статус / передавать диалог | ✅ | ✅ (переназначение) | ✅ | ❌ |
| Внутренние заметки | ✅ | ✅ | ✅ | ❌ |
| Статистика: все сотрудники | ✅ | ✅ | ❌ (только своя) | ❌ |
| Управление шаблонами (общими) | ✅ | ✅ | ❌ (только личные) | ❌ |
| Управление ботами | ✅ | ❌ | ❌ | ❌ |
| Аккаунты Авито (подключение) | ✅ | ❌ | ❌ | ❌ |
| Сотрудники и роли | ✅ | ❌ | ❌ | ❌ |
| Журнал аудита | ✅ | ✅ (просмотр) | ❌ | ❌ |

### 5.2. Как это выглядит для каждой роли

- **Админ** — весь интерфейс: Чаты, Статистика, Настройки (все разделы).
- **Руководитель** — Чаты (все; поле ввода заменено плашкой «Режим просмотра — назначьте менеджера или передайте диалог», доступны заметки и переназначение), Статистика (все сотрудники, фильтр по менеджеру), из настроек — только общие шаблоны. В списке чатов дополнительный фильтр «по менеджеру».
- **Менеджер** — Чаты (вкладки «Мои» / «Все» / «Новые»; отвечать может в любом открытом диалоге), свои личные шаблоны, свой профиль, виджет «моя статистика за сегодня» (диалогов, ср. время ответа). Раздел «Статистика» и «Настройки» команды скрыты.
- **Наблюдатель** — только Чаты в режиме чтения: без поля ввода, без заметок, без смены статусов; фильтры и поиск доступны. Ни статистики, ни настроек. (Роль для стажёров и контроля качества.)

Все действия пишутся в `audit_log` (кто прочитал/написал/передал/изменил настройки) — для внутреннего инструмента это главный инструмент разбора инцидентов.

---

## 6. Интеграция с сайтом partner-lead-centre.ru

- **Поддомен**: `chat.partner-lead-centre.ru` (A-запись на VPS LeadChat, TLS через Let's Encrypt). API — на том же домене под `/api` (без CORS-танцев).
- **SSO**: у основного сайта нет полноценного IdP, поэтому на старте — независимые учётки LeadChat (сотрудников всё равно заводит админ, их десятки, не тысячи). Задел на будущее: страница входа LeadChat умеет принимать подписанный JWT (`/login?sso_token=...`, HS256 с общим секретом, TTL 60 сек) — если на основном сайте появится личный кабинет сотрудника, ссылка «Открыть LeadChat» будет логинить бесшовно. Это ~1 день работы с каждой стороны, когда понадобится.

  > ⚠ 23.08.2026: поле `sso_shared_secret` из конфигурации СНЯТО. Кода у задела нет ни строки — ни ручки `/login?sso_token=`, ни проверки подписи, — а пустое поле рядом с настоящими секретами в `.env.prod.example` создавало у администратора уверенность, что SSO включается заполнением строки. Поле заводится вместе с реализацией, а не до неё.
- **Стилизация**: палитра и логотип Lead Partner (светлая тема как основная — как на сайте, фирменный зелёный `#3cc13b`; тёмная — опционально), шрифт сайта (Proxima Nova → Roboto/Inter), тот же тон коммуникации. Фавикон и название «LeadChat by Lead Partner». Токены — docs/10-UX-DESIGN-SYSTEM.md.
- **Ссылки**: в закрытой части основного сайта — кнопка «Перейти в LeadChat»; на странице входа LeadChat — ссылка на сайт и на скачивание десктоп-приложения (`chat.partner-lead-centre.ru/download` отдаёт свежий инсталлятор `LeadChat-Setup.exe` из CI; NSIS — основной канал, MSI — для GPO).

---

## 7. План разработки

Оценки — для 1–2 fullstack-разработчиков; этапы сдаются работающими инкрементами.

| Этап | Содержание | Срок | Результат |
|---|---|---|---|
| **0. Фундамент** | Repo, Docker Compose, CI, скелеты FastAPI/React, схема БД, миграции, auth (JWT + роли) | 1 нед | Логин в пустой кабинет |
| **1. Ядро Авито (MVP-1)** | OAuth-подключение аккаунта, шифрование и refresh токенов, webhook gateway + Redis Streams + воркер, загрузка истории, reconciliation-поллинг | 2 нед | Сообщения из Авито появляются в БД и в realtime-ленте |
| **2. Рабочее место (MVP-2)** | Экран «Чаты» (3 колонки), WebSocket-доставка, отправка сообщений, статусы диалогов, карточка клиента, шаблоны, заметки, передача коллеге, поиск | 3 нед | **Команда реально работает в LeadChat** (веб) |
| **3. Роли и статистика** | Матрица прав на всех endpoint, UI по ролям, audit_log, экран статистики + экспорт | 1.5 нед | Подключаются руководители и наблюдатели |
| **4. Десктоп** | Tauri-обёртка, трей, нативные уведомления, автозапуск, офлайн-кэш, инсталлятор + автообновление, страница /download | 1.5 нед | MSI для Windows раздаётся сотрудникам |
| **5. Боты** | Движок сценариев (send/ask/menu/condition/handoff), редактор в настройках, расписания, привязка к аккаунтам; затем шаг `ai_answer` + классификатор негатива (Claude API) | 2.5 нед | Первичный приём и ночные диалоги ведёт бот |
| **6. Полировка → релиз 1.0** | Нагрузочная проверка, Sentry, бэкапы (pg_dump + офсайт), онбординг-инструкция для сотрудников, приёмка | 1 нед | Релиз |

**Итого до полного релиза: ~12–13 недель; команда начинает работать в системе уже на 6-й неделе** (после этапа 2). Фаза 2.0 (после релиза): Telegram-адаптер, round-robin распределение, AI-подсказки оператору, визуальный конструктор ботов, мобильное PWA.

Риски: (1) квоты/модерация API Авито — заявку на доступ к Messenger API подать в первый же день этапа 0; (2) формат вебхуков меняется — парсер в одном месте (`AvitoAdapter.parse_webhook`) + сырые payload'ы складываем в лог-таблицу 30 дней; (3) недоставка вебхуков — закрыта reconciliation-поллингом.

---

## 8. Примеры кода

> Эндпоинты и поля Авито сверить с актуальной документацией https://developers.avito.ru при старте — формат ниже соответствует Messenger API v3 / OAuth v1.

### 8.1. OAuth 2.0 Авито: подключение аккаунта и refresh

```python
# app/integrations/avito/oauth.py
import httpx, secrets
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from app.core.crypto import encrypt, decrypt          # AES-256-GCM

AVITO_TOKEN_URL = "https://api.avito.ru/token"
# База OAuth-страницы — из settings (env AVITO_AUTH_URL, 05 §4 / 08 §1.3):
# в тестах подменяется на страницу-заглушку fake-avito (07 §2)

def build_authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    return settings.avito_auth_url + "?" + urlencode({
        "response_type": "code",
        "client_id": settings.avito_client_id,
        "scope": "messenger:read,messenger:write,user:read",
        "state": state,  # CSRF: генерируем, кладём в Redis с TTL 10 мин
    })

async def exchange_code(code: str) -> dict:
    """authorization_code -> первая пара токенов."""
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(AVITO_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.avito_client_id,
            "client_secret": settings.avito_client_secret,
        })
        r.raise_for_status()
        return r.json()   # {access_token, refresh_token, expires_in, ...}

async def refresh_tokens(account, db, redis) -> None:
    """Refresh с локом: refresh_token одноразовый, двойное использование сжигает его."""
    lock = await redis.set(f"lock:token:{account.id}", "1", nx=True, ex=30)
    if not lock:
        return  # кто-то уже обновляет
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(AVITO_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": decrypt(account.refresh_token_enc),
                "client_id": settings.avito_client_id,
                "client_secret": settings.avito_client_secret,
            })
        if r.status_code == 400:                     # доступ отозван
            account.status = "needs_reauth"
            await db.commit()
            await notify_admins(f"Аккаунт «{account.title}» требует переподключения")
            return
        r.raise_for_status()
        data = r.json()
        account.access_token_enc = encrypt(data["access_token"])
        account.refresh_token_enc = encrypt(data["refresh_token"])  # новый!
        account.token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=data["expires_in"] - 300)        # запас 5 минут
        account.status = "active"
        await db.commit()
    finally:
        await redis.delete(f"lock:token:{account.id}")

# Планировщик (scheduler-процесс): каждые 30 минут
#   SELECT * FROM avito_accounts
#   WHERE status='active' AND token_expires_at < now() + interval '2 hours'
# -> refresh_tokens() по каждому.
```

```python
# app/api/routes/avito_connect.py — endpoints подключения (только admin)
@router.get("/avito/connect")
async def avito_connect(user=Depends(require_role("admin")), redis=Depends(get_redis)):
    state = secrets.token_urlsafe(32)
    await redis.set(f"oauth_state:{state}", user.id, ex=600)
    return RedirectResponse(build_authorize_url(state))

@router.get("/avito/callback")
async def avito_callback(code: str, state: str, db=Depends(get_db), redis=Depends(get_redis)):
    if not await redis.getdel(f"oauth_state:{state}"):
        raise HTTPException(403, "Invalid state")
    tokens = await exchange_code(code)
    profile = await avito_get_self(tokens["access_token"])   # GET /core/v1/accounts/self
    account = await upsert_avito_account(db, profile, tokens)
    await setup_webhook(account)                              # см. 8.3
    await enqueue_history_backfill(account.id)                # догрузка истории
    return RedirectResponse("/settings/accounts?connected=1")
```

### 8.2. Отправка сообщения через API Авито

```python
# app/integrations/avito/adapter.py
class AvitoAdapter:
    BASE = "https://api.avito.ru"

    async def send_message(self, account, chat_id: str, text: str) -> str:
        """Возвращает external_message_id. Одна попытка авто-рефреша на 401."""
        for attempt in (1, 2):
            token = decrypt(account.access_token_enc)
            async with httpx.AsyncClient(timeout=15) as http:
                r = await http.post(
                    f"{self.BASE}/messenger/v1/accounts/{account.avito_user_id}"
                    f"/chats/{chat_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"message": {"text": text}, "type": "text"},
                )
            if r.status_code == 401 and attempt == 1:
                await refresh_tokens(account, self.db, self.redis)
                await self.db.refresh(account)
                continue
            if r.status_code == 429:
                raise RateLimited(retry_after=int(r.headers.get("Retry-After", 5)))
            r.raise_for_status()
            return r.json()["id"]

# app/api/routes/messages.py — endpoint для UI
@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: UUID, payload: SendMessageIn,
                       user=Depends(require_permission("messages:send")),  # admin|manager
                       db=Depends(get_db)):
    conv = await get_conversation_or_404(db, conv_id)
    msg = await create_outbound_message(db, conv, user, payload.text)  # status=pending
    await arq.enqueue_job("deliver_message", msg.id)   # доставка в фоне, с ретраями
    return MessageOut.from_orm(msg)

# worker: deliver_message -> AvitoAdapter.send_message
#   успех  -> delivery_status=delivered + Pub/Sub событие в WebSocket Hub
#   ошибка -> ретраи (экспоненциально, 5 попыток) -> failed + кнопка «повторить» в UI
```

### 8.3. Приём вебхука (входящее сообщение)

```python
# app/api/routes/webhooks.py
@router.post("/hooks/avito/{account_id}")
async def avito_webhook(account_id: UUID, request: Request,
                        redis=Depends(get_redis), db=Depends(get_db)):
    account = await get_account(db, account_id)
    if not account or not secrets.compare_digest(
            request.query_params.get("secret", ""), account.webhook_secret):
        raise HTTPException(403)
    payload = await request.json()
    # Ничего не обрабатываем здесь: кладём в очередь и мгновенно отвечаем 200,
    # иначе Авито сочтёт webhook нерабочим и отключит его.
    await redis.xadd("webhooks:avito", {
        "account_id": str(account_id),
        "payload": json.dumps(payload),
    })
    return {"ok": True}


# app/workers/inbound.py — consumer group поверх Redis Streams
async def process_webhook_entry(entry_id, fields, db, redis):
    account = await get_account(db, UUID(fields["account_id"]))
    event = AvitoAdapter().parse_webhook(json.loads(fields["payload"]))
    # event: chat_id, message_id, author_id, text, item_info, created_at

    if event.author_id == account.avito_user_id:
        return  # эхо нашего же исходящего (отправлено с телефона/сайта Авито)

    async with db.begin():
        client = await upsert_client(db, event)                # + рейтинг из API
        conv = await upsert_conversation(db, account, client, event)
        inserted = await insert_message_idempotent(db, conv, event)
        if not inserted:
            return                                             # дубль (retry вебхука)
        conv.last_message_at = event.created_at
        if conv.status == "closed":
            conv.status, conv.assignee_id = "new", None        # клиент вернулся
        maybe_extract_phone(conv, event.text)                  # телефон -> карточка

    await redis.publish("events", json.dumps({                 # -> WebSocket Hub
        "type": "message:new",
        "conversation_id": str(conv.id),
        "message": event.as_ui_dict(),
    }))

    if should_run_bot(conv, account):     # новый диалог, бот включён, оператор молчит
        await arq.enqueue_job("bot_step", conv.id, event.text)
```

```python
# Регистрация вебхука при подключении аккаунта
async def setup_webhook(account) -> None:
    token = decrypt(account.access_token_enc)
    url = (f"https://chat.partner-lead-centre.ru/api/hooks/avito/"
           f"{account.id}?secret={account.webhook_secret}")
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post("https://api.avito.ru/messenger/v3/webhook",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"url": url})
        r.raise_for_status()
```

---

## 9. Безопасность (сводно)

- **Сессии**: access JWT 15 мин + refresh-cookie (httpOnly, Secure, SameSite=Lax) 14 дней с ротацией; logout отзывает refresh (denylist в Redis). Десктоп использует тот же механизм.
- **Пароли**: argon2id; заведение сотрудника — только одноразовой ссылкой; блокировка после 10 неудачных попыток (по IP и по учётке).
- **RBAC на бэкенде**: каждый endpoint — `require_permission(...)`; UI-скрытие ролей — только UX.
- **Токены Авито**: AES-256-GCM, ключ вне БД; в логах — фильтр секретов; refresh под Redis-локом.
- **Webhook endpoint**: секрет в URL + `compare_digest`; rate-limit; тело > 1 МБ отклоняется.
- **API**: HTTPS-only (HSTS), rate-limit на login, CORS только на свой домен, Pydantic-валидация всего входного.
- **Данные**: ежедневный `pg_dump` + офсайт-копия; media — недоступны без подписанной ссылки; audit_log на все значимые действия.
- **Десктоп**: MSI подписан; автообновления проверяют подпись (tauri-updater); локальный SQLite-кэш шифруется ключом из Windows DPAPI.
