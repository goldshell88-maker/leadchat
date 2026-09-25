# LeadChat — спецификация REST API и WebSocket-протокола

Документ детализирует API-контракт платформы, спроектированной в [DESIGN.md](../DESIGN.md).
Стек, схема БД, роли и общая архитектура — там; здесь — только контракт, по которому фронтенд
(веб + Tauri) и бэкенд пишутся независимо.

Статус: **v1, готово к реализации** · Все примеры — рабочие payload'ы, поля соответствуют
таблицам `users / avito_accounts / clients / conversations / messages / bots / templates / audit_log`.

---

## 1. Общие соглашения

### 1.1. Базовый URL и версионирование

```
https://chat.partner-lead-centre.ru/api/v1
```

- Всё публичное API живёт под `/api/v1`. Ломающие изменения → `/api/v2` (не планируется до фазы 2.0).
- Исключения, живущие вне версии:
  - `POST /api/hooks/avito/{account_id}` — вебхуки Авито (URL зарегистрирован на стороне Авито, версионировать нельзя, см. раздел 10);
  - `GET /api/ws` — WebSocket (см. раздел 11);
  - `GET /api/health` — liveness для Uptime-Kuma и docker-healthcheck (без авторизации, отвечает `{"status":"ok","db":true,"redis":true,"version":"..."}`);
  - `GET /api/health/deep` — расширенная диагностика для мониторинга: лаг очереди, протухающие токены, failed-доставки (контракт — 05-DEPLOY-OPS §7.2).
- `Content-Type: application/json; charset=utf-8` везде, кроме загрузки медиа (`multipart/form-data`) и экспорта статистики (CSV/XLSX).
- CORS не нужен: SPA и API на одном origin (DESIGN §6). Tauri ходит на тот же origin через https.

### 1.2. Аутентификация

Механизм из DESIGN §9 (не менять): access JWT 15 мин + refresh-cookie 14 дней с ротацией.

- Каждый запрос (кроме `auth/*`, health, вебхуков): заголовок `Authorization: Bearer <access_jwt>`.
- JWT payload: `{"sub": "<user.id>", "role": "manager", "exp": ..., "iat": ..., "jti": "..."}`.
  Роль кладём в токен только как кэш для UI; **проверка прав на бэкенде всегда идёт по строке из БД**
  (роль могли сменить, `is_active` могли снять — токен живёт максимум 15 минут, это допустимое окно,
  но деактивация дополнительно кладёт `user.id` в Redis-denylist `revoked_users` на 15 мин, и middleware её проверяет).
- Refresh-токен — только в httpOnly-cookie `lc_refresh` (Secure, SameSite=Lax, Path=`/api/v1/auth`).
  В теле ответов refresh не появляется никогда.

Ответ на невалидный/просроченный access — всегда `401 unauthorized` (фронт по нему делает
`POST /auth/refresh` и повторяет запрос; если и refresh дал 401 — редирект на `/login`).

### 1.3. Единый формат ошибок

Любой не-2xx ответ — один и тот же envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Поле text не может быть пустым",
    "details": {
      "fields": [{ "field": "text", "rule": "min_length", "message": "минимум 1 символ" }]
    },
    "request_id": "req_9f2c1a7b"
  }
}
```

- `code` — машиночитаемый, стабильный (по нему ветвится фронт). `message` — человекочитаемый, по-русски, можно показывать пользователю. `details` — опционален, структура зависит от кода. `request_id` — сквозной id запроса (тот же уходит в Sentry и JSON-логи).
- Реализация: один exception-handler FastAPI поверх `HTTPException` и кастомного `ApiError(code, message, status, details)`; ошибки Pydantic конвертируются в `validation_error` с `details.fields`.

Каталог кодов (общие для всех endpoints — в описаниях ниже не повторяются):

| HTTP | code | Когда |
|---|---|---|
| 400 | `validation_error` | Тело/параметры не прошли Pydantic-валидацию |
| 401 | `unauthorized` | Нет/просрочен/битый access JWT |
| 403 | `forbidden` | Права роли не позволяют (`require_permission` не прошёл) |
| 404 | `not_found` | Сущность не существует **или** недоступна (не раскрываем существование) |
| 409 | `conflict` | Нарушение уникальности / конфликт состояния (уточняется в `details.reason`) |
| 413 | `payload_too_large` | Тело больше лимита (JSON 256 КБ, media 20 МБ, webhook 1 МБ) |
| 422 | `unprocessable` | Синтаксически валидно, семантически нет (например, закрыть уже закрытый диалог) |
| 429 | `rate_limited` | Превышен rate-limit; ответ несёт заголовок `Retry-After` (сек) |
| 500 | `internal_error` | Наша ошибка; `message` всегда generic, детали только в Sentry |
| 503 | `upstream_unavailable` | Авито/Claude недоступны и запрос нельзя выполнить фоново |

Специфичные коды (описаны у своих endpoints): `invalid_credentials`, `account_locked`,
`invite_expired`, `account_needs_reauth`, `message_undeliverable`, `bot_scenario_invalid`,
`idempotency_mismatch`, `read_only_role`.

### 1.4. Пагинация

Две схемы, выбор не случаен:

**Cursor-based — только лента сообщений** (`GET /conversations/{id}/messages`).
Offset по партиционированной таблице с постоянной вставкой в хвост даёт дубли/пропуски при
скролле — поэтому keyset-курсор по `(created_at, id)`:

```
GET /api/v1/conversations/{id}/messages?limit=50&before=<cursor>
```

- `cursor` — непрозрачная строка: `base64url(created_at_iso + "|" + message_id)`. Клиент её не парсит.
- `before=<cursor>` — сообщения **старее** курсора (скролл вверх), `after=<cursor>` — **новее** (догон после reconnect). Одновременно нельзя (`validation_error`).
- Без курсора — последние `limit` сообщений (первое открытие диалога).
- Ответ всегда несёт готовые курсоры:

```json
{
  "items": [ { "...": "см. MessageOut, отсортированы по created_at ASC" } ],
  "page": {
    "prev_cursor": "MjAyNi0wOC0wM1QxMDoxMjowMFp8YzRl...",
    "next_cursor": "MjAyNi0wOC0wNFQwOTowMTowMFp8ZjJh...",
    "has_more_before": true,
    "has_more_after": false
  }
}
```

**Offset-based — все остальные списки** (диалоги, сотрудники, шаблоны, боты, аудит):

```
GET /api/v1/conversations?limit=50&offset=0
```

- `limit`: default 50, max 200. `offset`: default 0.
- Ответ:

```json
{
  "items": [ ... ],
  "page": { "limit": 50, "offset": 0, "total": 873 }
}
```

`total` считается `count(*)` по тем же фильтрам; для списка диалогов на наших объёмах
(десятки тысяч строк) это дёшево, кэшировать не нужно.

### 1.5. Даты и время

- Все timestamps в API — **ISO 8601 в UTC с суффиксом `Z`**, миллисекунды опциональны:
  `"2026-08-04T09:41:03.512Z"`. В БД — `timestamptz`, конвертаций «в московское» на бэкенде нет.
- Отображение в локальном времени — задача фронта (`Intl.DateTimeFormat`, TZ пользователя).
- Входные параметры-даты (`date_from`, `date_to` в статистике) — либо полный ISO-timestamp,
  либо `YYYY-MM-DD` (интерпретируется как граница суток в TZ из query-параметра `tz`,
  default `Europe/Moscow` — отчёты «за день» должны совпадать с интуицией сотрудников).

### 1.6. Идемпотентность отправки сообщений

Проблема: менеджер нажал «отправить», сеть моргнула, фронт ретраит `POST` — без защиты клиент
получит два одинаковых сообщения. Особенно критично для Tauri-офлайн-очереди (DESIGN §3.6).

Решение — `client_message_id`:

- Клиент генерирует UUID v4 **на каждое нажатие «отправить»** (не на каждый HTTP-ретрай!) и шлёт в теле `POST /conversations/{id}/messages`.
- Бэкенд перед вставкой делает `SET NX idem:msg:{conversation_id}:{client_message_id} = <message_id>, EX 86400` в Redis.
  - Ключ поставился → это первая попытка: создаём сообщение, отвечаем `201`.
  - Ключ уже есть → возвращаем **уже созданное** сообщение с кодом `200` (не 201) и заголовком `X-Idempotent-Replay: true`. Тело идентично первому ответу.
- Если ключ есть, но текст в повторе отличается — `409 idempotency_mismatch` (клиент сгенерировал client_message_id неправильно, это баг фронта, его надо увидеть).
- TTL 24 ч покрывает офлайн-очередь десктопа с запасом.
- То же поле обязателен для заметок (`POST .../notes`) — они тоже уходят из офлайн-очереди.
- `client_message_id` возвращается в `MessageOut` (и, как следствие, в WS-событии `message:new`) —
  десктоп дедуплицирует по нему ⏳-строки офлайн-очереди (04-DESKTOP §5.4). Для сообщений,
  созданных не через API (входящие, бот, system), поле `null`.

### 1.7. Rate limits (per-user, скользящее окно в Redis)

| Область | Лимит |
|---|---|
| `POST /auth/login` | 10/мин на IP **и** 10/мин на email (после — `account_locked`, DESIGN §9) |
| Отправка сообщений | 60/мин на пользователя |
| Остальной API | 600/мин на пользователя |
| Webhook endpoint | 100/сек на аккаунт (защита от шторма, дальше 429 — Авито ретраит) |

---

## 2. Auth

Права: не требуются (это и есть вход), кроме `GET /auth/me`.

### 2.1. `POST /auth/login`

```json
// Запрос
{ "email": "anna@partner-lead-centre.ru", "password": "•••", "remember": true }
```

```json
// 200
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "0d4f0a1e-6b7c-4b1e-9a2d-1f3e5c7a9b0d",
    "email": "anna@partner-lead-centre.ru",
    "full_name": "Анна Смирнова",
    "role": "manager",
    "is_active": true
  }
}
```

+ `Set-Cookie: lc_refresh=<token>; HttpOnly; Secure; SameSite=Lax; Path=/api/v1/auth; Max-Age=1209600`
  (`remember: false` → session-cookie без Max-Age).

Ошибки: `401 invalid_credentials` (одинаковый ответ для «нет такого email» и «неверный пароль»),
`403 account_locked` (`details.retry_after_sec`), `403 forbidden` если `is_active = false`
(`message`: «Учётная запись отключена, обратитесь к администратору»).

Побочный эффект: запись в `audit_log` (`action: "auth.login"`).

### 2.2. `POST /auth/refresh`

Тело пустое; refresh берётся из cookie. Ответ — как у login (новый access) + **ротация**:
старый refresh попадает в Redis-denylist, выдаётся новый `Set-Cookie`. Повторное использование
уже отротированного refresh → `401 unauthorized` + отзыв всей цепочки токенов пользователя
(классическая защита от кражи cookie).

### 2.3. `POST /auth/logout`

Тело пустое. `204 No Content`. Refresh — в denylist, cookie гасится (`Max-Age=0`).
`audit_log: "auth.logout"`.

### 2.4. Установка пароля по invite-ссылке

Приглашение создаёт админ (см. §3.2), сотрудник получает ссылку вида
`https://chat.partner-lead-centre.ru/invite/<token>` (страница фронта).

**`GET /auth/invite/{token}`** — валидация токена перед показом формы:

```json
// 200
{ "email": "gleb@partner-lead-centre.ru", "full_name": "Глеб Беляков" }
```

`404 invite_expired` — токена нет/просрочен/использован (одинаково, не раскрываем причину).

**`POST /auth/invite/accept`**

```json
// Запрос
{ "token": "inv_XkQ9...64url", "password": "минимум 10 символов" }
```

```json
// 200 — сразу логиним: тело как у POST /auth/login (+ refresh-cookie)
```

Ошибки: `404 invite_expired`, `400 validation_error` (слабый пароль: < 10 символов).
Токен одноразовый: хранится в Redis `invite:{token} → user_id`, TTL 72 ч, `GETDEL` при accept.
Пароль → `argon2id` → `users.password_hash`. `audit_log: "user.invite_accepted"`.

### 2.5. `GET /auth/me` — право: любой аутентифицированный

Возвращает объект `user` (как в login) + вычисленный набор прав:

```json
{
  "id": "0d4f0a1e-...", "email": "anna@...", "full_name": "Анна Смирнова",
  "role": "manager", "is_active": true,
  "permissions": ["conversations:read", "messages:send", "conversations:manage",
                  "notes:write", "templates:own", "stats:own"]
}
```

Фронт строит UI по `permissions`, не по `role` — при изменении матрицы прав фронт менять не придётся.

---

## 3. Users (сотрудники)

Все endpoints — право `users:manage` (**только admin**), кроме оговорённых.

### 3.1. `GET /users` — список

Query: `limit`, `offset`, `q` (поиск по имени/email), `role`, `is_active`.

```json
// 200
{
  "items": [
    {
      "id": "0d4f0a1e-...", "email": "anna@partner-lead-centre.ru",
      "full_name": "Анна Смирнова", "role": "manager",
      "is_active": true, "is_online": true,
      "invite_pending": false,
      "created_at": "2026-06-01T08:00:00Z"
    }
  ],
  "page": { "limit": 50, "offset": 0, "total": 14 }
}
```

- `is_online` — из presence-реестра WebSocket-хаба (Redis `presence:*`, см. §11.6).
- `invite_pending: true` — приглашён, пароль ещё не установил.
- Для экрана «Передать коллеге» менеджеру нужен список сотрудников → отдельный облегчённый
  endpoint **`GET /users/assignable`** (право `conversations:manage`, т.е. admin/head/manager):
  только `id, full_name, role, is_online` активных пользователей ролей admin/manager.

### 3.2. `POST /users` — пригласить сотрудника

```json
// Запрос
{ "email": "gleb@partner-lead-centre.ru", "full_name": "Глеб Беляков", "role": "manager" }
```

```json
// 201
{
  "user": { "id": "7c1b...", "email": "gleb@...", "full_name": "Глеб Беляков",
            "role": "manager", "is_active": true, "invite_pending": true,
            "created_at": "2026-08-04T10:00:00Z" },
  "invite_url": "https://chat.partner-lead-centre.ru/invite/inv_XkQ9...",
  "invite_expires_at": "2026-08-07T10:00:00Z"
}
```

`invite_url` показывается админу один раз — он сам передаёт её сотруднику (почтового сервиса в MVP нет).
В `users.password_hash` пишется случайный недостижимый хэш до момента accept.
Ошибки: `409 conflict` (`details.reason: "email_taken"`). `audit_log: "user.invited"`.

### 3.3. `POST /users/{id}/invite` — перевыпустить ссылку

`200` с телом как у 3.2 (новый токен, старый гасится). `422 unprocessable`, если пароль уже установлен.

### 3.4. `PATCH /users/{id}` — имя / роль

```json
// Запрос (любое подмножество)
{ "full_name": "Глеб А. Беляков", "role": "head" }
```

`200` → обновлённый объект user. Смена роли действует немедленно (см. §1.2 про denylist).
Ошибки: `422 unprocessable` — админ понижает сам себя, оставляя систему без админов
(`details.reason: "last_admin"`). `audit_log: "user.role_changed"` с `details: {from, to}`.

### 3.5. `POST /users/{id}/deactivate` / `POST /users/{id}/activate`

`200` → объект user. Деактивация: refresh-токены в denylist, `user.id` в `revoked_users` (мгновенный
разлогин), presence сбрасывается, открытые диалоги **не** переназначаются автоматически (руководитель
видит их через фильтр «по менеджеру» и передаёт вручную — осознанное решение, не терять контекст).
`422 unprocessable` при деактивации последнего активного админа. Удаления пользователей нет —
только деактивация (на `users.id` ссылаются `messages`, `conversations`, `audit_log`).

---

## 4. Avito-accounts

Все endpoints — право `accounts:manage` (**только admin**), кроме `GET /avito-accounts`
(право `accounts:read` — admin и head: руководителю нужен фильтр по аккаунту в чатах и статистике;
head видит список **без** управляющих кнопок).

### 4.1. `GET /avito-accounts`

```json
// 200
{
  "items": [
    {
      "id": "b2a4c6e8-...", "title": "LP-Москва",
      "avito_user_id": 123456789,
      "status": "active",                      // active | needs_reauth | disabled
      "token_expires_at": "2026-08-05T06:10:00Z",
      "webhook": { "status": "ok",             // ПАМЯТЬ о последней попытке подписаться:
                                               // ok | failed | not_registered | unregister_failed
                   "url": "https://chat.partner-lead-centre.ru/api/hooks/avito/b2a4c6e8-...",
                   "last_event_at": "2026-08-04T09:58:12Z",
                   "state": "ok",              // ok | quiet | warning | critical — ЧТО ПОКАЗЫВАТЬ
                   "reason": null,             // машинный повод; null — всё штатно
                   "message": "События приходят, последнее в 19:58. Сверка подписки 16:54, расхождений нет.",
                   "headline": "События приходят.",          // одна фраза: что и чем грозит
                   "detail": "Последнее в 19:58. Сверка подписки 16:54, расхождений нет.",
                   "silence_minutes": 4, "quiet_minutes": 4,
                   "rhythm_minutes": 32, "rhythm_samples": 118, "rhythm_window_days": 14,
                   "threshold_minutes": 96,
                   "checked_at": "2026-08-04T09:54:00Z", "check_result": "ours",
                   "action": null },           // подпись кнопки: null | "rewebhook"
      "token": { "state": "ok", "reason": null,
                 "message": "Токен активен. Обновится автоматически 13.08 в 16:07",
                 "headline": "Токен активен.",
                 "detail": "Обновится автоматически 13.08 в 16:07.",
                 "expires_at": "2026-08-05T06:10:00Z", "expires_in_minutes": 1380,
                 "auto_refresh_at": "2026-08-05T04:10:00Z",
                 "last_refresh_at": "2026-08-04T06:10:00Z", "last_refresh_ok": true,
                 "last_error": null, "failures": 0,
                 "action": null },             // null | "refresh_token"
      "bot_id": "e1f2a3b4-...",
      "created_at": "2026-06-02T12:00:00Z"
    }
  ],
  "page": { "limit": 50, "offset": 0, "total": 6 }
}
```

`webhook.last_event_at` — время последнего принятого вебхука (Redis `webhook_last:{account_id}`).

> **⚠ ИСПРАВЛЕНО 13.08. Здесь стояло правило, отменённое ещё 12 августа:** «если > 15 мин
> при наличии активности в reconciliation — фронт показывает ⚠ у статуса». Это ровно тот
> расчёт на стороне фронта, из-за которого экран горел жёлтым круглосуточно: «⚠️ Токен
> истекает через 23 ч» на всех каналах, при том что токен Авито живёт сутки и 23 часа
> остатка — норма. Люди читали строку как аварию и жали «Обновить токен» руками каждый
> день. Цена такого индикатора не в лишних нажатиях: он приучает не смотреть и туда, где
> однажды загорится настоящая поломка.
>
> **Действующее правило: решает СЕРВЕР, экран рисует присланное.** Поля `state` и
> `message` считает `app/services/channel_health.py` — он один знает и ритм канала
> (обычная пауза этого канала, а не время на стене), и результат сверки подписки.
> Фронту читать остальные поля и собирать фразу самому ЗАПРЕЩЕНО: возьмёт хоть одно
> число — и немедленно заведёт по нему своё правило, то самое. Запрет записан в коде,
> `ChannelHealth.tsx`.
>
> Документ отстал от кода на месяц и описывал отменённое поведение как действующее —
> а следующий читатель верит документу. Папка `docs/` вне git, поэтому расхождение
> ничем и не ловилось.

> **`headline` + `detail` — та же фраза, разделённая на две (с 13.08).** Янтарный абзац
> выходил на пять-шесть строк в колонке карточки: 190 знаков у затянувшейся тишины.
> `headline` — что случилось и чем грозит, одной фразой; `detail` — числа, обычный ритм
> канала, время сверки, причина отказа. Экран рисует первую янтарной со значком, вторую
> мельче и серым.
>
> **`message` при этом НЕ укорочен и остаётся полем-страховкой.** Сервер и фронт
> выкатываются порознь: старая сборка экрана читает только `message` и обязана показать
> фразу целиком, иначе в окно между выкатками человек теряет половину. Новая сборка при
> старом сервере видит пустой `headline` и рисует `message`. Оба новых поля
> необязательны в обе стороны. Складывать их обратно в одно можно будет, когда обе
> стороны выкачены.
>
> Делит **сервер**, и это то же правило, что абзацем выше: экран получает две ГОТОВЫЕ
> строки и не разбирает ни одну из них.

> **Образец выше показывает блоки `webhook` и `token` целиком, но сам ответ шире.**
> Сверено с живым ответом стенда 13.08: строка аккаунта несёт ещё `own_keys`,
> `is_service`, `lead_src_key`, `backfill` (объект прогресса загрузки истории),
> `operators` (`count` + `preview`), `stats` и `created`. Они здесь не расписаны —
> у этого раздела правилась ровно та часть, что врала. Разворачивать остальное надо
> тем же способом: сверкой с ответом, а не по памяти.
Токены (`access_token_enc`/`refresh_token_enc`) в API **не отдаются никогда**, `webhook_secret` — тоже.

### 4.2. `GET /avito/connect-url` — начать OAuth

DESIGN §8.1 описывает `GET /avito/connect` с 302-редиректом. Детализация: браузерный переход
(`window.location.href`) не несёт заголовок `Authorization`, поэтому проверить право админа на
redirect-endpoint'е нечем. Принятый вариант — тот же код §8.1 (генерация `state` в Redis,
`build_authorize_url`), но endpoint отвечает JSON'ом на обычный Bearer-запрос, а редирект делает фронт:

```json
// 200
{ "url": "https://avito.ru/oauth?response_type=code&client_id=...&state=..." }
```

### 4.3. `GET /avito/callback?code=...&state=...`

Публичный (приходит браузером админа с Авито). Логика — DESIGN §8.1: проверка `state`,
обмен кода, upsert в `avito_accounts`, регистрация вебхука, постановка backfill-задачи в ARQ.
Ответ — `302` на `/settings/accounts?connected=1` (или `?error=oauth_failed`).
`audit_log: "account.connected"`.

### 4.4. `POST /avito-accounts/{id}/reconnect`

Для статуса `needs_reauth`: `200` → `{"url": "https://avito.ru/oauth?..."}"` — тот же OAuth-флоу,
`state` в Redis помечен `reconnect:{account_id}`, callback обновляет токены существующей записи
(матчинг по `avito_user_id`; если админ авторизовал **другой** аккаунт Авито — `302` на
`/settings/accounts?error=account_mismatch`, ничего не пишем).

### 4.5. `POST /avito-accounts/{id}/disable` / `POST /avito-accounts/{id}/enable`

`200` → объект аккаунта. Disable: снимаем вебхук на стороне Авито (`DELETE`-вызов адаптера),
`status = 'disabled'`, reconciliation и refresh токенов по аккаунту останавливаются; диалоги и история
остаются читаемыми. Enable — обратно (если токены живы, иначе `422` c `details.reason: "needs_reauth"`).
`audit_log: "account.disabled" / "account.enabled"`.

### 4.6. `PATCH /avito-accounts/{id}`

```json
{ "title": "LP-Москва (Савёловская)", "bot_id": "e1f2a3b4-..." }   // bot_id: null — отвязать бота
```

`200` → объект аккаунта. `422`, если `bot_id` указывает на несуществующего/выключенного бота — привязать можно, предупреждение отдаём в `details.warning: "bot_disabled"` (это не ошибка).

### 4.7. `POST /avito-accounts/{id}/webhook/check`

Ручная проверка/перерегистрация вебхука (кнопка в UI): дергает Авито `POST /messenger/v3/webhook`
заново. `200` → блок `webhook` как в §4.1. `503 upstream_unavailable`, если Авито недоступен.

---

## 5. Conversations

Право на чтение: `conversations:read` — **все роли** (в том числе observer).
Право на управление (статус/назначение): `conversations:manage` — admin, head, manager.

### 5.1. `GET /conversations` — список (левая колонка)

Query-параметры (все опциональны, комбинируются):

| Параметр | Тип | Смысл |
|---|---|---|
| `tab` | `mine \| all \| new \| closed` | Вкладки UI. `mine`: `assignee_id = я` и `status != closed`. `all`: `status != closed`. `new`: `status = new` (не назначен). `closed`: `status = closed`. Default `all` |
| `q` | string | Поиск: имя клиента (`clients.name ILIKE`), телефон (нормализованный `clients.phone`), текст сообщений (FTS `messages.search @@ websearch_to_tsquery('russian', q)`) |
| `account_id` | uuid | Фильтр по аккаунту Авито |
| `assignee_id` | uuid | Фильтр по менеджеру (UI показывает его admin/head) |
| `unread_only` | bool | Только с непрочитанными |
| `tag` | string | Фильтр по тегу диалога (`conversations.tags @> ARRAY[tag]`, DESIGN §4.4), напр. `tag=негатив` |
| `updated_since` | ISO-дата | Диалоги, изменившиеся после метки — **для догона после reconnect WS** (§11.7) и дельта-синка десктопа (04 §5.4). Работает по колонке `conversations.updated_at` (DESIGN §4.4; обновляется при любом изменении строки: новое сообщение, смена статуса, назначение) |
| `limit`, `offset` | int | §1.4 |

Сортировка фиксированная (как в DESIGN §3.3): сначала с `unread_count > 0` (внутри этой группы диалоги с тегом «негатив» — первыми, DESIGN §4.3), внутри групп — по `last_message_at DESC`.

```json
// 200
{
  "items": [
    {
      "id": "c4e5f6a7-...",
      "status": "in_progress",
      "channel": "avito",
      "account": { "id": "b2a4c6e8-...", "title": "LP-Москва" },
      "client": { "id": "d1e2f3a4-...", "name": "Иван Петров", "phone": "+79261234567",
                  "avito_rating": 4.9 },
      "assignee": { "id": "0d4f0a1e-...", "full_name": "Анна Смирнова" },
      "item": { "title": "Ремонт iPhone 13", "url": "https://avito.ru/...", "price": "от 1500 ₽" },
      "last_message": { "body": "А сколько будет стоить замена экрана?",
                        "direction": "in", "created_at": "2026-08-04T09:40:12Z" },
      "unread_count": 2,
      "bot_active": false,
      "tags": ["негатив"],
      "transferred_to_me": true,
      "last_message_at": "2026-08-04T09:40:12Z"
    }
  ],
  "page": { "limit": 50, "offset": 0, "total": 120 }
}
```

- `unread_count` — по read-маркерам (Redis hash `read:{user_id} → {conversation_id: last_read_message_created_at}`; непрочитанные = входящие новее маркера). Маркер двигает `POST /conversations/{id}/read`.
- `transferred_to_me` — значок ⚑: диалог назначен мне другим пользователем, и я ещё не открывал его после назначения.

### 5.2. `GET /conversations/{id}` — деталь (шапка + правая карточка)

```json
// 200 — всё из элемента списка выше, плюс:
{
  "id": "c4e5f6a7-...",
  "...": "поля как в списке",
  "bot_vars": { "problem": "разбит экран iPhone 13", "phone": "+79261234567" },
  "external_chat_id": "u2i-abc123",
  "first_client_at": "2026-08-03T18:22:00Z",
  "client_conversations_count": 3
}
```

`first_client_at` — время первого входящего сообщения клиента (колонки `created_at` у
`conversations` нет — DESIGN §4.4, обоснование в 06-STATS-REPORTS §1.2).

`404 not_found` — нет диалога. Наблюдатель получает тот же ответ (чтение доступно всем ролям).

### 5.3. `POST /conversations/{id}/read` — отметить прочитанным

Право: `conversations:read`. Тело пустое. `204`. Двигает read-маркер текущего пользователя на
последнее сообщение диалога; WS-событие `conversation:updated` уходит остальным сессиям этого же
пользователя (синхронизация бейджей веб ↔ десктоп).

### 5.4. `PATCH /conversations/{id}/status` — смена статуса

Право: `conversations:manage`.

```json
// Запрос
{ "status": "closed" }        // new | in_progress | closed
```

`200` → объект диалога. Правила переходов:
- `closed` → `new` руками не делается: клиент написал — воркер сам переоткрывает (DESIGN §8.3);
  но менеджер может `closed → in_progress` («вернуть в работу», назначается на него).
- `new → in_progress` автоматически происходит при первом ответе менеджера (см. §6.2) —
  этим endpoint'ом пользуются для ручных случаев.
- Повторная установка того же статуса → `422 unprocessable` (`details.reason: "same_status"`).

`audit_log: "conversation.status_changed"` с `{from, to}`. WS: `conversation:updated` всем.

### 5.5. `POST /conversations/{id}/assign` — назначить / передать

Право: `conversations:manage`. Head пользуется этим же endpoint'ом (переназначение — его основное право).

```json
// Запрос
{
  "assignee_id": "7c1b...",        // null — снять назначение (вернуть в «Новые»)
  "comment": "торгуется, дай скидку до 10% — я согласовал"   // опционально
}
```

```json
// 200 — объект диалога + созданная системная запись:
{
  "conversation": { "...": "как в §5.2, assignee обновлён, status: in_progress" },
  "system_message": {
    "id": "a9b8c7d6-...", "direction": "system", "sender_type": "system",
    "body": "Диалог передан: Анна Смирнова → Глеб Беляков. Комментарий: торгуется, дай скидку до 10% — я согласовал",
    "created_at": "2026-08-04T10:05:00Z"
  }
}
```

- Комментарий попадает в ленту как `direction: system` — виден сотрудникам, клиенту не отправляется.
- `assignee_id = null` → `status` становится `new`.
- `422 unprocessable`: назначение на наблюдателя/руководителя (`details.reason: "assignee_cannot_chat"` — head читает всё и так, отвечать не может), на деактивированного (`"assignee_inactive"`).
- WS: `conversation:assigned` (получает и новый ответственный — у него звук + ⚑), `conversation:updated` остальным. `audit_log: "conversation.assigned"`.

### 5.6. `GET /conversations/{id}/client-history` — прошлые диалоги клиента

Право: `conversations:read`.

```json
// 200
{
  "client": { "id": "d1e2f3a4-...", "name": "Иван Петров" },
  "items": [
    { "id": "9a8b...", "status": "closed", "item": { "title": "Ремонт MacBook Air" },
      "account": { "title": "LP-Москва" },
      "assignee": { "full_name": "Глеб Беляков" },
      "last_message_at": "2026-05-11T14:00:00Z", "messages_count": 18 }
  ]
}
```

Все диалоги `clients.id` того же клиента, кроме текущего, по `last_message_at DESC` (без пагинации —
их единицы). Клик в UI открывает `/chats/:id` этого диалога.

---

## 6. Messages

### 6.1. `GET /conversations/{id}/messages` — лента

Право: `conversations:read`. Cursor-пагинация — §1.4. Дополнительный параметр
`include_notes=false` не предусмотрен: заметки — часть ленты; **observer их не видит** —
для роли observer бэкенд всегда исключает `direction = 'note'` из выдачи (право `notes:read`
есть у admin/head/manager).

```json
// 200
{
  "items": [
    {
      "id": "f2a3b4c5-...",
      "conversation_id": "c4e5f6a7-...",
      "direction": "in",                    // in | out | note | system
      "sender_type": "client",              // client | operator | bot | system
      "sender": null,                       // для operator: {id, full_name}
      "body": "Здравствуйте! Экран разбит, почём?",
      "attachments": [],
      "delivery_status": "delivered",       // pending | delivered | failed
      "created_at": "2026-08-03T18:22:05Z"
    },
    {
      "id": "a1b2c3d4-...",
      "direction": "out", "sender_type": "operator",
      "sender": { "id": "0d4f0a1e-...", "full_name": "Анна Смирнова" },
      "body": "Добрый день! Замена экрана iPhone 13 — от 8 900 ₽",
      "attachments": [ { "media_id": "m_5f6a...", "kind": "image",
                         "url": "/api/v1/media/2026/08/5f/5f6a3c2e-....jpg?sig=...&exp=...",
                         "name": "price.jpg", "size": 183204 } ],
      "delivery_status": "delivered",
      "client_message_id": "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59",
      "created_at": "2026-08-03T18:24:40Z"
    },
    {
      "id": "e5d4c3b2-...",
      "direction": "note", "sender_type": "operator",
      "sender": { "id": "0d4f0a1e-...", "full_name": "Анна Смирнова" },
      "body": "торгуется, дать скидку до 10%",
      "attachments": [], "delivery_status": "delivered",
      "created_at": "2026-08-03T18:25:10Z"
    }
  ],
  "page": { "prev_cursor": "...", "next_cursor": "...",
            "has_more_before": true, "has_more_after": false }
}
```

`attachments[].url` — подписанная nginx-ссылка (DESIGN §1.4), TTL 1 час; фронт перезапрашивает
деталь при истечении. В пути раздачи всегда relpath файла на диске
(`{yyyy}/{mm}/{2hex}/{uuid}.{ext}` — 05 §3.3); `media_id` (`m_…`) — только идентификатор
в телах запросов/ответов, в URL раздачи он не используется.

### 6.2. `POST /conversations/{id}/messages` — отправить

Право: `messages:send` — **admin, manager** (head — read-only по матрице DESIGN §5.1, получает `403 read_only_role`).

```json
// Запрос
{
  "text": "Добрый день! Замена экрана iPhone 13 — от 8 900 ₽",
  "client_message_id": "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59",
  "attachments": [ { "media_id": "m_5f6a..." } ]        // опционально, до 5 шт
}
```

```json
// 201 (повтор — 200 + X-Idempotent-Replay: true, см. §1.6)
{
  "id": "a1b2c3d4-...", "conversation_id": "c4e5f6a7-...",
  "direction": "out", "sender_type": "operator",
  "sender": { "id": "0d4f0a1e-...", "full_name": "Анна Смирнова" },
  "body": "Добрый день! Замена экрана iPhone 13 — от 8 900 ₽",
  "attachments": [ { "media_id": "m_5f6a...", "kind": "image", "url": "...", "name": "price.jpg", "size": 183204 } ],
  "delivery_status": "pending",
  "client_message_id": "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59",
  "created_at": "2026-08-04T10:12:00Z"
}
```

Семантика — DESIGN §8.2: ответ мгновенный со статусом `pending`, доставка через ARQ-воркер
(5 ретраев экспоненциально), итог приходит WS-событием `message:status`
(`delivered` или `failed`). Побочные эффекты в той же транзакции:
- если `conversation.status = 'new'` и `assignee_id IS NULL` → `status = 'in_progress'`, `assignee_id = я` («кто взял — тот и ведёт»);
- если в диалоге активен бот → `bot_active = false` (оператор вмешался, DESIGN §4.3 п.6);
- `last_message_at` обновляется.

Ошибки: `422 unprocessable` — диалог `closed` (`details.reason: "conversation_closed"`, фронт
предлагает «вернуть в работу»); `409 account_needs_reauth` — аккаунт Авито этого диалога в
`needs_reauth`/`disabled` (сообщение принять нельзя — доставить будет некому);
`409 idempotency_mismatch`; `413` — текст > 4000 символов (лимит мессенджера Авито ~1000 на
сообщение — воркер сам режет длинные тексты на части, но 4000 — предел здравого смысла).

### 6.3. `POST /messages/{id}/retry` — повторить failed

Право: `messages:send`. Тело пустое.

```json
// 200
{ "id": "a1b2c3d4-...", "delivery_status": "pending", "...": "объект сообщения" }
```

Заново ставит `deliver_message` в ARQ (счётчик ретраев обнуляется). `422 unprocessable`, если
статус не `failed` (`details.reason: "not_failed"`). `409 account_needs_reauth` — как в §6.2.

### 6.4. `POST /conversations/{id}/notes` — внутренняя заметка

Право: `notes:write` — admin, head, manager (head **может** писать заметки — это не сообщение клиенту).

```json
// Запрос
{ "text": "торгуется, дать скидку до 10%", "client_message_id": "018f3c2b-..." }
```

`201` → объект сообщения с `direction: "note"`, `delivery_status: "delivered"` (никуда не
отправляется, «доставлена» сразу). WS: `message:new` всем, кроме observer-сессий.
Редактирования/удаления заметок в MVP нет (заметка = факт в истории; `audit_log` не обманешь).

### 6.5. `POST /media` — загрузка вложения

Право: `messages:send`. `multipart/form-data`, поле `file`. Лимит 20 МБ; типы: изображения
(jpeg/png/webp), pdf. Файл → `/var/leadchat/media/{yyyy}/{mm}/{2hex}/{uuid}.{ext}`
(DESIGN §1.4; fan-out по первым двум hex-символам uuid — 05 §3.3).

```json
// 201
{ "media_id": "m_5f6a...", "kind": "image", "name": "price.jpg", "size": 183204,
  "url": "/api/v1/media/2026/08/5f/5f6a3c2e-....jpg?sig=...&exp=..." }
```

`media_id` живёт 24 ч неприкреплённым (Redis), потом файл собирает GC. Прикрепление — через
`attachments` в §6.2.

---

## 7. Templates (быстрые ответы)

Права: `templates:own` (admin, head, manager — личные), `templates:shared` (admin, head — общие).
Observer шаблонов не видит (ему нечем отвечать) — `403`.

Модель (таблица `templates`): `owner_id IS NULL` → общий шаблон; иначе личный.
Переменные в `body`: `{имя}` (клиент), `{менеджер}` (текущий пользователь), `{объявление}` —
подстановку делает **фронт** при вставке в поле ввода (бэкенд хранит как есть).

### 7.1. `GET /templates`

Query: `scope=all|shared|personal` (default `all` — личные текущего пользователя + общие),
`folder`, `q` (поиск по title/body), `limit`, `offset`.

```json
// 200
{
  "items": [
    { "id": "t1a2...", "owner_id": null, "title": "Приветствие",
      "body": "Здравствуйте, {имя}! Это сервис Lead Partner 👋", "folder": "Приветствия" },
    { "id": "t3c4...", "owner_id": "0d4f0a1e-...", "title": "Моя скидка",
      "body": "Могу предложить скидку 5% при заказе сегодня", "folder": null }
  ],
  "page": { "limit": 50, "offset": 0, "total": 23 }
}
```

### 7.2. `GET /templates/folders`

`200` → `{ "shared": ["Приветствия", "Цены", "Доставка"], "personal": ["Мои скидки"] }` —
`SELECT DISTINCT folder` по обеим областям (для выпадающих списков UI).

### 7.3. `POST /templates`

```json
// Запрос
{ "title": "Приветствие", "body": "Здравствуйте, {имя}!", "folder": "Приветствия", "shared": true }
```

`shared: true` требует `templates:shared` (иначе `403`); `shared: false` (default) → `owner_id = я`.
`201` → объект шаблона.

### 7.4. `PATCH /templates/{id}` / `DELETE /templates/{id}`

Правила доступа: личный шаблон меняет/удаляет только владелец (и admin); общий — только
`templates:shared`. `PATCH` принимает `title/body/folder` (перевести личный в общий нельзя —
создайте новый общий). `DELETE` → `204`. Чужой личный шаблон → `404 not_found` (не раскрываем).

---

## 8. Bots

Право на всё: `bots:manage` — **только admin** (DESIGN §5.1).
Схема сценария — DESIGN §4.1 (типы шагов `send/ask/menu/condition/ai_answer/handoff/close/tag/note`).

### 8.1. `GET /bots`

```json
// 200
{
  "items": [
    {
      "id": "e1f2a3b4-...", "name": "Первичный приём",
      "is_enabled": true,
      "schedule": { "always": true },
      "accounts": [ { "id": "b2a4c6e8-...", "title": "LP-Москва" } ],
      "scenario_steps_count": 8,
      "knowledge_base_present": true
    }
  ],
  "page": { "limit": 50, "offset": 0, "total": 2 }
}
```

`accounts` — вычисляется по `avito_accounts.bot_id` (привязка живёт на аккаунте, DESIGN §4.4).

### 8.2. `GET /bots/{id}` — деталь (полный сценарий)

Форматы `scenario` и `schedule` нормативно заданы в 02-BOT-ENGINE (§1.1–1.4 — структура и
JSON Schema сценария, §2.5 — расписание); пример ниже валиден по схеме 02 §1.4.

```json
// 200
{
  "id": "e1f2a3b4-...", "name": "Первичный приём", "is_enabled": true,
  "schedule": { "always": false, "timezone": "Europe/Moscow",
                "intervals": [ { "days": ["mon","tue","wed","thu","fri","sat","sun"],
                                 "start": "20:00", "end": "10:00" } ] },
  "scenario": {
    "version": 1,
    "revision": 3,
    "entry": "greet",
    "steps": [
      { "id": "greet", "type": "send",
        "params": { "text": "Здравствуйте, {client_name}! Подскажите, что случилось с техникой?" },
        "next": "ask_problem" },
      { "id": "ask_problem", "type": "ask",
        "params": { "text": null, "var": "problem", "validate": "any",
                    "max_attempts": 1, "timeout": "24h" },
        "next": "ai_draft", "on_timeout": "handoff_no_reply", "on_invalid": null },
      { "id": "ai_draft", "type": "ai_answer",
        "params": { "confidence_threshold": 0.7, "max_reply_len": 800, "context_messages": 10 },
        "next": "check_hours", "on_low_confidence": null },
      { "id": "check_hours", "type": "condition",
        "params": {
          "conditions": [ { "if": { "kind": "work_hours", "from": "10:00", "to": "20:00",
                                    "timezone": "Europe/Moscow" }, "next": "handoff_day" } ],
          "else": "ask_phone" } },
      { "id": "ask_phone", "type": "ask",
        "params": { "text": "Мастер ответит утром. Оставьте телефон — перезвоним первыми ✔",
                    "var": "phone", "validate": "phone", "max_attempts": 2, "timeout": "12h" },
        "next": "tag_contact", "on_timeout": "handoff_night", "on_invalid": "handoff_night" },
      { "id": "tag_contact", "type": "tag",
        "params": { "tags": ["контакт собран"] }, "next": "handoff_night" },
      { "id": "handoff_day", "type": "handoff", "params": { "reason": "scenario" } },
      { "id": "handoff_night", "type": "handoff", "params": { "reason": "scenario" } },
      { "id": "handoff_no_reply", "type": "handoff", "params": { "reason": "scenario" } }
    ]
  },
  "knowledge_base": "Замена экрана iPhone 13 — от 8900 ₽, срок 1–2 часа. ..."
}
```

### 8.3. `POST /bots` / `PUT /bots/{id}`

Тело — как деталь §8.2 без `id` (PUT — полная замена; PATCH не даём: сценарий — атомарный документ).
Валидация сценария на бэкенде — двухслойная (02-BOT-ENGINE: JSON Schema §1.4 + семантический
валидатор графа §5.2): все `next`/ветки указывают на существующие `id` шагов, `entry` существует,
нет недостижимых шагов, `ai_answer` требует непустой `knowledge_base`. Нарушение →
`422 bot_scenario_invalid`:

```json
{ "error": { "code": "bot_scenario_invalid", "message": "Шаг ask_phone: ссылка next на несуществующий шаг 'tag_contct'",
             "details": { "step_id": "ask_phone", "reason": "broken_ref", "ref": "tag_contct" } } }
```

`201`/`200` → деталь бота. Изменение сценария **не** влияет на диалоги, где бот уже идёт по шагам
(позиция и переменные — в `conversations.bot_vars`; воркер завершает их по старой логике best-effort,
при несовпадении шага делает handoff). `audit_log: "bot.updated"`.

### 8.4. `POST /bots/{id}/enable` / `POST /bots/{id}/disable`

`200` → деталь. Disable мгновенно останавливает бота во всех активных диалогах
(`bot_active = false` не ставим ретроактивно — воркер просто проверяет `is_enabled` перед каждым шагом).

### 8.5. `PUT /bots/{id}/accounts` — привязка к аккаунтам

```json
// Запрос — полный список аккаунтов этого бота
{ "account_ids": ["b2a4c6e8-...", "c3b5d7f9-..."] }
```

`200` → `{"accounts": [...]}`. Реализация: `UPDATE avito_accounts SET bot_id = ... WHERE id = ANY(...)`
+ обнуление у отвязанных. Если аккаунт был привязан к другому боту — молча перепривязывается
(у аккаунта один бот, `avito_accounts.bot_id` — единственная истина).

### 8.6. Песочница сценария — `/bots/sandbox/*` (тест без Авито)

Тестирование сценария — сессионная песочница (детальный контракт — 02-BOT-ENGINE §5.3):
работает с **несохранённым черновиком** из редактора, диалог не создаётся, клиенту ничего
не отправляется, состояние сессии — в Redis (TTL 1 час).

- **`POST /bots/sandbox/start`** — тело: `{ scenario, knowledge_base, schedule, client_name,
  item_title, now_override, ai_mode: "real" | "stub" }` → `{ session_id, events }`
  или `422 bot_scenario_invalid` со списком ошибок валидации.
- **`POST /bots/sandbox/{session_id}/message`** — тело `{ text }` (сообщение «клиента») →
  `{ events, state }`: события тика (ответы бота, трассировка шагов, ai_call, handoff, теги)
  + текущее состояние (`step`, `waiting`, `vars`, `counters`).
- **`POST /bots/sandbox/{session_id}/fire-timeout`** — промотать таймаут `ask`/`menu`
  без ожидания (эмуляция `bot_ask_timeout`).
- **`DELETE /bots/sandbox/{session_id}`** — закрыть сессию.

`ai_mode: "real"` вызывает реальный Claude API (это и есть смысл теста базы знаний;
`503 upstream_unavailable` — Claude недоступен), `"stub"` — детерминированные заглушки для
отладки логики графа офлайн. Таймаут AI — 10 сек (DESIGN §4.5).

### 8.7. `DELETE /bots/{id}`

`204`. `409 conflict` (`details.reason: "bot_in_use"`), если на бота ссылаются аккаунты —
сначала `PUT .../accounts` с пустым списком (осознанное действие вместо каскада).

---

## 9. Stats

Владелец контракта `/stats/*` — профильный документ **06-STATS-REPORTS §4** (словарь метрик,
SQL, точные формы ответов); здесь — сводка endpoints и прав. При расхождении истина — 06.

Права (DESIGN §3.1/§5.2): `stats:all` — admin, head (любые фильтры). **Manager получает `403`
на весь раздел, кроме `GET /stats/my/today`** (право `stats:own` — виджет «моя статистика
за сегодня»). Observer → `403` на всё.

Общие query-параметры: `date_from`, `date_to` (даты по Москве, обе включительно — 06 §0.1;
default — последние 30 дней), `account_id` (uuid), `manager_id` (uuid, **повторяемый**:
`manager_id=a&manager_id=b`). Период > 366 дней → `400 period_too_long`. Каждый ответ несёт
`refreshed_at` — метку свежести materialized view (06 §3.2).

### 9.1. `GET /stats/summary` — карточки-метрики

Ответ — объект `cards`: значение за период + сравнение с предыдущим периодом той же длины
(`prev`, `delta_pct`). Точная форма — 06 §4.1.

### 9.2. `GET /stats/timeseries` — ряды для графиков

Дополнительно: `metric=conversations_new|conversations_closed|messages_in|messages_out|frt_operator_median|phones_collected`,
`group=day|hour` (`hour` разрешён при периоде ≤ 7 дней, иначе `400`). Ряд зануляется до
сплошного на бэкенде. Форма ответа — 06 §4.2.

### 9.3. `GET /stats/heatmap` — тепловая карта «час × день недели»

Ответ — всегда ровно 168 ячеек `{ "dow": 1, "hour": 10, "value": 42 }`;
`dow`: 1 = понедельник … 7 = воскресенье (ISO). Метрика — входящие сообщения клиентов.
Форма — 06 §4.3.

### 9.4. `GET /stats/managers` — таблица по менеджерам

Сортировка серверная: `sort=messages_sent|taken|answered|closed|frt_median_sec|frt_median_biz_sec`
(default `messages_sent`), `order=asc|desc`. Пагинации нет — менеджеров десятки.
Форма ответа (`rows` + `totals`) — 06 §4.4.

### 9.5. `POST /stats/export` + `GET /stats/export/{job_id}` — выгрузка

Экспорт **асинхронный** (ARQ-воркер, 06 §5): `POST` с телом
`{ format: "csv"|"xlsx", date_from, date_to, account_id, manager_id, sheets }` →
`202 {"job_id"}`. Ошибки: `400` — период > 366 дней, `409 export_already_running` — у
пользователя уже идёт экспорт, `429` — исчерпан дневной лимит (20/сутки).
`GET /stats/export/{job_id}` (доступен только автору) → статус `pending|running|done|failed`
+ подписанная nginx-ссылка на файл (TTL 24 ч). Лимиты и уборка — 06 §5.4.
`audit_log: "stats.exported"` (кто и что выгружал — это персональные данные клиентов).

### 9.6. `GET /stats/my/today` — виджет менеджера

Право `stats:own` (admin, head, manager). Только свои цифры: `user_id` берётся из JWT,
параметром не передаётся. Форма и SQL — 06 §6.

### 9.7. `GET /audit-log` — журнал аудита

Право: `audit:read` — admin, head (view-only). Query: `limit`, `offset`, `user_id`, `action`,
`entity`, `date_from`, `date_to`.

```json
// 200
{
  "items": [
    { "id": 18211, "user": { "id": "...", "full_name": "Анна Смирнова" },
      "action": "conversation.assigned", "entity": "conversation",
      "entity_id": "c4e5f6a7-...",
      "details": { "from": null, "to": "7c1b...", "comment": "..." },
      "created_at": "2026-08-04T10:05:00Z" }
  ],
  "page": { "limit": 50, "offset": 0, "total": 18211 }
}
```

---

## 10. Webhook Авито (внутренний контракт)

`POST /api/hooks/avito/{account_id}?secret=<webhook_secret>` — вне `/v1`, вызывается только Авито.
Реализация — DESIGN §8.3; здесь фиксируем контракт:

- **Авторизация**: `secret` из query сверяется `compare_digest` с `avito_accounts.webhook_secret`.
  Несовпадение/неизвестный аккаунт → `403` без тела. Никаких Bearer.
- **Тело**: сырой JSON Мессенджера Авито v3, > 1 МБ → `413`. Формат payload'а мы **не валидируем
  на этом endpoint'е** — парсинг делает воркер (`AvitoAdapter.parse_webhook`), сырой payload
  дополнительно пишется в лог-таблицу на 30 дней (риск №2 из DESIGN §7).
- **Ответ**: всегда немедленный `200 {"ok": true}` после `XADD` в Redis Stream `webhooks:avito` —
  даже если payload мусорный (иначе Авито отключит вебхук). Единственные не-200: `403`, `413`, `429`.
- **Идемпотентность**: ретраи Авито гасятся уникальным индексом
  `messages (conversation_id, external_message_id)` — воркер вставляет через `ON CONFLICT DO NOTHING`.
- **SLA обработчика**: endpoint не ходит в Авито и не пишет в PostgreSQL; p99 < 50 мс.

События, которые воркер извлекает из payload'ов: новое сообщение (основной путь),
эхо собственного исходящего (отброс по `author_id == avito_user_id`), прочтение чата клиентом
(обновление `delivery_status` не требуется — Авито не даёт read-receipts надёжно, игнорируем в MVP).

---

## 11. WebSocket-протокол

Endpoint: `wss://chat.partner-lead-centre.ru/api/ws` (nginx проксирует upgrade на WebSocket Hub).
Один сокет на клиентскую сессию (вкладка/окно Tauri); все события пользователя мультиплексируются в нём.

### 11.1. Авторизация — ticket

Заголовки при открытии WS из браузера не задать, cookie на WS-upgrade полагаться нельзя
(десктоп), токен в query-string просочился бы в логи nginx — поэтому одноразовый тикет:

**`POST /api/v1/ws/ticket`** (обычный Bearer-запрос; право: любой аутентифицированный)

```json
// 200
{ "ticket": "wst_Zk8pQ...48url", "expires_in": 60 }
```

- Тикет — случайная строка в Redis: `ws_ticket:{ticket} → user_id`, TTL 60 сек, `GETDEL` при подключении (строго одноразовый).
- Подключение: `wss://chat.partner-lead-centre.ru/api/ws?ticket=wst_Zk8pQ...`
- Невалидный тикет → сервер закрывает сокет кодом `4401` до первого кадра.
- Query-логирование `/api/ws` в nginx отключено (`access_log off` на location) — тикет всё равно одноразовый, но в логах ему делать нечего.
- Протухание access JWT **не** рвёт открытый сокет: соединение аутентифицировано тикетом на всю жизнь. Деактивация пользователя / logout → сервер закрывает сокет `4403` (слушает `revoked_users`).

### 11.2. Формат кадров

Все кадры — текстовые JSON, одна структура в обе стороны:

```json
{ "type": "message:new", "ts": "2026-08-04T10:12:01.204Z", "data": { ... } }
```

- `type` — имя события, `data` — payload, `ts` — серверное время генерации (клиент сохраняет
  максимальный `ts` как `last_event_ts` для догона, §11.7). Клиентские кадры поле `ts` не шлют.
- Неизвестный `type` обе стороны молча игнорируют (forward-совместимость).
- Бинарных кадров нет; вложения ходят только через REST.

### 11.3. Серверные события — полный каталог

Хаб подписан на Redis Pub/Sub канал `events` (DESIGN §1.1) и фильтрует по получателям.
Правило доставки по умолчанию: события диалогов получают **все** подключённые пользователи
(у всех ролей есть чтение всех диалогов), кроме оговорённых исключений.

**`message:new`** — новое сообщение (входящее, исходящее чужой сессии, бот, заметка, system).

```json
{ "type": "message:new", "ts": "2026-08-04T10:12:01.204Z",
  "data": {
    "conversation_id": "c4e5f6a7-...",
    "message": { "...": "полный MessageOut, как в §6.1" },
    "conversation_patch": { "last_message_at": "2026-08-04T10:12:01Z",
                            "status": "in_progress", "unread_delta": 1 }
  } }
```

`conversation_patch` позволяет обновить строку списка без запроса детали.
Исключение: `direction: "note"` не доставляется observer-сессиям (§6.1).

**`message:status`** — итог доставки исходящего (воркер завершил `deliver_message`).

```json
{ "type": "message:status", "ts": "...",
  "data": { "conversation_id": "c4e5f6a7-...", "message_id": "a1b2c3d4-...",
            "delivery_status": "failed",
            "error": "Авито: чат недоступен (клиент удалил диалог)" } }
```

`error` присутствует только при `failed`; текст — человекочитаемый, показывается у ✗.

**`conversation:updated`** — изменилась шапка/строка диалога (статус, unread, карточка клиента,
извлечён телефон, bot_active, tags — например бот повесил «негатив»). Payload — `conversation_id`
+ `patch` с изменёнными полями объекта из §5.1:

```json
{ "type": "conversation:updated", "ts": "...",
  "data": { "conversation_id": "c4e5f6a7-...",
            "patch": { "status": "closed", "tags": ["негатив"],
                       "client": { "phone": "+79261234567" } } } }
```

**`conversation:assigned`** — назначение/передача (§5.5). Доставляется всем; новый ответственный
дополнительно получает флаг:

```json
{ "type": "conversation:assigned", "ts": "...",
  "data": { "conversation_id": "c4e5f6a7-...",
            "assignee": { "id": "7c1b...", "full_name": "Глеб Беляков" },
            "assigned_by": { "id": "0d4f...", "full_name": "Анна Смирнова" },
            "comment": "торгуется, дай скидку до 10%",
            "is_for_you": true } }
```

`is_for_you: true` → фронт играет звук, вешает ⚑, показывает toast (в Tauri — нативный тост Windows).

**`typing`** — клиент с Авито печатает (если Авито даёт такое событие — best-effort) **или**
коллега печатает в том же диалоге (ретрансляция клиентского кадра `typing`, §11.4):

```json
{ "type": "typing", "ts": "...",
  "data": { "conversation_id": "c4e5f6a7-...",
            "source": "operator",                    // client | operator
            "user": { "id": "0d4f...", "full_name": "Анна Смирнова" } } }
```

Доставляется только сессиям, подписанным на этот диалог (`subscribe`, §11.4). Индикатор гасится
фронтом сам через 5 сек без повторного события.

**`presence:online`** — смена онлайн-статуса сотрудника (для списка команды и «Передать коллеге»):

```json
{ "type": "presence:online", "ts": "...",
  "data": { "user_id": "7c1b...", "status": "online" } }   // online | away | offline
```

`away` — статус из трея десктопа («отошёл», DESIGN §3.6). Offline публикуется хабом через 30 сек
после закрытия последнего сокета пользователя (grace на reconnect).

**`account:needs_reauth`** — refresh токена аккаунта провалился (DESIGN §1.5).
Доставляется **только admin-сессиям**:

```json
{ "type": "account:needs_reauth", "ts": "...",
  "data": { "account_id": "b2a4c6e8-...", "title": "LP-Москва" } }
```

Десктоп показывает системное уведомление; веб — красный бейдж в настройках.

**`notify`** — универсальное служебное уведомление (тост в UI), чтобы не плодить типы под
редкие случаи (завершился backfill истории, реконсиляция досоздала сообщения и т.п.):

```json
{ "type": "notify", "ts": "...",
  "data": { "level": "info",                      // info | warning | error
            "title": "История загружена",
            "text": "Аккаунт «LP-Москва»: загружено 1 214 диалогов",
            "audience_hint": "admin" } }
```

### 11.4. Клиентские кадры

**`subscribe`** — сообщить хабу, какой диалог открыт (нужно только для адресного `typing`;
остальные события и так приходят):

```json
{ "type": "subscribe", "data": { "conversation_id": "c4e5f6a7-..." } }   // null — закрыл диалог
```

Одна активная подписка на сокет (открыт один диалог); новая заменяет старую.

**`typing`** — я печатаю в открытом диалоге (шлётся не чаще раза в 3 сек):

```json
{ "type": "typing", "data": { "conversation_id": "c4e5f6a7-..." } }
```

Хаб ретранслирует подписчикам диалога (кроме отправителя) и — если Авито поддерживает
typing-нотификацию — дергает адаптер best-effort.

**`ping`** — прикладной heartbeat (§11.5):

```json
{ "type": "ping", "data": { "n": 42 } }
```

Сервер отвечает `{ "type": "pong", "ts": "...", "data": { "n": 42 } }`.

Любой синтаксически битый клиентский кадр → сервер отвечает
`{ "type": "error", "data": { "code": "bad_frame" } }` и продолжает работать (не рвёт сокет).

### 11.5. Heartbeat

- Клиент шлёт `ping` каждые **25 сек**; нет `pong` за 10 сек → соединение считается мёртвым,
  клиент закрывает сокет и идёт в reconnect (§11.7). Прикладной ping (а не только WS-фрейм ping)
  нужен, чтобы ловить полумёртвые соединения через прокси/VPN, где TCP жив, а данные не ходят.
- Сервер закрывает сокеты, от которых 60 сек не было ни одного кадра (код `4408`).
- nginx: `proxy_read_timeout 90s` на location `/api/ws` — больше серверного таймаута.

### 11.6. Presence

Первый сокет пользователя → хаб пишет `presence:{user_id} = online` (Redis, TTL 90 сек,
продлевается на каждом ping) и публикует `presence:online`. Статус `away` клиент меняет
REST-запросом **`PUT /api/v1/presence`** `{"status": "away"}` (право: любой аутентифицированный) —
хаб разошлёт событие.

### 11.7. Reconnect и догон пропущенного

Стратегия клиента (одинакова для веба и Tauri):

1. Сокет умер → reconnect с экспоненциальным backoff + jitter: 1с → 2с → 4с → … максимум 30с
   (`delay = min(30, 2^attempt) * (0.5 + random()*0.5)`), без ограничения числа попыток.
2. Перед каждой попыткой — новый тикет (`POST /ws/ticket`); если тикет-запрос дал 401 →
   сначала `POST /auth/refresh`.
3. После успешного подключения — **догон через REST**, потому что WS-хаб события не буферизирует
   (сознательное решение: реплей событий = дубль хранилища; REST — единственная истина):
   - `GET /conversations?updated_since=<last_event_ts>&limit=200` → обновить строки списка;
   - для **открытого** диалога: `GET /conversations/{id}/messages?after=<next_cursor>` —
     докачать хвост ленты (курсор был сохранён из последнего ответа §6.1 / последнего `message:new`);
   - `last_event_ts` хранится с запасом −30 сек (перекрытие безопасно: сообщения дедуплицируются
     фронтом по `message.id`).
4. Офлайн дольше 30 минут (десктоп проснулся из сна) → не догонять дифф, а перезагрузить список
   первой страницей (`GET /conversations`) — дешевле и надёжнее.

Серверные коды закрытия: `4401` — тикет невалиден (получить новый и переподключиться),
`4403` — пользователь деактивирован/разлогинен (уйти на `/login`), `4408` — таймаут heartbeat
(обычный reconnect), `1012` — рестарт сервиса (обычный reconnect).

---

## 12. Каталог прав (require_permission)

Единственное место истины — таблица DESIGN §5.1; здесь — её проекция на строки-права:

| Право | admin | head | manager | observer |
|---|:-:|:-:|:-:|:-:|
| `conversations:read` | ✅ | ✅ | ✅ | ✅ |
| `messages:send` | ✅ | ❌ | ✅ | ❌ |
| `conversations:manage` (статус, назначение) | ✅ | ✅ | ✅ | ❌ |
| `notes:read` / `notes:write` | ✅ | ✅ | ✅ | ❌ |
| `templates:own` | ✅ | ✅ | ✅ | ❌ |
| `templates:shared` | ✅ | ✅ | ❌ | ❌ |
| `stats:own` | ✅ | ✅ | ✅ | ❌ |
| `stats:all` | ✅ | ✅ | ❌ | ❌ |
| `bots:manage` | ✅ | ❌ | ❌ | ❌ |
| `accounts:read` | ✅ | ✅ | ❌ | ❌ |
| `accounts:manage` | ✅ | ❌ | ❌ | ❌ |
| `users:manage` | ✅ | ❌ | ❌ | ❌ |
| `audit:read` | ✅ | ✅ | ❌ | ❌ |

Реализация: `ROLE_PERMISSIONS: dict[str, frozenset[str]]` в `app/core/rbac.py`;
`require_permission("messages:send")` — FastAPI-dependency, читающая пользователя из JWT
и роль из БД. `403 forbidden` при отсутствии права; `403 read_only_role` — специализация
для head при попытке отправить сообщение (фронт показывает плашку «Режим просмотра», DESIGN §5.2).

---

## 13. Матрица «endpoint → роли»

| Endpoint | Метод | Право | admin | head | manager | observer |
|---|---|---|:-:|:-:|:-:|:-:|
| `/auth/login`, `/auth/refresh`, `/auth/invite/*` | POST/GET | — (публичные) | ✅ | ✅ | ✅ | ✅ |
| `/auth/logout`, `/auth/me`, `/ws/ticket`, `/presence` | POST/GET/PUT | аутентификация | ✅ | ✅ | ✅ | ✅ |
| `/users` | GET | `users:manage` | ✅ | ❌ | ❌ | ❌ |
| `/users` | POST | `users:manage` | ✅ | ❌ | ❌ | ❌ |
| `/users/{id}` | PATCH | `users:manage` | ✅ | ❌ | ❌ | ❌ |
| `/users/{id}/invite` | POST | `users:manage` | ✅ | ❌ | ❌ | ❌ |
| `/users/{id}/deactivate` · `/activate` | POST | `users:manage` | ✅ | ❌ | ❌ | ❌ |
| `/users/assignable` | GET | `conversations:manage` | ✅ | ✅ | ✅ | ❌ |
| `/avito-accounts` | GET | `accounts:read` | ✅ | ✅ | ❌ | ❌ |
| `/avito/connect-url`, `/avito-accounts/{id}/reconnect` | GET/POST | `accounts:manage` | ✅ | ❌ | ❌ | ❌ |
| `/avito/callback` | GET | — (state-токен) | ✅ | ❌ | ❌ | ❌ |
| `/avito-accounts/{id}` | PATCH | `accounts:manage` | ✅ | ❌ | ❌ | ❌ |
| `/avito-accounts/{id}/disable` · `/enable` | POST | `accounts:manage` | ✅ | ❌ | ❌ | ❌ |
| `/avito-accounts/{id}/webhook/check` | POST | `accounts:manage` | ✅ | ❌ | ❌ | ❌ |
| `/conversations` | GET | `conversations:read` | ✅ | ✅ | ✅ | ✅ |
| `/conversations/{id}` | GET | `conversations:read` | ✅ | ✅ | ✅ | ✅ |
| `/conversations/{id}/read` | POST | `conversations:read` | ✅ | ✅ | ✅ | ✅ |
| `/conversations/{id}/status` | PATCH | `conversations:manage` | ✅ | ✅ | ✅ | ❌ |
| `/conversations/{id}/assign` | POST | `conversations:manage` | ✅ | ✅ | ✅ | ❌ |
| `/conversations/{id}/client-history` | GET | `conversations:read` | ✅ | ✅ | ✅ | ✅ |
| `/conversations/{id}/messages` | GET | `conversations:read` (заметки — `notes:read`) | ✅ | ✅ | ✅ | ✅* |
| `/conversations/{id}/messages` | POST | `messages:send` | ✅ | ❌ | ✅ | ❌ |
| `/messages/{id}/retry` | POST | `messages:send` | ✅ | ❌ | ✅ | ❌ |
| `/conversations/{id}/notes` | POST | `notes:write` | ✅ | ✅ | ✅ | ❌ |
| `/media` | POST | `messages:send` | ✅ | ❌ | ✅ | ❌ |
| `/templates`, `/templates/folders` | GET | `templates:own` | ✅ | ✅ | ✅ | ❌ |
| `/templates` (личный) | POST/PATCH/DELETE | `templates:own` | ✅ | ✅ | ✅ | ❌ |
| `/templates` (общий, `shared: true`) | POST/PATCH/DELETE | `templates:shared` | ✅ | ✅ | ❌ | ❌ |
| `/bots`, `/bots/{id}/*`, `/bots/sandbox/*` | * | `bots:manage` | ✅ | ❌ | ❌ | ❌ |
| `/stats/summary` · `/timeseries` · `/heatmap` · `/managers`, `/stats/export` (+ статус job'а) | GET/POST | `stats:all` | ✅ | ✅ | ❌ | ❌ |
| `/stats/my/today` | GET | `stats:own` | ✅ | ✅ | ✅ | ❌ |
| `/audit-log` | GET | `audit:read` | ✅ | ✅ | ❌ | ❌ |
| `/api/hooks/avito/{account_id}` | POST | webhook_secret | — | — | — | — |
| `/api/ws` | WS | ticket | ✅ | ✅ | ✅ | ✅ |

\* observer получает ленту без `direction = 'note'`.

---

*Остальные документы серии: 02-BOT-ENGINE (движок сценариев ботов), 03-FRONTEND (структура
React/Tauri, сторы, WS-клиент), 04-DESKTOP (Tauri-обёртка, офлайн, автообновление),
05-DEPLOY-OPS (Compose, nginx, CI), 06-STATS-REPORTS (статистика и отчёты),
07-TESTING-SECURITY (тесты и безопасность), 08-BACKEND-CORE (ядро бэкенда:
inbound-конвейер, воркеры, WS Hub, scheduler, CLI).*
