# Аудит, шаг 0 — инвентаризация (10 августа 2026)

Полная карта продукта, собранная шестью независимыми проходами по коду со
встречной сверкой. Ничего не выдумано: каждое утверждение подкреплено путём к
файлу и номером строки, а чего не нашли — так и написано.

## Цифры

| Что | Сколько |
|---|---|
| Маршрутов фронтенда | 19 (бриф называет 9) |
| Путей в OpenAPI | 99 |
| Таблиц в базе | 15 |
| Миграций | 23 |
| Файлов интерфейса (без тестов) | 178 |
| Ролей | 4 — admin, head, manager, observer (бриф называет 2) |
| Находок «есть в коде, не выведено» | 83 |

## Три расхождения с брифом, которые меняют объём аудита

1. **Ролей четыре, а не две.** `app/core/rbac.py:7`. «Руководитель» и
   «Наблюдатель» — не заготовка: под них написаны отдельные ветки интерфейса
   (плашка «Режим просмотра», отсутствие композера, скрытый колокольчик) и
   отдельный код ошибки на сервере. Роль `manager` из брифа не имеет доступа
   НИ К ОДНОМУ разделу кроме `/chats` и профиля — семь маршрутов дают ей
   редирект.

2. **Пятая ось прав, которой в брифе нет вовсе:** `handles_conversations` —
   «ведёт ли человек диалоги», отдельно от роли. У заказчика так настроен
   отдел «СТАРШИЕ - ЧАТЫ»: администрируют, но обращений не получают.

3. **Раздела `/settings/quick-replies` не существует.** Фактический путь —
   `/settings/templates`; строки «quick-replies» нет в репозитории ни разу.
   Сверх брифа существуют `/notifications`, `/updates`, `/ui-kit`,
   `/settings/bots/:id`, `/login`, `/invite/:token`.

## Срез: Маршруты и навигация

СРЕЗ: МАРШРУТЫ И НАВИГАЦИЯ (LeadChat, leadchat)

Единственный роутер приложения — `createBrowserRouter` в frontend/src/app/router.tsx:60. Подключён в frontend/src/app/App.tsx:47 через `RouterProvider`. Других роутеров в проде нет (проверено grep по `createBrowserRouter|createHashRouter|createMemoryRouter|<Route|useRoutes` — совпадения только в router.tsx и в тестах `frontend/src/test/*`, плюс `MemoryRouter` в dev-стенде frontend/dev/preview.tsx:47).

═══════════════════════════════════════════════
1. ПОЛНАЯ ТАБЛИЦА МАРШРУТОВ (19 записей)
═══════════════════════════════════════════════

ПУБЛИЧНЫЕ (вне RequireAuth)

1) `/login` — router.tsx:61
   Компонент: `LoginPage` (frontend/src/features/auth/LoginPage.tsx:36), импорт статический (router.tsx:4).
   Право: нет. Если сессия уже жива — сам уводит: `<Navigate to={from} replace />` (LoginPage.tsx:75), где `from` берётся из `location.state.from.pathname`, по умолчанию `/chats` (LoginPage.tsx:55-56).
   Ссылки в интерфейсе: кнопка «Выйти» в меню аватара → `navigate("/login", {replace:true})` (AppLayout.tsx:134); «Перейти ко входу» с экрана протухшего приглашения (InvitePage.tsx:63); редирект guard'а `RequireAuth` (router.tsx:31).
   Внутри страницы есть режим «Не помню пароль» — НЕ маршрут, а состояние `forgot` (LoginPage.tsx:52, переключатель на LoginPage.tsx:217, форма `ForgotPasswordForm` LoginPage.tsx:150). Перезагрузка страницы этот режим теряет.
   В подвале: `<a href="/download">` (LoginPage.tsx:229) — это НЕ SPA-маршрут, его обслуживает nginx (docker/nginx/templates/leadchat.conf.template:219-220 → 302 на /download/LeadChat-Setup.exe) и внешняя ссылка на partner-lead-centre.ru (LoginPage.tsx:231).

2) `/invite/:token` — router.tsx:62
   Компонент: `InvitePage` (features/auth/InvitePage.tsx:72), статический импорт (router.tsx:5).
   Право: нет, «правом» служит сам токен. `token` читается через `useParams` (InvitePage.tsx:73).
   Ссылок в интерфейсе НЕТ — попасть можно только по ссылке из письма/мессенджера. После успеха: `navigate("/chats", {replace:true})` (InvitePage.tsx:93).

3) `*` (catch-all) — router.tsx:200 → `NotFoundPage` (router.tsx:44-58)
   ВАЖНО: объявлен НА ВЕРХНЕМ УРОВНЕ, вне `RequireAuth` и вне `AppLayout`. Значит: (а) неавторизованный пользователь по кривому адресу видит «Страница не найдена», а не уходит на /login; (б) страница рисуется без шапки и без рельсы навигации — единственный выход — кнопка «К диалогам» → `/chats` (router.tsx:53).

ЗАЩИЩЁННЫЕ СЕССИЕЙ (дети `<RequireAuth />`, router.tsx:64) И ОБЁРНУТЫЕ В `<AppLayout />` (router.tsx:67)

4) `/` (index) — router.tsx:69 → `<Navigate to="/chats" replace />`. Ссылок нет, это вход по корню домена.

5) `/chats` — router.tsx:70 → `ChatsPage` (features/chats/ChatsPage.tsx:20), статический импорт (router.tsx:6).
   Право: только сессия, ПРАВА НЕ ПРОВЕРЯЮТСЯ (даже `conversations:read`).
   Ссылки: первый пункт рельсы (AppLayout.tsx:246-271, `to="/chats"` строка 247), кнопка «К диалогам» на 404 (router.tsx:53), возврат из ленты на мобильной раскладке (ChatThreadPane.tsx:293), редирект `RequirePermission` (router.tsx:41), хоткей (useChatHotkeys.ts:261), после закрытия диалога со статистики (StatsPage.tsx:116).

6) `/chats/:id` — router.tsx:113 → тот же `ChatsPage` (комментарий в коде: «тот же компонент — deep-link»).
   Право: только сессия.
   Ссылки: клик по строке в таблице разбора (`features/table/TablePage.tsx:508`), клик по строке очереди «Входящие» (`features/chats/inbox/useInbox.ts:224`), клик по строке в списке (`features/chats/components/list/ChatListPane.tsx:635`), история прошлых обращений клиента в правой карточке (`features/chats/components/card/ClientCardPane.tsx:477`), хоткеи j/k/Enter (`features/hotkeys/useChatHotkeys.ts:97,162,235`), переход к следующему после действия (`features/chats/hooks/useConversationActions.ts:116`), deep-link из десктопного тоста (`frontend/src/platform/index.ts:133`).
   Несуществующий id: 404 от API → тост «Диалог не найден» + `navigate("/chats",{replace:true})` (ChatThreadPane.tsx:80-86).

7) `/dialogs` — router.tsx:94, ЛЕНИВЫЙ (`lazy`, router.tsx:95-97) → `TablePage` (features/table/TablePage.tsx).
   Право: `RequirePermission anyOf ["stats:all"]` (router.tsx:74) → admin и head.
   Ссылка: второй пункт рельсы, но только при `can("stats:all")` (AppLayout.tsx:275-286, `to="/dialogs"` строка 278). Подпись в интерфейсе — «Разбор диалогов», а не «Диалоги».

8) `/updates` — router.tsx:102, ЛЕНИВЫЙ (router.tsx:103-105) → `UpdatesPage` (features/updates/UpdatesPage.tsx:36).
   Право: только сессия — доступно ВСЕМ ролям, включая наблюдателя.
   Ссылка: ТОЛЬКО пункт «Что нового» в меню аватара (AppLayout.tsx:171-174), с точкой непрочитанного. В рельсе не выведен.

9) `/ui-kit` — router.tsx:108, ЛЕНИВЫЙ (router.tsx:109-111) → `UiKitPage` (features/uikit/UiKitPage.tsx).
   Право: только сессия.
   Ссылок В ИНТЕРФЕЙСЕ НЕТ НИ ОДНОЙ. Проверено grep'ом `ui-kit` по всему репозиторию: единственное совпадение — сама строка router.tsx:108. Попасть можно исключительно вводом адреса.

10) `/notifications` — router.tsx:120, ЛЕНИВЫЙ (router.tsx:121-123) → `NotificationsPage` (features/notifications/NotificationsPage.tsx:136).
    Право: guard'а НЕТ намеренно (комментарий router.tsx:114-119). Содержимое гейтится внутри: `useNotificationsEnabled()` = `can("conversations:manage")` (features/notifications/useNotifications.ts:27-30, константа features/notifications/catalog.ts:24). Роль без права видит текст «Вашей роли уведомления не приходят» (NotificationsPage.tsx:182-192) вместо редиректа. По rbac это отсекает `observer` (app/core/rbac.py:59).
    Ссылки: «Показать все» в панели колокольчика (features/notifications/NotificationPanel.tsx:107-118) и «ещё» в критичной плашке (features/notifications/CriticalBanners.tsx:87). В рельсе нет.

11) `/stats` — router.tsx:132, ЛЕНИВЫЙ (router.tsx:133) → `StatsPage` (features/stats/StatsPage.tsx).
    Право: `RequirePermission anyOf ["stats:all"]` (router.tsx:127) → admin и head.
    Ссылка: третий пункт рельсы под тем же `can("stats:all")` (AppLayout.tsx:288-299, `to="/stats"` строка 291). У менеджера вместо экрана — виджет «моя статистика» (в подвале списка, а на мобильной раскладке — пункт меню аватара, AppLayout.tsx:163 при условии AppLayout.tsx:125).

ВЕТКА `/settings/*` — дети `<SettingsLayout />` (router.tsx:138; компонент features/settings/SettingsLayout.tsx:9). Layout рисует вторую колонку-навигацию, пункты которой строятся по правам (SettingsLayout.tsx:15-42).

12) `/settings` — router.tsx:140 → `<Navigate to="/settings/profile" replace />`. Редирект живёт ВНУТРИ SettingsLayout, то есть боковая навигация настроек успевает отрисоваться.

13) `/settings/profile` — router.tsx:141 → `ProfilePage` (features/settings/profile/ProfilePage.tsx:33), статический импорт (router.tsx:8).
    Право: нет, все роли.
    Ссылки: четвёртый пункт рельсы «Настройки» ведёт именно сюда (AppLayout.tsx:301-310, `to="/settings/profile"` строка 303), пункт «Профиль» в меню аватара (AppLayout.tsx:164), пункт «Профиль» в навигации настроек (SettingsLayout.tsx:40-42) — единственный пункт без условия по праву.

14) `/settings/accounts` — router.tsx:144 → `AccountsPage` (features/settings/accounts/AccountsPage.tsx:556), статический импорт (router.tsx:7).
    Право: `anyOf ["accounts:read","accounts:manage"]` (router.tsx:143) → admin и head (head имеет только `accounts:read`, app/core/rbac.py:44).
    Ссылки: пункт «Аккаунты Авито» в навигации настроек при `can("accounts:read")` (SettingsLayout.tsx:15-19); кнопка из пустого состояния списка чатов `onGoSettings={() => navigate("/settings/accounts")}` (ChatListPane.tsx:615).
    Внешний вход: сюда же возвращает OAuth Авито — бэкенд редиректит на `${FRONTEND_BASE}/settings/accounts?…` (app/api/routes/avito_connect.py:51 константа, :379-381 хелпер, :615 успех `connected=1`, :578 `error=account_mismatch`, :531 `error=connect_link_expired`, :549/554/569/574 `error=oauth_failed`).

15) `/settings/templates` — router.tsx:148 → `TemplatesPage` (features/settings/templates/TemplatesPage.tsx:8), статический импорт (router.tsx:10).
    Право: `anyOf ["templates:shared"]` (router.tsx:147) → admin и head (app/core/rbac.py:33,41). МЕНЕДЖЕР СЮДА НЕ ПОПАДАЕТ.
    Ссылка: пункт «Быстрые ответы» при `can("templates:shared")` (SettingsLayout.tsx:20-24).

16) `/settings/bots` — router.tsx:158, ЛЕНИВЫЙ (router.tsx:159) → `BotsPage`.
    Право: `anyOf ["bots:manage"]` (router.tsx:155) → только admin.
    Ссылки: пункт «Боты» при `can("bots:manage")` (SettingsLayout.tsx:25-29); возврат из редактора (BotEditor.tsx:193 кнопка на экране «Бот не найден», BotEditor.tsx:211 хлебная крошка «← К списку ботов»).

17) `/settings/bots/:id` — router.tsx:162, ЛЕНИВЫЙ (router.tsx:163) → `BotEditor` (features/settings/bots/BotEditor.tsx, `useParams` строка 69).
    Право: то же `bots:manage` (общий guard router.tsx:155).
    Ссылки: только из списка ботов — после создания (BotsPage.tsx:37), после дублирования (BotsPage.tsx:87), клик по карточке (BotsPage.tsx:166). В меню не выведен.
    Несуществующий id: `EmptyState` «Бот не найден / Возможно, его удалили» + кнопка «К списку ботов» (BotEditor.tsx:184-199).

18) `/settings/distribution` — router.tsx:175, ЛЕНИВЫЙ (router.tsx:176-179) → `DistributionTab` (features/settings/distribution/DistributionTab.tsx:40).
    Право: `anyOf ["settings:manage"]` (router.tsx:172) → только admin (app/core/rbac.py:29,33).
    Ссылка: пункт «Распределение» при `can("settings:manage")` (SettingsLayout.tsx:30-34).
    Замечание по неймингу: компонент называется `DistributionTab` (вкладка), но используется как самостоятельная страница.

19) `/settings/team` — router.tsx:189, ЛЕНИВЫЙ (router.tsx:190) → `TeamPage` (features/settings/team/TeamPage.tsx:38).
    Право: `anyOf ["users:manage","audit:read"]` (router.tsx:186) → admin (обе) и head (только `audit:read`).
    Ссылка: пункт «Команда» при `canAny("users:manage","audit:read")` (SettingsLayout.tsx:35-39).
    Внутри — вкладки по правам: «Сотрудники» (`users:manage`) и «Журнал аудита» (`audit:read`), TeamPage.tsx:24-36, фильтр TeamPage.tsx:40. Если видимых вкладок ноль — `EmptyState` «Для вашей роли здесь пока нет разделов» (TeamPage.tsx:51-52).

═══════════════════════════════════════════════
2. ЧТО ВЫВЕДЕНО В МЕНЮ, А ЧТО НЕТ
═══════════════════════════════════════════════

Рельса слева (AppLayout.tsx:243-312) — ВСЕГО 4 ПУНКТА:
• Чаты → /chats (всегда)
• Разбор диалогов → /dialogs (только при `stats:all`)
• Статистика → /stats (только при `stats:all`)
• Настройки → /settings/profile (всегда)

Меню аватара (AppLayout.tsx:154-176): статус присутствия (`PresenceMenuItems`), «моя статистика» (только мобильная раскладка + роль manager + `stats:own`, AppLayout.tsx:163), Профиль → /settings/profile, Что нового → /updates, Выйти.

Колокольчик (AppLayout.tsx:237 → NotificationBell.tsx) → панель → «Показать все» → /notifications. Колокольчик целиком скрыт для ролей без `conversations:manage` (NotificationBell.tsx:20).

Навигация настроек (SettingsLayout.tsx:14-43): Аккаунты Авито, Быстрые ответы, Боты, Распределение, Команда, Профиль.

ПОПАСТЬ ТОЛЬКО ВВОДОМ АДРЕСА (ни одной ссылки в интерфейсе): `/ui-kit`, `/invite/:token` (по внешней ссылке), `/settings` (голый, без хвоста), `/` (корень).
ПОПАСТЬ ТОЛЬКО ЧЕРЕЗ НЕОЧЕВИДНЫЙ ВХОД: `/updates` (только меню аватара), `/notifications` (только панель колокольчика/плашка), `/settings/bots/:id` (только из списка ботов), `/chats/:id` (только из списков и хоткеев).

═══════════════════════════════════════════════
3. ЭКРАНЫ 404 / ОШИБОК / ЗАГРУЗКИ / ВХОДА
═══════════════════════════════════════════════

404 (страница не найдена): `NotFoundPage`, router.tsx:44-58. Текст «Страница не найдена» / «Такой страницы нет или она переехала» + кнопка «К диалогам». Рисуется на `minHeight: 100dvh` без каркаса приложения.

Экран входа: `LoginPage` (features/auth/LoginPage.tsx). Состояния: `invalid_credentials` (общий текст «Неверный email или пароль» + тряска карточки, строки 90-97, 210-214), `account_locked` с обратным отсчётом (99-105, 154-158, таймер 63-71), `forbidden` (106-111), сетевая ошибка «Сервер недоступен» (112-114, 164-168), прочее (115-116). Тип состояний — LoginPage.tsx:22-27.

Приглашение: `/invite/:token` — три состояния: скелетон загрузки (InvitePage.tsx:103-115), «Ссылка устарела» (`ExpiredView`, InvitePage.tsx:52-69, срабатывает на `invite_expired`/404/410, условие строка 97-101), ошибка загрузки с кнопкой «Повторить» (117-130).

Загрузка приложения: `FullscreenLoader` (shared/ui/FullscreenLoader.tsx:5) — логотип + спиннер на весь экран. Используется в двух местах: `RequireAuth` пока `bootstrapped === false` (router.tsx:30) и `LoginPage` (строка 73).

Лениво загружаемые маршруты фолбэка НЕ ИМЕЮТ: используется `lazy` роутера v6, а не `React.Suspense` — на экране остаётся предыдущая страница до прихода чанка (развёрнутое обоснование в комментарии router.tsx:77-93). `Suspense` встречается только внутри двух страниц с `fallback={null}` (AccountsPage.tsx:349-351, BotEditor.tsx:351-353).

ЭКРАНА ОШИБКИ МАРШРУТА НЕТ. `errorElement` не задан ни на одном маршруте (grep по frontend/src: 0 совпадений), глобального `ErrorBoundary`/`componentDidCatch` тоже нет (grep: 0). Значит падение рендера или сбой загрузки ленивого чанка (обрыв сети после деплоя) отдаёт встроенный экран ошибки react-router-dom (пакет `^6.26.0`, frontend/package.json:26) — не локализованный и не оформленный под продукт.

Соединение: не экраны, а полосы — `ConnectionIndicator` (точка в шапке, опрос /api/health раз в 30 с, AppLayout.tsx:41-80) и `ConnectionBanner` («Нет соединения — переподключаемся…» / «Соединение восстановлено», AppLayout.tsx:83-113).

═══════════════════════════════════════════════
4. РЕДИРЕКТЫ И ЗАЩИТА МАРШРУТОВ
═══════════════════════════════════════════════

Guard 1 — сессия: `RequireAuth` (router.tsx:26-33).
• `bootstrapped === false` → `FullscreenLoader` (ждём тихий refresh по httpOnly-куке).
• `user === null` → `<Navigate to="/login" state={{ from: loc }} replace />`. Исходный адрес сохраняется и после входа туда возвращают (LoginPage.tsx:55-56, 86).
• Начальная загрузка сессии: `sessionStore.bootstrap()` (shared/stores/sessionStore.ts:99-119), запускается в App.tsx:13.

Guard 2 — права: `RequirePermission` (router.tsx:39-42).
• `canAny(...) === false` → `<Navigate to="/chats" replace />`. НЕ страница 403 — редирект. Обоснование в комментарии router.tsx:35-38: «пользователь никогда не видел пункт меню, он сюда попал только по прямой ссылке».
• Права берутся исключительно из `GET /auth/me` (shared/auth/usePermissions.ts:31-42, комментарий 26-30), фронт роль→права не хардкодит.

Что происходит при 401 в фоне: единичный тихий refresh, при неудаче — `useSessionStore.getState().clear()` (shared/api/http.ts:112-116, :152-155, :179-182), после чего `RequireAuth` уводит на /login.

Смена роли/пользователя на лету: `useRoleUiSync` (shared/auth/useRoleUiSync.ts) чистит кэш запросов и сессионное UI-состояние; вызывается из AppLayout.tsx:196.

ОСОБЫЙ СЛУЧАЙ: если `GET /auth/me` не ответил, сессия остаётся, а `permissions` выставляются в пустой массив (sessionStore.ts:73-76 и :114-116). Тогда ВСЕ `RequirePermission` возвращают false, и человек заперт в `/chats` + `/settings/profile` + `/updates` + `/ui-kit`, без единого сообщения о причине.

Доступ по ролям (сверено с app/core/rbac.py:32-60):
• admin — все 19 маршрутов.
• head — всё, кроме `/settings/bots`, `/settings/bots/:id`, `/settings/distribution`. На `/settings/team` видит только вкладку «Журнал аудита».
• manager — `/chats`, `/chats/:id`, `/settings/profile`, `/updates`, `/ui-kit`, `/notifications`. Всё остальное → редирект на `/chats`.
• observer — то же, что у менеджера, но `/notifications` показывает «Вашей роли уведомления не приходят» (нет `conversations:manage`).

Серверный SPA-fallback: `try_files $uri /index.html` (docker/nginx/templates/leadchat.conf.template:339-341) — любой неизвестный адрес отдаёт приложение, дальше решает роутер. Вне SPA живут: `/api/*`, `/download*` (:206-275), `/connect/*` (:305-311, переписывается в `/api/v1/avito/connect/$1`), `/fake-oauth` (:330), `/healthz` (:41), `/assets/*` (:287-297).

═══════════════════════════════════════════════
5. ГЛУБОКИЕ ССЫЛКИ: ЧТО В URL, А ЧТО ТЕРЯЕТСЯ
═══════════════════════════════════════════════

В URL КОДИРУЕТСЯ (переживает перезагрузку):
• Активный диалог — `/chats/:id`. Прямо заявлено источником истины: ChatsPage.tsx:18 «URL — источник истины по активному диалогу; стор — зеркало», зеркалирование в стор — ChatsPage.tsx:31-34.
• Токен приглашения — `/invite/:token` (InvitePage.tsx:73).
• Идентификатор бота — `/settings/bots/:id` (BotEditor.tsx:69).
• Возврат OAuth Авито — `?connected=1` / `?error=…` на `/settings/accounts`. Читается один раз, показывает тост и СРАЗУ ВЫЧИЩАЕТСЯ: `setSearchParams({}, {replace:true})` (AccountsPage.tsx:557-586, чистка на строке 585). Обрабатываются `connected=1` и `account_mismatch` явно, остальные коды падают в общую ветку «Не удалось подключить аккаунт» (AccountsPage.tsx:577-583).
• Признак «фокус в композер» — `?reply=1`, но только в десктопном deep-link'е (desktop/src-tauri/src/lib.rs:312-314).
Это ИСЧЕРПЫВАЮЩИЙ список: `useSearchParams` во всём проде встречается ровно один раз (AccountsPage.tsx:15,557), `useParams` — три раза (ChatsPage.tsx:21, InvitePage.tsx:73, BotEditor.tsx:69).

В URL НЕ КОДИРУЕТСЯ И ТЕРЯЕТСЯ ПРИ ПЕРЕЗАГРУЗКЕ:
• Таблица разбора `/dialogs` — ВСЕ 11 параметров в локальном `useState`: период, произвольный диапазон дат, статус, аккаунт, исполнитель, тег, бот, сортировка, направление, offset (features/table/TablePage.tsx:132-141). Ссылку на «отчёт за такой-то период по такому-то каналу» коллеге отправить нечем.
• Статистика `/stats` — пресет периода, период, менеджеры, аккаунт, метрика, группировка, сортировка, порядок (features/stats/StatsPage.tsx:27-35).
• Журнал уведомлений `/notifications` — пресет, произвольные даты, важность, вид, «только непрочитанные», offset (features/notifications/NotificationsPage.tsx:138-144).
• Вкладка на `/settings/team` — `useState` (TeamPage.tsx:42), сбрасывается на первую доступную.
• Вкладка на `/settings/profile` — `useState("account")` (ProfilePage.tsx:36).
• Фильтры и вкладка списка чатов (Все/Мои/Новые/Закрытые + поиск `q` + канал + менеджер + статус + тег) — в zustand-сторе `chatUiStore.filters` (shared/stores/chatUiStore.ts:27, форма типа — shared/api/queryKeys.ts:21-28) и НЕ персистятся: `partialize` сохраняет только `soundEnabled`, `drafts`, `draftsOwnerId` (chatUiStore.ts:140-144). Явный комментарий chatUiStore.ts:11-12: «Персистятся ТОЛЬКО черновики и звук; фильтры, активный диалог и оверлей карточки — сессионные».
• Открытость очереди «Входящие» — `inboxOpen` там же (chatUiStore.ts:35), тоже сессионная.
• Оверлей карточки клиента на узких экранах — `clientCardOpen` (chatUiStore.ts:38), сбрасывается при каждой смене диалога (ChatsPage.tsx:37-42).
• Режим «Не помню пароль» на /login (LoginPage.tsx:52).

Адаптив, влияющий на смысл URL: пороги в shared/lib/breakpoints.ts — `MOBILE_MAX = 767`, `NARROW_MAX = 1359`. Ниже 768px `/chats` показывает ТОЛЬКО список, а `/chats/:id` — ТОЛЬКО ленту (ChatsPage.tsx:45-52). То есть одна и та же ссылка на разных ширинах открывает разные экраны.

═══════════════════════════════════════════════
6. ВТОРАЯ ТОЧКА ВХОДА В МАРШРУТЫ: ДЕСКТОП (Tauri)
═══════════════════════════════════════════════

Схема `leadchat://` (desktop/src-tauri/src/lib.rs:42). Белый список корней — desktop/src-tauri/src/lib.rs:61:
  `["chats", "settings", "stats", "team", "templates"]`
Разбор и санитайзинг — `parse_deep_link` (lib.rs:277-299); пустой путь даёт `/chats` (lib.rs:284). Маршрут уходит во фронт событием `navigate` (lib.rs:49, эмит на :331 и notify.rs:398), с отложенной очередью на холодный старт (lib.rs:303-332).
Фронтовый приёмник — features/platform: `listenSafe<string>(NAVIGATE_EVENT, …)` (frontend/src/platform/tauri/notifier.ts:241-244), который прогоняет путь через `conversationIdFromPath` — регулярка `/chats\/([^/?#]+)/` (notifier.ts:188-191) — и вызывает колбэк ТОЛЬКО если id найден. Дальше `openConversation` делает `router.navigate('/chats/'+id)` (frontend/src/platform/index.ts:128-134).
Следствие: реально работает ровно один корень — `chats`. См. раздел «мёртвое».

═══════════════════════════════════════════════
7. НЕПРОДОВЫЙ СТЕНД ВЁРСТКИ (отдельная навигация)
═══════════════════════════════════════════════

frontend/dev-preview.html + frontend/dev/preview.tsx — параллельная навигация через query-параметры: `?screen=<ключ>&theme=light|dark&w=<px>` (документация в шапке файла, preview.tsx:19-39). Список экранов — `SCREENS` (preview.tsx:375+): profile, accounts, team, distribution и др. Работает на `MemoryRouter` (preview.tsx:47), в производственную сборку не входит (Vite собирает единственный вход index.html — комментарий в dev-preview.html:9-12).

### Есть в коде, но не выведено (10)

* **Маршрут /ui-kit — живой каталог компонентов дизайн-системы. Ни одной ссылки в интерфейсе.**
  * где: frontend/src/app/router.tsx:107-112 (объявление), компонент frontend/src/features/uikit/UiKitPage.tsx. Grep по строке «ui-kit» во всём репозитории даёт ровно одно совпадение — router.tsx:108.
  * почему важно: Страница доступна ЛЮБОМУ вошедшему, включая наблюдателя и менеджера: она сидит внутри RequireAuth, но без RequirePermission. Попасть можно только вводом адреса, и это единственная защита. Страница рендерит настоящие компоненты продукта на выдуманных данных (UiKitPage.tsx:24-34).
* **Deep-link корни «team» и «templates» ведут в НЕСУЩЕСТВУЮЩИЕ маршруты SPA.**
  * где: desktop/src-tauri/src/lib.rs:61 — DEEP_LINK_ROOTS = ["chats","settings","stats","team","templates"]. parse_deep_link (lib.rs:277-299) собирает путь как "/" + segments.join("/"), то есть leadchat://team → «/team», leadchat://templates → «/templates». В router.tsx таких маршрутов нет — есть только /settings/team (router.tsx:189) и /settings/templates (router.tsx:148).
  * почему важно: Белый список утверждает, что эти разделы можно открыть по ссылке из Windows. Если бы событие дошло до роутера, человек увидел бы страницу «Страница не найдена». Фактически не увидит и её — см. следующий пункт.
* **Deep-link корни «settings», «stats», «team», «templates» фронтендом молча игнорируются — работает только «chats».**
  * где: Rust принимает и эмитит их: desktop/src-tauri/src/lib.rs:61, :331. Фронтовый обработчик — frontend/src/platform/tauri/notifier.ts:241-244: он прогоняет путь через conversationIdFromPath (notifier.ts:188-191, регулярка /chats\/([^/?#]+)/) и вызывает колбэк ТОЛЬКО при найденном id. Дальше frontend/src/platform/index.ts:128-134 умеет открывать исключительно /chats/{id}.
  * почему важно: Четыре из пяти разрешённых корней deep-link'а — мёртвый код: ссылка leadchat://stats поднимет окно приложения и не сделает ничего. Расхождение между тем, что разрешает Rust, и тем, что умеет фронт, ничем не покрыто.
* **Ни на одном маршруте нет errorElement, во всём приложении нет ErrorBoundary.**
  * где: frontend/src/app/router.tsx — grep «errorElement|ErrorBoundary|componentDidCatch|hydrateFallback» по всему frontend/src даёт 0 совпадений. Роутер — react-router-dom ^6.26.0 (frontend/package.json:26). Точка монтирования frontend/src/main.tsx:28-32 тоже без обёртки.
  * почему важно: Семь маршрутов грузятся лениво (router.tsx:95,103,109,121,133,159,163,176,190). Сбой загрузки чанка после деплоя или падение рендера отдаёт встроенный экран react-router — англоязычный, со стеком, без кнопки возврата. Для оператора в смене это выглядит как «система умерла».
* **Catch-all маршрут 404 объявлен ВНЕ RequireAuth — неавторизованный по кривому адресу не уходит на вход.**
  * где: frontend/src/app/router.tsx:200 — { path: "*", element: <NotFoundPage /> } стоит на верхнем уровне массива, соседом с /login (:61) и /invite/:token (:62), а не внутри { element: <RequireAuth />, children: [...] } (:63-199).
  * почему важно: Человек без сессии, набравший /chatz или /settings/accaunts, видит «Страница не найдена» с кнопкой «К диалогам» → /chats → и только тогда его перекидывает на /login. Два шага вместо одного, и страница 404 при этом рисуется без шапки и без рельсы.
* **У менеджера «Быстрые ответы» существуют, но НЕ в разделе настроек — только вкладкой в профиле.**
  * где: Маршрут /settings/templates закрыт правом templates:shared (router.tsx:147), которого у роли manager нет (app/core/rbac.py:48-58). Пункт навигации тоже скрыт (SettingsLayout.tsx:20-24). Личные шаблоны выведены отдельной вкладкой на /settings/profile при can("templates:own") (ProfilePage.tsx:112-128).
  * почему важно: Право templates:own объявлено в каталоге (shared/auth/usePermissions.ts:14, app/core/rbac.py:16), но НЕ используется ни одним guard'ом маршрута — только для сборки вкладки. Оператор, которому сказали «быстрые ответы в настройках», в настройках их не найдёт.
* **Маршрут /notifications не выведен ни в рельсу, ни в меню — попасть можно только через выпадающую панель колокольчика.**
  * где: router.tsx:120 (объявление, без guard'а). Единственные входы: features/notifications/NotificationPanel.tsx:107-118 («Показать все») и features/notifications/CriticalBanners.tsx:87. В AppLayout.tsx:243-312 (рельса) и AppLayout.tsx:154-176 (меню аватара) ссылки нет.
  * почему важно: Колокольчик целиком скрыт для ролей без conversations:manage (NotificationBell.tsx:20). Для наблюдателя это значит: маршрут существует, отдаёт текст «Вашей роли уведомления не приходят» (NotificationsPage.tsx:186-188), а дойти до него в интерфейсе нельзя вовсе.
* **Маршрут /updates («Что нового») спрятан в меню аватара, в рельсе его нет.**
  * где: router.tsx:101-106 (объявление, без guard'а). Единственный вход — AppLayout.tsx:171-174.
  * почему важно: Экран заведён ради переезда команды с Jivo (обоснование в UpdatesPage.tsx:8-18 и AppLayout.tsx:165-170) — то есть рассчитан на людей, которые интерфейс ещё не знают. Точка непрочитанного (AppLayout.tsx:173) видна только тому, кто уже открыл меню аватара.
* **Компонент страницы распределения называется вкладкой — след прежней структуры.**
  * где: features/settings/distribution/DistributionTab.tsx:40 — export function DistributionTab(). Импортируется как самостоятельный Component маршрута /settings/distribution (router.tsx:175-180).
  * почему важно: Не дефект поведения, но признак: раньше это была вкладка внутри другой страницы. Такое же имя-след — TeamPage с настоящими вкладками (TeamPage.tsx:24-36) рядом с DistributionTab без них.
* **Параллельная навигация стенда вёрстки через query-параметры — отдельная поверхность, не связанная с роутером.**
  * где: frontend/dev-preview.html и frontend/dev/preview.tsx: ?screen=<ключ из SCREENS>&theme=&w= (документация preview.tsx:19-39, карта экранов preview.tsx:375+, MemoryRouter preview.tsx:47).
  * почему важно: В производственную сборку не входит (Vite собирает единственный вход index.html — dev-preview.html:9-12), но это второй список экранов, который может разойтись с router.tsx: сейчас в SCREENS есть profile/accounts/team/distribution и нет bots, stats, dialogs, updates, notifications.

### Расхождения с брифом и чего не нашёл

РАСХОЖДЕНИЯ С БРИФОМ ЗАКАЗЧИКА

1) «/settings/quick-replies» — ТАКОГО МАРШРУТА НЕТ. В коде он называется `/settings/templates` (frontend/src/app/router.tsx:148). Подпись в интерфейсе действительно «Быстрые ответы» (SettingsLayout.tsx:22, TemplatesPage.tsx:15), но адрес — `templates`. Слово «quick-replies» во всём репозитории не встречается ни разу (проверено grep по frontend/src, app, docker, desktop).

2) РОЛЕЙ НЕ ДВЕ, А ЧЕТЫРЕ. Бриф называет «Администратор, Менеджер». В коде: `admin`, `head`, `manager`, `observer`.
   • app/core/rbac.py:7 — ROLES = ("admin","head","manager","observer"), матрица прав rbac.py:32-60.
   • app/models/user.py:16 — CheckConstraint "role IN ('admin','head','manager','observer')".
   • frontend/src/shared/auth/usePermissions.ts:4 — type Role = "admin" | "head" | "manager" | "observer".
   • Подписи в интерфейсе: AppLayout.tsx:33-38 и ProfilePage.tsx:14-19 — «Администратор», «Руководитель», «Менеджер», «Наблюдатель».
   Для маршрутов это принципиально: `/dialogs`, `/stats`, `/settings/accounts`, `/settings/templates`, `/settings/team` открыты роли `head`, которой в брифе нет вовсе. А роль `manager` из брифа не имеет доступа НИ К ОДНОМУ из разделов `/dialogs`, `/stats`, `/settings/accounts`, `/settings/templates`, `/settings/bots`, `/settings/distribution`, `/settings/team` — все семь дают редирект на `/chats` (router.tsx:41).

3) БРИФ ПЕРЕЧИСЛЯЕТ 9 РАЗДЕЛОВ, В КОДЕ 19 МАРШРУТОВ. Не названы в брифе: `/login`, `/invite/:token`, `/` (редирект на /chats), `/chats/:id`, `/updates`, `/ui-kit`, `/notifications`, `/settings` (редирект на профиль), `/settings/bots/:id`, `*` (404).

4) НАЗВАНИЕ РАЗДЕЛА `/dialogs` В ИНТЕРФЕЙСЕ — «Разбор диалогов», не «Диалоги» (AppLayout.tsx:276 tooltip, :280 aria-label). Заголовок самой страницы и её назначение — управленческий отчёт (TablePage.tsx:17-31), а не список переписок. Слово «диалоги» в продукте занято дважды: кнопка на 404 «К диалогам» ведёт на `/chats` (router.tsx:53-55), а не на `/dialogs`.

5) БРИФ ПОДРАЗУМЕВАЕТ ПЛОСКИЙ СПИСОК РАЗДЕЛОВ, В КОДЕ ДВА УРОВНЯ ВЛОЖЕННОСТИ ПЛЮС ВКЛАДКИ БЕЗ АДРЕСА. `/settings/team` содержит вкладки «Сотрудники» и «Журнал аудита» (TeamPage.tsx:24-36), `/settings/profile` — «Учётная запись», «Интерфейс», «Быстрые ответы» (ProfilePage.tsx:57-129). Ни одна вкладка не имеет своего адреса — сослаться на «журнал аудита» ссылкой нельзя.

ЧЕГО НЕ НАШЁЛ (проверял, отсутствует)
• Экрана 403 / «нет доступа» — нет: `RequirePermission` молча редиректит на `/chats` (router.tsx:39-42), человек не узнаёт, что упёрся в право.
• `errorElement` / `ErrorBoundary` / `hydrateFallbackElement` — 0 совпадений по frontend/src.
• `loader` / `action` / `shouldRevalidate` react-router — не используются вовсе; данные грузит TanStack Query внутри компонентов.
• Отдельного маршрута восстановления пароля — нет, это режим внутри LoginPage (LoginPage.tsx:52, 150, 217).
• Маршрута регистрации — нет; единственный путь заведения сотрудника — `/invite/:token`.
• Вложенных маршрутов внутри `/chats` (например, `/chats/:id/client` или `/chats/:id/notes`) — нет: правая карточка и вкладки внутри неё адресами не кодируются.
• Каких-либо `<Route>`-элементов вне router.tsx в проде — нет (совпадения только в frontend/src/test/*.tsx: BotsPage.test.tsx:25-28, BotEditorValidation.test.tsx:30-31, ForgotPassword.test.tsx:23-25, BotSandbox.test.tsx:92-93, LoginPage.test.tsx:17-19).

ЧТО СТОИТ ПРОВЕРИТЬ ДРУГИМ СРЕЗАМ АУДИТА (за границей моего)
• Внешний вход `/connect/{token}` при протухшем токене редиректит на `/settings/accounts?error=connect_link_expired` (app/api/routes/avito_connect.py:531 + :379-381 + :51). Но по этой ссылке приходит ЧЕЛОВЕК БЕЗ УЧЁТНОЙ ЗАПИСИ В LEADCHAT (прямо сказано в докстринге avito_connect.py:500-508) — он упрётся сначала в `/login`, а после входа (если он вообще сотрудник) ещё и в право `accounts:read`. Куда он должен попасть на самом деле — вопрос за пределами маршрутов фронтенда.
• `/settings/accounts` обрабатывает только `connected=1` и `account_mismatch`; коды `connect_link_expired` и `oauth_failed` попадают в общую ветку «Не удалось подключить аккаунт. Попробуйте ещё раз» (AccountsPage.tsx:577-583) — сообщение не соответствует причине.

## Срез: API и WebSocket

# СРЕЗ «API». Инвентаризация LeadChat, leadchat

Соответствия проверены сплошным перебором: все декораторы `@router.*` в `app/api/routes/` (грепом) против всех вызовов `http.get/post/patch/put/del`, `request<>`, `requestForm`, `requestBlob`, `fetchHealth`, `fetch(` в `frontend/src` (кроме `frontend/src/test/`).

## 0. Как всё смонтировано

- Фабрика приложения: `app/main.py:62-178`. Префикс `/api/v1` (`main.py:101`), поверх него подключаются роутеры без собственных префиксов (кроме `auth` — `main.py:102`).
- Вне `/api/v1`: `health.router` (`main.py:173`), `webhooks.router` (`main.py:174`), и WS-алиас `app.websocket("/api/ws")` (`main.py:177`).
- Часть роутеров монтируется «защищённо» через `importlib`: `avito_connect` (`main.py:145-150`), `avito_accounts` (`main.py:152-155`), `stats` и `audit` (`main.py:160-170`). Отсутствие модуля не роняет приложение — только `log.warning`.
- Клиент: `frontend/src/shared/api/http.ts`. База — `${VITE_API_BASE ?? ""}/api/v1` (`http.ts:4-6`). Единый конверт ошибок (`http.ts:11-44`), single-flight refresh на 401 (`http.ts:73-125`).
- Ключи кэша объявлены в одном месте: `frontend/src/shared/api/queryKeys.ts:72-161`.

## 1. Матрица ролей и прав (источник истины)

`app/core/rbac.py:7` — ролей **четыре**: `admin`, `head`, `manager`, `observer`.
`app/core/rbac.py:10-30` — 15 прав. `app/core/rbac.py:32-60` — раскладка:

| право | admin | head | manager | observer |
|---|---|---|---|---|
| conversations:read | ✓ | ✓ | ✓ | ✓ |
| messages:send | ✓ | — | ✓ | — |
| conversations:manage | ✓ | ✓ | ✓ | — |
| notes:read / notes:write | ✓ | ✓ | ✓ | — |
| templates:own | ✓ | ✓ | ✓ | — |
| templates:shared | ✓ | ✓ | — | — |
| stats:own | ✓ | ✓ | ✓ | — |
| stats:all | ✓ | ✓ | — | — |
| bots:manage | ✓ | — | — | — |
| accounts:read | ✓ | ✓ | — | — |
| accounts:manage | ✓ | — | — | — |
| users:manage | ✓ | — | — | — |
| audit:read | ✓ | ✓ | — | — |
| settings:manage | ✓ | — | — | — |
| conversations:close_queued | ✓ | — | — | — |

Проверка прав — `require_permission` в `app/api/deps.py:69-83`. Роль читается из строки БД, не из JWT (`deps.py:52-55`). Особый случай: `head` на `messages:send` получает `403 read_only_role` (`deps.py:77-79`) — фронт по этому коду рисует «Режим просмотра».
Фронт зеркалит каталог: `frontend/src/shared/auth/usePermissions.ts:4` (роли), `:8-24` (права); права приходят из `GET /auth/me`, матрица на фронте не захардкожена (`usePermissions.ts:26-42`).

---

## 2. ПОЛНЫЙ РЕЕСТР ЭНДПОИНТОВ

Легенда столбца «фронт»: ✔ — вызывается; ✖ — серверная ручка без вызывающего кода в `frontend/src`.

### 2.1 Auth — `app/api/routes/auth.py`

| Метод, путь | Строка | Право | Параметры | Ответ | Фронт |
|---|---|---|---|---|---|
| POST /auth/login | 120 | публично | body `{email, password, remember}` | `{access_token, expires_in, user}` + cookie `lc_refresh` (`auth.py:64-73`) | ✔ `shared/stores/sessionStore.ts:80` |
| POST /auth/refresh | 206 | cookie `lc_refresh` | — | тот же `LoginResponse`, ротация cookie | ✔ `shared/api/http.ts:93` |
| POST /auth/logout | 230 | любая авторизованная | — | 204 | ✔ `sessionStore.ts:86` |
| GET /auth/me | 248 | любая авторизованная | — | `{id,email,full_name,role,is_active,permissions[]}` | ✔ `sessionStore.ts:71,112` |
| GET /auth/invite/{token} | 260 | публично | path token | `{email, full_name}` | ✔ `features/auth/api.ts:6` |
| POST /auth/invite/accept | 275 | публично | `{token,password}` | `LoginResponse` | ✔ `features/auth/api.ts:11` |
| POST /auth/change-password | 323 | любая авторизованная | `{current_password,new_password}` | `{"status":"ok"}` | ✖ |
| PATCH /auth/me | 386 | любая авторизованная | `{full_name}` | `{"full_name": …}` | ✖ |

### 2.2 Диалоги — `app/api/routes/conversations.py`

| Метод, путь | Строка | Право | Параметры | Ответ | Фронт |
|---|---|---|---|---|---|
| GET /conversations | 181 | conversations:read | query: `tab` (`my`/`mine`, `all`, `new`, `closed`, `any`, `inbox` — `conversations.py:183`, разбор `services/conversations.py:339-390`), `q`, `status` (`^(new\|in_progress\|closed)$`), `account_id`, `assignee_id`, `unassigned`, `unread_only`, `tag`, `updated_since`, `limit` 1..200 (деф. 50), `offset` | `{items:[ConversationOut], page:{limit,offset,total}}` | ✔ `features/chats/api.ts:40`, догон `shared/realtime/applyWsEvent.ts:506` |
| GET /conversations/table | 251 | **stats:all** (`conversations.py:55`) | `status, account_id, assignee_id, tag, bot_active, date_from, date_to, q, sort, direction(asc\|desc), limit≤100, offset` | `{items:[TableRow], page}` | ✔ `features/table/api.ts:78` |
| GET /conversations/table/export | 295 | stats:all | те же, кроме пагинации | CSV `text/csv; charset=utf-8`, BOM, `Content-Disposition` (`conversations.py:336-343`) | ✔ `features/table/api.ts:83` |
| POST /conversations/{id}/participants | 354 | messages:send | `{user_id, reason?}` | `{participants:[…]}` | ✔ `features/chats/api.ts:129` |
| DELETE /conversations/{id}/participants/{user_id} | 423 | messages:send | — | `{participants:[…]}` | ✔ `features/chats/api.ts:139` |
| POST /conversations/{id}/transfer/accept | 503 | messages:send | — | деталь диалога | ✔ `components/thread/TransferBar.tsx:43` |
| POST /conversations/{id}/transfer/decline | 533 | messages:send | — | деталь диалога | ✔ там же |
| GET /conversations/{id} | 564 | conversations:read | — | ConversationOut + `bot_vars, external_chat_id, first_client_at, client_conversations_count, participants, unread_count` (`services/conversations.py:685-693`, `routes/conversations.py:574`) | ✔ `features/chats/api.ts:48` |
| GET /conversations/{id}/client-history | 582 | conversations:read | — | `{client:{id,name}, items:[…]}` (`services/conversations.py:749-756`) | ✔ `features/chats/api.ts:146` |
| GET /conversations/{id}/messages | 593 | conversations:read | `before`, `after`, `limit` (курсорная пагинация) | `{items:[MessageOut], page:{prev_cursor,next_cursor,has_more_before,has_more_after}}` (`services/conversations.py:1219-1229`). Заметки отсекаются по `notes:read` (`conversations.py:612`) | ✔ `features/chats/api.ts:59` |
| POST /conversations/{id}/read | 616 | conversations:read | — | 204 | ✔ `features/chats/api.ts:66` |
| PATCH /conversations/{id}/status | 649 | conversations:manage | `{status, outcome?, outcome_amount_rub?}` (`conversations.py:68-82`) | деталь диалога | ✔ `features/chats/api.ts:101` |
| POST /conversations/{id}/assign | 676 | conversations:manage | `{assignee_id \| null, comment?}` | `{conversation, system_message}` | ✔ `features/chats/api.ts:114` |
| POST /conversations/{id}/pin | 796 | conversations:read | — | `{"pinned": true}` | ✔ `features/chats/api.ts:194` |
| DELETE /conversations/{id}/pin | 815 | conversations:read | — | `{"pinned": false}` | ✔ `features/chats/api.ts:198` |

Форма элемента списка — `app/services/conversations.py:165-262`: `pinned, id, status, channel, account{id,title}, client{id,name,phone,avito_rating,blocked,blocked_reason}, assignee, item{title,url,price}, last_message{body,direction,created_at}, unread_count, undelivered, outcome, outcome_amount, bot_active, tags[], transferred_to_me, last_message_at, in_inbox, offered_at, escalated, transfer`.

### 2.3 Сообщения и вложения — `app/api/routes/messages.py`, `media.py`

| Метод, путь | Строка | Право | Параметры | Ответ | Фронт |
|---|---|---|---|---|---|
| POST /conversations/{id}/messages | messages.py:76 | messages:send | `{text, client_message_id, attachments:[{media_id}]}` | 201 MessageOut; повтор — 200 + `X-Idempotent-Replay: true` (`messages.py:66-73`) | ✔ `features/chats/api.ts:75` |
| POST /conversations/{id}/notes | messages.py:112 | **notes:write** | то же | 201/200 MessageOut | ✔ `features/chats/api.ts:80` |
| POST /messages/{id}/retry | messages.py:144 | messages:send | — | MessageOut | ✔ `features/chats/api.ts:85` |
| POST /media | media.py:74 | messages:send | multipart, поле `file` | `{media_id, kind, name, size, url}` | ✔ `features/chats/api.ts:164` (`requestForm`) |
| GET /media/{relpath:path} | media.py:87 | без auth, подпись `sig`+`exp` (`media.py:94`) | — | файл | ✖ прямых вызовов нет — ссылки подписываются в `message_out` (`services/conversations.py:155`) и открываются браузером; в проде путь до Python не доходит (`media.py:8-11`) |

### 2.4 Шаблоны — `app/api/routes/templates.py` (весь модуль под `templates:own`)

| Метод, путь | Строка | Параметры | Ответ | Фронт |
|---|---|---|---|---|
| GET /templates/folders | 143 | — | `{shared:[], personal:[]}` | ✔ `features/templates/api.ts:12` |
| GET /templates | 165 | `scope` (`all\|shared\|personal`), `folder`, `q`, `limit≤200`, `offset` | `{items,page}` | ✔ `features/templates/api.ts:7` (шлёт только `scope` и `limit=200`) |
| POST /templates | 209 | `{title,body,folder?,shared}` — `shared:true` требует `templates:shared` (`templates.py:215`) | TemplateOut | ✔ `api.ts:17` |
| PATCH /templates/{id} | 242 | `{title?,body?,folder?}` | TemplateOut | ✔ `api.ts:22` |
| DELETE /templates/{id} | 277 | — | 204 | ✔ `api.ts:27` |

### 2.5 Сотрудники — `app/api/routes/users.py`

| Метод, путь | Строка | Право | Параметры | Фронт |
|---|---|---|---|---|
| GET /users/assignable | 70 | conversations:manage | `include_inactive` | ✔ `shared/api/reference.ts:14` (флаг не шлёт) |
| GET /users | 138 | users:manage | `q, role, is_active, limit≤MAX, offset` | ✔ `settings/team/api.ts:33` |
| POST /users | 159 | users:manage | `{email,full_name,role}` → 201 `{user, invite_url, invite_expires_at}` | ✔ `team/api.ts:38` |
| POST /users/invite (алиас) | 160 | users:manage | то же | ✖ |
| POST /users/{id}/invite | 179 | users:manage | — | ✔ `team/api.ts:49` |
| POST /users/{id}/resend-invite (алиас) | 180 | users:manage | — | ✖ |
| POST /users/{id}/reset-password | 192 | users:manage | — | ✔ `team/api.ts:49` |
| POST /users/{id}/set-password | 209 | users:manage | `{password}` | ✔ `team/api.ts:111` |
| DELETE /users/{id} | 233 | users:manage | — | ✔ `team/api.ts:96` |
| PATCH /users/{id} | 251 | users:manage | `{full_name?, role?, handles_conversations?, department?}` | ✔ `team/api.ts:67` |
| POST /users/{id}/deactivate | 277 | users:manage | — | ✔ `team/api.ts:76` |
| POST /users/{id}/activate | 293 | users:manage | — | ✔ `team/api.ts:80` |

### 2.6 Очередь «Входящие» — `app/api/routes/inbox.py`

| Метод, путь | Строка | Право | Параметры | Ответ | Фронт |
|---|---|---|---|---|---|
| GET /inbox | 105 | conversations:read | `account_id`, `limit` 1..200, `offset` | `{items,page}` — элемент = ConversationOut + `waiting_seconds, waiting_human, declined_count, escalated` (`services/inbox.py:377-386`) | ✔ `features/chats/inbox/api.ts:41` (шлёт только limit/offset) |
| GET /inbox/count | 134 | conversations:read | — | `{count, escalated}` | ✔ `inbox/api.ts:69` |
| POST /conversations/{id}/claim | 152 | **messages:send** (`inbox.py:84`, право берётся из `ws/hub.py:61`) | — | `{conversation, count, escalated}`; гонка → 409 `already_claimed` | ✔ `inbox/api.ts:97` |
| POST /conversations/{id}/decline | 204 | messages:send | body необязательное `{reason?}` | `{conversation_id, declined, already_declined, reason, escalated_now, count, escalated}` | ✔ `inbox/api.ts:106` (тело не шлёт) |
| POST /conversations/{id}/decline/undo | 290 | messages:send | — | тот же DeclineOut | ✔ `inbox/api.ts:122` |
| POST /conversations/{id}/release | 357 | messages:send | — | `{conversation, count, escalated}` | ✔ `inbox/api.ts:117` |
| GET /inbox/stale | 409 | messages:send | query `days` **обязателен**, 1..365 (`services/queue_cleanup.py:49-51`) | предпросмотр | ✔ `inbox/api.ts:230` |
| POST /inbox/close-stale | 429 | messages:send | query `days` | `{closed, count, escalated}` | ✔ `inbox/api.ts:234` |

### 2.7 Уведомления — `app/api/routes/notifications.py` (весь модуль под `conversations:manage`, `services/notifications.py:87`)

| Метод, путь | Строка | Параметры | Фронт |
|---|---|---|---|
| GET /notifications | 73 | `severity, kind, date_from, date_to, unread_only, limit≤200, offset` → `{items, page, unread}` | ✔ `features/notifications/api.ts:27,31` |
| GET /notifications/unread-count | 122 | — → разбивка по важности | ✔ `api.ts:36` |
| POST /notifications/{id}/read | 133 | — → `{marked, unread}` | ✔ `api.ts:40` |
| POST /notifications/read-all | 147 | — | ✔ `api.ts:44` |
| POST /notifications/{id}/action | 159 | `{action?}` → `{action, result, unread}` | ✔ `api.ts:52` |

### 2.8 Статистика — `app/api/routes/stats.py`

Подроутер `protected` (`stats.py:43`) закрыт `stats:all`; `GET /stats/my/today` — `stats:own`.

| Метод, путь | Строка | Параметры | Фронт |
|---|---|---|---|
| GET /stats/summary | 76 | `date_from, date_to, account_id, manager_id` (повторяемый) | ✔ `features/stats/api.ts:35` |
| GET /stats/timeseries | 91 | + `metric` (деф. `conversations_new`), `group` (деф. `day`) | ✔ `api.ts:47` |
| GET /stats/heatmap | 109 | базовые (фронт `manager_id` не шлёт — `api.ts:54`) | ✔ `api.ts:55` |
| GET /stats/managers | 124 | + `sort` (деф. `messages_sent`), `order` (деф. `desc`); пагинации нет | ✔ `api.ts:63` |
| POST /stats/export | 175 | `{format, date_from, date_to, account_id, manager_id[], sheets[]}` → 202 `{job_id}` | ✔ `api.ts:68` |
| GET /stats/export/{job_id} | 202 | — → статус + подписанная ссылка | ✔ `api.ts:73` |
| GET /stats/my/today | 142 | — (`user_id` только из JWT) | ✔ `api.ts:78` |

### 2.9 Аккаунты Авито — `app/api/routes/avito_connect.py`

| Метод, путь | Строка | Право | Фронт |
|---|---|---|---|
| GET /avito/app | 232 | accounts:manage | ✔ `settings/accounts/AccountsPage.tsx:602`, `ConnectChannelWizard.tsx:78` |
| PUT /avito/app | 240 | accounts:manage | ✖ |
| POST /avito/app/mode | 272 | accounts:manage | ✔ `ConnectChannelWizard.tsx:93` |
| DELETE /avito/app | 298 | accounts:manage | ✖ |
| POST /avito/app/check | 321 | accounts:manage | ✖ |
| GET /avito/connect-url | 348 | accounts:manage | ✖ |
| POST /avito-accounts/{id}/reconnect | 357 | accounts:manage | ✔ `AccountsPage.tsx:173` |
| POST /avito-accounts/connect | 398 | accounts:manage | ✔ `ConnectChannelWizard.tsx:108` |
| POST /avito/connect-link | 492 | accounts:manage | ✖ |
| GET /avito/connect/{token} | 516 | публично (одноразовый токен) | ✖ |
| GET+POST /avito/callback | 537, 538 | публично (state-токен) | ✖ (редирект из браузера Авито) |
| GET /avito-accounts | 621 | accounts:read | ✔ `AccountsPage.tsx:51`, `shared/api/reference.ts:19` |
| POST /avito-accounts/{id}/disable | 657 | accounts:manage | ✔ `AccountsPage.tsx:206` |
| PATCH /avito-accounts/{id} | 692 | accounts:manage | ✔ `AccountsPage.tsx:265` |
| POST /avito-accounts/{id}/register-webhook | 727 | accounts:manage | ✔ `AccountsPage.tsx:239` |
| DELETE /avito-accounts/{id} | 773 | accounts:manage | ✔ `AccountsPage.tsx:279` |
| POST /avito-accounts/{id}/refresh-token | 825 | accounts:manage | ✔ `AccountsPage.tsx:297` |
| POST /avito-accounts/{id}/enable | 889 | accounts:manage | ✔ `AccountsPage.tsx:220` |

### 2.10 Операторы каналов — `app/api/routes/avito_accounts.py`

| Метод, путь | Строка | Право | Фронт |
|---|---|---|---|
| GET /avito-accounts/{id}/operators | 71 | accounts:read | ✔ `settings/accounts/api.ts:20` |
| PUT /avito-accounts/{id}/operators | 92 | accounts:manage | ✔ `settings/accounts/api.ts:37` |
| GET /me/channels | 127 | любая авторизованная | ✔ `settings/profile/api.ts:20` |
| GET /users/{user_id}/accounts | 142 | себе — любая; чужое — accounts:read (`avito_accounts.py:155`) | ✖ |
| GET /avito-accounts/{id}/subscriptions | 163 | accounts:manage | ✖ |

### 2.11 Прочее

| Метод, путь | Файл:строка | Право | Фронт |
|---|---|---|---|
| PUT /presence | presence.py:50 | любая авторизованная; статус из `PRESENCE_STATUSES` | ✔ `features/presence/usePresence.ts:42` |
| POST /clients/{id}/block | clients.py:61 | conversations:manage | ✔ `chats/components/card/BlockClientButton.tsx:48` |
| POST /clients/{id}/unblock | clients.py:90 | conversations:manage | ✔ там же |
| GET /clients/blocked | clients.py:115 | conversations:manage | ✖ |
| GET /audit-log | audit.py:34 | audit:read | ✔ `settings/team/api.ts:18` (`entity` не шлёт) |
| GET/PATCH /settings/distribution | settings.py:50, 58 | settings:manage | ✔ `settings/distribution/DistributionTab.tsx:44,63` |
| GET/PATCH /settings/work-hours | settings.py:115, 123 | settings:manage | ✔ `distribution/WorkHoursBlock.tsx:38,56` |
| POST /support/password-reset | support.py:79 | публично, 202 | ✔ `features/auth/api.ts:20` |
| POST /support/message | support.py:96 | любая авторизованная, 202 | ✔ `settings/profile/api.ts:10` |
| POST /internal/notify | internal.py:231 | заголовок `X-Internal-Token` (`internal.py:195-211`) | ✖ (скрипты `deploy/`) |
| GET /api/health | health.py:36 | публично | ✔ `shared/api/http.ts:213` |
| GET /api/health/deep | health.py:199 | публично | ✖ |
| POST /api/hooks/avito/{account_id} | webhooks.py:42 | `?secret=` + compare_digest | ✖ (Авито) |
| POST /ws/ticket | ws.py:28 | любая авторизованная | ✔ `shared/realtime/WsClient.ts:39` |
| WS /api/v1/ws | ws.py:39 | одноразовый тикет | ✔ `WsClient.ts:44` |
| WS /api/ws (алиас) | main.py:177 | тот же | ✖ |

### 2.12 Боты — `app/api/routes/bots.py` (весь модуль под `bots:manage`, `bots.py:61`)

Все 12 ручек вызываются: `/bots` (244 → `settings/bots/api.ts:19`), `/bots/{id}` (278 → `:23`), POST `/bots` (287 → `:28`), PUT `/bots/{id}` (321 → `:33`), enable/disable (364/373 → `:38`), PUT `/bots/{id}/accounts` (402 → `:43`), DELETE `/bots/{id}` (450 → `BotsPage.tsx:53`), песочница `start`/`message`/`fire-timeout`/`DELETE` (172/197/213/228 → `:52,:56,:61,:65`).

---

## 3. Вызовы фронта без серверной ручки

**Нет ни одного.** Проверены все точки выхода (`http.*`, `request<>`, `requestForm`, `requestBlob`, `fetchHealth`, прямые `fetch`) — каждая имеет соответствующий маршрут. Полный список серверных ручек без вызывающего кода — в разделе «dead_or_hidden».

---

## 4. WebSocket

### 4.1 Что шлёт сервер

Единая точка публикации — `app/ws/events.py:31-53`, конверт `{type, ts, data}` (+ внутренний `meta`, который Hub снимает — `hub.py:143`).

| Тип кадра | Где публикуется | Фильтр доставки | Разбирает фронт |
|---|---|---|---|
| `message:new` | `routes/conversations.py:162,414,449,480`; `routes/inbox.py:174,230,314,380`; `services/messages.py:678`; `services/inbound.py:372`; `bots/handoff.py:293-295`; `bots/engine.py:182,192` | заметки (`direction=='note'`) не уходят ролям без `notes:read` (`hub.py:184-189`) | ✔ `applyWsEvent.ts:567` |
| `message:status` | `services/messages.py:770` | общий | ✔ `applyWsEvent.ts:571` |
| `conversation:updated` | `routes/conversations.py:171,489,637,753`; `routes/inbox.py:198,272,325,400`; `services/messages.py:723`; `services/client_enrich.py:142`; `bots/engine.py:554,565,692`; `bots/handoff.py:296`; `scheduler/jobs/reclaim.py:102,281` | `only_user` для read-маркера (`conversations.py:641`) | ✔ `applyWsEvent.ts:580` |
| `conversation:assigned` | `routes/conversations.py:740` | Hub добавляет `is_for_you` (`hub.py:201-206`) | ✔ `applyWsEvent.ts:584` |
| `inbox:new` | `ws/hub.py:82`; `scheduler/jobs/awaiting.py:269`; `scheduler/jobs/reclaim.py:101`; `bots/handoff.py:318` | Hub добавляет `can_claim` (`hub.py:207-214`) | ✔ `applyWsEvent.ts:538` |
| `inbox:claimed` | `routes/inbox.py:183`; `routes/conversations.py:764`; `services/messages.py:706` | Hub добавляет `is_mine` (`hub.py:215-219`) | ✔ `applyWsEvent.ts:542` |
| `inbox:released` | `routes/inbox.py:389,447`; `routes/conversations.py:779` | `can_claim` | ✔ `applyWsEvent.ts:551` |
| `inbox:declined` | `routes/inbox.py:256,331` | `only_user` (`inbox.py:267,344`) | ✔ `applyWsEvent.ts:560` |
| `notify` | `services/notifications.py:672,675`; `services/avito_accounts.py:442,900,921` | `audience='admin'` → только админам (`hub.py:178-179`), либо `only_user` | ✔ `applyWsEvent.ts:615` → `features/notifications/wsNotify.ts` |
| `account:needs_reauth` | `workers/deliver.py:263`; `services/avito_accounts.py:474` | только `role=='admin'` (`hub.py:190-191`) | ✔ `applyWsEvent.ts:605` |
| `presence:online` | `ws/presence.py:76, 96, 115` | общий | ✖ **игнорируется** (`applyWsEvent.ts:621-623`) |
| `typing` | `ws/hub.py:245` (ретрансляция клиентского кадра) | только сессиям, подписанным на диалог (`hub.py:192-194`) | ✖ **игнорируется** (`applyWsEvent.ts:621-623`) |
| `pong` | `ws/hub.py:234` | адресно | ✔ обрабатывается в `WsClient.ts:62-65`, до диспетчера не доходит |
| `control:revoked` | `services/sessions.py:89` | служебный, наружу не отправляется — Hub закрывает сокеты кодом 4403 (`hub.py:145-159`) | — (фронт реагирует на код закрытия, `WsClient.ts:71-75`) |

### 4.2 Что шлёт клиент

Сервер понимает три кадра: `ping`, `subscribe`, `typing` (`ws/hub.py:223-256`); всё остальное — `{"type":"error","data":{"code":"bad_frame"}}`.
Фронт шлёт **только** `ping` — `frontend/src/shared/realtime/WsClient.ts:90`. Кадры `subscribe` и `typing` не отправляются никогда (единственный `.send(` во всём `frontend/src`).

### 4.3 Коды закрытия

4401 — битый тикет (`ws.py:55`), 4403 — неактивный/отозванный (`ws.py:67,77`), 4408 — нет кадров 60 с (`ws.py:92`). Фронт: 4403 → logout, остальное → reconnect (`WsClient.ts:70-78`).

### 4.4 Догон после обрыва

`applyWsEvent.ts:472-533`: всегда `GET /inbox/count`, затем `invalidate` очереди, при офлайне >30 мин — полный refetch, иначе `GET /conversations?updated_since=…&limit=200` и хвост ленты `GET /conversations/{id}/messages?after=…&limit=50`.

---

## 5. Пагинация, фильтры, сортировки: сервер vs фронт

### 5.1 Пагинация

| Ручка | Тип | Сервер | Фронт |
|---|---|---|---|
| GET /conversations | offset | limit 1..200, деф. 50 (`conversations.py:196`) | limit=50; в догоне 200 |
| GET /conversations/{id}/messages | **cursor** (`before`/`after`) | limit ≤ `MAX_MESSAGES_LIMIT` (`conversations.py:598`) | limit=50 (`chats/api.ts:57`) |
| GET /inbox | offset | limit 1..200, деф. 50 (`inbox.py:108`) | limit=50 (`inbox/api.ts:30,41`) |
| GET /conversations/table | offset | limit ≤100, деф. 50; `MAX_OFFSET=5000` (`services/conversation_table.py:60-64`) | `limit` не шлёт вовсе (`table/api.ts:61-75`) |
| GET /templates | offset | limit ≤200 | limit=200, `offset` не шлёт |
| GET /users | offset | limit ≤ `MAX_LIMIT` | limit=50 (`team/api.ts:23`) |
| GET /audit-log | offset | limit ≤200 | limit=50 (`team/api.ts:7`) |
| GET /notifications | offset | limit ≤200 | limit=50 / 10 (`notifications/api.ts:13,16`) |
| GET /bots | offset | limit ≤200 | не шлёт |
| GET /avito-accounts | offset | limit ≤100 | не шлёт |
| GET /clients/blocked | offset | limit ≤200 | не вызывается |
| GET /stats/managers | **без пагинации** (`stats.py:134`) | — | — |

### 5.2 Сортировки

- `GET /conversations` — сортировка **фиксированная серверная**, параметра нет: непрочитанные вверх, внутри «негатив» первым, затем `last_message_at DESC` (`services/conversations.py:486-489`). Фронт повторяет её локально при WS-патчах: `applyWsEvent.ts:42-55`.
- `GET /inbox` — `offered_at ASC` (сервис), фронт зеркалит в `insertInboxRow`/`queuedBefore` (`inbox/api.ts:158-200`).
- `GET /conversations/table` — `sort` из закрытого списка `last_message_at, updated_at, status, unread_count` (`services/conversation_table.py:69-76`), `direction=asc|desc`. Фронт дублирует список в `SORTABLE` (`table/api.ts:107`).
- `GET /stats/managers` — `sort` (деф. `messages_sent`) + `order`.
- `GET /audit-log` — фиксированная `created_at DESC, id DESC` (`audit.py:75`).
- `GET /templates` — фиксированная `folder NULLS LAST, title, id` (`templates.py:194-198`).

### 5.3 Фильтры, которые сервер поддерживает, а фронт не использует

| Ручка | Параметр | Строка сервера | Почему считаю неиспользуемым |
|---|---|---|---|
| GET /conversations | `unassigned` | `conversations.py:192` | в UI фильтра «Без ответственного» нет: `ChatListPane.tsx:252-260` строит `managerOptions` только из id сотрудников, `:334` кладёт значение в `assigneeId` |
| GET /conversations | `unread_only` | `conversations.py:193` | нет ни одного `set("unread_only")` |
| GET /conversations/table | `q` | `conversations.py:260` | `searchParams` (`table/api.ts:61-75`) его не собирает, в `TableQuery` (`table/api.ts:41-52`) поля нет |
| GET /inbox | `account_id` | `inbox.py:107` | `fetchInboxQueue` шлёт только limit/offset (`inbox/api.ts:41`) |
| GET /templates | `folder`, `q` | `templates.py:168-169` | фильтрация локальная — `TemplatesManager.tsx:172-185`; в `useTemplates.ts:6-8` это прямо названо «запасной путь» |
| GET /users/assignable | `include_inactive` | `users.py:71` | `reference.ts:14` шлёт запрос без параметров |
| GET /audit-log | `entity` | `audit.py:38` | `fetchAuditLog` (`team/api.ts:10-19`) собирает только `user_id, action, date_from, date_to` |
| POST /conversations/{id}/decline | тело `{reason}` | `inbox.py:207` | `inbox/api.ts:106` шлёт POST без тела |
| GET /conversations | `tab=inbox` | `conversations.py:202-227` | фронт ходит в отдельный ресурс `GET /inbox`; строки `tab: "inbox"` в `frontend/src` нет |

### Есть в коде, но не выведено (17)

* **POST /auth/change-password — смена своего пароля с проверкой текущего и обрывом чужих сессий**
  * где: app/api/routes/auth.py:323-383; вызывающего кода в frontend/src нет
  * почему важно: Экран профиля прямо противоречит серверу: frontend/src/features/settings/profile/ProfilePage.tsx:83 показывает текст «Пароль меняет администратор — напишите ему формой ниже». Функция написана, оттестирована формулировкой в docstring и недоступна.
* **PATCH /auth/me — смена своего имени**
  * где: app/api/routes/auth.py:386-419; вызывающего кода нет
  * почему важно: Имя видно клиенту в подписи сообщения. Сервер разрешает исправить его самому, интерфейс — нет: ProfilePage.tsx:65-77 показывает имя только текстом.
* **GET /clients/blocked — список помеченных клиентов с именами тех, кто пометил**
  * где: app/api/routes/clients.py:115-158; вызывающего кода нет
  * почему важно: Пометка ставится из карточки диалога (BlockClientButton.tsx:48), а снять её можно, только случайно наткнувшись на тот же диалог — ровно то, чего хотел избежать docstring ручки (clients.py:122-128). Экрана чёрного списка в роутере нет.
* **Весь сценарий «подключить аккаунт по одноразовой ссылке»: POST /avito/connect-link + GET /avito/connect/{token}**
  * где: app/api/routes/avito_connect.py:492-514 и 516-535; вызывающего кода нет
  * почему важно: Это решение задачи «пароли от аккаунтов Авито у разных людей» (docstring avito_connect.py:497-507). Ссылку некому выпустить: кнопки в интерфейсе нет, публичный маршрут /connect/{token} висит без источника.
* **GET /avito/connect-url — выдача OAuth-ссылки для подключения нового канала**
  * где: app/api/routes/avito_connect.py:348-354; вызывающего кода нет
  * почему важно: Docstring экрана аккаунтов утверждает обратное: frontend/src/features/settings/accounts/AccountsPage.tsx:554 пишет «OAuth-подключение через GET /avito/connect-url». Фактически подключение идёт только по ключам (POST /avito-accounts/connect), а OAuth остался лишь у переподключения (POST /avito-accounts/{id}/reconnect).
* **PUT /avito/app и DELETE /avito/app — задать/сбросить ключи приложения Авито**
  * где: app/api/routes/avito_connect.py:240-263 и 298-318; вызывающего кода нет
  * почему важно: PUT осознанно выведен из интерфейса — комментарий frontend/src/features/settings/accounts/ConnectChannelWizard.tsx:84-89 объясняет, что вместо него сделан POST /avito/app/mode. То есть ручка мёртвая по решению, а не по недосмотру, но живёт в API и пишет audit_log. DELETE («путь назад из „я тут напутал“») не выведен никуда.
* **POST /avito/app/check — проверка связи с Авито до подключения канала**
  * где: app/api/routes/avito_connect.py:321-345; вызывающего кода нет
  * почему важно: Ручка написана ровно ради кнопки в мастере подключения (docstring: «ошибка в ключах должна всплыть здесь, а не посреди OAuth»). Кнопки в ConnectChannelWizard.tsx нет.
* **GET /avito-accounts/{id}/subscriptions — кто сейчас подписан на события аккаунта в Авито**
  * где: app/api/routes/avito_accounts.py:163-210; вызывающего кода нет
  * почему важно: Единственный способ ответить на вопрос переезда «что будет с подпиской Jivo» (docstring avito_accounts.py:169-184, задача #39). Доступна только curl'ом.
* **GET /users/{user_id}/accounts — каналы другого сотрудника для карточки в «Команде»**
  * где: app/api/routes/avito_accounts.py:142-160; вызывающего кода нет
  * почему важно: Docstring прямо называет потребителя — «карточка человека в „Команде“ (11 §4.2)». В TeamMembersTab.tsx такого запроса нет. Аналогичная ручка про себя (/me/channels) используется.
* **Дубли путей POST /users/invite и POST /users/{id}/resend-invite**
  * где: app/api/routes/users.py:160 и users.py:180 (алиасы к users.py:159 и users.py:179)
  * почему важно: Заведены «чтобы не разойтись с документом» (users.py:18-21). Фронт использует только канонические POST /users и POST /users/{id}/invite — алиасы удваивают поверхность API и OpenAPI без пользователя.
* **WS-алиас /api/ws**
  * где: app/main.py:175-177 («канонический upgrade-путь по 01 §11»)
  * почему важно: Фронт жёстко зашит на /api/v1/ws (frontend/src/shared/realtime/WsClient.ts:42-44, там же комментарий про расхождение с документом). Канонический путь есть в коде и в nginx-контракте, но им никто не пользуется — расхождение спецификации и реализации законсервировано в комментариях с обеих сторон.
* **WS-кадры presence:online и typing доходят до фронта и молча выбрасываются**
  * где: сервер: app/ws/presence.py:76,96,115 и app/ws/hub.py:245. Фронт: типы объявлены в frontend/src/shared/realtime/wsEvents.ts:79-88, разбора нет — default-ветка frontend/src/shared/realtime/applyWsEvent.ts:621-623
  * почему важно: Присутствие («в сети/отошёл/не в сети») сервер рассылает всем при каждом подключении, отключении и смене статуса, но список сотрудников и «Передать коллеге» его в реальном времени не видят — is_online приходит только пачкой в HTTP-ответах. Индикатор «печатает» не работает вовсе.
* **Клиентские WS-кадры subscribe и typing никогда не отправляются**
  * где: сервер обрабатывает их в app/ws/hub.py:236-254; единственный send во фронте — frontend/src/shared/realtime/WsClient.ts:90, и это только ping
  * почему важно: Следствие цепочкой: Session.conversation_id (app/ws/hub.py:101) всегда None, поэтому фильтр доставки typing (app/ws/hub.py:192-194) не совпадёт ни с одной сессией. Даже если бы фронт начал слать typing, доставить его было бы некому — механизм подписки на диалог не задействован ни с одной стороны.
* **Поле for_user_id в кадрах conversation:updated**
  * где: app/api/routes/conversations.py:497 (передача принята/отклонена) и app/scheduler/jobs/reclaim.py:275 (передача протухла); фронт читает только conversation_id и patch — applyWsEvent.ts:580-582, applyConversationPatch:168
  * почему важно: Поле заведено, чтобы «хаб подставил is_for_you, и только у него зазвенит» (комментарий conversations.py:495-496), но Hub персонализирует по нему только conversation:assigned (hub.py:201-206). Для conversation:updated поле не читает ни сервер, ни клиент.
* **Поля кадров, которые фронт не читает: error_code в message:status, declined_count и undone в inbox:declined**
  * где: app/services/messages.py:768-769; app/api/routes/inbox.py:264 и inbox.py:342. Фронт: wsEvents.ts:44-62 (error_code не объявлен), applyWsEvent.ts:350-368 (declined_count/undone не разбираются)
  * почему важно: undone отличает «забрал отказ обратно» от «отказался» — без него обе операции обрабатываются одним кодом. Расхождение объявленного контракта и фактического разбора.
* **GET /api/health/deep**
  * где: app/api/routes/health.py:199-
  * почему важно: Не вызывается фронтом (это ожидаемо — ручка для мониторинга), фиксирую для полноты: в отличие от GET /api/health (frontend/src/shared/api/http.ts:213), у неё нет потребителя внутри продукта.
* **GET /media/{relpath} существует, но в проде до Python не доходит**
  * где: app/api/routes/media.py:87-103, объяснение в шапке модуля media.py:8-11
  * почему важно: Проверка подписи продублирована в двух местах (Python и nginx secure_link). Прямых вызовов из frontend/src нет — ссылки подписываются в message_out (app/services/conversations.py:155) и открываются браузером.

### Расхождения с брифом и чего не нашёл

РАСХОЖДЕНИЯ С БРИФОМ

1) Разделы. Бриф называет «/chats, /dialogs, /stats, /settings/{accounts,quick-replies,bots,distribution,team,profile}».
   Совпадают: /chats (frontend/src/app/router.tsx:70 и deep-link /chats/:id — :113), /dialogs (:94, под правом stats:all), /stats (:132, stats:all), /settings/accounts (:144), /settings/bots (:158-164, есть ещё /settings/bots/:id), /settings/distribution (:175), /settings/team (:189), /settings/profile (:141).
   НЕ СОВПАДАЕТ: раздела «/settings/quick-replies» в коде нет. Фактический путь — /settings/templates (router.tsx:148), пункт меню называется «Быстрые ответы» (frontend/src/features/settings/SettingsLayout.tsx:20-24), право доступа — templates:shared. Личные быстрые ответы живут отдельно — вкладкой внутри профиля (ProfilePage.tsx:112-128).
   ЕСТЬ СВЕРХ БРИФА: /notifications — центр уведомлений (router.tsx:120), /updates — «Что нового» (:102), /ui-kit — служебная страница дизайна, доступна по прямой ссылке любой роли без guard'а (:108), /settings без хвоста → редирект на профиль (:140), а также /login (:61) и /invite/:token (:62). Очередь «Входящие» отдельным разделом не оформлена: это отдельный API-ресурс (/inbox) внутри экрана /chats.

2) Роли. Бриф называет две — «Администратор, Менеджер». В коде их ЧЕТЫРЕ, и это не формальность:
   app/core/rbac.py:7 — ("admin", "head", "manager", "observer");
   frontend/src/shared/auth/usePermissions.ts:4 — тот же список;
   человеческие названия и описания — frontend/src/features/settings/profile/ProfilePage.tsx:14-26: admin «Администратор», head «Руководитель» (видит все диалоги, передаёт и пишет заметки, отвечать клиенту не может), manager «Менеджер», observer «Наблюдатель» (только чтение диалогов).
   Роль head — не декоративная: под неё сделан отдельный код ответа 403 read_only_role (app/api/deps.py:77-79), под который фронт рисует плашку «Режим просмотра»; ей закрыты messages:send, bots:manage, accounts:manage, users:manage, settings:manage, но открыты stats:all и audit:read. Роль observer имеет ровно одно право — conversations:read (rbac.py:59), из-за чего ей недоступны шаблоны, статистика и весь центр уведомлений.
   Разделение «кто принимает диалоги» построено на праве messages:send (app/ws/hub.py:58-66), то есть на admin+manager, — а не на именах ролей. Значит любое добавление роли-оператора не требует правок ни в очереди, ни в WS.

3) Дополнительно к брифу: у пользователей есть ещё два измерения, к ролям не сводимых, — флаг handles_conversations и department (PATCH /users/{id}, app/api/routes/users.py:271-272; миграции 0011_user_department, 0008_account_operators). То есть «оператор» и «права» разведены: человек с ролью manager может быть выведен из работы с диалогами, оставаясь менеджером.

ЧТО НЕ ПРОВЕРЯЛОСЬ / НЕ НАЙДЕНО

- Вызовов фронта без серверной ручки НЕ НАЙДЕНО ни одного: сверены все точки выхода в frontend/src (http.get/post/patch/put/del, request<>, requestForm, requestBlob, fetchHealth, прямые fetch), исключая frontend/src/test.
- Полные схемы ответов я приводил по коду сериализаторов (app/services/conversations.py:165-262, :1219-1229, :749-756; app/services/inbox.py:356-388) и по pydantic-моделям в app/schemas/. Отдельные поля ответов ботов, статистики и уведомлений я не расписывал по элементам — они описаны схемами в app/schemas/bots.py, app/schemas/notifications.py и app/services/stats.py, и в этот срез (карта эндпоинтов) их поэлементный разбор не входил.
- Не проверял, совпадают ли фактические значения metric/group у /stats/timeseries и sort у /stats/managers со списком допустимых на сервере: серверный валидатор живёт в app/services/stats.py, до которого этот срез не доходил. Фронтовые типы (TimeseriesMetric, ManagersSort в frontend/src/shared/api/types.ts) существуют, но сверку значений не делал.
- Тестовые моки (frontend/src/test/*) из инвентаря исключены сознательно: они обращаются к путям вроде /api/v1/bots/{id}/accounts, но это не вызывающий код продукта.

## Срез: Данные и Redis

# СРЕЗ «РОЛИ И ПРАВА» — LeadChat, инвентаризация (шаг 0)

Все пути от `.`.

---

## 1. ВСЕ РОЛИ В СИСТЕМЕ — ИХ ЧЕТЫРЕ, А НЕ ДВЕ

Единственное объявление ролей: `app/core/rbac.py:7`
```
ROLES: tuple[str, ...] = ("admin", "head", "manager", "observer")
```

Подтверждения в четырёх независимых местах:
- `app/models/user.py:16` — `CheckConstraint("role IN ('admin','head','manager','observer')", name="users_role")`
- `app/db/migrations/versions/0001_init.py:48` — тот же CHECK в миграции
- `app/schemas/users.py:22` — `Role = Literal["admin", "head", "manager", "observer"]`
- `frontend/src/shared/auth/usePermissions.ts:4` — `export type Role = "admin" | "head" | "manager" | "observer"`
- `app/cli.py:210`, `app/cli.py:174` — CLI `set-role`/`invite` принимает только эти четыре

Человеческие подписи ролей (то, что видит пользователь) — `frontend/src/features/settings/team/roles.ts:5-10`:
- admin → «Администратор»
- head → «Руководитель»
- manager → «Менеджер»
- observer → «Наблюдатель»

Те же подписи продублированы ещё в трёх файлах: `frontend/src/app/AppLayout.tsx:33-38`, `frontend/src/features/settings/profile/ProfilePage.tsx:14-19`, `frontend/src/features/chats/components/card/PeoplePicker.tsx:7`. Плюс пятая копия на сервере — `app/services/audit.py:157-162` (для журнала: «администратор», «руководитель», «менеджер», «наблюдатель»).

**ПЯТАЯ ОСЬ, КОТОРОЙ НЕТ В БРИФЕ.** Роль — не единственный признак доступа. `app/models/user.py:55-57` заводит `handles_conversations` (по умолчанию `true`): «ведёт ли человек диалоги» — ОТДЕЛЬНО от роли. Флаг убирает человека из очереди, автораздачи и списка «кому передать» (`app/services/conversations.py:96-135` `operator_pool_conditions`), но НЕ отнимает право отвечать. Плюс `app/models/user.py:63` `department` — свободная строка-отдел. В интерфейсе обе колонки видны в таблице сотрудников: `frontend/src/features/settings/team/TeamMembersTab.tsx:307-312` (заголовки «Отдел», «Ведёт диалоги»), тумблер — строка 366.

Каталог прав — 15 штук, `app/core/rbac.py:10-30`; порядок каталога стабилен, он же едет в `/auth/me` (`app/core/rbac.py:63-66` `permissions_for`, `app/api/routes/auth.py:248-257`).

---

## 2. МАТРИЦА: КАКОЕ ПРАВО ЧТО ОТКРЫВАЕТ

Источник — `app/core/rbac.py:32-60`; исполняемая копия того же закона —
`tests/unit/test_rbac.py:414-432` (`PERMISSION_MATRIX`), сторож совпадения — `tests/unit/test_rbac.py:445-452`.

Обозначения: A=admin, H=head, M=manager, O=observer.

| Право | A | H | M | O | Что открывает на сервере | Что открывает в интерфейсе |
|---|:-:|:-:|:-:|:-:|---|---|
| `conversations:read` | ✅ | ✅ | ✅ | ✅ | `GET /conversations`, `/{id}`, `/{id}/messages`, `/{id}/client-history`, `POST /{id}/read` (`app/api/routes/conversations.py:52`); очередь `GET /inbox`, `/inbox/count` (`app/api/routes/inbox.py:79`); закрепить/открепить `POST|DELETE /conversations/{id}/pin` | Раздел «Чаты» доступен всем без guard'а — `frontend/src/app/router.tsx:70` |
| `messages:send` | ✅ | ❌ | ✅ | ❌ | `POST /conversations/{id}/messages` (`app/api/routes/messages.py:34`), `POST /media` (`app/api/routes/media.py:28`), `POST /messages/{id}/retry`, `claim`/`decline`/`decline/undo`/`release` очереди (`app/api/routes/inbox.py:84`, право берётся из `app/ws/hub.py:61` `CLAIM_PERMISSION`), `transfer/accept`, `transfer/decline` (`app/api/routes/conversations.py:506,536`), `participants` POST/DELETE (`conversations.py:358,427`), `GET /inbox/stale`, `POST /inbox/close-stale` | Композер, вкладка «Входящие», кнопки «Принять/Отклонить», «Позвать коллегу», «Вернуть в очередь», «Пометить нежелательным», «Разгрузить…», блок «Мои каналы» |
| `conversations:manage` | ✅ | ✅ | ✅ | ❌ | `PATCH /conversations/{id}/status`, `POST /conversations/{id}/assign` (`conversations.py:56`), `GET /users/assignable` (`app/api/routes/users.py:73`), `POST /clients/{id}/block|unblock`, `GET /clients/blocked` (`app/api/routes/clients.py:31`), ВЕСЬ центр уведомлений — `app/services/notifications.py:87` `SECTION_PERMISSION = "conversations:manage"` | Кнопка «Закрыть», секция «Диалог» в карточке, «Передать», колокольчик в шапке |
| `notes:read` | ✅ | ✅ | ✅ | ❌ | Решает не доступ, а ОБЪЁМ выдачи: `app/api/routes/conversations.py:612` `include_notes=has_permission(user,"notes:read")`; тот же фильтр в WS — `app/ws/hub.py:183-189` | Секция «Заметки» в карточке клиента |
| `notes:write` | ✅ | ✅ | ✅ | ❌ | `POST /conversations/{id}/notes` (`app/api/routes/messages.py:35`) | Кнопка «+ добавить» в заметках; ветка «композер только заметок» у руководителя |
| `templates:own` | ✅ | ✅ | ✅ | ❌ | Все ручки `/templates` и `/templates/folders` (`app/api/routes/templates.py:34` `own_perm`) | Вкладка «Быстрые ответы» в профиле |
| `templates:shared` | ✅ | ✅ | ❌ | ❌ | Создание/правка ОБЩИХ шаблонов — `app/api/routes/templates.py:113` `_can_share`, отказ на строках 128-133 и в `create_template` | Пункт «Быстрые ответы» в меню настроек, вкладка «Общие» |
| `stats:own` | ✅ | ✅ | ✅ | ❌ | `GET /stats/my/today` (`app/api/routes/stats.py:34,144`) | Виджет «моя статистика за сегодня» |
| `stats:all` | ✅ | ✅ | ❌ | ❌ | `GET /stats/summary|timeseries|heatmap|managers`, `POST /stats/export` (`app/api/routes/stats.py:33,43`); таблица диалогов `GET /conversations/table` и `/table/export` (`app/api/routes/conversations.py:55`) | Разделы `/stats` и `/dialogs` в рельсе; фильтр «Менеджер ▾» в списке чатов |
| `bots:manage` | ✅ | ❌ | ❌ | ❌ | Все 12 ручек `/bots/*` (`app/api/routes/bots.py:61`) | Пункт «Боты», экраны `/settings/bots`, `/settings/bots/:id` |
| `accounts:read` | ✅ | ✅ | ❌ | ❌ | `GET /avito-accounts` (`app/api/routes/avito_accounts.py:64`), `GET /avito-accounts/{id}/operators`, ЧУЖИЕ каналы `GET /users/{id}/accounts` (`avito_accounts.py:155`) | Пункт «Аккаунты Авито», справочник каналов для фильтра |
| `accounts:manage` | ✅ | ❌ | ❌ | ❌ | Весь OAuth-контур: `connect-url`, `connect-link`, `connect`, `reconnect`, `disable`, `enable`, `register-webhook`, `refresh-token`, `PATCH/DELETE /avito-accounts/{id}`, ключи приложения `PUT|DELETE /avito/app`, `/avito/app/check`, `/avito/app/mode`, `PUT /avito-accounts/{id}/operators` (`avito_accounts.py:65`, `avito_connect.py` — 16 ручек) | Все кнопки на экране аккаунтов, мастер подключения, «Назначить операторов» |
| `users:manage` | ✅ | ❌ | ❌ | ❌ | 13 ручек `/users/*`: create, invite, resend-invite, reset-password, set-password, PATCH, DELETE, deactivate, activate (`app/api/routes/users.py:145-296`) | Вкладка «Сотрудники» внутри `/settings/team` |
| `audit:read` | ✅ | ✅ | ❌ | ❌ | `GET /audit-log` (`app/api/routes/audit.py:29`) | Вкладка «Журнал аудита» внутри `/settings/team` |
| `settings:manage` | ✅ | ❌ | ❌ | ❌ | `GET|PATCH /settings/distribution`, `GET|PATCH /settings/work-hours` (`app/api/routes/settings.py:24,50,58,115,123`) | Пункт «Распределение», экран `/settings/distribution` |

**Ролевые наборы целиком** (`app/core/rbac.py:32-60`):
- **admin** — все 15 прав (`frozenset(PERMISSIONS)`).
- **head** — 10: `conversations:read`, `conversations:manage`, `notes:read`, `notes:write`, `templates:own`, `templates:shared`, `stats:own`, `stats:all`, `accounts:read`, `audit:read`. Нет: `messages:send`, `bots:manage`, `accounts:manage`, `users:manage`, `settings:manage`.
- **manager** — 7: `conversations:read`, `messages:send`, `conversations:manage`, `notes:read`, `notes:write`, `templates:own`, `stats:own`.
- **observer** — 1: `conversations:read`.

**Проверка прав** — `app/api/deps.py:69-83`. Роль всегда читается из строки БД, не из JWT (`app/api/deps.py:52`, комментарий 73-74). Особый случай: `app/api/deps.py:77-79` — при отказе в `messages:send` руководителю отдаётся код `read_only_role` (а не `forbidden`), под него фронт рисует плашку «Режим просмотра». Тест на это — `tests/unit/test_rbac.py:472-495`.

**Матрица «ручка × роль»** — `tests/unit/test_rbac.py:21-92` (`MATRIX`), плюс сторож полноты `tests/unit/test_rbac.py:386-402`: любая ручка, не попавшая в `MATRIX`/`EXEMPT`/`COVERED_ELSEWHERE`, роняет CI. Публичные ручки — `tests/unit/test_rbac.py:96-120`.

---

## 3. ЧТО КАЖДАЯ РОЛЬ ВИДИТ НА КАЖДОМ ЭКРАНЕ

Права приходят с сервера в `/auth/me`, фронт роль→права НЕ хардкодит: `frontend/src/shared/auth/usePermissions.ts:26-42` (`can`, `canAny`). Guard'ы маршрутов — `frontend/src/app/router.tsx:39-42` (`RequirePermission`: не 403-страница, а редирект на `/chats`).

### 3.1. Навигационная рельса (`frontend/src/app/AppLayout.tsx:243-311`)

| Пункт | Условие | A | H | M | O |
|---|---|:-:|:-:|:-:|:-:|
| «Чаты» | без условия (строка 245) | ✅ | ✅ | ✅ | ✅ |
| «Разбор диалогов» → `/dialogs` | `can("stats:all")` (строка 275) | ✅ | ✅ | ❌ | ❌ |
| «Статистика» → `/stats` | `can("stats:all")` (строка 288) | ✅ | ✅ | ❌ | ❌ |
| «Настройки» | без условия (строка 301, комментарий 300: «внутри у роли своё наполнение») | ✅ | ✅ | ✅ | ✅ |
| Колокольчик уведомлений | `can("conversations:manage")` — `frontend/src/features/notifications/NotificationBell.tsx:20`, право в `frontend/src/features/notifications/catalog.ts:23` | ✅ | ✅ | ✅ | ❌ |
| «Моя статистика за сегодня» в меню аватара (мобильная раскладка) | `isMobile && role === "manager" && can("stats:own")` — `AppLayout.tsx:125` | ❌ | ❌ | ✅ | ❌ |
| «Профиль», «Что нового», «Выйти», статус «на месте/отошёл» | без условия (`AppLayout.tsx:162-175`) | ✅ | ✅ | ✅ | ✅ |

### 3.2. Экран «Чаты» — левая колонка (`frontend/src/features/chats/components/list/ChatListPane.tsx`)

| Элемент | Условие | A | H | M | O |
|---|---|:-:|:-:|:-:|:-:|
| Вкладка «Входящие» + счётчик очереди | `canClaim = can("messages:send")` (строка 167, показ — 464) | ✅ | ❌ | ✅ | ❌ |
| Кнопка «Разгрузить…» | `inboxOpen && canClaim` (строка 511) | ✅ | ❌ | ✅ | ❌ |
| Фильтр «Менеджер ▾» | `canFilterByManager = can("stats:all")` (строка 155) | ✅ | ✅ | ❌ | ❌ |
| Фильтр «Канал» | справочник по `can("accounts:read")` (строка 242), но список добирается из загруженных строк — оператору фильтр работает без права |  ✅ | ✅ | ✅(из строк) | ✅(из строк) |
| «→ Передать» из строки списка | `canTransfer = can("conversations:manage") && !inboxOpen` (строка 408) | ✅ | ✅ | ✅ | ❌ |
| Виджет «моя статистика» в подвале | `role === "manager" && can("stats:own")` (строка 658) | ❌ | ❌ | ✅ | ❌ |
| Вкладка по умолчанию | `frontend/src/shared/stores/chatUiStore.ts:72`: `role === "manager" ? "mine" : "all"` | Все | Все | Мои | Все |

### 3.3. Экран «Чаты» — лента и её низ

Низ панели выбирается одной функцией — `frontend/src/features/chats/components/composer/ThreadFooter.tsx:41-76`:
- `can("messages:send")` (A, M) → полный композер, либо «Принять/Отклонить» если диалог в очереди, либо плашка «диалог забрал коллега»;
- иначе `can("notes:write")` (H) → плашка **«👁 Режим просмотра — назначьте менеджера или передайте диалог»** (строка 69) + композер ТОЛЬКО заметок (`noteOnly`);
- иначе (O) → не возвращается ничего, кроме полосы передачи: лента идёт до низа (строка 76).

Композер (`frontend/src/features/chats/components/composer/Composer.tsx`): `canSendMessages = can("messages:send")` (строка 99); `showTemplates = canSendMessages && !isNote` (строка 100) — быстрые ответы и «/» есть только у A/M. Ссылка «Переподключить» в баннере отключённого аккаунта — `can("accounts:manage")` (строка 299).

Ряд действий в шапке (`frontend/src/features/chats/components/thread/ThreadActions.tsx`):

| Кнопка | Условие | A | H | M | O |
|---|---|:-:|:-:|:-:|:-:|
| Закрепить/открепить | `isMine` (assignee или участник, строка 48-50, показ 60) | ✅ если свой | ❌ (не бывает assignee) | ✅ если свой | ❌ |
| Вернуть в очередь | `canSend && iAmHolder && status!=="closed"` (строка 84) | ✅ | ❌ | ✅ | ❌ |
| Позвать коллегу | `canSend` (строка 98) | ✅ | ❌ | ✅ | ❌ |
| Передать диалог | `canManage` (строка 111) | ✅ | ✅ | ✅ | ❌ |
| Пометить нежелательным | `canSend` (строка 124) | ✅ | ❌ | ✅ | ❌ |
| «Закрыть» (отдельной заметной кнопкой) | `canManage && status!=="closed"` — `ChatThreadPane.tsx:392` | ✅ | ✅ | ✅ | ❌ |
| Баннер «диалог ведёт бот» | `conv.bot_active && canSend` — `ChatThreadPane.tsx:422` | ✅ | ❌ | ✅ | ❌ |
| «Повторить»/«Отменить» на недоставленном | `canSend ? onRetry : undefined` — `ChatThreadPane.tsx:467-468` | ✅ | ❌ | ✅ | ❌ |
| Ctrl+D «закрыть», Ctrl+T «передать» | оба проверяют `can("conversations:manage")` — `ChatThreadPane.tsx:252,273` | ✅ | ✅ | ✅ | ❌ |

### 3.4. Экран «Чаты» — карточка клиента (`frontend/src/features/chats/components/card/ClientCardPane.tsx`)

| Секция/элемент | Условие | A | H | M | O |
|---|---|:-:|:-:|:-:|:-:|
| Секция «Диалог» целиком | `canManage = can("conversations:manage")` (строка 201, показ 365) | ✅ | ✅ | ✅ | ❌ |
| Свободный селект «Ответственный» (вкл. «Без ответственного») | `canReassignFreely = canManage && (role==="admin"||role==="head")` (строка 206, показ 371) | ✅ | ✅ | ❌ | ❌ |
| Тот же ответственный как ТЕКСТ | иначе (строка 379-386) | — | — | ✅ | ❌ |
| Кнопка «Передать» | внутри `canManage`, без доп. условия (строка ~437) | ✅ | ✅ | ✅ | ❌ |
| Кнопка «Позвать» и крестики у позванных | `canInvite = can("messages:send")` (строка 204, показ 417 и 448) | ✅ | ❌ | ✅ | ❌ |
| Секция «Заметки» | `can("notes:read")` (строка 494) | ✅ | ✅ | ✅ | ❌ |
| Кнопка «+ добавить» в заметках | `can("notes:write")` (строка 154) | ✅ | ✅ | ✅ | ❌ |
| «История клиента» | без условия | ✅ | ✅ | ✅ | ✅ |

### 3.5. Меню настроек (`frontend/src/features/settings/SettingsLayout.tsx:14-43`)

| Пункт | Условие | A | H | M | O |
|---|---|:-:|:-:|:-:|:-:|
| «Аккаунты Авито» | `can("accounts:read")` (строка 15) | ✅ | ✅ | ❌ | ❌ |
| «Быстрые ответы» | `can("templates:shared")` (строка 20) | ✅ | ✅ | ❌ | ❌ |
| «Боты» | `can("bots:manage")` (строка 25) | ✅ | ❌ | ❌ | ❌ |
| «Распределение» | `can("settings:manage")` (строка 30) | ✅ | ❌ | ❌ | ❌ |
| «Команда» | `canAny("users:manage","audit:read")` (строка 35) | ✅ | ✅ | ❌ | ❌ |
| «Профиль» | без условия (строка 40) | ✅ | ✅ | ✅ | ✅ |

Итог: у менеджера и наблюдателя в настройках ровно один пункт — «Профиль» (зафиксировано тестом `frontend/src/test/SettingsNav.test.tsx:41-44`).

Guard'ы маршрутов повторяют то же самое — `frontend/src/app/router.tsx`: `/dialogs` → `stats:all` (74), `/stats` → `stats:all` (127), `/settings/accounts` → `accounts:read|accounts:manage` (143), `/settings/templates` → `templates:shared` (147), `/settings/bots*` → `bots:manage` (155), `/settings/distribution` → `settings:manage` (172), `/settings/team` → `users:manage|audit:read` (186). Без guard'а: `/chats`, `/chats/:id`, `/settings/profile`, `/notifications`, `/updates`, `/ui-kit`.

### 3.6. `/settings/team` (`frontend/src/features/settings/team/TeamPage.tsx:24-51`)

Вкладки строятся по правам: «Сотрудники» → `users:manage`, «Журнал аудита» → `audit:read`. Админ видит две, руководитель — только журнал, у остальных страница вообще недостижима (guard). Если прав ноль — «Для вашей роли здесь пока нет разделов» (строка 51).

### 3.7. `/settings/accounts` (`frontend/src/features/settings/accounts/AccountsPage.tsx`)

Экран открывается по `accounts:read` (A + H), но ВСЕ действия — по `accounts:manage` (только A): строки 145 (кнопка «Назначить операторов»), 464, 598-603 (`enabled: can("accounts:manage")` у запроса режима), 624, 669, 674. Руководитель видит состав каналов и статусы, но ни одной кнопки.

### 3.8. `/settings/profile` (`frontend/src/features/settings/profile/ProfilePage.tsx`)

Вкладки собираются по факту содержимого:
- «Учётная запись» — всем (роль подписана словами: `ProfilePage.tsx:80` «Роль: **X** — <пояснение>», словарь пояснений 21-26);
- «Интерфейс» — всем;
- «Быстрые ответы» — `can("templates:own")` (строка 112) → A/H/M, у наблюдателя вкладки нет вовсе.

Блок «Мои каналы» — `frontend/src/features/settings/profile/MyChannelsBlock.tsx:24,33`: `isOperator = can("messages:send")`, при отсутствии права компонент возвращает `null`. То есть A/M видят, H/O — нет.

Форма «Написать администратору» (`ContactAdminForm`) — без проверки роли, доступна всем четырём; сервер согласен: `app/api/routes/support.py:11` и `tests/unit/test_rbac.py:289-293`.

### 3.9. Центр уведомлений

Сервер: `app/services/notifications.py:87` — раздел целиком под `conversations:manage` (A/H/M, не O). `app/services/notifications.py:91-97` — какие рассылки видит роль: `AUDIENCE_PERMISSION = {"admin": "users:manage", "head": "audit:read"}`, то есть системные уведомления («аккаунт требует переподключения», «планировщик не отвечает», «мало места на диске») видит только админ, рабочие («клиент ждёт», эскалация очереди) — админ и руководитель, адресные — только получатель. Фильтр WS повторяет то же: `app/ws/hub.py:178` (`audience == "admin"` → только admin) и `app/ws/hub.py:190` (`account:needs_reauth` → только admin).

Фронт: `frontend/src/features/notifications/useNotifications.ts:26-29` и `catalog.ts:23`. Страница `/notifications` guard'а не имеет намеренно — наблюдатель получает честный текст «Вашей роли уведомления не приходят» (`frontend/src/features/notifications/NotificationsPage.tsx:182-187`), а не редирект.

### 3.10. WebSocket

`app/ws/hub.py:170-195` — фильтр видимости кадров по правам, не по строке роли: заметки уезжают только при `notes:read` (183-189). `app/ws/hub.py:196-221` — персонализация: в кадрах очереди подставляется `can_claim` (строка 214), вычисляемый из `CLAIM_PERMISSION = "messages:send"` (строки 61-66) — руководителю и наблюдателю очередь не звенит и кнопки «Принять» не рисует.

Смена роли на живой сессии: `app/services/users.py:405-419` — сокет закрывается кодом 4401, refresh-цепочка рвётся, но повторный вход не блокируется; право на HTTP читается из БД на каждом запросе. На фронте это ловит `frontend/src/shared/auth/useRoleUiSync.ts:24-38` — при смене пары (пользователь, роль) сбрасывается UI-состояние чатов и весь кэш TanStack Query.

---

## 4. РАСХОЖДЕНИЯ «СЕРВЕР vs ИНТЕРФЕЙС»

### 4.1. Сервер разрешает — интерфейс прячет

1. **`GET /me/channels` открыт всем четырём ролям**, интерфейс показывает блок только операторам.
   Сервер: `tests/unit/test_rbac.py:80` — `{A: ALLOW, H: ALLOW, M: ALLOW, O: ALLOW}`.
   Фронт: `frontend/src/features/settings/profile/MyChannelsBlock.tsx:24,33` — `if (!isOperator) return null` при `can("messages:send")`. Руководитель и наблюдатель свой список каналов увидеть не могут, хотя ручка им отвечает 200.

2. **`GET /stats/my/today` открыт A/H/M** (`app/api/routes/stats.py:34,144`; `tests/unit/test_rbac.py:59`), а виджет «моя статистика за сегодня» показывается ТОЛЬКО менеджеру — и по строке роли, а не по праву: `frontend/src/features/chats/components/list/ChatListPane.tsx:658` и `frontend/src/app/AppLayout.tsx:125` (`role === "manager" && can("stats:own")`). У админа и руководителя `stats:own` есть, виджета нет.

3. **`POST /conversations/{id}/assign` разрешён любому с `conversations:manage`**, включая менеджера (`app/api/routes/conversations.py:56`; `resolve_assignee` — `app/services/conversations.py:913-939` — проверяет только получателя, не автора). Интерфейс даёт менеджеру только модалку «Передать», а свободный селект «Ответственный» (в т.ч. пункт «Без ответственного») скрыт от него ЖЁСТКОЙ ПРОВЕРКОЙ РОЛИ: `frontend/src/features/chats/components/card/ClientCardPane.tsx:206` — `canManage && (role === "admin" || role === "head")`. Снять ответственного менеджер через интерфейс не может, через API — может.

4. **Общие шаблоны: `templates:own` даёт менеджеру `GET /templates`** (`tests/unit/test_rbac.py:49` — M: ALLOW), но пункт меню «Быстрые ответы» и вкладка «Общие» закрыты правом `templates:shared` (`SettingsLayout.tsx:20`, `TemplatesManager.tsx:149`). Читать общие шаблоны менеджер может (и читает — через пикер в композере), а увидеть их списком в настройках — нет. Это осознанное разделение «читать/править», но в интерфейсе оно выглядит как отсутствие раздела.

5. **`templates:own` есть у руководителя**, и он видит вкладку «Быстрые ответы» в профиле (`ProfilePage.tsx:112`), но вставить шаблон в ЗАМЕТКУ не может: пикер завязан на `messages:send` — `frontend/src/features/chats/components/composer/Composer.tsx:100` (`showTemplates = canSendMessages && !isNote`). Право есть, кнопки нет.

6. **`GET /clients/blocked` открыт A/H/M** (`app/api/routes/clients.py:115,31`; `tests/unit/test_rbac.py:91`) — во фронте вызывающего кода НЕТ (см. §5).

7. **`GET /users/{user_id}/accounts`** — «чужие каналы» под `accounts:read`, «свои» — любой роли (`app/api/routes/avito_accounts.py:155`). Во фронте вызывающего кода НЕТ.

### 4.2. Интерфейс показывает — сервер откажет

1. **Смена роли самому себе.** В таблице сотрудников селект роли для собственной строки НЕ отключён: `frontend/src/features/settings/team/TeamMembersTab.tsx:332-341` — `disabled={!u.is_active}`, и всё. Флаг `isMe` (строка 322) применён только к «Отключить» (425) и «Удалить» (446). Админ выбирает себе новую роль, подтверждает в модалке — и сервер отвечает `409 self_role_change` (`app/services/users.py:357-372`). Ошибка показывается (Alert в модалке, `TeamMembersTab.tsx:514-518`), но действие предложено заведомо невыполнимое.

2. **Понижение/удаление последнего администратора.** Тот же экран не знает про `_assert_admin_remains` (`app/services/users.py:139-147`): сервер отбивает 409, интерфейс предлагает.

3. **Тумблер «Ведёт диалоги».** Интерфейс отключает его для ролей без права отвечать: `frontend/src/features/settings/team/TeamMembersTab.tsx:370` — `disabled={!u.is_active || !canAnswer(u.role)}`, где `canAnswer` (`frontend/src/features/settings/team/roles.ts:31-33`) — ЛОКАЛЬНАЯ КОПИЯ серверного `ASSIGNABLE_ROLES`. Сервер же (`app/services/users.py:380-390`) `handles_conversations` ни с какой ролью не сверяет: через API можно поставить `true` наблюдателю. Практических последствий нет (пул фильтрует по роли — `app/services/conversations.py:126`), но проверка живёт только на фронте.

4. **Шпаргалка горячих клавиш обещает всем всё.** `frontend/src/features/hotkeys/catalog.ts:22-37` — плоский список без ролевой фильтрации, `HotkeysModal.tsx:25` рендерит его как есть. Наблюдатель по «?» читает «Ctrl+R — принять диалог», «Ctrl+T — передать», «/ — быстрые ответы». Нажатия молча ничего не делают: потребители нонсов живут внутри веток по правам (`InboxDecisionBar` рендерится только под `messages:send`; Ctrl+D/Ctrl+T проверяют `conversations:manage` — `ChatThreadPane.tsx:252,273`).

5. **Пустой ряд действий у наблюдателя.** `ThreadActions` (`frontend/src/features/chats/components/thread/ThreadActions.tsx:59`) рендерится безусловно из `ChatThreadPane.tsx:372`; для наблюдателя все пять кнопок скрыты и остаётся пустой `<div role="group" aria-label="Действия с диалогом">`.

---

## 5. РОЛИ «НАБЛЮДАТЕЛЬ» И «РУКОВОДИТЕЛЬ»: ЧЕМ ОТЛИЧАЮТСЯ И ГДЕ ЭТО ВИДНО ЧЕЛОВЕКУ

### Руководитель (head)
**Формально:** 10 из 15 прав. Ключевое отличие от админа — нет `messages:send`, `users:manage`, `accounts:manage`, `bots:manage`, `settings:manage`.

**Что видит:** «Чаты» (весь поток, вкладка по умолчанию «Все» — `chatUiStore.ts:72`), «Разбор диалогов», «Статистику всех», в настройках — «Аккаунты Авито» (без кнопок), «Быстрые ответы», «Команда» (только вкладка «Журнал аудита»), «Профиль». Колокольчик уведомлений есть, но системные тревоги ему не приходят (`app/services/notifications.py:91`).

**Где это видно человеку:**
- Плашка вместо поля ввода: **«👁 Режим просмотра — назначьте менеджера или передайте диалог»** — `frontend/src/features/chats/components/composer/ThreadFooter.tsx:69`. Под ней остаётся композер ТОЛЬКО заметок (строка 71, `noteOnly`).
- Сервер отдаёт для этого специальный код ошибки `read_only_role` вместо `forbidden` — `app/api/deps.py:77-79`; тест `tests/unit/test_rbac.py:486-487` проверяет и код, и текст «Режим просмотра».
- В очереди «Входящие» вкладки у него нет вовсе (`ChatListPane.tsx:167`), а на действия очереди сервер отвечает тем же `read_only_role` (`app/api/routes/inbox.py:29-31`).
- Подпись роли: «Руководитель» — `AppLayout.tsx:35` (под именем в шапке), `ProfilePage.tsx:80` с пояснением «видит все диалоги, передаёт их и пишет заметки — отвечать клиенту не может» (`ProfilePage.tsx:23`), `roles.ts:14` с другим текстом: «Все диалоги без отправки, статистика всех сотрудников, журнал аудита» (в модалке приглашения — `InviteModal.tsx:109`).
- Смена статуса руководителем НЕ делает его ответственным — `app/services/conversations.py:879` (`can_answer_clients(actor.role)`), тест `tests/unit/test_rbac.py:498-521` (после `status=in_progress` `assignee is None`). Его переназначение помечается в журнале отдельным словом: `app/services/conversations.py:950` → `_ASSIGN_LABELS["head"] = "Диалог переназначен руководителем"` (`app/services/audit.py:169`).
- Ему нельзя передать диалог: `ASSIGNABLE_ROLES` (`app/services/conversations.py:73`) выводится из `messages:send`, и `resolve_assignee` отбивает 422 `assignee_cannot_chat` (`app/services/conversations.py:932-938`).

### Наблюдатель (observer)
**Формально:** ровно ОДНО право — `conversations:read` (`app/core/rbac.py:59`).

**Что видит:** только «Чаты» в режиме чтения + «Настройки → Профиль» с двумя вкладками («Учётная запись», «Интерфейс» — вкладки «Быстрые ответы» нет, `ProfilePage.tsx:112`). Ни статистики, ни разбора диалогов, ни очереди, ни колокольчика.

**Где это видно человеку:**
- Низ ленты пуст — ни композера, ни плашки: `ThreadFooter.tsx:76` возвращает только полосу передачи (то есть в норме — `null`). Лента идёт до низа.
- Заметок он не видит физически, а не «скрыто в CSS»: сервер отдаёт ленту без `direction='note'` (`app/api/routes/conversations.py:609-612`, `app/services/conversations.py:1133`), поиск по заметкам ему отключён (`app/services/conversations.py:329`), превью последнего сообщения-заметки в список не попадает (`app/services/conversations.py:594`), и по WebSocket заметки до него не доезжают (`app/ws/hub.py:183-189`). Тест — `tests/unit/test_rbac.py:548-563` + `tests/unit/test_observer_notes.py`.
- На любую мутацию диалога сервер отвечает именно `forbidden`, а не `read_only_role` — `tests/unit/test_rbac.py:524-545` (комментарий 543-544: «read_only_role — специализация для head, наблюдателю плашку „назначьте менеджера“ показывать не за что»).
- Уведомления: 403 на всех пяти ручках центра (`app/services/notifications.py:85-87`, тест `tests/unit/test_notifications.py::test_observer_sees_nothing_at_all`), колокольчик не рисуется (`NotificationBell.tsx:20`), страница `/notifications` честно пишет «Вашей роли уведомления не приходят» (`NotificationsPage.tsx:182-187`).
- Шаблонов у него нет вовсе: `templates:own` отсутствует, все ручки `/templates` отвечают 403 (`app/api/routes/templates.py:6`, `tests/unit/test_rbac.py:49-50`).
- Подпись роли: «Наблюдатель» — `AppLayout.tsx:37`, пояснение в профиле «только чтение диалогов» (`ProfilePage.tsx:25`), в модалке приглашения — «Только чтение диалогов: без ответов, заметок и смены статусов» (`roles.ts:16`).
- Что ЕМУ ВСЁ-ТАКИ доступно, кроме чтения: сменить своё имя (`PATCH /auth/me`), свой пароль (`POST /auth/change-password`), своё состояние «на месте/отошёл» (`PUT /presence`), закрепить диалог (`POST /conversations/{id}/pin` — право `conversations:read`), написать администратору (`POST /support/message` — `tests/unit/test_rbac.py:289-293`). Все пять — в `COVERED_ELSEWHERE` теста RBAC со ссылками на профильные тесты.

### Одной строкой
Руководитель — «всё вижу, всем распоряжаюсь, клиенту не пишу»: у него 10 прав, включая управление диалогами, заметки, статистику всех и журнал аудита. Наблюдатель — «только смотрю переписку с клиентом»: одно право, и даже внутренняя кухня команды (заметки) от него закрыта на уровне выдачи данных, а не интерфейса.

### Есть в коде, но не выведено (10)

* **GET /api/v1/clients/blocked — список нежелательных клиентов**
  * где: app/api/routes/clients.py:115 (право `conversations:manage`, app/api/routes/clients.py:31); строка матрицы — tests/unit/test_rbac.py:91. Во всём frontend/src нет ни одного вызова: grep по «clients/» находит только POST block/unblock в frontend/src/features/chats/components/card/BlockClientButton.tsx:48
  * почему важно: Пометить клиента нежелательным можно, а посмотреть, кого уже пометили, — негде. Снять пометку получится только случайно наткнувшись на диалог этого клиента. Ручка написана, покрыта правом и тестом, но экрана у неё нет.
* **GET /api/v1/users/{user_id}/accounts — каналы другого сотрудника**
  * где: app/api/routes/avito_accounts.py:145-160 (собственный список — любой роли, чужой — под `accounts:read`); в матрице — tests/unit/test_rbac.py:335. Во frontend вызова нет; в frontend/src/features/settings/team/ слово «channels» не встречается вовсе
  * почему важно: Docstring прямо говорит «карточка человека в „Команде“ (11 §4.2)» — такой карточки в TeamMembersTab.tsx не существует. Администратор не может ответить на вопрос «почему Петрову не приходят обращения», не открыв экран аккаунтов и не пройдя по всем каналам.
* **GET /api/v1/avito-accounts/{account_id}/subscriptions — кто сейчас подписан на события аккаунта в Авито**
  * где: app/api/routes/avito_accounts.py:163+ (право `accounts:manage`); tests/unit/test_rbac.py:160-163. Во frontend/src слово «subscriptions» не встречается ни разу
  * почему важно: Ручка сделана под конкретный вопрос переезда с Jivo (задача #39: сосуществуют ли подписки). Ответ можно получить только curl'ом — администратор, который будет принимать решение о параллельном пилоте, до него не доберётся.
* **Маршрут /ui-kit без единого guard'а**
  * где: frontend/src/app/router.tsx:107-112 — внутри RequireAuth, но без RequirePermission; на него нет ни одной ссылки в интерфейсе (grep «ui-kit» даёт только router.tsx и сам UiKitPage.tsx:126)
  * почему важно: Витрина дизайн-системы доступна прямой ссылкой любой роли, включая наблюдателя. Не утечка данных, но страница «для того, кто правит дизайн» лежит в проде и открывается кому угодно.
* **Шпаргалка горячих клавиш не фильтруется по ролям**
  * где: frontend/src/features/hotkeys/catalog.ts:22-37 — плоский массив HOTKEYS; frontend/src/features/hotkeys/HotkeysModal.tsx:25 рендерит его целиком без usePermissions
  * почему важно: Наблюдатель и руководитель по «?» читают «Ctrl+R — принять диалог», «Ctrl+T — передать», «/ — быстрые ответы». Нажатие молча не делает ничего: потребители нонсов живут внутри веток по правам. Комментарий в самом файле (строки 12-16) описывает ровно эту беду для Ctrl+T — «шпаргалка, обещающая несуществующее, заставляет решить, что сломалось приложение», — но ролевой случай не закрыт.
* **Тестовая фикстура прав отстала от rbac.py на одно право**
  * где: frontend/src/test/usePermissions.test.tsx:13-50 — SERVER_PERMISSIONS.admin перечисляет 14 прав и обрывается на «audit:read»; в app/core/rbac.py:29 у admin есть пятнадцатое — `settings:manage`
  * почему важно: Комментарий фикстуры (строки 8-10) обещает: «если матрица разъедется, тест должен упасть». Он не упадёт: фикстура — локальная копия, ни с чем не сверяемая. То же в frontend/src/test/SettingsNav.test.tsx:29-33, где «порядок пунктов» ожидает пять названий и не знает про «Распределение», — сторож ролевого меню проверяет меню, которого у настоящего админа нет.
* **Серверная проверка «handles_conversations только для ролей, умеющих отвечать» существует только во фронте**
  * где: frontend/src/features/settings/team/roles.ts:31-33 (canAnswer — локальная копия ASSIGNABLE_ROLES) и TeamMembersTab.tsx:370 (disabled). Сервер: app/services/users.py:380-390 присваивает флаг без всякой сверки с ролью
  * почему важно: Через API наблюдателю можно поставить «ведёт диалоги = да». Пул операторов его всё равно отфильтрует по роли (app/services/conversations.py:126), поэтому вреда нет — но в таблице сотрудников у него загорится галочка, которая ничего не означает.
* **Подписи и пояснения ролей продублированы пятью независимыми копиями с расходящимися текстами**
  * где: frontend/src/features/settings/team/roles.ts:5-17, frontend/src/app/AppLayout.tsx:33-38, frontend/src/features/settings/profile/ProfilePage.tsx:14-26, frontend/src/features/chats/components/card/PeoplePicker.tsx:7, app/services/audit.py:157-162
  * почему важно: Руководителю в модалке приглашения обещают «Все диалоги без отправки, статистика всех сотрудников, журнал аудита» (roles.ts:14), а в его же профиле — «видит все диалоги, передаёт их и пишет заметки — отвечать клиенту не может» (ProfilePage.tsx:23). Два разных описания одной роли, и ни одно не упоминает аккаунты Авито, которые он видит.
* **Пустой контейнер действий у наблюдателя**
  * где: frontend/src/features/chats/components/thread/ThreadActions.tsx:59 — <div role="group" aria-label="Действия с диалогом"> рендерится безусловно из ChatThreadPane.tsx:372, а все пять кнопок внутри скрыты правами
  * почему важно: Скринридер объявляет группу «Действия с диалогом», в которой нет ни одного действия. Визуально — пустой отступ в шапке ленты.
* **DESIGN.md §5.1/§5.2 разошёлся с кодом**
  * где: DESIGN.md:403-425. В матрице §5.1 десять строк — нет `settings:manage` (app/core/rbac.py:29). В §5.2 сказано, что руководителю «из настроек — только общие шаблоны», тогда как frontend/src/features/settings/SettingsLayout.tsx:15,20,35 даёт ему ещё «Аккаунты Авито» и «Команда»
  * почему важно: Документ назван источником истины в шапке app/core/rbac.py:1-2 и в тестах (tests/unit/test_rbac.py:405-412 — «таблица 01 §12 продублирована здесь как исполняемая копия закона»). Копия ушла вперёд оригинала: приёмка по DESIGN.md отклонит верное поведение.

### Расхождения с брифом и чего не нашёл

РАСХОЖДЕНИЯ С БРИФОМ

1. РОЛЕЙ ЧЕТЫРЕ, А НЕ ДВЕ. Бриф называет «Администратор, Менеджер». В коде — admin, head, manager, observer: app/core/rbac.py:7, CHECK-ограничение БД app/models/user.py:16, миграция app/db/migrations/versions/0001_init.py:48, тип фронта frontend/src/shared/auth/usePermissions.ts:4. Тест tests/unit/test_rbac.py:440-442 (`test_roles_are_exactly_the_four_of_design_5_1`) запирает ровно четыре — добавить пятую или убрать существующую без падения CI нельзя. Роли «head» (Руководитель) и «observer» (Наблюдатель) — не заготовка на будущее: у них своя ветка интерфейса (плашка «Режим просмотра» — ThreadFooter.tsx:69), свой код ошибки на сервере (`read_only_role`, app/api/deps.py:77-79) и свои тесты.

2. К «двум ролям» брифа примыкает ПЯТАЯ ОСЬ, которой в брифе нет вовсе: `handles_conversations` (app/models/user.py:55-57) — «ведёт ли человек диалоги», отдельно от роли. Администратор может администрировать и не получать обращения; у заказчика так настроен отдел «СТАРШИЕ - ЧАТЫ». Плюс `department` (app/models/user.py:63). Обе колонки выведены в таблицу сотрудников (TeamMembersTab.tsx:307-312).

3. РАЗДЕЛ «/settings/quick-replies» НЕ СУЩЕСТВУЕТ. Строки «quick-replies» / «quick_replies» нет нигде в репозитории (проверил grep'ом по всему дереву, исключая node_modules/.venv/.git). Фактический маршрут — `/settings/templates`, компонент frontend/src/features/settings/templates/TemplatesPage.tsx, подключён в frontend/src/app/router.tsx:147-149. Подпись пункта в меню — «Быстрые ответы» (SettingsLayout.tsx:22), то есть совпадает НАЗВАНИЕ, но не адрес.

4. Остальные разделы брифа сходятся: /chats (router.tsx:70, 113), /dialogs (router.tsx:94), /stats (router.tsx:132), /settings/accounts (143-144), /settings/bots (158), /settings/distribution (175), /settings/team (189), /settings/profile (141).

5. В брифе НЕ УПОМЯНУТЫ четыре живых маршрута: `/notifications` (router.tsx:120), `/updates` (router.tsx:102), `/ui-kit` (router.tsx:108) и редирект `/settings` → `/settings/profile` (router.tsx:140). Из них guard по правам стоит только там, где перечислено в §3.5 инвентаря; `/notifications`, `/updates` и `/ui-kit` доступны любой авторизованной роли, включая наблюдателя.

ЧЕГО НЕ НАШЁЛ

- Не нашёл нигде во фронте хардкода «роль → права», кроме тестовой фикстуры frontend/src/test/usePermissions.test.tsx:13-50. Единственный источник — список из GET /auth/me (usePermissions.ts:26-42, app/api/routes/auth.py:256). Это правильно и стоит отметить отдельно.
- Не нашёл ни одного экрана, где ветвление шло бы по строке роли БЕЗ основания. Таких мест ровно три, и все объяснены комментарием: ClientCardPane.tsx:206 (свободный селект ответственного — только admin/head), ChatListPane.tsx:658 и AppLayout.tsx:125 (виджет «моя статистика» — только менеджеру), chatUiStore.ts:72 (вкладка по умолчанию).
- Не нашёл серверной проверки «handles_conversations совместим с ролью» — она есть только во фронте (roles.ts:31-33).
- Не нашёл ролевой фильтрации в шпаргалке горячих клавиш.
- Не нашёл текста самого брифа заказчика в репозитории: DESIGN-BRIEF-PROMPT.md (117 строк) описывает пять разделов интерфейса (строка 85: «Настройки — сотрудники, каналы Авито, боты, распределение, профиль») и о ролях не говорит вообще. Ролевая модель разложена в DESIGN.md §5.1-§5.2 (строки 403-425), и там она уже четырёхролевая. Сверку с формулировкой «две роли» веду по тексту задачи, а не по файлу.
- Не проверял (вне среза): визуальную часть, вёрстку, производительность, качество самих экранов. Ничего не менял — только чтение.

## Срез: Компоненты и токены

# СРЕЗ «ДАННЫЕ» — LeadChat, leadchat

Все пути ниже — от корня `.`.

---

## 1. ТАБЛИЦЫ (app/models/)

Всего **16 таблиц** + 1 материализованное представление. Партиционирована **одна** — `messages`.

Базовый класс: `app/models/base.py:15` — `Base(DeclarativeBase)` с обязательным naming convention (`app/models/base.py:6-12`).
Кросс-диалектные типы: `app/models/types.py` — `CIText` (citext на PG / TEXT иначе, стр. 14), `JSONB` (jsonb / JSON, стр. 17), `TextArray` (text[] / JSON, стр. 20).

### 1.1 `users` — app/models/user.py:13

| поле | тип | нюансы |
|---|---|---|
| `id` | Uuid PK | default `uuid.uuid4` (стр. 19) |
| `email` | CIText, UNIQUE, NOT NULL | стр. 20 |
| `password_hash` | Text NOT NULL | argon2id (стр. 21) |
| `full_name` | Text NOT NULL | стр. 22 |
| `role` | Text NOT NULL | стр. 23 |
| `is_active` | Boolean NOT NULL default True | стр. 24 |
| `deleted_at` | DateTime(tz) NULL | стр. 39, мягкое удаление |
| `handles_conversations` | Boolean NOT NULL, server_default `true` | стр. 55-57 |
| `department` | Text NULL | стр. 63, свободная строка, не справочник |
| `last_assigned_at` | DateTime(tz) NULL | стр. 77, тай-брейк автораздачи |
| `created_at` | DateTime(tz) NOT NULL server_default now() | стр. 78 |

CHECK: `users_role` — `role IN ('admin','head','manager','observer')` (стр. 16).
Индексы: `pk_users`, `uq_users_email`, `ix_users_handles_conversations` (частичный, `WHERE is_active AND handles_conversations` — 0011), `ix_users_deleted` (частичный, `WHERE deleted_at IS NOT NULL` — 0017).
Связи-обратно: `conversations.assignee_id/claimed_by_id/transfer_to_id/transfer_by_id/outcome_by_id`, `messages.sender_user_id`, `templates.owner_id`, `clients.blocked_by_id`, `app_settings.updated_by_id`, `account_operators.user_id`, `conversation_participants.user_id/invited_by_id`, `conversation_pins.user_id`, `notifications.recipient_id`, `notification_reads.user_id`.

### 1.2 `avito_accounts` — app/models/account.py:12

`id` Uuid PK; `title` Text NN; `avito_user_id` BigInteger UNIQUE NN; `access_token_enc` LargeBinary NN (AES-256-GCM); `refresh_token_enc` LargeBinary NN; `token_expires_at` DateTime(tz) NN; `status` Text NN default `active` (`active|needs_reauth|disabled`, стр. 21-23); `webhook_secret` Text NN; `client_id` Text NULL (стр. 28, миграция 0018); `client_secret_enc` LargeBinary NULL (стр. 29); `bot_id` Uuid FK→`bots.id` NULL (стр. 30); `created_at`.
Индексы: `pk_avito_accounts`, `uq_avito_accounts_avito_user_id`, `ix_avito_accounts_bot_id` (частичный `WHERE bot_id IS NOT NULL` — 0005:54).

### 1.3 `clients` — app/models/client.py:13

`id` Uuid PK; `channel` Text NN default `avito`; `external_id` Text NN; `name` Text NULL; `phone` Text NULL; `avito_rating` Numeric NULL (стр. 22); `blocked_at` DateTime(tz) NULL (стр. 41); `blocked_by_id` Uuid FK→users ON DELETE SET NULL (стр. 42); `blocked_reason` Text NULL (стр. 45).
Ограничения: `uq_clients_channel_external_id` (стр. 15), `ix_clients_blocked` (частичный `WHERE blocked_at IS NOT NULL` — 0013).

### 1.4 `conversations` — app/models/conversation.py:25 (самая широкая таблица, 30 колонок)

Ядро (0001): `id` Uuid PK; `channel` Text NN default `avito`; `external_chat_id` Text NN; `account_id` Uuid FK→avito_accounts NN; `client_id` Uuid FK→clients NN; `assignee_id` Uuid FK→users NULL; `status` Text NN default `new` (`new|in_progress|closed`, стр. 40-42); `bot_active` Boolean NN default False; `bot_vars` JSONB NN default {}; `tags` TextArray NN default []; `item_title`/`item_url`/`item_price` Text NULL; `last_message_at` DateTime(tz) NULL; `updated_at` DateTime(tz) NN server_default now() + `onupdate` (стр. 57-62).

Счётчик (0002): `unread_count` Integer NN server_default 0 (стр. 52).

Очередь «Входящие» (0007): `offered_at` (стр. 77); `claimed_by_id` Uuid FK→users (стр. 82, единственный арбитр гонки принятия); `claimed_at` (стр. 83); `declined_by` TextArray NN default [] (стр. 87); `escalated_at` (стр. 90).

Автораздача (0010): `auto_assigned_at` DateTime(tz) NULL (стр. 104).

Клиент ждёт (0015): `awaiting_since` DateTime(tz) NULL (стр. 126).

Недоставленное (0019): `undelivered_at` DateTime(tz) NULL (стр. 150).

Результат обращения (0020): `outcome` Text NULL (стр. 162); `outcome_amount` BigInteger NULL — **в копейках** (стр. 168); `outcome_at` (стр. 172); `outcome_by_id` Uuid FK→users (стр. 173).

Передача с подтверждением (0012): `transfer_to_id`, `transfer_by_id` (FK→users, ON DELETE SET NULL), `transfer_at`, `transfer_comment` Text (стр. 197-203).

Ограничения/индексы: `uq_conversations_channel_external_chat_id` (стр. 28); `ix_conversations_tags` — **GIN** по `tags` (стр. 29); + из миграций: `ix_conversations_last_message_at` (`last_message_at DESC NULLS LAST`), `ix_conversations_status_last_message`, `ix_conversations_assignee_status`, `ix_conversations_updated_at`, `ix_conversations_account_id` (все 0002); `idx_conversations_account_status`, `idx_conversations_client` (0004); `ix_conversations_bot_active` — частичный по `updated_at WHERE bot_active` (0005:55); `ix_conversations_inbox_wait`, `ix_conversations_inbox_account`, `ix_conversations_claimed_by` — частичные по предикату очереди (0007:122-125 `IN_QUEUE`); `ix_conversations_auto_assigned` (0010); `ix_conversations_transfer_pending`, `ix_conversations_transfer_to` (0012); `ix_conversations_awaiting` (0015); `ix_conversations_undelivered` (0019); `ix_conversations_outcome` (0020). Итого **20 индексов** на одной таблице.

### 1.5 `messages` — app/models/message.py:26 — **ЕДИНСТВЕННАЯ ПАРТИЦИОНИРОВАННАЯ**

`PARTITION BY RANGE (created_at)`, DDL руками в 0001_init.py:206-228. Составной PK `(id, created_at)`.

`id` Uuid; `conversation_id` Uuid FK→conversations NN; `external_message_id` Text NULL; `client_message_id` Text NULL (tempId фронта, добавлено 0003); `direction` Text NN (`in|out|note|system`, стр. 63); `sender_type` Text NN (`client|operator|bot|system`, стр. 64-66); `sender_user_id` Uuid FK→users NULL; `body` Text NULL; `attachments` JSONB NN default []; `delivery_status` Text NN default `delivered` (`pending|delivered|failed`); `created_at` DateTime(tz) **PK-часть**, default `_now` (стр. 73-78).

**Колонка `search tsvector GENERATED ALWAYS AS (to_tsvector('russian', coalesce(body,''))) STORED` живёт только в БД** — в ORM-модели её нет намеренно (докстринг стр. 7-8; DDL — 0001_init.py:219-220). Исключена из autogenerate: `app/db/migrations/env.py:34-35`.

Индексы: `pk_messages (id, created_at)`; `uq_messages_conversation_external_created` — частичный UNIQUE `(conversation_id, external_message_id, created_at) WHERE external_message_id IS NOT NULL` (модель стр. 30-38 / 0001:230-236); `uq_messages_conversation_client_message_created` — частичный UNIQUE `(conversation_id, client_message_id, created_at) WHERE client_message_id IS NOT NULL` (модель стр. 44-52 / 0003:49-53); `ix_messages_search` — **GIN** (0001:237); `ix_messages_conversation_created` (0002:82); `idx_messages_operator_out` — частичный `WHERE direction='out' AND sender_type='operator'` (0004:72-76); `idx_messages_client_in` — частичный `WHERE direction='in' AND sender_type='client'` (0004:78-83).

Партиции: помесячные `messages_yYYYYmMM`, создаются джобом `ensure_message_partitions` (`app/scheduler/partitions.py`), окно **24 месяца назад + 1 вперёд** (`MONTHS_BACK = 24` стр. 46, `MONTHS_AHEAD = 1` стр. 50). Первые две партиции ставит миграция 0002:86-95. Второй слой — точное покрытие по факту данных (`PartitionCoverage`, докстринг стр. 20-24). Партиции исключены из autogenerate: `app/db/migrations/env.py:30`.

### 1.6 `templates` — app/models/template.py:11
`id` Uuid PK; `owner_id` Uuid FK→users NULL (**NULL = общий шаблон**, стр. 15-17); `title` Text NN; `body` Text NN; `folder` Text NULL. Индексов, кроме PK и FK, нет.

### 1.7 `bots` — app/models/bot.py:14
`id` Uuid PK; `name` Text NN; `is_enabled` Boolean NN default True; `schedule` JSONB NN default `{"always": true}`; `scenario` JSONB NN (граф шагов); `knowledge_base` Text NULL; `created_at`, `updated_at` (обе добавлены 0005, `updated_at` с `onupdate`, стр. 32-34).

### 1.8 `audit_log` — app/models/audit.py:17
`id` BigInteger IDENTITY PK (на SQLite — Integer, стр. 14); `user_id` Uuid NULL — **без FK намеренно** (стр. 1, 21); `action` Text NN; `entity` Text NULL; `entity_id` Text NULL; `details` JSONB NULL; `created_at`.
Индексы: `idx_audit_action_created (action, created_at)`, `idx_audit_entity (entity, entity_id, created_at)` — оба 0004.

### 1.9 `webhook_raw_log` — app/models/webhook_raw.py:18
`id` BigInteger Identity(always=True) PK; `account_id` Uuid NULL — **без FK по замыслу** (стр. 3, 27); `stream_id` Text NN; `payload` JSONB NN; `received_at`; `processed` Boolean NN default False; `error` Text NULL.
Индексы: `uq_webhook_raw_stream` (UNIQUE), `ix_webhook_raw_received`, `ix_webhook_raw_account_received` (стр. 21-23).
Retention 30 дней — `app/scheduler/main.py:98-108`, крон 03:10 UTC (стр. 147-149).

### 1.10 `notifications` — app/models/notification.py:52
`id` Uuid PK; `recipient_id` Uuid FK→users ON DELETE CASCADE NULL; `audience` Text NULL (`admin|head`); `kind` Text NN; `severity` Text NN (`critical|warning|info`); `title` Text NN; `body` Text NULL; `entity_type` Text NULL; `entity_id` Text NULL; `dedup_key` Text NULL; `repeat_count` Integer NN default 1; `last_seen_at` NN; `read_at` NULL; `created_at` NN; `expires_at` DateTime(tz) **NN**.
Константы: `SEVERITIES` (стр. 48), `AUDIENCES` (стр. 49).
4 CHECK: `severity`, `audience`, `addressing` (`(recipient_id IS NOT NULL) <> (audience IS NOT NULL)`, стр. 59), `repeat_count >= 1` (стр. 60).
4 индекса: `ix_notifications_recipient_unread` (частичный `WHERE read_at IS NULL`), `ix_notifications_audience_created` (частичный `WHERE audience IS NOT NULL`), `ix_notifications_dedup_key` (частичный), `ix_notifications_expires_at` (стр. 63-86).

### 1.11 `notification_reads` — app/models/notification.py:120
Составной PK `(notification_id, user_id)`, обе стороны FK ON DELETE CASCADE; `read_at` NN. Индекс `ix_notification_reads_user_id`.

### 1.12 `account_operators` — app/models/account_operator.py:56
Составной PK `(account_id, user_id)` (стр. 59); обе стороны FK **ON DELETE CASCADE** (стр. 68, 77); `created_at` NN. Индекс `ix_account_operators_user (user_id, account_id)` — покрывающий (стр. 60).

### 1.13 `app_settings` — app/models/app_setting.py:32
`key` Text **PK**; `value` JSONB NN; `updated_at` NN с `onupdate`; `updated_by_id` Uuid FK→users ON DELETE SET NULL. Индексов сверх PK нет.
Реальные ключи объявлены в `app/services/app_settings.py`: `distribution.enabled` (стр. 28), `distribution.max_active` (стр. 32), `cleanup.enabled` (стр. 46), `cleanup.days` (стр. 49), `stats.work_start_hour` / `stats.work_end_hour` (стр. 70-71). Реестр `SPECS` — стр. 89-101.

### 1.14 `conversation_participants` — app/models/conversation_participant.py:40
Составной PK `(conversation_id, user_id)`, обе стороны CASCADE; `invited_by_id` Uuid FK→users ON DELETE SET NULL; `invited_at` NN; `reason` Text NULL. Индекс `ix_conversation_participants_user (user_id, conversation_id)`.

### 1.15 `conversation_pins` — app/models/conversation_pin.py:31
Составной PK `(user_id, conversation_id)`, обе стороны CASCADE; `pinned_at` NN. Индекс `ix_conversation_pins_user (user_id, pinned_at)`. Использует `PgUUID(as_uuid=True)` напрямую вместо `Uuid` — единственная модель с таким расхождением (стр. 25, 43, 46).

### 1.16 `message_idempotency` — app/models/message_idempotency.py:29
Составной PK `(conversation_id, client_message_id)`; `message_id` Uuid NN; `created_at` NN. **Намеренно НЕ партиционирована** (докстринг стр. 8-13: партиционирование `messages` обессмыслило прежний уникальный индекс). FK нет ни на одно поле. Индексов сверх PK нет — чистка по `created_at` не реализована (докстринг стр. 15-17 обещает «ночным заданием, когда вырастет»).

---

## 2. МИГРАЦИИ (app/db/migrations/versions/) — 23 штуки, линейная цепочка 0001→0023

| # | файл | что добавила |
|---|---|---|
| 0001 | `0001_init.py` | `CREATE EXTENSION citext`; таблицы `users`, `bots`, `avito_accounts`, `clients`, `conversations`, `templates`, `audit_log`; `messages` сырым DDL — `PARTITION BY RANGE (created_at)` + generated `search tsvector` + PK `(id, created_at)` (стр. 206-228); частичный UNIQUE `uq_messages_conversation_external_created`; GIN `ix_messages_search`, GIN `ix_conversations_tags`. Партиции НЕ создаёт. |
| 0002 | `0002_inbound.py` | `webhook_raw_log` + 3 индекса; `conversations.unread_count`; 5 индексов списка диалогов; `ix_messages_conversation_created`; **первые две помесячные партиции** `messages` (текущий + следующий месяц, стр. 86-95). |
| 0003 | `0003_client_message_id.py` | `messages.client_message_id` + частичный UNIQUE `uq_messages_conversation_client_message_created`. Докстринг фиксирует отказ от `CONCURRENTLY` (стр. 14-24). |
| 0004 | `0004_stats_indexes_and_mv.py` | SQL-функция `business_seconds_between(t0,t1,work_start,work_end)` — рабочие часы 10-20 Europe/Moscow, `STABLE STRICT` (стр. 48-65); 6 индексов статистики (`idx_messages_operator_out`, `idx_messages_client_in`, `idx_conversations_account_status`, `idx_conversations_client`, `idx_audit_action_created`, `idx_audit_entity`); **материализованное представление `mv_conversation_stats`** (стр. 108-180) + 5 индексов, включая обязательный UNIQUE `mv_conversation_stats_pk`. |
| 0005 | `0005_bots_engine.py` | `bots.created_at`/`updated_at`; `ix_avito_accounts_bot_id`; `ix_conversations_bot_active`; **вставка дефолтного бота** `b07f0001-…-000000000001` «Первичный приём», только на пустой таблице (стр. 207-219). |
| 0006 | `0006_notifications.py` | таблицы `notifications` (+4 CHECK, +4 индекса) и `notification_reads` (+индекс). |
| 0007 | `0007_inbox_queue.py` | `conversations`: `offered_at`, `claimed_by_id` (+FK), `claimed_at`, `declined_by`, `escalated_at`; **бэкофилл** — все с `assignee_id` считаются принятыми (стр. 147-151), все ничьи и не закрытые встают в очередь с `offered_at = COALESCE(last_message_at, updated_at)` (стр. 152-156); 3 частичных индекса по предикату `IN_QUEUE` (стр. 122-125). |
| 0008 | `0008_account_operators.py` | таблица `account_operators` (составной PK, обе FK CASCADE) + `ix_account_operators_user`. Бэкофилла нет намеренно. |
| 0009 | `0009_app_settings.py` | таблица `app_settings` (PK по `key`, FK `updated_by_id` SET NULL). Бэкофилла нет намеренно. |
| 0010 | `0010_auto_assigned_at.py` | `conversations.auto_assigned_at` + частичный индекс. Бэкофилла нет намеренно. |
| 0011 | `0011_user_department.py` | `users.handles_conversations` (server_default true), `users.department`; частичный `ix_users_handles_conversations`. |
| 0012 | `0012_transfer_pending.py` | `conversations.transfer_to_id`/`transfer_by_id` (обе FK SET NULL), `transfer_at`, `transfer_comment`; 2 частичных индекса. |
| 0013 | `0013_client_blocklist.py` | `clients.blocked_at`/`blocked_by_id` (FK SET NULL)/`blocked_reason`; частичный `ix_clients_blocked`. |
| 0014 | `0014_conversation_participants.py` | таблица `conversation_participants` (3 FK: CASCADE, CASCADE, SET NULL) + `ix_conversation_participants_user`. |
| 0015 | `0015_awaiting_reply.py` | `conversations.awaiting_since` + частичный индекс. |
| 0016 | `0016_conversation_pins.py` | таблица `conversation_pins` + `ix_conversation_pins_user`. |
| 0017 | `0017_user_deleted.py` | `users.deleted_at` + частичный индекс. |
| 0018 | `0018_account_own_keys.py` | `avito_accounts.client_id`, `avito_accounts.client_secret_enc`. Индексов не добавляет. |
| 0019 | `0019_conversation_undelivered.py` | `conversations.undelivered_at` + частичный индекс. |
| 0020 | `0020_conversation_outcome.py` | `conversations.outcome`, `outcome_amount` (BigInteger), `outcome_at`, `outcome_by_id` (FK inline) + частичный `ix_conversations_outcome`. **CHECK-ограничения на справочник значений НЕТ.** |
| 0021 | `0021_message_idempotency.py` | таблица `message_idempotency` (составной PK). Индексов и FK не добавляет. |
| 0022 | `0022_user_last_assigned_at.py` | `users.last_assigned_at` + **бэкофилл** `max(updated_at)` по диалогам каждого ответственного (стр. 55-67). Индекса не добавляет. |
| 0023 | `0023_work_hours_setting.py` | функция `stats_work_hour(setting_key, fallback)` — `STABLE`, не `STRICT` (стр. 63-71); `CREATE OR REPLACE business_seconds_between` — сигнатура та же, умолчания параметров заменены на вызовы `stats_work_hour(…)` (стр. 74-91). MV не пересобирается. |

**Настройка alembic:** `app/db/migrations/env.py` — асинхронный движок, `include_object` (стр. 31-37) исключает из autogenerate партиции `messages_y\d+.*`, `mv_conversation_stats` и колонку `messages.search`.

---

## 4. REDIS — все ключи

Клиент-синглтон: `app/core/redis.py:13-24`, `decode_responses=True`.

### Сессии и доступ
| ключ | тип | TTL | пишет | читает |
|---|---|---|---|---|
| `refresh:{token}` | string (JSON `{user_id, remember}`) | `refresh_ttl_days`=14 сут (`app/core/config.py:99`) | `core/security.py:116` | `security.py:137,153` (GETDEL) |
| `refresh_rotated:{token}` | string (user_id) | 14 сут | `security.py:146,157` | `security.py:140` (детект переиспользования) |
| `user_refresh:{user_id}` | set токенов | 14 сут (`expire`, стр. 118) | `security.py:117` | `security.py:124` (SMEMBERS при отзыве) |
| `invite:{token}` | string (user_id) | `INVITE_TTL_SECONDS`=72 ч (`security.py:31`) | `security.py:170` | `security.py:177,181` |
| `invite_user:{user_id}` | string (token) | 72 ч | `security.py:171` | `security.py:166,183` |
| `revoked_users:{user_id}` | string (unix-время отзыва) | `jwt_access_ttl_seconds`=900 с | `services/sessions.py:82` | `api/deps.py`; снимается `sessions.py:105` |
| `ws_ticket:{ticket}` | string (user_id) | `ws_ticket_ttl_seconds`=60 с (`config.py:142`) | `api/routes/ws.py:35` | `ws.py:53` (GETDEL, одноразовый) |
| `auth:lock_audited:{sha(email)}` | string «1», NX | `max(ttl,1)` | `api/routes/auth.py:117` | сам себя (маркер однократности) |
| `login_fail:email:{sha24(email)}` | счётчик | 60 с (`login_guard.py:41`) | `login_guard.py:79` | `login_guard.py:66`, TTL — стр. 68 |
| `login_fail:ip_emails:{ip}` | set хэшей почт | 60 с | `login_guard.py:82` | `login_guard.py:73` (SCARD ≥ 30) |
| `support:pwreset:email:{sha24}` | счётчик | окно из `support.py` | `support.py:177` | там же |
| `support:pwreset:ip:{ip}` | счётчик | окно | `support.py:183` | там же |
| `support:message:user:{user_id}` | счётчик | 3600 с (`support.py:160`) | `support.py:282` | там же |

### Пер-юзерное состояние
| ключ | тип | TTL | пишет | читает |
|---|---|---|---|---|
| `read:{user_id}` | **hash** `{conversation_id: last_read_at ISO}` | `read_marker_ttl_days`=90 сут, продлевается на каждой записи (`read_markers.py:48-49,117,120`) | `read_markers.py:119` (HSET) | `read_markers.py:82` (HGET), `:95` (HMGET на страницу списка) |
| `transferred:{user_id}` | set id диалогов | 30 сут (`conversations.py:85`) | `conversations.py:407` (SADD) | `conversations.py:418` (SMEMBERS); снимает `:413` (SREM) |
| `presence:{user_id}` | string `online`/`away` | `ws_presence_ttl_seconds`=90 с (`config.py:144`) | `ws/presence.py:75,94,103` | `presence.py:50,61` (MGET), `:92,102` |
| `presence:conns:{user_id}` | **zset** conn_id→timestamp | без TTL; чистится `zremrangebyscore` (`presence.py:110-112`) и исчезает пустым | `presence.py:81,101` | `presence.py:80,113` (ZCARD) |

### Идемпотентность и очереди
| ключ | тип | TTL | пишет | читает |
|---|---|---|---|---|
| `idem:msg:{conversation_id}:{client_message_id}` | string (message_id), SET NX | `IDEM_TTL_SECONDS`=86400 (`messages.py:46`) | `messages.py:122,143` | `messages.py:125` |
| `webhooks:avito` | **Redis Stream** | без TTL (растёт/подрезается consumer-группой) | `api/routes/webhooks.py:69` (XADD) | `workers/inbound.py:144` (XREADGROUP, группа `workers` — стр. 37), `:180` (XAUTOCLAIM), `api/routes/health.py:100,104,124` |
| `webhooks:avito:dlq` | Stream | без TTL | `workers/inbound.py:187` — после 5 доставок (`MAX_DELIVERIES`, стр. 39) | никто в коде не читает |
| `ratelimit:webhook:{account_id}:{unix_sec}` | счётчик | 2 с (`webhooks.py:37`) | `webhooks.py:35` | там же |
| `ratelimit:avito:{account_id}:{minute}` | счётчик | `WINDOW_TTL`=120 с (`ratelimit.py:27`) | `ratelimit.py:38` | там же (+`decr` при отказе) |
| `lock:bot:{conversation_id}` | string «1», NX | `LOCK_TTL_SECONDS`=30 с (`bots/runtime.py:51`) | `runtime.py:284` | снимает `:293` |
| `lock:token:{account_id}` | string «1», NX | 30 с (`avito_accounts.py:57`) | `avito_accounts.py:410` | снимает `:` (delete) |
| arq-очередь + `arq:queue:health-check` | arq | внутренние | `workers/main.py:84`, `services/messages.py:84` (`_job_id=deliver:{message_id}`) | воркер |

### Состояние каналов и подключения
| ключ | тип | TTL | пишет | читает |
|---|---|---|---|---|
| `webhook:state:{account_id}` | string (JSON `{status, url}`) | **без TTL** | `avito_accounts.py:137` | `avito_accounts.py:141` |
| `webhook_last:{account_id}` | string (ISO) | **без TTL** (`webhooks.py:75` — `set` без `ex`) | `webhooks.py:75` | `api/routes/avito_connect.py:136` |
| `backfill:{account_id}` | string (offset) | `BACKFILL_PROGRESS_TTL`=7 сут (`avito_accounts.py:63`) | `avito_accounts.py:885` | `avito_accounts.py:806,949` |
| `backfill:failed:{account_id}` | string «1» | 7 сут (стр. 71) | `avito_accounts.py:919` | `avito_accounts.py:950` |
| `oauth_state:{state}` | string (JSON `{user_id, reconnect_account_id}`) | `OAUTH_STATE_TTL_SECONDS`=600 (стр. 965) | `avito_accounts.py:982` | `avito_connect.py:551` (GETDEL) |
| `connect_link:{token}` | string (JSON `{issued_by}`) | `CONNECT_LINK_TTL_SECONDS`=24 ч (стр. 989) | `avito_accounts.py:1013` | `avito_accounts.py:1027` (GETDEL) |
| `notified:reauth:{account_id}` | string «1», NX | `REAUTH_NOTIFY_TTL`=300 с (`deliver.py:55`) | `workers/deliver.py:261` | сам себя |
| `media:{media_id}` | string (JSON метаданных) | `UNATTACHED_TTL_SECONDS`=24 ч (`media.py:62`) | `media.py:377` | `media.py:384` |

### Статистика
| ключ | тип | TTL | пишет | читает |
|---|---|---|---|---|
| `stats:refreshed_at` | string (ISO) | **без TTL** | `scheduler/jobs/stats.py:64` | `services/stats.py:605` |
| `stats:heatmap:{account|all}:{from}:{to}` | string (JSON) | `HEATMAP_TTL_SECONDS`=600 (`stats.py:93`) | `stats.py:906` | `stats.py:889` |
| `stats:my:{user_id}:{YYYY-MM-DD}` | string (JSON) | `MY_TODAY_TTL_SECONDS`=30 (`stats.py:94`) | `stats.py:906` | `stats.py:889` |
| `stats:export:{job_id}` | **hash** (status/url/error) | `EXPORT_URL_TTL_SECONDS`=86400 (`stats.py:1208`) | `stats.py:1340,1364,1653,1674,1685,1689` | `stats.py:1377` |
| `stats:export:active:{user_id}` | string (job_id), NX | `EXPORT_ACTIVE_TTL_SECONDS`=3600 (стр. 1210) | `stats.py:1291` | там же; снимает `:1314` |
| `stats:export:quota:{user_id}:{день МСК}` | счётчик | до полуночи МСК (`seconds_to_msk_midnight`, стр. 1281) | `stats.py:1300` | там же (лимит 20/сут) |

### Живость и служебное
| ключ | тип | TTL | пишет | читает |
|---|---|---|---|---|
| `scheduler:alive` | string (ISO) | 120 с | `scheduler/main.py:52` | `api/routes/health.py:184`, `watchdog.py:445` |
| `watchdog:alive` | string (ISO) | `WATCHDOG_ALIVE_TTL_SECONDS`=1800 (`watchdog.py:111`) | `watchdog.py:703` | в коде читателя нет (внешний мониторинг) |
| `watchdog:backup:last_ok` | string (ISO) | `BACKUP_OK_TTL_SECONDS`=7 сут (`internal.py:53`) | `api/routes/internal.py:240` | `watchdog.py:492` |
| `canary:webhook` | string «1», NX | 60 с | `workers/inbound.py:283` | сам себя (не чаще раза в минуту) |
| `sandbox:{admin_id}:{session_id}` | string (JSON сессии) | `SESSION_TTL_SECONDS`=3600 (`bots/sandbox.py:53`) | `sandbox.py:185` | `sandbox.py:193` |

### Pub/Sub
Единственный канал — **`events`** (`app/ws/events.py:16`). Публикует `publish_event` (`events.py:53`) — единственная точка публикации; дублирующая публикация в тот же канал есть в `services/avito_accounts.py` (`redis.publish("events", …)`). Читает `app/ws/hub.py` в каждом процессе uvicorn. Конверт: `{type, ts, data}` + внутренний `meta{audience, exclude_user, only_user}`, который хаб срезает перед отправкой клиенту (`events.py:47-52`).

---

## 5. МАТЕРИАЛИЗОВАННЫЕ ПРЕДСТАВЛЕНИЯ И ВЫЧИСЛЯЕМЫЕ МЕТРИКИ

### 5.1 `mv_conversation_stats` — единственное MV
Определение: `app/db/migrations/versions/0004_stats_indexes_and_mv.py:108-180`. Гранулярность — строка на диалог, только те, где есть хотя бы одно входящее от клиента (`WHERE f.first_client_at IS NOT NULL`, стр. 120).

Колонки: `conversation_id`, `account_id`, `client_id`, `assignee_id`, `status`, `first_client_at`, `first_operator_at`, `first_operator_user_id`, `first_bot_at`, `frt_operator_sec`, `frt_operator_biz_sec` (через `business_seconds_between`), `frt_bot_sec`, `msgs_in`, `msgs_out_operator`, `msgs_out_bot`, `has_operator_reply`, `closed_at`, `closed_by`.

Устройство: `CROSS JOIN LATERAL` за первым клиентским сообщением + 4 `LEFT JOIN LATERAL` (первый ответ оператора, первый ответ бота, счётчики сообщений, последнее закрытие из `audit_log` по `action='conversation.status_changed' AND details->>'to'='closed'`, стр. 169-179). Провалившиеся отправки (`delivery_status = 'failed'`) исключены везде.

Индексы MV (стр. 182-190): `mv_conversation_stats_pk` UNIQUE по `conversation_id` (**обязателен для CONCURRENTLY**), `_first_client_at`, `_account`, `_operator`, `_closed_at`.

Обновление: `REFRESH MATERIALIZED VIEW CONCURRENTLY` ежечасно — `app/scheduler/jobs/stats.py:50-66`, в режиме AUTOCOMMIT с `statement_timeout`; после успеха пишет `stats:refreshed_at` в Redis (стр. 63-64).

### 5.2 SQL-функции в БД
* `business_seconds_between(t0, t1, work_start interval, work_end interval) → bigint` — `STABLE STRICT`, окно 10-20 Europe/Moscow, 7 дней в неделю. Создана 0004:48-65, переопределена 0023:74-91.
* `stats_work_hour(setting_key text, fallback int) → interval` — `STABLE`, **не** `STRICT`, читает `app_settings`. Создана 0023:63-71. Подставлена в умолчания параметров `business_seconds_between`, поэтому смена рабочих часов из интерфейса действует без миграции (0023:29-38).

### 5.3 Где что считается (Python)
* **`app/services/stats.py`** — весь раздел `/stats`: `parse_period` (стр. 140), питоновский эталон `business_seconds_between` (стр. 192) — вторая реализация тех же рабочих часов, сверяется с SQL-версией тестом; `frt_aggregate` (396, из MV `_FRT_MV_SQL` или live `_FRT_LIVE_SQL`), `snapshot_now` (420, live по `conversations`), `conversations_closed` (449), `bot_closed` (499 — **live из `audit_log`, не из MV**, обоснование в докстринге стр. 30-33), `phones_collected` (541), `repeat_contacts` (587 — из MV), `summary` (625), `timeseries` (794), `heatmap` (853, кэш 600 с), `managers`/`manager_rows` (1009/1059, из MV), `my_today` (1165 — **без MV**, live + кэш 30 с), экспорт CSV/XLSX (1318-1754).
* **`app/services/account_stats.py`** — сводка по каналу за 7 дней (`WINDOW_DAYS = 7`, стр. 53): `total`, `missed` (`NOT has_operator_reply`), `daily[7]`. Считается **по MV** (`_SQL`, стр. 65-75) — то есть отстаёт на час, о чём сказано явно.
* **`app/services/conversation_table.py`** — таблица `/dialogs`: метрики считаются подзапросами по `messages` **только для отрезанной страницы в 50 строк** (докстринг стр. 17-30); сортировка по колонкам-метрикам намеренно запрещена, глубина ограничена `MAX_OFFSET`.
* **`app/services/read_markers.py`** — `unread_count` на чтении: входящие с `created_at` новее маркера, с fallback на колонку `conversations.unread_count`, если маркера нет (докстринг стр. 14-17); один HMGET на страницу + GROUP BY по `messages` с глобальной нижней границей, чтобы отсеклись партиции (стр. 19-23).
* **`app/services/distribution.py`** — нагрузка оператора: `_load_by_user` (стр. 115, `COUNT` активных диалогов по `assignee_id`), `max_active_per_operator` (103), `eligible_candidates` (173), `pick_assignee` (234).
* **`app/services/inbox.py`** — очередь и время ожидания; предикат очереди `queue_condition` обязан совпадать с `IN_QUEUE` из 0007.

---

## 6. РОЛИ И РАЗДЕЛЫ — СВЕРКА С БРИФОМ

**Роли — в коде их ЧЕТЫРЕ, не две.** `app/core/rbac.py:7`: `("admin", "head", "manager", "observer")`. Тот же список закреплён CHECK-ограничением `users_role` (`app/models/user.py:16`) и подписями в интерфейсе (`frontend/src/app/AppLayout.tsx:34-37`: Администратор / Руководитель / Менеджер / Наблюдатель). Права: `ROLE_PERMISSIONS` (`rbac.py:31-58`), каталог из 15 разрешений (`rbac.py:11-28`).

**Разделы — в коде их БОЛЬШЕ, и одно название не совпадает.** `frontend/src/app/router.tsx:60-201`:
* совпадает с брифом: `/chats` (стр. 70), `/dialogs` (94), `/stats` (132), `/settings/accounts` (144), `/settings/bots` (158) + `/settings/bots/:id` (162), `/settings/distribution` (175), `/settings/team` (189), `/settings/profile` (141);
* **вместо `/settings/quick-replies` — `/settings/templates`** (стр. 148). Строк `quick-replies`/`quick_replies` в репозитории нет вовсе;
* **в брифе не названы**: `/login` (61), `/invite/:token` (62), `/chats/:id` (113), `/notifications` (120), `/updates` (102), `/ui-kit` (108), `/settings` → редирект на профиль (140), `*` → 404 (200).

### Есть в коде, но не выведено (13)

* **`clients.avito_rating` (Numeric) — объявлено, читается, но НИКОГДА не заполняется production-кодом**
  * где: объявление app/models/client.py:22; чтение app/services/conversations.py:191-193; вывод в интерфейсе frontend/src/features/chats/components/card/ClientCardPane.tsx:254-256. Записи нет нигде: единственные присваивания — tests/unit/test_conversations_manage.py:38 и tests/load/fill.py:199
  * почему важно: Карточка клиента содержит блок «★ N · на Авито», который в бою не покажется никогда: колонка всегда NULL. Либо поле надо заполнять из адаптера Авито, либо убирать блок из карточки — сейчас это молчаливо мёртвая ветка UI.
* **Таблицы `notifications` и `notification_reads` НЕ регистрируются на `Base.metadata` при импорте `app.models`**
  * где: app/models/__init__.py — файл `app/models/notification.py` не импортирован (в отличие от 15 остальных моделей, стр. 4-18); при этом app/db/migrations/env.py:18 делает ровно `import app.models`. Проверено запуском: `import app.models` даёт 14 таблиц без notifications, `import app.main` — 16
  * почему важно: `alembic revision --autogenerate` увидит в БД две таблицы, которых нет в target_metadata, и сгенерирует `op.drop_table("notifications")` + `op.drop_table("notification_reads")`. В тестах схема создаётся только по счастливой случайности: tests/unit/conftest.py:21 импортирует `app.main`, который тянет роутер уведомлений, который тянет модель — то есть корректность зависит от порядка импортов.
* **`conversation_pins.pinned_at` — пишется server_default'ом, но не читается ничем**
  * где: объявление app/models/conversation_pin.py:48; входит вторым столбцом в индекс `ix_conversation_pins_user` (стр. 39). Сортировка закреплённых сделана через EXISTS 0/1 — app/services/pins.py:48-68 (`pin_order`), время закрепления не участвует
  * почему важно: Порядок между закреплёнными диалогами определяется общей сортировкой списка, а не порядком закрепления. Второй столбец индекса не работает ни на один запрос.
* **`conversation_participants.invited_by_id` — пишется, но не читается**
  * где: запись app/services/participants.py:83 (`invited_by_id=actor.id`); выдача `view()` app/services/participants.py:100-125 возвращает только `id`, `full_name`, `reason`, `invited_at`
  * почему важно: «Кто позвал» сохраняется в БД, но нигде не показывается и не используется в правах — приглашённый не видит, от кого пришёл вызов.
* **`webhook_raw_log.processed` — пишется, но не читается ни одним запросом**
  * где: объявление app/models/webhook_raw.py:33; запись app/workers/inbound.py:207 (через `_mark_raw`, вызовы на стр. 238, 241, 249, 252, 257). Единственный читатель журнала — сторож app/scheduler/jobs/watchdog.py:566-570 — фильтрует по `error IS NOT NULL`, а не по `processed`; уборка app/scheduler/main.py:104 удаляет по `received_at`
  * почему важно: Поле выглядит как признак «разобрано», по которому можно найти застрявшие вебхуки, но такого запроса нет. Флаг платит за себя записью на каждом событии и не окупается.
* **Справочник значений `conversations.outcome` не закреплён в БД: CHECK-ограничения нет, а комментарий в модели ссылается на константу, которой в модели не существует**
  * где: app/models/conversation.py:159 — «СПРАВОЧНИК ЗАКРЫТЫЙ (`OUTCOMES` ниже)», но ниже в файле `OUTCOMES` нет; настоящая константа — app/services/conversations.py:54 (`visit, declined, not_our_profile, spam, no_reply`); проверка только в сервисе (app/services/conversations.py:833) и в Literal схемы (app/api/routes/conversations.py:78); миграция 0020_conversation_outcome.py CHECK не создаёт
  * почему важно: Любая запись мимо сервиса (скрипт, ручной SQL, будущая ручка) положит в колонку произвольную строку, и сводка по результатам обращений разъедется — ровно то, чем миграция обосновывала закрытый справочник.
* **`prune_marker_hash` — функция подрезки хэша read-маркеров, не вызываемая ниоткуда, кроме теста**
  * где: app/services/read_markers.py:241-258; докстринг обещает «Вызывается по желанию (крон/скрипт)», но ни в app/scheduler/main.py:133-179 (build_scheduler), ни в app/cli.py вызова нет. Единственный вызов — tests/unit/test_read_markers.py:221
  * почему важно: Хэш `read:{user_id}` растёт без ограничения в пределах своего 90-дневного TTL: у оператора с тысячами диалогов за квартал это тысячи полей, читаемых HMGET на каждой странице списка. Средство борьбы написано и не подключено.
* **`clear_marker`, `marker_expires_at`, `access_revoked_at` — публичные функции без единого production-вызова**
  * где: app/services/read_markers.py:123 (`clear_marker` — только tests/unit/test_read_markers.py:203), app/services/read_markers.py:261 (`marker_expires_at` — вызовов нет вообще), app/services/sessions.py:108 (`access_revoked_at` — вызовов нет вообще, докстринг честно говорит «Для проверок и тестов»)
  * почему важно: Сценарий «пометить диалог непрочитанным» реализован на сервере, но в интерфейс не выведен — ручки, которая звала бы `clear_marker`, нет.
* **Поток `webhooks:avito:dlq` наполняется, но не читается ничем в коде**
  * где: запись app/workers/inbound.py:187-190 (после `MAX_DELIVERIES = 5`, стр. 39); чтения нет — health-ручка смотрит только основной поток (app/api/routes/health.py:100-124), сторож watchdog.py DLQ не проверяет
  * почему важно: Вебхук, провалившийся пять раз, уезжает в очередь, о которой никто не узнает: ни уведомления, ни счётчика в /health. Потерянные сообщения клиентов молча копятся в Redis без TTL.
* **`conversations.channel` и `clients.channel` — многоканальность объявлена, но захардкожена в 'avito' на каждом пути**
  * где: объявления app/models/conversation.py:33 и app/models/client.py:18; все записи и выборки — константа: app/services/inbound.py:116,122,157,170; app/services/avito_accounts.py:583,586,598,604. Фильтра по каналу нет ни в списке диалогов, ни в таблице /dialogs, ни в API
  * почему важно: Колонка входит в уникальные ключи (`uq_clients_channel_external_id`, `uq_conversations_channel_external_chat_id`) и в каждый индекс-предикат, но различающего значения не принимает. Это задел, а не работающая фича.
* **`message_idempotency` растёт без чистки — ночное задание обещано в докстринге и не написано**
  * где: app/models/message_idempotency.py:15-17 («Чистить их можно ночным заданием по `created_at`, когда таблица вырастет»); в app/scheduler/main.py:133-179 такого джоба нет, индекса по `created_at` в 0021_message_idempotency.py тоже нет
  * почему важно: Строка пишется на каждое исходящее сообщение и не удаляется никогда. Когда чистку понадобится добавить, `DELETE ... WHERE created_at < …` пойдёт seq scan'ом по всей таблице.
* **Дефолтный бот «Первичный приём» вставляется миграцией с фиксированным UUID**
  * где: app/db/migrations/versions/0005_bots_engine.py:59-60 (`DEFAULT_BOT_ID = "b07f0001-0000-4000-8000-000000000001"`), вставка стр. 207-219 с условием `WHERE NOT EXISTS (SELECT 1 FROM bots)`, `is_enabled = false`
  * почему важно: Данные заводятся схемной миграцией, а не сидом. Сценарий бота продублирован в двух местах — 0005_bots_engine.py:120-188 и frontend/src/features/settings/bots/defaultScenario.ts:63-105, — и разъехаться им теперь есть на чём.
* **Рабочие часы для статистики реализованы дважды: SQL-функция и питоновский эталон**
  * где: SQL — app/db/migrations/versions/0023_work_hours_setting.py:74-91 (читает `app_settings` через `stats_work_hour`); Python — app/services/stats.py:192 (`business_seconds_between`), константы WORK_START/WORK_END берутся из app/services/stats.py:85-86 как `SPECS[...].default`, то есть 10 и 20 ЖЁСТКО, без чтения таблицы настроек
  * почему важно: После смены рабочих часов из интерфейса SQL-версия начнёт считать по новым, а питоновская останется на умолчаниях. Сверочный тест двух реализаций при изменённой настройке разойдётся, а метрики из разных источников перестанут сходиться.

### Расхождения с брифом и чего не нашёл

## РАСХОЖДЕНИЯ С БРИФОМ

**1. Роли: бриф называет две, в коде их четыре.**
Бриф: «Администратор, Менеджер». Код: `app/core/rbac.py:7` — `("admin", "head", "manager", "observer")`. Это не догадка: тот же список закреплён CHECK-ограничением на уровне БД (`app/models/user.py:16` — `role IN ('admin','head','manager','observer')`, миграция `0001_init.py:47-49`), у каждой роли свой набор прав (`app/core/rbac.py:31-58`), и у всех четырёх есть русские подписи в интерфейсе (`frontend/src/app/AppLayout.tsx:34-37`, `frontend/src/features/settings/profile/ProfilePage.tsx:15-18`). Роль `head` («Руководитель») — не декоративная: под неё заведена аудитория уведомлений (`app/models/notification.py:49` — `AUDIENCES = ("admin", "head")`) и отдельная ветка гардов маршрутов (`frontend/src/app/router.tsx:186`). `observer` («Наблюдатель») имеет ровно одно право — `conversations:read`.

**2. Раздел `/settings/quick-replies` в коде отсутствует — он называется `/settings/templates`.**
`frontend/src/app/router.tsx:148`; таблица — `templates` (`app/models/template.py:12`), ручки — `app/api/routes/templates.py`. Строк `quick-replies`, `quick_replies`, `quickReplies` в репозитории нет ни в одном файле (проверено grep'ом по `frontend/src` и `app/`).

**3. Бриф перечисляет 4 раздела верхнего уровня, в коде их 7 плюс служебные.**
Сверх брифа: `/notifications` — центр уведомлений (`router.tsx:120`, за ним две таблицы БД и целый сервис `app/services/notifications.py`), `/updates` — «что нового» (`router.tsx:102`), `/ui-kit` — витрина компонентов (`router.tsx:108`), `/chats/:id` — deep-link (`router.tsx:113`), `/login` и `/invite/:token` вне гарда (`router.tsx:61-62`), `/settings` → редирект на профиль (`router.tsx:140`), `*` → 404 (`router.tsx:200`). Раздел уведомлений — самый заметный пропуск брифа: под него отведены таблицы `notifications`/`notification_reads`, 4 CHECK-ограничения, 4 индекса и ежесуточная уборка (`app/scheduler/main.py:111-128`).

---

## ЧЕГО НЕ НАШЁЛ

* **Партиционированных таблиц, кроме `messages`, нет.** Проверил все 16 моделей и все 23 миграции: `PARTITION BY` встречается ровно один раз — `0001_init.py:226`. `message_idempotency` не партиционирована намеренно и об этом сказано прямо (`app/models/message_idempotency.py:8-13`).
* **Материализованное представление ровно одно** — `mv_conversation_stats`. Обычных VIEW в миграциях не нашёл вовсе.
* **Триггеров, правил (RULE), row-level security в миграциях не нашёл.** Единственная серверная логика в БД — две SQL-функции (`business_seconds_between`, `stats_work_hour`) и одна generated-колонка (`messages.search`).
* **Не нашёл индекса на `templates`** сверх PK и FK по `owner_id`: выборка «мои + общие» и `SELECT DISTINCT folder` (`app/api/routes/templates.py:148-155`) идут без поддержки. На текущем объёме это ничего не стоит, отмечаю как факт, а не как проблему.
* **Не нашёл чтения ключа `watchdog:alive`** внутри проекта (`app/scheduler/jobs/watchdog.py:703` только пишет) — по смыслу его читает внешний мониторинг, но конфигурации этого мониторинга в срезе «данные» я не проверял.
* **Не нашёл в коде вызова `presence` со значением, отличным от `online`/`away`** — третьего состояния нет, «не в сети» выражено отсутствием ключа (`app/ws/presence.py:26-30`), и это зафиксировано явно.
* **Границы TTL для ключей `webhook:state:{account_id}`, `webhook_last:{account_id}` и `stats:refreshed_at` не нашёл** — эти три `set` вызываются без `ex` (`app/services/avito_accounts.py:137`, `app/api/routes/webhooks.py:75`, `app/scheduler/jobs/stats.py:64`). Живут вечно; для первых двух это ограничено числом каналов (девять), третий — один ключ.
* **Не проверял** содержимое `deploy/`, `docker/`, `desktop/`, `fake_avito/` и `tests/load/` — они за границей среза «данные»; `tests/load/fill.py:199` упомянут только как единственное место, где заполняется `avito_rating`.
* **Не выполнял** миграции и не подключался к живой БД — вся картина собрана чтением исходников. Фактическое состояние схемы на проде (<IP прода>) может отличаться, если миграции применены не до 0023.

## Срез: Тексты и терминология

## 0. Что осмотрено

Корень интерфейса — `frontend/src`.
Всего 224 файла в `src`, из них 116 `.tsx`/`.ts` вне `src/test`, 15 959 строк `.tsx` (без тестов), 24 файла `.css`.
Точка входа: `src/main.tsx` → `src/app/App.tsx` → `src/app/providers.tsx` + `src/app/router.tsx`.

---

## 1. ПЕРЕИСПОЛЬЗУЕМЫЕ КОМПОНЕНТЫ

### 1.1 `src/shared/ui` — 8 модулей

| Модуль | Что делает | Где используется (файл:строка импорта) |
|---|---|---|
| `EmptyState.tsx:124` | Пустое/ошибочное состояние: иллюстрация 88px (8 вариантов `Illustration`, 3 тона), заголовок, описание, слот действия, опциональный `live: "alert"\|"status"` | 11 файлов, 25 мест: `features/settings/bots/BotsPage.tsx:7,116,129`; `bots/BotEditor.tsx:23,188`; `settings/accounts/AccountsPage.tsx:22,652,662`; `settings/team/TeamMembersTab.tsx:24,273,287`; `team/AuditLogTab.tsx:6,199,210`; `team/TeamPage.tsx:6,51`; `table/TablePage.tsx:10,450,460`; `templates/TemplatesManager.tsx:7,251,261`; `uikit/UiKitPage.tsx:17,419`; `chats/components/thread/ChatThreadPane.tsx:10,50,438,448`; `chats/components/list/ChatListPane.tsx:15,90,113,125,127,130,141,587,600` |
| `FullscreenLoader.tsx:5` | Логотип + спиннер на 100dvh, показывается на bootstrap-рефреше | `app/router.tsx:13,30`; `features/auth/LoginPage.tsx:17,73` |
| `Icon.tsx` | 41 экспортированная иконка (сетка 24, обводка 2, `currentColor`, `aria-hidden`), общий `Svg` на строке 24 | 20 файлов; полная карта — в разделе 2.2 |
| `LogoMark.tsx:8` | Фирменный знак LP на `--lc-brand` | `app/AppLayout.tsx:23,213`; `auth/LoginPage.tsx:18,141`; `auth/InvitePage.tsx:19,56,163`; `shared/ui/FullscreenLoader.tsx:2,13` |
| `PageHeader.tsx:24` | Шапка страницы: `h1` + описание + слот действий; CSS — `page-header.css` | 7 страниц: `bots/BotsPage.tsx:13,97`; `distribution/DistributionTab.tsx:8,117`; `team/TeamPage.tsx:7,47`; `profile/ProfilePage.tsx:12,136`; `settings/templates/TemplatesPage.tsx:1,14`; `accounts/AccountsPage.tsx:31,608`; `notifications/NotificationsPage.tsx:34,185` |
| `StatusDot.tsx:23` | Точка состояния, 5 тонов `DotTone` (`online/offline/away/danger/muted`), `label` для скринридера | `accounts/AccountsPage.tsx:29,327`; `team/TeamMembersTab.tsx:23,382`; `presence/PresenceMenuItems.tsx:2,30,39` |
| `UserAvatar.tsx:16` | Аватар сотрудника: инициалы, цвет из `avatarColorIndex(name)`, класс `.lc-avatar` | `app/AppLayout.tsx:24,142`; `profile/ProfilePage.tsx:6,65`; `accounts/AccountsPage.tsx:23,128` |
| `toast.tsx` | Единая точка тостов: `showToast` (78), `showUndoToast` (131), объект `toast` (161) с `success/info/warning/error/errorPersistent/hide` | `showToast` — 16 файлов; `toast.*` — 20 файлов; `showUndoToast` — только `chats/inbox/useInbox.ts:27,212` |

### 1.2 `src/features/*/components` — 39 файлов

**`features/chats/components/card/` (5)**
- `ClientCardPane.tsx:194` — правая колонка. Внутренние приватные подкомпоненты: `Section:53`, `ExternalLink:83`, `usePhoneHighlight:105`, `NotesSection:119`. Используется: `chats/ChatsPage.tsx:7,49,59,61`.
- `PeoplePicker.tsx:28` — общий выбор сотрудника (поиск + список с точками онлайна). Используется двумя окнами: `InviteDialog.tsx:4,67`, `TransferDialog.tsx:4,57`.
- `TransferDialog.tsx:16` — «Передать». Вызывается из `ChatListPane.tsx:21,679`, `ChatThreadPane.tsx:24,489`, `ClientCardPane.tsx:34,496`, `hooks/useConversationActions.ts`.
- `InviteDialog.tsx:20` — «Позвать коллегу». Вызывается из `ChatThreadPane.tsx:23,495`, `ClientCardPane.tsx:33,502`.
- `BlockClientButton.tsx:28` (экспорт называется `BlockClientDialog`, имя файла с ним не совпадает) — пометка «нежелательный». Вызывается из `ChatThreadPane.tsx:25,502`.

**`features/chats/components/composer/` (3)**
- `Composer.tsx:73` — поле ввода, вложения, режим «Сообщение/Заметка», счётчик, баннеры ошибок. Из `ThreadFooter.tsx:7,54,61,71`.
- `TemplatePickerPopover.tsx:48` — пикер быстрых ответов (`/` или ⚡). Из `Composer.tsx:14,476`.
- `ThreadFooter.tsx:22` — выбор низа панели по роли/очереди. Из `ChatThreadPane.tsx:14,483`.

**`features/chats/components/list/` (4)**
- `ChatListPane.tsx:150` — левая колонка: вкладки, поиск, фильтры, виртуализация. Приватные: `ListSkeleton:70`, `EmptyInbox:88`, `EmptyByTab:98`. Из `ChatsPage.tsx:8,56`.
- `ConversationListItem.tsx:61` — карточка диалога. Приватные: `previewText:11`, `urgency:34`. Из `ChatListPane.tsx:22,629` и `uikit/UiKitPage.tsx:20,310-349`.
- `ClientAvatar.tsx:8` — аватар клиента. Из `ConversationListItem.tsx:6,140`, `ChatThreadPane.tsx:19,300`, `MessageBubble.tsx:5,168`.
- `WaitGauge.tsx:40` — ячейка времени/шкала ожидания. Из `ConversationListItem.tsx:59,234`. Единственный потребитель.

**`features/chats/components/thread/` (5)**
- `ChatThreadPane.tsx:525` (+ приватные `ThreadSkeleton:30`, `EmptyThreadPlaceholder:47`, `ThreadView:61`). Из `ChatsPage.tsx:9,48,57`.
- `MessageBubble.tsx:103` (+ `DeliveryStatusIcon:31`, `Attachments:64`, `kindOf:9`, `formatSize:16`). Из `ChatThreadPane.tsx:15,462` и `UiKitPage.tsx:21,355-391`.
- `ThreadActions.tsx:28` — 5 иконочных действий шапки. Из `ChatThreadPane.tsx:26,372`.
- `TransferBar.tsx:29` — полоса предложенной передачи. Из `ThreadFooter.tsx:5,38`.
- `OutcomeModal.tsx:25` — «чем закончилось». Из `ChatThreadPane.tsx:27,511`.

**`features/settings/bots/components/` (15)**
- `StepCard.tsx:15` — карточка шага сценария. Из `BotEditor.tsx:283`.
- `ScheduleEditor.tsx:14` — расписание бота. Из `BotEditor.tsx:250`.
- `StepRefSelect.tsx:13` (+ `DEFAULT_REF_VALUE:10`, `DEFAULT_REF_LABEL:11`) — переход на шаг. Используется 7 формами шагов.
- `IntInput.tsx:17` — целое число без Mantine `NumberInput`. Из `AiAnswerForm`, `AskForm`, `MenuForm`.
- `VariableTextarea.tsx:12` — текст с вставкой `{переменных}`. Из 6 форм шагов.
- `StepForms/index.tsx:16` (`StepForm`) — диспетчер по типу шага, из `StepCard.tsx:6,177`; и 9 форм: `SendForm`, `AskForm`, `MenuForm`, `ConditionForm`, `AiAnswerForm`, `HandoffForm`, `CloseForm`, `TagForm`, `NoteForm` + `types.ts`. Каждая используется ровно из `index.tsx`.

**`features/stats/components/` (7)**
- `StatCard.tsx:27` — карточка-метрика. Из `SummaryCards.tsx:3` (8 вызовов).
- `SummaryCards.tsx:26` — три группы карточек. Из `StatsPage.tsx:15,168`.
- `MetricChart.tsx:95` (+ экспорт `PLOT_W:27`, `labelWidth:48`, `labelIndexes:65` — только для тестов). Из `StatsPage.tsx:13,172`.
- `Heatmap.tsx:12` — 7×24 CSS-grid. Из `StatsPage.tsx:11,183`.
- `ManagersTable.tsx:103` — таблица по менеджерам. Из `StatsPage.tsx:12,192`.
- `StatsFilters.tsx:21` — шапка фильтров. Из `StatsPage.tsx:14,125`.
- `ExportModal.tsx:19` — выгрузка. Из `StatsPage.tsx:10,205`.

### 1.3 Переиспользуемые компоненты ВНЕ этих двух каталогов

Их шесть, и в системе они не значатся:
- `features/templates/TemplatesManager.tsx:146` — используется на ДВУХ экранах: `settings/templates/TemplatesPage.tsx:2,17` и `settings/profile/ProfilePage.tsx:3,123` (`personalOnly`).
- `features/settings/distribution/WorkHoursBlock.tsx:34` — вложен в `DistributionTab.tsx:6,200`.
- `features/notifications/NotificationPanel.tsx:66` + `NotificationRow.tsx:17` — из `NotificationBell.tsx:3,46`.
- `features/stats/MyTodayWidget.tsx:61` и `MyTodayMenuItem:119` — из `ChatListPane.tsx:8,659` и `AppLayout.tsx:12,163`.
- `features/presence/PresenceMenuItems.tsx:20` — из `AppLayout.tsx:31,162`.
- `features/chats/inbox/InboxDecisionBar.tsx:14` + `InboxClaimedBanner:78` — из `ThreadFooter.tsx:4,44,60`.
- `features/settings/team/OneTimeLinkModal.tsx:13` — из `TeamMembersTab.tsx` и `NotificationsPage.tsx:352`.

---

## 2. ОБЪЯВЛЕНО И НЕ ИСПОЛЬЗУЕТСЯ

### 2.1 Компоненты
**Ни одного React-компонента без вызывающего кода не нашёл.** Все 116 экспортов имеют потребителя в проде.

Оговорки:
- `UiKitPage` (`features/uikit/UiKitPage.tsx:126`) вызывается только из `router.tsx:110` по адресу `/ui-kit`, которого НЕТ ни в рельсе (`AppLayout.tsx:243-311`), ни в меню пользователя (`AppLayout.tsx:154-176`), ни в настройках. Попасть — только по прямой ссылке.
- `ConversationListItem` и `MessageBubble` из прода вызываются, но часть их состояний (эскалация, бот, «негатив») отрисовывается только в UI Kit.

### 2.2 Иконки (`shared/ui/Icon.tsx`)
**8 из 41 не используются нигде, включая UI Kit-каталог (он рендерит их через `Object.entries(Icons)` на `UiKitPage.tsx:427`, то есть «использование» там не адресное):**
`IconFilter:147`, `IconArrowDown:171`, `IconSmile:193`, `IconMic:202`, `IconSparkles:216`, `IconPlus:289`, `IconMail:304`, `IconEdit:318`.
**1 иконка используется только в UI Kit:** `IconArchive:257` (`UiKitPage.tsx:281`).
**1 используется только внутри `toast.tsx`:** `IconInfo:76`.

### 2.3 Токены (детально в разделе 4)
48 из 236 объявленных токенов не имеют ни одной ссылки нигде, включая сам `lc-vars.css`.

### 2.4 API тостов
- `toast.errorPersistent` (`toast.tsx:167`) — единственный вызов в проде отсутствует; вызывается только из `UiKitPage.tsx:298`.
- `toast.info` (`toast.tsx:163`) — только `UiKitPage.tsx:292`.
- `toast.warning` (`toast.tsx:164`) — только `UiKitPage.tsx:295`.
- `toast.hide` (`toast.tsx:169`) — ноль вызовов вообще.

### 2.5 Мёртвые CSS-классы
- `.lc-card` (`app/lc-base.css:356`) и `.lc-card--interactive` (`:364`, с `:hover` на 372 и `prefers-reduced-motion` на 379) — «базовая форма карточки из брифа», ни один компонент её не носит.
- `.conv-card__time` (`chat-list.css:350`) — заменена `WaitGauge`, осталась.
- `.conv-card__chip--wait` (`chat-list.css:402` и `:545`) — `chip.tone` в `ConversationListItem.tsx:103-122` принимает только `"danger"`.
- `.thread-status` (`chat-thread.css:122`), `.thread-status__dot` (`:134`), `.thread-status[data-status=…]` (`:141,146,151`), `.thread-status-select` (`:525`), `.thread-header__actions` (`:103`), `.thread-composer-stub` (`:515`) — остатки убранного из шапки селекта статусов (см. комментарий `ChatThreadPane.tsx:385-390`).
- `.msg-typing`, `.msg-typing__bubble`, `.msg-typing__dot`, `@keyframes msg-typing-bounce` (`chat-thread.css:440-473`, плюс `:586`) — разметка есть ТОЛЬКО в `UiKitPage.tsx:394-400`.
- `.wizard__steps` (`connect-wizard.css:6,22,36`), `.wizard__step-body` (`:29`), `.wizard__foot--split` (`:60`), `.wizard__alt` (`:68`) — остатки многошагового мастера, который свёлся к одному экрану.
- `.accounts-page__foot` (`accounts.css:148`), `.accounts-page__link` (`:154,166,170`).
- `.audit__empty` (`team.css:110`).

---

## 3. ДУБЛИРОВАНИЕ

### 3.1 Два аватара — один и тот же компонент дважды
`shared/ui/UserAvatar.tsx:16` и `features/chats/components/list/ClientAvatar.tsx:8` — побайтово одна логика (инициалы + `var(--lc-avatar-N-bg/ink)`, `fontSize: size*0.35`), отличаются только источником хеша и классом. Хеш-функция одна и та же: `shared/lib/clientColor.ts:19` — `clientColorIndex = avatarColorIndex` (прямой алиас). CSS тоже продублирован: `.lc-avatar` (`lc-base.css:484`) и `.client-avatar` (`chat-list.css:280`) — одинаковые правила, разница в одной строке `user-select: none`.

### 3.2 Три реализации `plural()` с РАЗНЫМИ правилами
- Канон: `shared/lib/plural.ts:6` — `mod10>=2&&mod10<=4 && (mod100<10 || mod100>=20)`.
- Копия: `features/settings/accounts/WeekBars.tsx:34` — `(mod100 < 12 || mod100 > 14)`. **Правило другое**, то есть на числах 112–114 и 12–14 копии расходятся.
- Копия: `platform/tauri/notifier.ts:114`.
- Четвёртая, специализированная: `features/stats/lib/heatmap.ts:60` (`pluralIncoming`).
Канон при этом импортируют только `features/notifications/time.ts:2` и `shared/api/rateLimit.ts:1`.

### 3.3 Четыре объявления `ROLE_LABELS`
- `features/settings/team/roles.ts:5` (канон, вместе с `ROLE_HINTS:12`, `ROLE_ORDER:19`, `ROLE_OPTIONS:21`, `canAnswer:31`).
- `app/AppLayout.tsx:33` — свой Record с теми же значениями.
- `features/settings/profile/ProfilePage.tsx:14` — свой Record + **свой `ROLE_HINTS:21` с ДРУГИМИ текстами**, чем `roles.ts:12`.
- `features/chats/components/card/PeoplePicker.tsx:7` — свой Record со строчной буквы.
Канон импортируют только `AssignOperatorsModal.tsx:4`, `InviteModal.tsx:9`, `TeamMembersTab.tsx:21`.

### 3.4 Два `STATUS_OPTIONS` и два `STATUS_LABEL`, четыре `NEGATIVE_TAG`
- `ChatListPane.tsx:57` («Новые/В работе/Закрытые») vs `ClientCardPane.tsx:38` («Новый/В работе/Закрыт»).
- `AccountsPage.tsx:42` (статусы канала) vs `TablePage.tsx:45` (статусы диалога) — одно имя, разный смысл.
- `NEGATIVE_TAG = "негатив"` объявлен трижды: `ConversationListItem.tsx:9`, `ClientCardPane.tsx:44`, `shared/realtime/applyWsEvent.ts:39`.

### 3.5 Пустые состояния — три способа
1. `EmptyState` — 25 мест (канон).
2. Голый `<Text>` без иллюстрации и действия: `ClientCardPane.tsx:221` («Выберите диалог»), `:143` («Заметок пока нет»), `:470` («Первое обращение»); `ManagersTable.tsx:153`; `MetricChart.tsx:339`; `NotificationPanel.tsx:94`; `TemplatePickerPopover.tsx:146`; `PeoplePicker.tsx:81`; `WeekBars.tsx:57`; `UnloadQueueDialog.tsx:67`.
3. Свои блоки-обёртки: `.lc-journal__empty` (`NotificationsPage.tsx:298`), `.stats-error` (`ManagersTable.tsx:132`, `Heatmap.tsx:49`, `MetricChart.tsx:202`, `StatsPage.tsx:144`), `.tpl-popover__state`, `.transfer-list__state`.

### 3.6 Скелетоны — девять реализаций
- Mantine `<Skeleton>`: `BotEditor.tsx:177-180`, `BotsPage.tsx:109-111`, `DistributionTab.tsx:89-92`, `InvitePage.tsx:107-111`.
- Свои CSS: `.conv-skeleton` (`ChatListPane.tsx:70-84`), `.thread-skeleton` (`ChatThreadPane.tsx:30-43`), `.card-skeleton` (`ClientCardPane.tsx:231-235`), `.audit__skeleton` (`AuditLogTab.tsx:193` + `TeamMembersTab.tsx:267`), `.tpl-manager__skeleton` (`TemplatesManager.tsx:245`), `.lc-journal__skeleton` (`NotificationsPage.tsx:283`), `.stats-skeleton` с 5 модификаторами (`stats.css:502-524`), `.account-card--skeleton` (`AccountsPage.tsx:648`).
- Плюс просто спиннер вместо скелетона: `TablePage.tsx:446`, `MyChannelsBlock.tsx:49`, `PeoplePicker.tsx:67`, `TemplatePickerPopover.tsx:132`, `UnloadQueueDialog.tsx:61`, `ClientCardPane.tsx:464`, `AccountsPage.tsx:686`.
Анимация при этом одна на всех (`lc-base.css:506 @keyframes lc-skeleton-pulse`) — то есть свели только пульс, но не разметку.

### 3.7 Таблицы — четыре варианта
- Mantine `<Table>`: `TablePage.tsx:472`, `BotsPage.tsx:142`, `HotkeysModal.tsx:23`.
- Свой `<table className="audit__table">`: `AuditLogTab.tsx:214`, `TeamMembersTab.tsx:298`.
- Свой `<table className="managers lc-table--cards">`: `ManagersTable.tsx:162`.
- Свои `<table className="tpl-table">` (`TemplatesManager.tsx:271`) и `<table className="lc-journal__table">` (`NotificationsPage.tsx:306`).
Мобильный «карточный» режим `lc-table--cards` (`app/lc-table-cards.css`) подключён только к трём из шести.

### 3.8 Пагинация — три копии, две подписи
- `TablePage.tsx:523-545` — «Назад» / «**Дальше**».
- `AuditLogTab.tsx:232-254` — «Назад» / «**Вперёд**».
- `NotificationsPage.tsx:326-348` — «Назад» / «**Вперёд**».
Разметка вида `{offset+1}–{…} из {total}` продублирована во всех трёх.

### 3.9 Вкладки — три реализации
- `.lc-tabs` / `.lc-tabs__tab` (`lc-base.css:527-574`) — `TeamPage.tsx:54`, `ProfilePage.tsx:138`.
- `.chat-tabs` / `.chat-tabs__tab` (`chat-list.css:100-116`, пилюли) — `ChatListPane.tsx:455`.
- `.tpl-manager__tabs` (`templates.css`) — `TemplatesManager.tsx:191`.

### 3.10 Заголовок страницы — ПЯТЬ разных
- `PageHeader` → `.page-header__title` = `var(--lc-fz-page)` = **28px** (`page-header.css:27`, `lc-vars.css:159`).
- `TablePage.tsx:316` — `<Title order={1} fz={24}>`.
- `LoginPage.tsx:142` — `<Title order={1} fz={24}>`.
- `UpdatesPage.tsx:45` — `<Title order={1} fz={24}>`.
- `router.tsx:47` (404) — `<Title order={1} fz={24}>`.
- `StatsPage.tsx:122` — `<Title order={1} fz={20}>`.
- `NotificationsPage.tsx:198` — `<Title order={1} className="page-header__title">`, то есть класс шапки без самого компонента (при том что `PageHeader` в этом файле импортирован на строке 34 и применён на 185 — в другой ветке).
При этом doc-комментарий `PageHeader.tsx:17` утверждает: «КАНОН: `h1`, **20px**, вес 600» — а код даёт 28px. Комментарий и код разошлись.

### 3.11 Заголовок секции — три способа
- `<Title order={2} fz={16}>` — `AppearanceBlock.tsx:33`, `HotkeysBlock.tsx:22`, `MyChannelsBlock.tsx:40`, `AboutAppBlock.tsx:54`, `BotEditor.tsx:270`.
- `<Text fz="var(--lc-fz-section)" fw={600}>` (не заголовок семантически) — `WorkHoursBlock.tsx:72`.
- `<Text component="h2" className="stats-panel__title">` — `ManagersTable.tsx:118`, `Heatmap.tsx:32`, `MetricChart.tsx:150`; `<h2 className="stats-group__title">` — `SummaryCards.tsx:38,58,85`; `<h2 className="uikit__h2">` — `UiKitPage.tsx:87`.

### 3.12 Подтверждение действия — три механизма
- Нативный `window.confirm`: `AccountsPage.tsx:498` (отключить канал), `:533` (удалить канал), `BotsPage.tsx:223` (удалить бота).
- Mantine `<Modal>` с парой «Отмена/Удалить»: `StepCard.tsx:193`, `TemplatesManager.tsx:356`, `TeamMembersTab` (pending-состояние).
- Модалка без вопроса, сразу с объяснением: `BlockClientButton.tsx:65,83`.

### 3.13 Подвал модалки — три вёрстки
`<Group justify="flex-end">` (`TransferDialog.tsx:80`, `InviteDialog.tsx:91`, `TemplatesManager.tsx:365`, `UnloadQueueDialog.tsx:94`, `ExportModal.tsx:94`, `StepCard.tsx:205`) vs `<div className="card-blocked__actions">` (`BlockClientButton.tsx:70,100`) vs `<div className="wizard__foot">` (`ConnectChannelWizard.tsx:223`) vs `<Group justify="space-between">` (`OneTimeLinkModal.tsx:46`).

### 3.14 Расчёт «клиент ждёт» — два независимых
- Канон `shared/lib/waiting.ts` (`waitingSince:45`, `waitingForRow:67`, пороги `WAIT_WARN_MIN=5`, `WAIT_LATE_MIN=15`) — использует `ConversationListItem.tsx:94` и `WaitGauge.tsx`.
- Своя копия внутри `ChatThreadPane.tsx:159-171`: собственный `useMemo`, свой перевод в «мин/ч/дн», свой порог `minutes >= 15` литералом. Порог 15 продублирован ещё и в `TablePage.tsx:43` (там, правда, честно взят из токена `WAIT_LATE_MIN * 60`).

### 3.15 Точка «в сети» — три реализации
`StatusDot` (`shared/ui/StatusDot.tsx`) vs `.transfer-row__dot` (`PeoplePicker.tsx:96`) vs `.lc-conn__dot` (`AppLayout.tsx:71`).

### 3.16 Числовое поле — два
`IntInput` (`bots/components/IntInput.tsx:17`, свой, ради бандла) vs Mantine `NumberInput` (`DistributionTab.tsx:147`, `WorkHoursBlock.tsx:81,91`, `OutcomeModal.tsx:77`, `UnloadQueueDialog.tsx:49`).

### 3.17 Скрытый для глаз текст — два
Mantine `<VisuallyHidden>` (`AccountsPage.tsx:137`, `NotificationPanel.tsx:53`) vs свой класс `.lc-visually-hidden` (`TeamMembersTab.tsx:315`, `ManagersTable.tsx:198`).

### 3.18 Значок сортировки — два
`<IconChevronDown>` с `rotate(180deg)` (`TablePage.tsx:490`) vs текстовые `▲/▼` (`ManagersTable.tsx:188`).

---

## 4. ТОКЕНЫ ДИЗАЙНА

### 4.1 Группы в `src/app/lc-vars.css` — 236 объявлений

| § | Группа | Строки | Состав |
|---|---|---|---|
| §1.1 | Нейтральная шкала Slate | 41–50 | 10 |
| §1.2 | Тёмные поверхности ink | 54–59 | 6 |
| §1.3–1.7 | Цветные шкалы: blue 6, green 6, red 6, amber 6, purple 5 | 62–98 | 29 |
| §1.8 | Бренд | 102–103 | 2 |
| §2.1 | Сетка 8pt `--lc-space-0…9` | 115–124 | 10 |
| §2.2 | Радиусы `xs/sm/md/lg/xl/pill` | 127–132 | 6 |
| §2.3 | Колонки: `col-list`, `col-card`, `gutter`, `header-h`, `rail-w` | 135–139 | 5 |
| §2.4 | Типографика: `font`, `font-mono`, `fz-page/section/card/body/caption`, `fw-regular/medium/semibold`, `lh-body/heading`, `num` | 148–176 | 14 |
| §2.5 | Движение: `dur-fast/dropdown/base`, `ease/ease-out/ease-spring`, `motion-fast/base/dropdown` | 179–190 | 9 |
| §2.6 | Прозрачности | 193–195 | 3 |
| §2.7 | Слои z-index | 199–205 | 7 |
| §3 | Тёмная тема: сырые имена (`void, bg-app/panel/raise/selected, line*, text-1…6, empty-art, brand*, warn*, danger*, bot*, bubble-in*, note*, row-time-w`) + семантические алиасы (поверхности, текст, primary, success, danger, warning, info, accent, тени, пузыри, статусы, присутствие, непрочитанные, графики/тепло) | 233–510 | ~135 имён |
| §4 | Светлая тема — те же имена, другие значения | 521–674 | ~100 переопределений |
| §5 | Палитра аватаров, 8 пар × 2 темы | 686–721 | 16 имён |
| §6 | База документа: `body`, фокус-кольцо, скроллбар, `prefers-reduced-motion` | 725–793 | — |

### 4.2 Объявлено и НЕ используется вообще (48 имён, ни одной ссылки нигде, включая сам файл)

`--lc-void` (233), `--lc-ink-400/500/700/800/900` (54–59), `--lc-slate-800` (49), `--lc-red-300/400/500` (78–80), `--lc-bot`, `--lc-bot-text`, `--lc-bot-bg` (278–280), `--lc-brand-dark` (103), `--lc-brand-bg` (261), `--lc-bubble-in-line`, `--lc-bubble-in-hot` (283–284), `--lc-note-bg`, `--lc-note-text`, `--lc-note-meta` (285–287), `--lc-line-soft` (239), `--lc-on-primary` (365), `--lc-on-success` (383), `--lc-primary-hover` (366), `--lc-primary-solid` (359), `--lc-primary-solid-hover` (360), `--lc-primary-solid-active` (361), `--lc-success-solid` (381), `--lc-success-solid-hover` (382), `--lc-danger-solid-hover` (388), `--lc-chart-2/3/4` (502–504), `--lc-space-0` (115), `--lc-space-7` (121), `--lc-radius-xl` (131), `--lc-lh-heading` (172), `--lc-ease-out` (184), `--lc-motion-base` (189), `--lc-motion-dropdown` (190), `--lc-opacity-muted` (194), `--lc-opacity-overlay` (195), `--lc-text-6` (250), `--lc-z-sticky/rail/header/modal/toast` (199–205).

Отдельно про них:
- **Все семь `--lc-z-*` объявлены, но применяются только два** (`--lc-z-overlay`, `--lc-z-banner`). Остальные пять — заявленный, но не работающий реестр слоёв; шапка, рельса, модалка и тосты берут z-index из Mantine.
- `--lc-primary-solid`, `--lc-success-solid` фигурируют в `UiKitPage.tsx:108-109` как СТРОКИ в массиве `ACCENTS` (рисуются образцы цвета), но ни один компонент их не применяет — при том что в шапке `lc-vars.css:22-33` вокруг пары «яркий/плотный» построено целое правило.
- `--lc-on-primary` (365) снабжён комментарием «Потребителей у токена сейчас ноль» — то есть мёртвость известна и зафиксирована.
- `--lc-chart-2/3/4` — три из четырёх цветов диаграмм. `MetricChart.tsx` рисует одной серией и берёт только `--lc-chart-1`.
- `--lc-bot*` и `--lc-note*` — дубли `--lc-accent*` и `--lc-bubble-note*`, заведённые в редизайне 7 августа и не разведённые.

### 4.3 42 примитива живут только алиасами внутри `lc-vars.css`
`--lc-slate-100…900` (кроме 800), `--lc-blue-300…950`, `--lc-green-300/400/600/950`, `--lc-red-600/700/950`, `--lc-amber-300…950`, `--lc-purple-300…950`, `--lc-ink-600`, `--lc-bg-app/panel/selected`, `--lc-line`, `--lc-line-strong`, `--lc-warn-bg`, `--lc-font`, `--lc-dur-dropdown`, `--lc-focus-halo`. Это нормально для слоя примитивов, но означает, что «сырых» имён (`--lc-bg-panel`, `--lc-line`) компоненты не видят, а видят только алиасы — и разделение слоёв в файле не документировано.

### 4.4 Вторая, параллельная шкала — `src/app/theme.ts`
Тема Mantine НЕ читает токены, а объявляет числа заново:
- `theme.ts:243-249` `fontSizes`: xs 12, **sm 13**, md 14, lg 16, xl 18. Токенов `13px` и `18px` в `lc-vars.css` нет вовсе.
- `theme.ts:271-277` `spacing`: xs 4, sm 8, md 16, lg 24, xl 32 — сдвинуто относительно `--lc-space-N` (4=space-1, 8=space-2, 16=space-4…), то есть имена не совпадают со шкалой.
- `theme.ts:281-287` `radius` — те же значения, что `--lc-radius-*`, но литералами.
- `theme.ts:262` `headings.h1 = 24px` — против `--lc-fz-page = 28px`.
- `theme.ts:64-150` — шесть кортежей палитры (`lp`, `info`, `green`, `red`, `amber`, `violet`, `dark`) HEX-литералами; комментарий на `:119-137` прямо признаёт, что это «второй экземпляр палитры».
Тени — единственное, что взято из токенов (`theme.ts:291-297`).

### 4.5 «Магические» значения мимо токенов

**Цвет:**
- `app/lc-base.css:162` — `box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.28)` — единственный сырой rgba вне файла токенов (это `--lc-red-500` с альфой).
- `platform/UpdateBanner.tsx:36` — `<Text c="red">` (имя цвета Mantine вместо `--lc-danger-text`).
- `CriticalBanners.tsx:54,59` — `color="red"` / `color="gray"`; `MyChannelsBlock.tsx:63` — `<Alert color="yellow">`; `LoginPage.tsx:155,160,165,170` — `color="yellow"/"red"`; `BotEditor.tsx:306` — `color={"yellow"}`; `Heatmap.tsx:40` — `color="gray"`. HEX ни одного вне `lc-vars.css`/`theme.ts` — это соблюдено.

**Отступы и размеры в CSS (мимо `--lc-space-*`):**
`lc-base.css:553` `padding: 8px 12px` (`.lc-tabs__tab`); `settings.css:32` `padding: 8px 12px` (`.settings-nav__item`); `templates.css:30` `padding: 4px 12px`, `:45` `gap: 6px`, `:104` `padding: 6px 8px`, `:109` `padding: 8px`; `template-picker.css:23` `gap: 6px`, `:61,73,109` `padding: 6px …`; `chat-thread.css:223,442` `padding: 3px 0`; `notifications.css:236` `padding: 4px …`, `:276` `padding: 6px …`; `stats.css:306` `padding: 6px 8px`, `:378,430,588` `gap: 4px`, `:404` `padding: 8px …`, `:480` `margin-left: 6px`; `my-channels.css:17` `padding: 6px 10px`; `team.css:41` `padding: 6px …`, `:71` `margin-left: 6px`; `week-bars.css:15` `gap: 3px`, `:45` `gap: 4px`; `chat-list.css:316` `gap: 3px`; `bots.css:17` `padding-bottom: 96px`; `uikit.css:63,144` `6px`.

**Кегли в CSS мимо `--lc-fz-*`:** `chat-list.css:115` `font-size: 13px`; `settings.css:36` `font-size: 14px`.

**Размеры в JSX (числовые пропы Mantine вместо `var(--lc-space-N)`):** `BotEditor.tsx:178,179` `mb={8}`, `:228` `mb={6}`, `:244` `mt={4}`, `:324` `gap={2}`; `BotsPage.tsx:109,110` `mb={8}`, `:188` `gap={4}`; `ScheduleEditor.tsx:47,54` `mt={4}`, `:57` `gap={2}`; `SandboxDrawer.tsx:228,243` `mt={4}`, `:265` `mt={22}`; `VariableTextarea.tsx:54,88` `gap={4} mb={2} mt={2}`; `TemplatesManager.tsx:92` `mt={6}`; `TransferDialog.tsx:76` / `InviteDialog.tsx:87` `mt={4}`; `TeamMembersTab.tsx`, `AssignOperatorsModal.tsx` — ширины `w={150}`, `w={140}`, `w={110}` и т.п.

**Размеры иконок — 11 разных значений, токена нет:** `size={10}` ×1, `12` ×2, `13` ×16, `14` ×29, `15` ×4, `16` ×8, `17` ×5, `18` ×7, `20` ×9, `24` ×1, `28` ×1. В `Icon.tsx:24` дефолт 18, но им почти не пользуются.

**Прочие числа-литералы:** `ROW_HEIGHT = 84` (`ChatListPane.tsx:68`) при том, что комментарий на `:66` говорит «карточка 78 + зазор 6»; `estimateSize: () => 64` (`ChatThreadPane.tsx:108`); `initialRect: { width: 320, height: 600 }` (`ChatListPane.tsx:347`) при `--lc-col-list: 340px`; `LINE_HEIGHT = 20`, `TEXTAREA_PADDING = 8`, `MAX_ROWS = 6` (`Composer.tsx:54-63`); `W=720, H=240, PAD` (`MetricChart.tsx:24-26`); `AppShell header={{height:56}} navbar={{width:56}}` (`AppLayout.tsx:209`) — есть токены `--lc-header-h`/`--lc-rail-w` с теми же значениями, но они не читаются.

### 4.6 Эмодзи в интерфейсе вопреки собственной доктрине
`Icon.tsx:5-18`, `EmptyState.tsx:8-11` и `StatusDot.tsx:4-11` подробно объясняют, почему эмодзи в продукте недопустимы («ОС рисует их по-своему, `currentColor` не действует»). Фактически эмодзи стоят в продовом коде:
- `features/notifications/catalog.ts:74-127` — **26 эмодзи** как иконки всех типов уведомлений (`🔌 📵 💾 ⏱ 🛰 📥 📤 🗄 🔒 🤖 🔑 ✉️ 🚫 😠 ⏳ 🔁 ⚑ 🙈 🧹 📩 🔔`), рендерятся в `NotificationPanel.tsx:44` и `CriticalBanners.tsx:43`.
- `features/settings/bots/scenario.ts:30-38` — 9 эмодзи как иконки типов шагов (`💬 📋 🔀 ✨ 👤 ✅ 🏷 🗒`), рендерятся в `StepCard.tsx:77` и в подписях `Select` на `StepCard.tsx:161`.
- `AccountsPage.tsx:446` `🔄 Переподключить`; `BotEditor.tsx:311` `🧪 Протестировать`; `SandboxDrawer.tsx:222` `🧪 Песочница`, `:62,76,88,99,104` (`🔀 🔎 ✨ 🏷 👤`); `OneTimeLinkModal.tsx:48` `📋 Скопировать`; `LoginPage.tsx:228` `🖥`; `ThreadFooter.tsx:69` `👁 Режим просмотра`; `defaultScenario.ts:18,69,79,96` (`👋 ✔ 🙂 🤖`); `scenario.ts:152` `🙂`.

### 4.7 Текстовые глифы вместо существующих иконок
`✕` вместо `IconX`: `Composer.tsx:348`, `ScheduleEditor.tsx:108`, `StepCard.tsx:147`, `ConditionForm.tsx:192`, `MenuForm.tsx:84`, `ChatListPane.tsx:577` («Сбросить ✕»).
`↑ / ↓` вместо `IconArrowDown` (объявлена и не используется): `StepCard.tsx:125,136`.
`✓` вместо `IconCheck`: `AppLayout.tsx:108`.
`⬇` вместо `IconDownload`: `StatsFilters.tsx:141`.
`⋮` вместо `IconMore`: `TemplatesManager.tsx:324`.
`ⓘ` вместо `IconInfo`: `TransferDialog.tsx:77`, `InviteDialog.tsx:88`.
`←` вместо `IconChevronLeft`: `BotEditor.tsx:212`.
`↓ новые сообщения`: `ChatThreadPane.tsx:478`.
`+` текстом вместо `IconPlus` (объявлена и не используется): `BotsPage.tsx:102,135`, `TeamMembersTab.tsx:256`, `TemplatesManager.tsx:224`, `ClientCardPane.tsx:182`, `BotEditor.tsx:282,297`, `ScheduleEditor.tsx:118`.

### 4.8 Расхождение UI Kit с реальностью
`UiKitPage.tsx:172` подписывает `--lc-fz-page` как «24 Semibold», фактически 28px (`lc-vars.css:159`); `:174` подписывает `--lc-fz-section` как «18 Semibold», фактически 20px (`lc-vars.css:160`). Живой каталог, который «не может разойтись с приложением» (комментарий `:25-30`), разошёлся в подписях.

---

## 5. СОСТОЯНИЯ: ГДЕ ЕСТЬ, ГДЕ НЕТ

### 5.1 Полный набор (skeleton + empty + error + retry)
- `ChatListPane.tsx` — `ListSkeleton:70`, `EmptyState error:587` с «Повторить», четыре разных пустых состояния (`EmptyInbox:88`, `EmptyByTab:98`, поиск `:600`).
- `ChatThreadPane.tsx` — `ThreadSkeleton:30`, error `:438` с «Повторить», empty `:448`, «диалог не выбран» `:50`.
- `AccountsPage.tsx` — скелетон `:646`, error `:651`, empty `:661`.
- `BotsPage.tsx` — скелетон `:107`, error `:115`, empty `:128`.
- `TeamMembersTab.tsx` — скелетон `:266`, error `:272` (`live="alert"`), empty `:283`.
- `AuditLogTab.tsx` — скелетон `:192`, error `:198` (`live="alert"`), empty `:209`.
- `TemplatesManager.tsx` — скелетон `:244`, error `:250`, empty `:260`.
- `BotEditor.tsx` — скелетон `:174`, error `:185`.
- `NotificationsPage.tsx` — скелетон `:282`, error `:288`, empty `:297` (последние два — свои блоки, не `EmptyState`).
- `StatsPage.tsx` + `MetricChart` + `Heatmap` + `ManagersTable` — у каждого свой скелетон и свой `.stats-error` с «Повторить»; empty — голый текст.

### 5.2 Дыры

| Место | Чего нет |
|---|---|
| `ClientCardPane.tsx:228-238` | **Нет ветки ошибки.** `if (!conversation) return скелетон` — при отказе `useConversationDetail` карточка показывает скелетон бесконечно. `detail.isError` в файле не проверяется ни разу (проверяется только `history.isError` на `:465`). |
| `WorkHoursBlock.tsx` (весь файл) | **Ни загрузки, ни ошибки.** `q.isPending`/`q.isError` не используются в разметке вообще. Пока идёт запрос — на экране стоят дефолты 10/20; при отказе они же остаются, выглядя как сохранённые значения. Кнопка «Сохранить часы» при этом заблокирована (`dirty=false`), то есть на экране молчаливо неверные цифры. |
| `TablePage.tsx:445-448` | Нет скелетона — крутится `<Loader size="sm">` посреди пустой страницы, хотя это таблица и скелетон строк напрашивается. |
| `MyChannelsBlock.tsx:47-57` | Загрузка — спиннер + «Загружаем…»; ошибка — голый `<Text role="alert">` **без кнопки «Повторить»**. |
| `ConnectChannelWizard.tsx:76-80` | У запроса `/avito/app` нет ни загрузки, ни ошибки. При отказе предупреждение «это имитатор» просто не покажется, и кнопка «Подключить» останется активной. |
| `SandboxDrawer.tsx` | `isError` не встречается ни разу — у песочницы нет состояния ошибки. |
| `DistributionTab.tsx:97-108` | Ошибка — свой блок `<Text c="var(--lc-danger-text)">` + кнопка, вместо `EmptyState illustration="error"`. |
| `NotificationPanel.tsx:92-97` | Есть пустое, нет ошибки (данные из стора — отказ загрузки не отражается). |
| `PeoplePicker.tsx:65-83`, `TemplatePickerPopover.tsx:130-151` | Есть загрузка (спиннер) и ошибка с «Повторить», нет скелетона. |
| `Composer.tsx` | Есть баннеры ошибок отправки (`:296,306,328`), нет состояния загрузки/ошибки для самого диалога. |
| `TransferDialog`, `InviteDialog`, `UnloadQueueDialog`, `ExportModal`, `OutcomeModal`, `InviteModal`, `OneTimeLinkModal` | Есть `loading` у кнопок, нет ветки ошибки на уровне окна — отказ уходит в тост. |
| `AppearanceBlock`, `HotkeysBlock`, `UpdatesPage`, `UiKitPage`, `HotkeysModal` | Асинхронности нет — состояния не нужны. |

### 5.3 Кнопка «Повторить» — где есть, где нет
Есть: `ChatListPane:591`, `ChatThreadPane:442`, `AccountsPage:656`, `BotsPage:121`, `TeamMembersTab:278`, `AuditLogTab:204`, `TemplatesManager:255`, `TablePage:454`, `NotificationsPage:293`, `StatsPage:148`, `MetricChart:206`, `Heatmap:53`, `ManagersTable:136`, `PeoplePicker:74`, `TemplatePickerPopover:139`, `ClientCardPane:466` (история), `DistributionTab:102`.
Нет: `MyChannelsBlock:55`, `WorkHoursBlock`, `UnloadQueueDialog:63`, `NotificationPanel`, `SandboxDrawer`, `ConnectChannelWizard`.

---

## 6. КАРТА РАЗДЕЛОВ ПО КОДУ (`src/app/router.tsx`)

| Путь | Компонент | Guard | В навигации |
|---|---|---|---|
| `/login` | `LoginPage` (61) | — | нет |
| `/invite/:token` | `InvitePage` (62) | — | нет |
| `/` | → `/chats` (69) | `RequireAuth` | — |
| `/chats`, `/chats/:id` | `ChatsPage` (70, 113) | `RequireAuth` | рельса `AppLayout:246` |
| `/dialogs` | `TablePage`, lazy (94-98) | `stats:all` (74) | рельса `AppLayout:278`, подпись «Разбор диалогов» |
| `/stats` | `StatsPage`, lazy (132) | `stats:all` (127) | рельса `AppLayout:291` |
| `/notifications` | `NotificationsPage`, lazy (120) | нет | только колокольчик и «Показать все» |
| `/updates` | `UpdatesPage`, lazy (102) | нет | меню аватара `AppLayout:171` |
| `/ui-kit` | `UiKitPage`, lazy (108) | нет | **нигде** |
| `/settings` | → `/settings/profile` (140) | `RequireAuth` | рельса `AppLayout:302` |
| `/settings/profile` | `ProfilePage` (141) | нет | `SettingsLayout:40` |
| `/settings/accounts` | `AccountsPage` (144) | `accounts:read`\|`accounts:manage` (143) | `SettingsLayout:16` |
| `/settings/templates` | `TemplatesPage` (148) | `templates:shared` (147) | `SettingsLayout:21`, подпись «Быстрые ответы» |
| `/settings/bots`, `/settings/bots/:id` | `BotsPage`/`BotEditor`, lazy (159, 163) | `bots:manage` (155) | `SettingsLayout:26` |
| `/settings/distribution` | `DistributionTab`, lazy (176) | `settings:manage` (172) | `SettingsLayout:31` |
| `/settings/team` | `TeamPage`, lazy (190) | `users:manage`\|`audit:read` (186) | `SettingsLayout:36` |
| `*` | `NotFoundPage` (44, 200) | — | — |

### Есть в коде, но не выведено (20)

* **Восемь иконок объявлены и не вызываются нигде: IconFilter, IconArrowDown, IconSmile, IconMic, IconSparkles, IconPlus, IconMail, IconEdit**
  * где: frontend/src/shared/ui/Icon.tsx:147, 171, 193, 202, 216, 289, 304, 318
  * почему важно: IconSmile и IconMic — это ровно те эмодзи-кнопки, которых, по комментарию Composer.tsx:26-41, «намеренно нет» (нет пикера, Авито не принимает аудио): значки нарисованы под функции, решено не делать. IconPlus и IconArrowDown при этом заменяются текстовыми «+» и «↑/↓» в восьми и двух местах соответственно.
* **IconArchive используется только в демо-каталоге**
  * где: frontend/src/shared/ui/Icon.tsx:257 → единственный вызов features/uikit/UiKitPage.tsx:281 (пункт меню «В архив»)
  * почему важно: Архива в продукте нет — ConversationListItem.tsx:243 прямо пишет, что «архив» в API отсутствует. Иконка и демо-пункт меню обещают несуществующее действие.
* **Три метода тостов вызываются только из UI Kit, четвёртый — ноль раз**
  * где: frontend/src/shared/ui/toast.tsx:163 (info), :164 (warning), :167 (errorPersistent), :169 (hide); вызовы — features/uikit/UiKitPage.tsx:292, 295, 298
  * почему важно: errorPersistent заводился под «ошибка обязана дождаться человека» (комментарий toast.tsx:166). В проде вместо него везде вызывается showToast с autoClose:false вручную (AccountsPage.tsx:575, 583; ConnectChannelWizard.tsx:137, 156) — то есть API есть, но им не пользуются.
* **Индикатор набора текста: CSS, анимация и разметка есть, в продукте не рендерится**
  * где: CSS — frontend/src/features/chats/components/thread/chat-thread.css:436-473 и :586; разметка — только frontend/src/features/uikit/UiKitPage.tsx:394-400
  * почему важно: Событие WS `typing` объявлено в контракте (shared/realtime/wsEvents.ts:79-83), но applyWsEvent.ts:622 его явно игнорирует с пометкой «спринт 3+». Готовый компонент лежит, сервер (по комментарию chat-thread.css:436) событие шлёт, а интерфейс его выбрасывает.
* **WS-события typing и presence:online принимаются и молча отбрасываются**
  * где: frontend/src/shared/realtime/wsEvents.ts:79-88 (объявлены в union), frontend/src/shared/realtime/applyWsEvent.ts:620-623 (ветка default с комментарием «спринт 3+»)
  * почему важно: presence:online при этом в интерфейсе показывается — но берётся из HTTP-ответов (TeamMembersTab.tsx:50-66, PeoplePicker.tsx:98), то есть состояние «в сети» обновляется только по перезапросу, хотя живой канал для него уже есть.
* **Базовая карточка из брифа .lc-card / .lc-card--interactive не носится ни одним компонентом**
  * где: frontend/src/app/lc-base.css:356-382 (включая :hover на 372 и блок prefers-reduced-motion на 378)
  * почему важно: Комментарий на :355 называет её «базовой формой из брифа» (радиус 16, рамка, тень), а все карточки продукта — .conv-card, .account-card, .stat-card, .card-section, .settings-block — собраны каждая по-своему. Единая форма объявлена и не введена.
* **48 токенов не имеют ни одной ссылки нигде, включая сам файл токенов**
  * где: frontend/src/app/lc-vars.css — в том числе --lc-void:233, --lc-bot/-text/-bg:278-280, --lc-note-bg/-text/-meta:285-287, --lc-bubble-in-line:283, --lc-bubble-in-hot:284, --lc-primary-solid:359, --lc-primary-solid-hover:360, --lc-primary-solid-active:361, --lc-on-primary:365, --lc-primary-hover:366, --lc-success-solid:381, --lc-success-solid-hover:382, --lc-on-success:383, --lc-danger-solid-hover:388, --lc-chart-2/3/4:502-504, --lc-radius-xl:131, --lc-space-0:115, --lc-space-7:121, --lc-lh-heading:172, --lc-ease-out:184, --lc-motion-base:189, --lc-motion-dropdown:190, --lc-opacity-muted:194, --lc-opacity-overlay:195, --lc-text-6:250, --lc-ink-400/500/700/800/900:54-59, --lc-line-soft:239, --lc-red-300/400/500:78-80, --lc-slate-800:49, --lc-brand-dark:103, --lc-brand-bg:261, --lc-z-sticky/rail/header/modal/toast:199-205
  * почему важно: Пять из семи токенов слоёв мертвы — реестр z-index, заведённый «чтобы не выяснять экспериментально» (комментарий :197), в реальности не применён ни к шапке, ни к рельсе, ни к модалкам, ни к тостам. Пара «яркий/плотный» акцент, вокруг которой построена шапка файла (:17-33), не используется: --lc-primary-solid и --lc-success-solid не применяет никто, они лишь перечислены строками в массиве UiKitPage.tsx:108-109. --lc-on-primary снабжён честным комментарием «Потребителей у токена сейчас ноль» (:362).
* **Мёртвые CSS-классы после трёх перестроек экранов**
  * где: frontend/src/features/chats/components/thread/chat-thread.css:103 (.thread-header__actions), :122-155 (.thread-status и его 4 модификатора), :515 (.thread-composer-stub), :525 (.thread-status-select); features/chats/components/list/chat-list.css:350 (.conv-card__time), :402 и :545 (.conv-card__chip--wait); features/settings/accounts/connect-wizard.css:6, 22, 29, 36 (.wizard__steps, .wizard__step-body), :60 (.wizard__foot--split), :68 (.wizard__alt); features/settings/accounts/accounts.css:148 (.accounts-page__foot), :154-170 (.accounts-page__link); features/settings/team/team.css:110 (.audit__empty)
  * почему важно: Каждая группа — след удалённой функции: селект статуса из шапки ленты (ChatThreadPane.tsx:385-390 объясняет, почему убран), отдельная колонка времени в строке (заменена WaitGauge), многошаговый мастер подключения канала (ConnectChannelWizard.tsx:27-29 объясняет, что шаги свернули). Стили остались и продолжают грузиться.
* **Ветки пустых состояний для вкладок «Новые» и «Закрытые», которых в ряду вкладок больше нет**
  * где: frontend/src/features/chats/components/list/ChatListPane.tsx:124-127 (case "new", case "closed" в EmptyByTab), при том что TABS на :45-48 содержит только mine и all
  * почему важно: Тип ConversationTab в shared/api/queryKeys.ts:16 всё ещё четырёхзначный, и код достижим только через горячие клавиши Alt+3/Alt+4 (useChatHotkeys.ts:40-45), которые ставят tab:'all' + status — то есть эти две ветки не сработают никогда: при Alt+3 tab станет 'all', и покажется ветка 'all'. Мёртвый код, замаскированный под живой.
* **Страница /ui-kit не выведена ни в одну навигацию**
  * где: маршрут — frontend/src/app/router.tsx:107-112; рельса — app/AppLayout.tsx:243-311; меню аватара — app/AppLayout.tsx:154-176; меню настроек — features/settings/SettingsLayout.tsx:14-43
  * почему важно: Живой каталог системы (452 строки, UiKitPage.tsx) доступен только по прямой ссылке и без guard'а по правам — его увидит любой вошедший, включая наблюдателя. Он же — единственный потребитель IconArchive, .msg-typing и трёх методов toast.*, из-за чего они выглядят «используемыми» при автоматическом поиске.
* **Комментарий PageHeader обещает канон 20px, код даёт 28px, тема Mantine — 24px**
  * где: frontend/src/shared/ui/PageHeader.tsx:17 («КАНОН: h1, 20px, вес 600»); frontend/src/shared/ui/page-header.css:27 (font-size: var(--lc-fz-page)); frontend/src/app/lc-vars.css:159 (--lc-fz-page: 28px); frontend/src/app/theme.ts:262 (h1: 24px)
  * почему важно: Компонент заводили именно ради того, чтобы «заголовок перестал прыгать в размере» (PageHeader.tsx:10-13). Сейчас на продукте пять разных размеров h1: 28 (PageHeader), 24 (TablePage.tsx:316, LoginPage.tsx:142, UpdatesPage.tsx:45, router.tsx:47), 20 (StatsPage.tsx:122).
* **UI Kit подписывает два токена неверными числами**
  * где: frontend/src/features/uikit/UiKitPage.tsx:172 («Заголовок страницы — 24 Semibold», фактически --lc-fz-page = 28px) и :174 («Заголовок секции — 18 Semibold», фактически --lc-fz-section = 20px)
  * почему важно: Страница заявлена как источник истины, который «не может разойтись с приложением» (комментарий :25-30). Подписи он всё же нарисованные, и они уже разошлись — тот, кто сверяет вёрстку по каталогу, получит неверные числа.
* **Копия plural() с другим правилом склонения**
  * где: frontend/src/features/settings/accounts/WeekBars.tsx:34-40 против канона frontend/src/shared/lib/plural.ts:6-12
  * почему важно: Условие few различается: канон даёт (mod100 < 10 || mod100 >= 20), копия — (mod100 < 12 || mod100 > 14). На 12, 13, 14, 112-114 обращениях копия скажет «113 обращения» вместо «113 обращений». Третья копия — platform/tauri/notifier.ts:114.
* **Своё ROLE_HINTS в профиле с текстами, отличными от канона**
  * где: frontend/src/features/settings/profile/ProfilePage.tsx:21-26 против frontend/src/features/settings/team/roles.ts:12-17
  * почему важно: Одна и та же роль описана в двух местах разными словами: для head канон говорит «Все диалоги без отправки, статистика всех сотрудников, журнал аудита», профиль — «видит все диалоги, передаёт их и пишет заметки — отвечать клиенту не может». Сотрудник читает про свои права одно, администратор в списке команды — другое.
* **Собственный расчёт времени ожидания в шапке ленты, мимо shared/lib/waiting.ts**
  * где: frontend/src/features/chats/components/thread/ChatThreadPane.tsx:159-171 (свой useMemo, свой перевод мин/ч/дн, порог 15 литералом) против frontend/src/shared/lib/waiting.ts:45-73
  * почему важно: Модуль waiting.ts заводился ровно для того, чтобы расчёт был «одно место на весь продукт» (комментарий :3-7), и его же комментарий говорит, что раньше расчёт был только в шапке. Шапка на канон так и не переехала: у неё свой порог «поздно» и своя логика (по последнему сообщению direction in/out), у списка — своя (по unread_count). Значения могут расходиться на одном и том же диалоге.
* **26 эмодзи как иконки уведомлений и 9 как иконки шагов бота — вопреки трижды записанному запрету**
  * где: frontend/src/features/notifications/catalog.ts:74-127 (KIND_CATALOG) и :127 (FALLBACK 🔔), рендер — features/notifications/NotificationPanel.tsx:44 и CriticalBanners.tsx:43; frontend/src/features/settings/bots/scenario.ts:30-38 (STEP_META), рендер — StepCard.tsx:77 и :161
  * почему важно: Запрет на эмодзи подробно обоснован в трёх местах системы: Icon.tsx:5-18, EmptyState.tsx:8-11, StatusDot.tsx:4-11 («ОС рисует их сама, размер и цвет из CSS не управляются, на Windows/macOS/Android три разных значка»). Продукт живёт и в браузере, и в приложении для Windows. Точечные вкрапления есть ещё в AccountsPage.tsx:446, BotEditor.tsx:311, OneTimeLinkModal.tsx:48, LoginPage.tsx:228, ThreadFooter.tsx:69, SandboxDrawer.tsx:62-222, defaultScenario.ts:18-96.
* **Карточка клиента не имеет ветки ошибки — при отказе запроса показывает скелетон бесконечно**
  * где: frontend/src/features/chats/components/card/ClientCardPane.tsx:228-238 (if (!conversation) → скелетон); detail.isError в файле не проверяется ни разу
  * почему важно: Правая колонка при упавшем запросе выглядит вечно загружающейся. Соседние панели того же экрана (ChatThreadPane.tsx:437, ChatListPane.tsx:586) ошибку обрабатывают и дают «Повторить» — то есть на одном экране два разных поведения при одном и том же отказе сети.
* **Блок рабочих часов не показывает ни загрузку, ни ошибку — при отказе выдаёт дефолты 10:00–20:00 как сохранённые**
  * где: frontend/src/features/settings/distribution/WorkHoursBlock.tsx:36-53 (q.isPending и q.isError не используются в разметке нигде), значения по умолчанию — :41-42
  * почему важно: От этого окна зависит вся метрика «скорость первого ответа» (комментарий :14-19). Администратор, открывший экран при недоступном сервере, увидит 10–20 и решит, что так и настроено. Кнопка «Сохранить часы» при этом заблокирована (dirty=false), так что даже переспросить нечем.
* **Три места используют нативный window.confirm вместо модалки продукта**
  * где: frontend/src/features/settings/accounts/AccountsPage.tsx:498 (отключить и стереть канал) и :533 (удалить канал); frontend/src/features/settings/bots/BotsPage.tsx:223 (удалить бота)
  * почему важно: Это три самых разрушительных действия в продукте (удаление всей переписки канала, удаление сценария бота) — и единственные, которые спрашивают подтверждение системным окном без стилей, без фокус-ловушки и без кнопки-опасности красным. Рядом в том же коде есть Mantine-модалки ровно под это (StepCard.tsx:193, TemplatesManager.tsx:356).
* **Реестр слоёв объявлен, но применены два токена из семи**
  * где: frontend/src/app/lc-vars.css:199-205 — используются только --lc-z-overlay и --lc-z-banner; --lc-z-sticky, --lc-z-rail, --lc-z-header, --lc-z-modal, --lc-z-toast не читает никто
  * почему важно: Комментарий на :197 объясняет: числа заданы здесь, «чтобы „а какой z-index больше“ не выяснялось экспериментально». Шапка, рельса, модалки и тосты сейчас берут порядок наложения из Mantine — то есть именно экспериментально.

### Расхождения с брифом и чего не нашёл

## РАСХОЖДЕНИЯ С БРИФОМ

### 1. Роли: бриф называет две, в коде четыре
Бриф: «Администратор, Менеджер».
Код (фронт): `frontend/src/shared/auth/usePermissions.ts:4` — `type Role = "admin" | "head" | "manager" | "observer"`.
Код (бэк, тот же список): `app/core/rbac.py:7` — `ROLES = ("admin", "head", "manager", "observer")`, матрица прав `rbac.py:32-60`.
Русские подписи: `frontend/src/features/settings/team/roles.ts:5-10` — Администратор / **Руководитель** / Менеджер / **Наблюдатель**.

Две недостающие роли — не декорация, под них построена половина интерфейса:
- **head (Руководитель)**: guard `/stats` и `/dialogs` держится на `stats:all`, который есть у admin и head (`router.tsx:74, 127`); guard `/settings/team` — `anyOf ["users:manage","audit:read"]` именно ради head (`router.tsx:186` + `TeamPage.tsx:24-37`); низ ленты у него отдельный — плашка «Режим просмотра» + композер только заметок (`ThreadFooter.tsx:64-74`); в `ClientCardPane.tsx:206` для него свободный селект «ответственный».
- **observer (Наблюдатель)**: у него ровно одно право `conversations:read` (`rbac.py:59`), и под него написаны отдельные ветки в `ThreadFooter.tsx:76` (низа панели нет вовсе), `ClientCardPane.tsx:494` (нет секции заметок), `ChatListPane.tsx:167` (нет вкладки «Входящие»), `NotificationBell.tsx:20` (нет колокольчика), `ProfilePage.tsx:112` (нет вкладки быстрых ответов).

Итого 15 прав (`usePermissions.ts:8-24`), а не два уровня доступа.

### 2. Раздел `/settings/quick-replies` в коде называется иначе
Бриф: `/settings/quick-replies`. Фактический маршрут: `/settings/templates` (`frontend/src/app/router.tsx:148`), компонент `features/settings/templates/TemplatesPage.tsx`, guard `templates:shared`. Пункт меню при этом подписан по-русски «Быстрые ответы» (`SettingsLayout.tsx:21-23`), заголовок страницы тоже (`TemplatesPage.tsx:14`). То есть расходятся только адрес и имена файлов; для пользователя раздел называется как в брифе.

Дополнительно: раздел живёт на двух экранах — общие шаблоны на `/settings/templates`, личные встроены вкладкой в профиль (`ProfilePage.tsx:112-128`, `TemplatesManager personalOnly`). В брифе это один раздел.

### 3. Раздел `/dialogs` есть, но называется в интерфейсе иначе
Маршрут `/dialogs` существует (`router.tsx:94`), компонент `features/table/TablePage.tsx`. В рельсе подписан «Разбор диалогов» (`AppLayout.tsx:276, 281`), заголовок страницы — «Диалоги» (`TablePage.tsx:317`). Каталог в коде — `features/table`, а не `features/dialogs`; имя роутинга и имя фичи не совпадают.

### 4. Разделов больше, чем в брифе — пять не перечисленных
- `/login` (`router.tsx:61`) и `/invite/:token` (`router.tsx:62`) — вход и установка пароля по одноразовой ссылке.
- `/notifications` (`router.tsx:120`) — журнал уведомлений, 361 строка, свои фильтры, своя таблица, своя пагинация. В брифе не упомянут.
- `/updates` (`router.tsx:102`) — «Что нового», журнал релизов из `features/updates/changelog.ts`. Доступен из меню аватара (`AppLayout.tsx:171`).
- `/ui-kit` (`router.tsx:108`) — живой каталог дизайн-системы, 452 строки, **без guard'а и без единой ссылки в навигации**.
- `/settings` — редирект на `/settings/profile` (`router.tsx:140`), `*` — страница 404 (`router.tsx:200`).

### 5. Состав `/settings/*` совпадает с брифом полностью
`accounts` ✓ (`router.tsx:144`), `bots` ✓ (`:158` + редактор `:162`), `distribution` ✓ (`:175`), `team` ✓ (`:189`), `profile` ✓ (`:141`), `templates` (= «quick-replies» брифа) ✓ (`:148`). Ни одного лишнего, ни одного недостающего.

### 6. Чего в брифе просили, а в коде нет (со ссылкой на объяснение в коде)
- **Семь кнопок композера** — реально три. `Composer.tsx:26-41` объясняет каждое отсутствие: эмодзи-пикер (нет своего, системный работает), AI-ассистент (не решён провайдер, Anthropic из РФ недоступен), голосовое (Авито не принимает аудио от продавца). Иконки под две из них (`IconSmile:193`, `IconMic:202`) при этом нарисованы и лежат мёртвыми.
- **«Приоритет» отдельным свойством диалога** — `ConversationListItem.tsx:29-32`: «В брифе есть „приоритет“ отдельным свойством, но в API его нет и придумывать его на фронте нельзя». Вместо него — вычисляемая срочность `urgency()` (`:34-46`).
- **Быстрые действия по наведению в списке** — из брифа их несколько, реализовано ровно одно («Передать»). `ConversationListItem.tsx:241-247`: «Закрепить», «архив» и «чёрный список» в API пока нет.
- **Зелёный акцент везде** — исходящий пузырь оставлен синим осознанно, с арифметикой контраста в `lc-vars.css:426-450`.

---

## ЧЕГО НЕ НАШЁЛ / ГРАНИЦЫ ПРОВЕРКИ

1. **Самого брифа заказчика в репозитории не нашёл.** Формулировки брифа сверял только по цитатам в комментариях кода (`lc-vars.css:17,126,134,141`, `theme.ts:52,241,279`, `Composer.tsx:27`, `ConversationListItem.tsx:29`, `AppearanceBlock.tsx:6`). Документы `docs/16-DESIGN-SYSTEM-2026.md` и `docs/17`, на которые ссылается почти каждый комментарий, в `leadchat/docs/` **отсутствуют** — там лежат только файлы 12, 19–27 и HANDOFF. То есть проверить «код против дизайн-системы» напрямую нельзя: сама дизайн-система как документ недоступна, есть только пересказы в комментариях.

2. **Не проверял визуально.** Всё в отчёте — статический разбор исходников. Контрасты, наложения, поведение на трёх ширинах и в двух темах не измерял; про существование стенда `frontend/dev/preview.tsx` (+ `dev-preview.html`) знаю, но не запускал — он в сборку не входит и покрывает 5 экранов из ~15 (`dev/preview.tsx:375-392`).

3. **Не смотрел `src/test/` как источник истины** — 70 тестовых файлов исключал из подсчёта использований, чтобы «используется только в тесте» не читалось как «живое». Отдельно отметил случаи, где потребитель есть только в UI Kit.

4. **Не разбирал**, что именно рисуют CSS-файлы построчно (24 файла, ~6 000 строк). Проверил только: объявление/использование классов, наличие HEX и rgba, отступы и кегли мимо токенов. Мёртвые классы искал сопоставлением с текстом `.tsx` — динамические имена (`msg--${kind}`, `conv-card__chip--${tone}`, `lc-notify-severity--${severity}`, `card-section--${modifier}`, `var(--lc-avatar-${idx}-bg)`) разбирал руками и из списка мёртвых исключил.

5. **Доступность (a11y) в срез не входила** — фиксировал только то, что попалось по дороге: `role="alert"`/`live` у пустых состояний, `aria-label` у иконочных кнопок, `lc-visually-hidden` против `<VisuallyHidden>`. Полного аудита не делал.

6. **Не сверял `shared/api/types.ts`** (1 000+ строк) с контрактом сервера — это не мой срез.

7. **Backend трогал ровно один раз** — `app/core/rbac.py`, чтобы подтвердить число ролей. Остальное про сервер в отчёте не утверждаю.

## Срез: Роли и права

# ИНВЕНТАРИЗАЦИЯ ТЕКСТОВ ИНТЕРФЕЙСА LeadChat (шаг 0, срез «тексты»)

Корень: `.`
Все пути ниже — от этого корня. Формат: `файл:строка`.
Собрано скриптом-экстрактором (снимает комментарии, оставляет строковые литералы и JSX-текст) + ручная сверка. Тестовые файлы (`frontend/src/test/`) исключены.

---

# ЧАСТЬ 0. КАРТА ЭКРАНОВ (по коду, не по брифу)

Источник: `frontend/src/app/router.tsx:60-201`

| Маршрут | Компонент | Гард | Строка |
|---|---|---|---|
| `/login` | LoginPage | — | router.tsx:61 |
| `/invite/:token` | InvitePage | — | router.tsx:62 |
| `/` → `/chats` | Navigate | RequireAuth | router.tsx:69 |
| `/chats` | ChatsPage | RequireAuth | router.tsx:70 |
| `/chats/:id` | ChatsPage (тот же) | RequireAuth | router.tsx:113 |
| `/dialogs` | TablePage (lazy) | `stats:all` | router.tsx:94 |
| `/stats` | StatsPage (lazy) | `stats:all` | router.tsx:132 |
| `/notifications` | NotificationsPage (lazy) | без гарда | router.tsx:120 |
| `/updates` | UpdatesPage (lazy) | без гарда | router.tsx:102 |
| `/ui-kit` | UiKitPage (lazy) | без гарда | router.tsx:108 |
| `/settings` → `/settings/profile` | Navigate | — | router.tsx:140 |
| `/settings/profile` | ProfilePage | все роли | router.tsx:141 |
| `/settings/accounts` | AccountsPage | `accounts:read\|manage` | router.tsx:144 |
| `/settings/templates` | TemplatesPage | `templates:shared` | router.tsx:148 |
| `/settings/bots`, `/settings/bots/:id` | BotsPage / BotEditor | `bots:manage` | router.tsx:158,162 |
| `/settings/distribution` | DistributionTab | `settings:manage` | router.tsx:175 |
| `/settings/team` | TeamPage | `users:manage\|audit:read` | router.tsx:189 |
| `*` | NotFoundPage | — | router.tsx:200 |

Роли: `admin | head | manager | observer` — `frontend/src/shared/auth/usePermissions.ts:4`, `app/core/rbac.py:7`.

---

# ЧАСТЬ 1. ТЕКСТЫ ИНТЕРФЕЙСА (frontend/src), ПО ЭКРАНАМ

## 1.1. Общий каркас: шапка, рельса, меню пользователя
`frontend/src/app/AppLayout.tsx`

Подписи ролей (локальная копия!): 34 «Администратор», 35 «Руководитель», 36 «Менеджер», 37 «Наблюдатель».

Индикатор соединения: 61 «В сети», 63 «Подключение…», 65 «Сбой на сервере», 66 «Нет соединения»; 69 тултип `Сервер отвечает · v{version}`; 70 aria `Соединение: {label}`.
Полоса соединения: 101 «Нет соединения — переподключаемся…», 108 «✓ Соединение восстановлено».
Бренд: 215 «LeadChat», 220 «by Lead Partner».
Меню пользователя: 140 aria «Меню пользователя», 155 — e-mail как `Menu.Label`, 164 «Профиль», 172 «Что нового», 173 aria «есть непрочитанное», 175 «Выйти».
Шапка: 227 тултип «Горячие клавиши (?)», 231 aria «Горячие клавиши».
Рельса: 244 aria «Основная навигация», 245 «Чаты», 276/281 «Разбор диалогов», 289/294 «Статистика», 301/306 «Настройки».
Бейдж рельсы словами: `frontend/src/shared/stores/badges.ts:60-62` → `в очереди: N`, `непрочитанных: N`, `Чаты — …` / «Чаты».

## 1.2. 404
`frontend/src/app/router.tsx:48` «Страница не найдена», 51 «Такой страницы нет или она переехала», 54 кнопка «К диалогам».

## 1.3. `/login`
`frontend/src/features/auth/LoginPage.tsx`
143 «LeadChat by Lead Partner», 146 «Мы ремонтируем — Вы зарабатываете».
156 «Слишком много попыток входа. Повторите через {отсчёт}», 166 «Сервер недоступен. Проверьте соединение», 109 «Учётная запись отключена, обратитесь к администратору», 119 «Не получилось войти. Попробуйте ещё раз», 212 «Неверный email или пароль».
178 label «Email», 188 label «Пароль», 197 «Запомнить меня», 208 кнопка «Войти», 218 ссылка «Не помню пароль».
Подвал: 229 «Скачать приложение для Windows», 232 `partner-lead-centre.ru`.

### Форма «не помню пароль»
`frontend/src/features/auth/ForgotPasswordForm.tsx`
25 (экспорт-константа) «Если такой сотрудник есть, администратор получит заявку»; 68 «Администратор выдаст новую ссылку установки пароля и передаст её вам.»; 71 и 109 «Вернуться ко входу»; 81 «Не помню пароль»; 84 «Оставьте email — заявка уйдёт администратору, он выдаст новую ссылку установки пароля.»; 88 «Сервер недоступен — заявка не отправлена. Проверьте соединение»; 93 «Слишком много заявок — эта не отправлена. Попробуйте {через N …}»; 106 «Отправить заявку».

## 1.4. `/invite/:token`
`frontend/src/features/auth/InvitePage.tsx`
Сила пароля: 31 «слабый», 32 «средний», 33 «надёжный».
58 «Ссылка устарела», 61 «Ссылка недействительна, устарела или уже использована. Запросите новую у администратора», 64 «Перейти ко входу».
122 «Не получилось загрузить приглашение», 125 «Повторить».
148 «Пароль не подходит: минимум 10 символов», 165 «Здравствуйте, {имя}!», 168 «Придумайте пароль для входа в LeadChat», 176 «Не получилось сохранить пароль. Попробуйте ещё раз».
184 label `Пароль (мин. 10 символов)`, 192 error `Минимум 10 символов`, 203 «Повторите пароль», 207 «Пароли не совпадают», 210 «Сохранить и войти».

## 1.5. `/chats` — список слева
`frontend/src/features/chats/components/list/ChatListPane.tsx`
Вкладки (TABS): 46 «Мои», 47 «Все»; вкладка очереди 471 «Входящие» (+466 aria `Входящие, в очереди: N`).
Фильтр состояния (STATUS_OPTIONS): 58 «Новые», 59 «В работе», 60 «Закрытые».
Заголовок 414 «Чаты», счётчик 425 title «Ждут в очереди» / «Всего диалогов», 426 aria `{…}: {total}`.
Поиск: 436 placeholder «Поиск по имени, телефону или сообщению…», 437 aria «Поиск по диалогам», 442 подсказка «Ctrl+K».
Фильтры: 301/302 «Фильтр по статусу» / «Статус: любой»; 313/314 «Фильтр по каналу» / «Канал: все»; 327/328 «Фильтр по менеджеру» / «Менеджер: все»; 457 aria «Фильтры диалогов»; 551 кнопка «Фильтры»; 557 aria `Сужений: N`.
Очередь: 518 «Разгрузить…».
Найдено: 574 `Найдено: {N}`, 577 «Сбросить ✕».
Ошибки/пустые: 589 «Не получилось загрузить» + 592 «Повторить»; 602 «Ничего не нашлось» + 603 «Ничего не найдено по «{q}». Ищем по имени, телефону и тексту сообщений» + 606 «Сбросить поиск».
Пустые по вкладкам (`EmptyByTab`/`EmptyInbox`): 92 «Очередь пуста» + 93 «Новые обращения появятся здесь — их будет видно и слышно»; 115 «Пока тихо» + 116 «У вас пока нет диалогов» + 119 «Посмотреть новые»; 125 «Все диалоги разобраны» + «Новых диалогов нет — всё разобрано»; 127 «Закрытых диалогов пока нет»; 132 «Диалогов пока нет» + 133 «Подключите аккаунт Авито — и они появятся здесь» + 136 «К настройкам»; 141 «Диалогов пока нет».
Загрузка: 642 aria «Загрузка диалогов»; 411 aria секции «Список диалогов».
Подвал: 672 `Никто не берёт: {N}`.

### Строка списка
`frontend/src/features/chats/components/list/ConversationListItem.tsx`
13 «Сообщений пока нет», 14 «Вложение», 15 префикс `Вы: {текст}`, 82 «Клиент» (фолбэк имени), 201 «без объявления».
Чипы: 106/107 «не отправлено» + title «Ответ оператора не дошёл до клиента — откройте диалог и повторите»; 113/114 «никто не берёт · {ждёт}» + title «От диалога отказались все, кому он доступен»; 118 «негатив» + title «Клиент недоволен».
136 aria `Диалог: {имя}, непрочитанных: N`; 170/171 «Закреплено вами» / «Закреплено»; 177/178 «Диалог передан вам» / «Передан вам»; 250/254 «Передать диалог» / `Передать диалог: {имя}`.

### Индикатор ожидания
`frontend/src/features/chats/components/list/WaitGauge.tsx`
31-37 форматы `{N}м`, `{N}ч`, `{N}д`, «99+д»; 71 title `Отказались все — клиент ждёт {…}`; 72 aria `Никто не ведёт, ждёт {…}`; 99 title `Клиент ждёт ответа {…}`; 100 aria `Ждёт {…}`.

## 1.6. `/chats` — лента справа
`frontend/src/features/chats/components/thread/ChatThreadPane.tsx`
50 «Выберите диалог слева» + «или нажмите Ctrl+K для поиска».
83 тост «Диалог не найден» / «Возможно, ссылка устарела».
166-169 форматы `{N} мин` / `{N} ч` / `{N} дн`.
287 aria секции «Переписка»; 294 aria «Назад к списку»; 303 имя клиента (фолбэк «Клиент»); 332 title «Клиент ждёт ответа»; 334 `ждёт {…}`.
363 «Следующий ({N})»; 399 «Закрыть» (title `Ctrl+Shift+Enter` — расходится с подсказкой композера, см. §5 «Расхождения»); 406/410 aria «Карточка клиента» / кнопка «Клиент».
416 «Не получилось загрузить диалог» / «Загрузка…»; 424 «Диалог ведёт бот — ваше сообщение отключит его»; 430 aria «Загрузка истории»; 440/443 «Не получилось загрузить» / «Повторить»; 448 «Сообщений пока нет»; 478 «↓ новые сообщения».

### Пузыри
`frontend/src/features/chats/components/thread/MessageBubble.tsx`
17-19 «Б», «КБ», «МБ»; 34 aria «Отправляется», 41 «Не доставлено», 47 «Доставлено»; 173 автор-фолбэк «Оператор»; 177 «Бот»; 182 «Заметка · {автор} · видно только сотрудникам»; 194 `Не доставлено: {ошибка}` / «Не отправилось»; 199 «Повторить»; 207 «Удалить».

### Пять действий над диалогом
`frontend/src/features/chats/components/thread/ThreadActions.tsx`
59 aria «Действия с диалогом»; 61/66 «Открепить» / «Закрепить у себя»; 85/89 «Вернуть в очередь»; 99/103 «Позвать коллегу»; 112/116 «Передать диалог»; 128/140 «Снять пометку «нежелательный»» / «Пометить как нежелательного».

### Полоса передачи
`frontend/src/features/chats/components/thread/TransferBar.tsx`
48 «Диалог принят» / «Вы отказались от диалога»; 51-52 «Не получилось» / «Попробуйте ещё раз»; 64 `{Коллега} передаёт вам диалог`; 66 `Ждёт подтверждения: {имя}`; 67 `{кто} → {кому}: ждёт подтверждения`; 81 «Диалог пока за вами — отвечайте клиенту, если он напишет.»; 94 «Принять»; 103 «Отказаться».

### Подвал ленты для «только чтение»
`frontend/src/features/chats/components/composer/ThreadFooter.tsx:69` «👁 Режим просмотра — назначьте менеджера или передайте диалог».

## 1.7. `/chats` — композер
`frontend/src/features/chats/components/composer/Composer.tsx`
275 aria «Панель отправки»; 277 «Диалог закрыт.»; 285 «Вернуть в работу».
298 «Аккаунт Авито отключён — отправка невозможна» + 301 «Переподключить»; 308 «Сообщение слишком длинное — сократите текст»; 330-331 «Авито не принимает файлы от нас — сообщение не уйдёт. Приложите файл к заметке или отправьте клиенту ссылку.»
336 aria «Вложения»; 340 «загрузка…»; 341 «не загрузилось»; 345 aria `Убрать вложение {имя}`.
368 «Длинное сообщение придёт клиенту частями».
384 placeholder «Заметка — увидят только сотрудники» / «Напишите сообщение…»; 385 aria «Текст заметки» / «Текст сообщения».
392/396 «Быстрые ответы · /» / aria «Быстрые ответы»; 408/412 «Прикрепить файл»; 433 aria «Отправить заметку» / «Отправить сообщение».
444 aria «Режим ввода»; 452 «Сообщение»; 461 «Заметка».
469-470 подсказка «Enter — отправить · Shift+Enter — перенос · Ctrl+D — закрыть диалог».

### Пикер быстрых ответов
`frontend/src/features/chats/components/composer/TemplatePickerPopover.tsx`
27 группы «ОБЩИЕ» / «МОИ»; 113 aria «Быстрые ответы»; 116 заголовок «Быстрые ответы»; 123 placeholder «найти шаблон…»; 124 aria «Поиск по быстрым ответам»; 129 aria «Список быстрых ответов»; 137/140 «Не получилось загрузить» / «Повторить»; 146 «Ничего не нашлось» / «Шаблонов пока нет»; 149 «Управлять шаблонами» / «Создать первый»; 181 «↑↓ — выбрать · Enter — вставить · Esc — закрыть»; 183 «Управлять шаблонами».

## 1.8. `/chats` — карточка клиента
`frontend/src/features/chats/components/card/ClientCardPane.tsx`
Статусы: 39 «Новый», 40 «В работе», 41 «Закрыт».
140 «Заметки»; 143 «Заметок пока нет»; 150 автор-фолбэк «Сотрудник»; 162 placeholder «Заметка — увидят только сотрудники»; 163 aria «Текст заметки»; 168 «Отмена»; 171 «Сохранить»; 180 aria `Добавить заметку к диалогу {клиент}`; 182 «+ добавить».
220/222 пустое «Карточка клиента» / «Выберите диалог — здесь появится карточка клиента».
246 тост «Телефон скопирован»; 252 «Клиент»; 256 `★ {rating} · на Авито`; 268 aria «Скопировать телефон»; 273 «(из диалога)»; 277 «телефон не указан».
291 «Нежелательный клиент»; 299 «Сообщения приходят, но диалог не встаёт в очередь.»
312 «Объявление»; 318 «Открыть на Авито →».
324 «Теги»; 353 «Чем закончилось».
366 «Диалог»; 369/374 «Ответственный»; 376 «Без ответственного»; 387/389 «Вы» / «Не назначен».
405 «Позваны»; 412 title `{причина}` или «Позван(а) в диалог»; 415 «Вы»; 422 «Выйти из диалога» / `Убрать {имя}`.
442 «Передать»; 455 «Позвать».
462 «История клиента»; 467 «Повторить»; 471 «Первое обращение клиента»; 478 «Без объявления».

### Модалка «Позвать»
`frontend/src/features/chats/components/card/InviteDialog.tsx`
61 «Позвать в диалог»; 63-64 «Диалог останется за вами. Коллега получит уведомление и увидит диалог в своих «Моих».»; 74 «Кого позвать»; 78 «Зачем зовёте» + 79 placeholder «скажи, чинится ли эта модель»; 88 «ⓘ Придёт в уведомлении — коллега сразу поймёт, что от него нужно»; 93 «Отмена»; 96 «Позвать».

### Модалка «Передать»
`frontend/src/features/chats/components/card/TransferDialog.tsx`
56 «Передать диалог»; 67 «Комментарий коллеге (необязательно)» + 68 placeholder «торгуется, дай скидку до 10%»; 77 «ⓘ Комментарий увидит только команда»; 82 «Отмена»; 85 «Передать».

### Выбор сотрудника (общий для «Передать»/«Позвать»)
`frontend/src/features/chats/components/card/PeoplePicker.tsx`
Роли строчными (ЧЕТВЁРТАЯ копия подписей ролей): 8 «администратор», 9 «руководитель», 10 «менеджер», 11 «наблюдатель».
35 label по умолчанию «Кому»; 36 «Никого не нашлось»; 58 placeholder «поиск по имени»; 64 aria «Сотрудники»; 72/75 «Не получилось загрузить сотрудников» / «Повторить»; 99 aria «в сети»/«офлайн»; 104 « (офлайн)».

### Пометка клиента
`frontend/src/features/chats/components/card/BlockClientButton.tsx`
53 «Клиент помечен» / «Пометка снята»; 55 «Не получилось» / «Попробуйте ещё раз».
65 «Снять пометку» + 67-68 «Клиент снова станет обычным: его обращения будут вставать в очередь и звенеть у команды.» + 72 «Отмена» + 75 «Снять пометку».
83 «Пометить клиента» + 85-87 «Его сообщения по-прежнему будут приходить и сохраняться — вы их не потеряете. Перестанет только одно: диалог не встанет в очередь и не будет звенеть у команды.» + 94 «Почему» + 95 description «Через полгода никто не вспомнит, кого и за что пометили» + 96 placeholder «Пишет каждый день, ничего не заказывает» + 102 «Отмена» + 105 «Пометить».

### Результат обращения
`frontend/src/features/chats/components/thread/OutcomeModal.tsx`
39 «Чем закончилось обращение?»; 78 «Сумма заказа, ₽ — если известна» + 79 «Только для выезда. Можно оставить пустым» + 80 placeholder «например, 4500»; 89 «Закрыть без ответа».
Варианты — `frontend/src/features/chats/outcomes.ts:26-30`: «Выезд назначен» (мастер поедет), «Отказ» (передумал, дорого, нашёл сам), «Не наш профиль» (такую технику не ремонтируем), «Спам» (без подсказки), «Нет ответа» (написал и пропал).

## 1.9. `/chats` — очередь («Входящие»)
`frontend/src/features/chats/inbox/InboxDecisionBar.tsx`
44 aria «Решение по диалогу из очереди»; 46 «Диалог ждёт в очереди — примите его, чтобы ответить»; 56 «Принять диалог»; 65 «Отклонить»; 84 `Диалог принял {кто}`; 88 «Следующий в очереди».

`frontend/src/features/chats/inbox/UnloadQueueDialog.tsx`
42 «Разгрузить очередь»; 44-46 «Закроются диалоги, которые стоят в очереди и в которых давно тихо. Взятые кем-то не тронутся. Если клиент напишет снова, диалог вернётся сам.»; 51 «Тихо дольше, дней»; 64 «Не получилось посчитать»; 68 «Закрывать нечего — таких диалогов нет»; 73 `Закроется диалогов: {N}`; 78 «Без имени»; 80 `· молчит {N} дн`; 86 `и ещё {N}`; 96 «Отмена»; 99 `Закрыть {N}`.

Тосты очереди — `frontend/src/features/chats/inbox/useInbox.ts`:
70-71 «Диалог уже занят» / `Диалог принял {имя}`; 130-131 «Не получилось принять диалог» / «Попробуйте ещё раз»; 171-172 «Диалог вернулся в очередь» / «Его снова видят все операторы»; 177-178 «Не получилось вернуть диалог»; 213-215 «Диалог отклонён» / «От него отказались все» / кнопка «Вернуть»; 225 «Диалог вернулся в вашу очередь»; 229-230 «Вернуть не получилось» / «Диалог уже приняли или закрыли»; 239-240 «Не получилось отклонить» / «Диалог остался в очереди — попробуйте ещё раз»; 281-282 «Очередь разгружена» / «Закрывать было нечего» / `Закрыто диалогов: {N}`; 287 «Не получилось разгрузить».

## 1.10. `/chats` — тосты действий над диалогом
`frontend/src/features/chats/hooks/useConversationActions.ts`
83-84 «Статус не изменился» / «Этот статус уже стоит» / «Не получилось сменить статус — попробуйте ещё раз»; 144-147 «Диалог передан» / `Ответственный: {имя}` / «Диалог вернулся в «Новые»»; 154-160 «Не получилось передать» / «Этому сотруднику нельзя назначать диалоги» / «Сотрудник деактивирован» / «Попробуйте ещё раз»; 187-188 «Позвали в диалог» / `{имя} получит уведомление` / «Коллега получит уведомление»; 195-199 «Не получилось позвать» / «Этот сотрудник и так ведёт диалог»; 215 «Не получилось убрать».
`frontend/src/features/chats/hooks/usePins.ts:34-40` «Не получилось закрепить» / «Закреплённых уже максимум — открепите что-нибудь» / «Закреплять можно только свои диалоги».

## 1.11. Горячие клавиши
`frontend/src/features/hotkeys/HotkeysModal.tsx:18` «Горячие клавиши»; 20-21 «Основные сочетания совпадают с Jivo — переучиваться не нужно. Всё то же самое по-прежнему можно сделать мышью.»
Реестр `frontend/src/features/hotkeys/catalog.ts:23-36`:
Ctrl+R «Принять диалог — как в Jivo»; Ctrl+D «Отметить решённым (закрыть) — как в Jivo»; Ctrl+T «Передать диалог — как в Jivo»; Ctrl+↑/↓ «Предыдущий и следующий диалог — как в Jivo»; Ctrl+Backspace «Отклонить диалог — вне поля ввода; в Jivo такого не было»; Ctrl+K «Поиск по диалогам — из любого раздела»; ↓/↑, J/K «Тоже перемещение по списку, без Ctrl»; Alt+↓/↑ «Следующий диалог с непрочитанными»; Alt+1…4 «Срезы списка: Мои, Все, Новые, Закрытые»; Enter «Открытый диалог — курсор в поле ввода»; Ctrl+Shift+N «Режим внутренней заметки»; «/» «Быстрые ответы — в пустом поле ввода»; Esc «Закрыть по очереди: карточку, заметку, поиск»; «?» «Эта справка — из любого места».
Тот же список во втором месте: `frontend/src/features/settings/profile/HotkeysBlock.tsx:23` «Горячие клавиши», 26-27 тот же абзац про Jivo, 35 «Показать сочетания», 38 «Или нажмите «?» в любом месте программы».

## 1.12. Уведомления (колокольчик, панель, плашки, журнал)
`frontend/src/features/notifications/NotificationBell.tsx:22` aria `Уведомления — непрочитанных: N` / «Уведомления»; 34 тултип «Уведомления».
`frontend/src/features/notifications/NotificationPanel.tsx:53` «не прочитано» (visually-hidden); 77/80 «Уведомления»; 88 «Отметить все прочитанными»; 95 «Пока ничего не происходило»; 115 «Показать все».
`frontend/src/features/notifications/CriticalBanners.tsx:60` «Подтвердить»; 82 aria «Критичные уведомления»; 88 `и ещё {N}`.
`frontend/src/features/notifications/NotificationsPage.tsx`
44-48 периоды «Всё время», «Сегодня», «7 дней», «30 дней», «Произвольный»; 52 «Любая важность»; 127 «Прочитано»; 184-187 «Уведомления» + «Вашей роли уведомления не приходят»; 194/199 «Уведомления» + 202 «Отметить все прочитанными»; 210 aria «Важность»; 223-224 aria «Тип события» / placeholder «Тип: любой»; 237 aria «Период»; 256-257 aria «Произвольный период» / placeholder «Выберите даты»; 273 «Только непрочитанные»; 291/294 «Не получилось загрузить уведомления» / «Повторить»; 300 «За выбранный период уведомлений нет»; 309-314 шапка таблицы «Время», «Важность», «Событие», «Что произошло», «Действие»; 328 `{от}–{до} из {всего}`; 337 «Назад»; 345 «Вперёд»; 354/356 «Новая ссылка установки пароля» + «Ссылка показывается один раз и действует 72 часа. Передайте её сотруднику лично.».
Каталог типов и важностей `frontend/src/features/notifications/catalog.ts`:
27-29 «Критичное», «Важное», «Обычное».
76-124 подписи типов: «Аккаунт требует переподключения», «Приём сообщений остановился», «Резервное копирование не выполнилось», «Планировщик не отвечает», «Система не отвечает снаружи», «Очередь входящих не разбирается», «Сообщения не уходят клиентам», «На диске мало места», «Сертификат скоро истекает», «AI временно недоступен», «Запрос на сброс пароля», «Сообщение администратору», «Учётная запись заблокирована», «Клиент недоволен», «Диалог без ответа», «Клиент вернулся в закрытый диалог», «Вам передали диалог», «Клиент ждёт вашего ответа», «Диалог никто не принял», «Ответ не дошёл до клиента», «Очередь разгружена автоматически», «Сообщения от клиентов не разбираются», «Канал отобрали: подписка пропала».
127 фолбэк «Уведомление»; 142-144 «Открыть диалог», «Открыть аккаунты», «Открыть команду»; 157 «Подтвердить».
Время: `frontend/src/features/notifications/time.ts:16` «только что»; 19 `{N} минуту/минуты/минут назад`; 30 `повторялось {N} раз/раза/раз`.
Тосты действия: `useNotifications.ts:149-150` «Готово» / «Действие выполнено»; 159-160 «Действие не выполнено» / «Попробуйте ещё раз».

## 1.13. Присутствие
`frontend/src/features/presence/PresenceMenuItems.tsx:26` «Моё состояние»; 34 «На месте»; 43 «Отошёл — новых обращений не давать».
`frontend/src/features/presence/usePresence.ts:59-60` «Состояние не сохранилось» / «Попробуйте ещё раз — пока система считает, что вы на месте».

## 1.14. `/dialogs` — «Разбор диалогов»
`frontend/src/features/table/TablePage.tsx`
46-48 статусы «Новый», «В работе», «Закрыт».
54-58 периоды «7 дней», «30 дней», «90 дней», «За всё время», «Свой период».
105-109 форматы `{N} с`, `{N} мин`, `{N} ч {N} м`.
Колонки: 181 «Клиент» (185 фолбэк «Клиент»), 193 «Статус», 203 «Оператор», 212 «Канал», 221 «Первый ответ», 235 «Длительность», 241 «Сообщений», 247 «Объявление», 256 «Последнее (МСК)».
Заголовок: 317 «Диалоги», 320 «Разбор обращений: скорость ответа, объём переписки, кто вёл.», 324 `Найдено: {N}`.
Фильтры: 331 aria «Период»; 342-343 «Фильтр по статусу» / «Статус: любой»; 354-355 «Фильтр по каналу» / «Канал: все»; 367-368 «Фильтр по оператору» / «Оператор: все»; 380-385 «Фильтр по боту» / «Бот: неважно» / «Бот ведёт» / «Бот выключен»; 403-404 «Фильтр по метке» / «Метка: любая»; 418-419 «Произвольный период» / «Выберите даты»; 431 «Сбросить».
441 «Выгрузить CSV»; 284-285 «Не удалось выгрузить — попробуйте ещё раз» / «Выгрузка не получилась».
452/455 «Не получилось загрузить» / «Повторить»; 462-466 «Ничего не нашлось» / «Попробуйте расширить период или снять фильтры» / «Сбросить фильтры»; 509 title «Открыть диалог»; 531 «Назад»; 534 `{от}–{до} из {всего}`; 542 «Дальше».

## 1.15. `/stats`
`frontend/src/features/stats/StatsPage.tsx:87` `{имя} (отключён)`; 123 «Статистика»; 146/158 «Не получилось загрузить статистику» / «Повторить».
Фильтры `components/StatsFilters.tsx`: 75 «Период»; 91-92 «Произвольный период» / «Выберите даты»; 112-113 «Менеджеры» / «Менеджеры: все»; 124-125 «Аккаунт Авито» / «Аккаунт: все»; 134 тултип «Агрегаты обновляются раз в час»; 141 «⬇ Экспорт».
Карточки `components/SummaryCards.tsx`: 38 «За период»; 42-52 «Новые диалоги» (tooltip «Диалоги, где первое сообщение клиента пришло в выбранный период»), «Закрыто» (tooltip «Диалоги, переведённые в статус «Закрыт» в выбранный период»); 59-60 «Прямо сейчас» + «не зависит от выбранного периода»; 65-78 «В работе», «Ждут ответа», caption «сейчас», tooltips «Снимок на сейчас — с прошлым периодом не сравнивается», «Диалоги в работе, где последнее сообщение — от клиента»; 85 «Качество работы за период»; 89-97 «Первый ответ (медиана)» + развёрнутый tooltip с «В рабочие часы 10:00–20:00…», «Нет данных за период»; 102-109 «Закрыто ботом», единица «пп», tooltip `{N} из {M} закрытых диалогов бот довёл без оператора`; 114-120 «Телефонов» + `Бот N · из текста N · вручную N`; 125-135 «Повторные» + «↩ вернулись · было {N}» + tooltip про переоткрытых.
`components/StatCard.tsx:20` единица «пп»; 51 «(hint)» + «к пред.».
График `components/MetricChart.tsx`: 14-19 метрики «Новые диалоги», «Закрыто диалогов», «Входящие сообщения», «Исходящие сообщения», «Первый ответ (медиана)», «Собрано телефонов»; 109 «нет данных»; 148/151 aria «График метрики» / «График»; 157 aria «Метрика графика»; 166 aria «Группировка»; 174 «по дням»; 177 тултип «По часам» доступно при периоде не больше 7 дней»; 191 «по часам»; 204/207 «Не получилось загрузить график» / «Повторить»; 340 «Нет данных за выбранный период».
Тепловая карта `components/Heatmap.tsx`: 29 aria «Тепловая карта входящих»; 33 «Когда пишут клиенты»; 36 «Входящие сообщения, день недели × час — видно, когда нужен дежурный»; 41 «все менеджеры»; 51/54 «Не получилось загрузить карту» / «Повторить»; 59 aria «Входящие по дням недели и часам»; 91/95 «меньше» / «больше».
`lib/heatmap.ts:42-51` «пн»…«вс», «Понедельник»…«Воскресенье»; 54-65 тултип `Вт 14:00 — N входящих`.
Таблица менеджеров `components/ManagersTable.tsx`: 31 «Менеджер»; 35 «ИТОГО»; 39 «Принято»; 47 «Отвечено»; 55 «Закрыто»; 63-64 «Первый ответ» + hint «Медиана времени от сообщения клиента до первого ответа оператора — за все часы суток»; 72-73 «В рабочие часы» + hint; 81 «Сообщений»; 117/119 aria «Таблица менеджеров» / «Менеджеры»; 134/137 «Не получилось загрузить таблицу» / «Повторить»; 154 «Нет данных за выбранный период»; 198 «Действия»; 221 aria `Показать статистику: {имя}`; 224 «отключён»; 232 тултип «Открыть диалоги этого менеджера»; 236 aria `Диалоги: {имя}`; 242 «Диалоги».
Экспорт `components/ExportModal.tsx`: 9-11 листы «Сводка», «Менеджеры», «Диалоги»; 51 «Выгрузка статистики»; 54 `Период: {…}`; 58/61 «Формат» / aria «Формат выгрузки»; 71/74 «Листы» / aria «Листы выгрузки»; 84 «CSV содержит только лист «Диалоги» — по строке на диалог»; 90 `Выгрузка учитывает выбранный фильтр по менеджерам ({N})`; 96 «Отмена»; 103 «Выгрузить»; 109 «Предыдущая выгрузка ещё готовится — дождитесь её окончания».
`hooks/useStatsExport.tsx`: 23 фолбэк имени файла «выгрузка»; 28-33 «Предыдущий экспорт ещё готовится», «Лимит выгрузок на сегодня исчерпан», «Период больше 366 дней — сузьте диапазон», «Не получилось запустить выгрузку»; 60-61 «Готовим выгрузку…» / «Файл появится здесь через несколько секунд»; 67 «Выгрузка не запущена»; 82-88 «Выгрузка готова» / `Скачать {файл}` / «Файл готов»; 99-100 «Выгрузка не удалась» / «Попробуйте сузить период»; 115-116 «Статус выгрузки недоступен» / «Проверьте соединение и запустите выгрузку заново».
`lib/delta.ts:48` «быстрее» / «медленнее»; `lib/format.ts:18-26` «с», «м», «ч»; 36 единица «пп»; 58 `Данные на {время}`.

## 1.16. Виджет «Моя статистика за сегодня»
`frontend/src/features/stats/MyTodayWidget.tsx:35-41` «В работе сейчас», «Ждут моего ответа», «Взято сегодня», «Закрыто сегодня», «Отправлено сообщений», «Первый ответ (медиана)», «Отвечено диалогов»; 78/131 «Статистика за сегодня недоступна»; 87 aria «Моя статистика за сегодня»; 89 `Сегодня: N взято · N закрыто`; 97 `{N} ждут ответа`; 106/126/128 «Моя статистика за сегодня».

## 1.17. `/settings` — навигация
`frontend/src/features/settings/SettingsLayout.tsx:14` aria «Разделы настроек»; 17 «Аккаунты Авито»; 22 «Быстрые ответы»; 27 «Боты»; 32 «Распределение»; 37 «Команда»; 41 «Профиль».

## 1.18. `/settings/accounts`
`frontend/src/features/settings/accounts/AccountsPage.tsx`
43-45 статусы «работает», «требует переподключения», «отключён».
71-74 состояние вебхука «не снят — Авито не ответил», «сбой», «не зарегистрирован»; 85-86 «Вебхук молчит — сообщения доезжают реконсиляцией с задержкой» / aria «Вебхук молчит»; 92-93 «Канал выключен, но подписка на стороне Авито осталась нашей — сообщения по-прежнему идут сюда. Нажмите «Отключить» ещё раз, когда Авито ответит.» / aria «Подписка не снята».
120 `Операторы: все/{N}`; 138-139 `Назначены: {список} и ещё {N}`; 151 aria `Назначить операторов на аккаунт {…}`; 154 «Назначить»; 160 «Никто не назначен — обращения канала видят все операторы».
Тосты: 179-180 «Не получилось начать переподключение»; 210-211 «Аккаунт отключён» / «Приём остановлен, переписка удалена»; 216 «Не получилось отключить»; 223 «Аккаунт включён»; 228-232 «Не получилось включить» / «Токены устарели за время простоя — нужно переподключение через Авито»; 242 «Подписка на входящие обновлена»; 247-251 «Авито не принял подписку» / «Сначала включите аккаунт» / «Попробуйте ещё раз через минуту»; 272 «Аккаунт переименован»; 275 «Не переименовалось»; 282 «Аккаунт удалён»; 289-290 «Аккаунт не удалён»; 300 «Токен обновлён»; 305-311 «Не получилось обновить» / «Авито отозвал доступ — нужно переподключение» / «Токен уже обновляется, подождите несколько секунд».
Карточка: 322 aria `Аккаунт {…}`; 327 `Статус: {…}`; 332 `id {…}`; 357/363/364 «Название аккаунта» / «Название» / «Так аккаунт будет называться в списках, фильтрах и статистике. На стороне Авито ничего не изменится.»; 372/379 «Отмена» / «Сохранить»; 388 `Токен: активен, до {…}`; 393 aria «Загрузка истории»; 395 `Загружаем историю… {N} чатов`; 412-413 `Загрузка истории сорвалась на {N} чатах. Уже загруженное на месте — повтор продолжит с этого места.`; 417 `Подключён {дата}`; 427 «Обновить токен»; 435 «Требует переподключения»; 438 «Токен отозван на стороне Авито. Приём сообщений остановлен.»; 446 «🔄 Переподключить»; 453-454 «Отключён вручную, переписка удалена. Включить можно в любой момент — новые обращения снова начнут приходить.»; 474 «Переименовать»; 483/485 «Подписаться на входящие заново, не переподключая аккаунт через Авито» / «Обновить подписку»; 499-501 confirm «Отключить аккаунт «{…}» и удалить его переписку? Приём новых обращений остановится, подписка на стороне Авито снимется, а все диалоги и сообщения этого аккаунта будут удалены без возможности вернуть.»; 508 «Отключить и стереть»; 520 «Включить»; 534-535 confirm «Удалить аккаунт «{…}» из системы? Вместе с ним будут удалены все его диалоги и сообщения. Отменить нельзя.»; 542 «Удалить».
Экран: 566-567 «Аккаунт подключён» / «Загружаем историю чатов…»; 572-573 «Переподключение отменено» / «Вы авторизовали другой аккаунт Авито»; 579-580 «Не удалось подключить аккаунт»; 609-610 «Аккаунты Авито» / «Каналы, обращения которых приходят в «Чаты»»; 625/675 «Подключить аккаунт»; 637-638 «Это не настоящий Авито. Система работает на встроенном имитаторе: он примет любые ключи и покажет выдуманный аккаунт. Настоящие обращения приходить не будут.» + 641 «Переключить»; 654/657 «Не получилось загрузить» / «Повторить»; 664-671 «Подключите первый аккаунт Авито» / «Нужны Client ID и Client Secret из кабинета разработчика того аккаунта — и обращения по всем его объявлениям пойдут в «Чаты»» / «Аккаунты подключает администратор».

### Неделя канала
`frontend/src/features/settings/accounts/WeekBars.tsx:26` «вс/пн/…/сб»; 58 «За неделю обращений не было»; 68 aria `Обращения по дням недели, всего {N}`; 89 `{N} обращение/обращения/обращений за неделю`; 93 тултип «Обращения, на которые никто ни разу не ответил»; 102 `· {N} без ответа`.

### Назначение операторов
`frontend/src/features/settings/accounts/AssignOperatorsModal.tsx`
31 «не может отвечать клиентам»; 98 «оператор»; 109 « · снимите галочку, чтобы сохранить аккаунт»; 182-186 «Операторы назначены» / `Канал «{…}» доступен всем операторам` / `На аккаунт «{…}» назначено: {N}`; 195 «Не получилось сохранить. Попробуйте ещё раз»; 226 `Назначить операторов на аккаунт «{…}»`; 230 «Только сотрудники, назначенные операторами, могут общаться с клиентами этого аккаунта.»; 237-245 «Изменения не сохранены» / «Закрыть экран и потерять расставленные галочки?» / «Остаться» / «Закрыть без сохранения»; 264/267 «Не получилось загрузить список сотрудников» / «Повторить»; 274-275 «Поиск по имени» / «Имя сотрудника»; 282 `Выбрано: {N} из {M}`; 289-294 «Набор нельзя сохранить» + «больше не ведёт/ведут аккаунт. Снимите галочку/галочки, чтобы сохранить: вернуть его/их можно будет, когда сотрудник снова станет оператором.»; 298/301 aria «Сотрудники» / «Никого не нашли»; 321-323 «Никто не выбран» + «Аккаунт будет доступен всем операторам. Так задумано: аккаунт без назначенных не закрывается, а открывается для всех, иначе обращения повисли бы.»; 331/341 «Отмена» / «Сохранить».

### Мастер подключения
`frontend/src/features/settings/accounts/ConnectChannelWizard.tsx`
96 «Переключено на настоящий Авито»; 100-101 «Не переключилось»; 131-135 «Это тот же аккаунт, что уже подключён» + «Ключи принадлежат каналу «{…}» — они обновлены, новый канал не появился. Для другого аккаунта Авито нужны его собственные Client ID и Client Secret, созданные в его кабинете разработчика.»; 142-143 `Аккаунт «{…}» подключён` / «Новые обращения по его объявлениям пойдут в «Чаты»»; 153-154 «Не подключилось»; 169 «Подключить аккаунт Авито»; 173-175 «Нужны два значения из кабинета разработчика Авито того аккаунта, который подключаете. Пароль от аккаунта не требуется — если ключей у вас нет, попросите их у того, кто ведёт этот аккаунт.»; 180 placeholder «из кабинета разработчика Авито»; 195-198 «Система смотрит на встроенный имитатор» + «Он примет любые ключи и покажет выдуманный аккаунт — настоящих обращений не будет. Переключите на настоящий Авито перед подключением.»; 206 «Переключить на настоящий Авито»; 214-219 «Приложению нужны права» + «Без них Авито примет ключи, но переписку не отдаст.»; 225 «Отмена»; 232 «Сначала переключите систему на настоящий Авито»; 237 «Подключить».

## 1.19. `/settings/templates` + менеджер шаблонов
`frontend/src/features/settings/templates/TemplatesPage.tsx:15-16` «Быстрые ответы» / «Общие шаблоны видит вся команда, личные — только вы».
`frontend/src/features/templates/TemplatesManager.tsx`
20 переменные «{имя}», «{менеджер}», «{объявление}»; 34 суффикс «(копия)»; 61 «Шаблон обновлён» / «Шаблон создан»; 64 «Не сохранилось» / «Попробуйте ещё раз»; 73 «Редактировать быстрый ответ» / «Новый быстрый ответ»; 79 «Название»; 85 «Текст»; 94 `{N} символов · вставить:`; 104-105 «Папка» / «без папки»; 119-120 «Общий шаблон — его увидит вся команда» / «Личный шаблон — виден только вам. Перевести личный в общий нельзя: создайте новый общий»; 125/139 «Отмена» / «Сохранить»; 164 «Шаблон удалён»; 167 «Не удалилось»; 191 aria «Область шаблонов»; 199 «Общие»; 208 «Мои»; 214-215 «поиск» / aria «Поиск по шаблонам»; 224 «+ Шаблон»; 231 «Все»; 239 «без папки»; 253/256 «Не получилось загрузить» / «Повторить»; 263-267 «Общих шаблонов нет» / «Личных шаблонов нет» + «Создайте первые — их увидит вся команда» / «Быстрые ответы экономят десятки минут в день»; 302-305 шапка «Название», «Текст», «Папка», aria «Действия»; 323 aria `Действия: {…}`; 329/332/335 «Редактировать» / «Дублировать» / «Удалить»; 359-363 «Удалить быстрый ответ?» + «Шаблон «{…}» исчезнет из пикера у всех, кто им пользуется»; 367/374 «Отмена» / «Удалить».
`frontend/src/features/templates/vars.ts:21` фолбэк «Клиент»; 23-32 подстановка «имя», «менеджер», «объявление».

## 1.20. `/settings/bots`
`frontend/src/features/settings/bots/BotsPage.tsx`
30/40/90 «Не удалось переключить бота» / «Не удалось создать бота» / «Не удалось дублировать бота» + «Попробуйте ещё раз»; 56 «Бот удалён»; 64-67 «Бот не удалён» / `Сначала отвяжите каналы: {…}`; 78 `{имя} (копия)`; 98-99 «Боты» + «Бот здоровается и собирает контекст, пока менеджеры заняты. Как только пишет оператор — бот замолкает в этом диалоге навсегда»; 102/135 «+ Создать бота»; 118-122 «Не удалось загрузить ботов» / «Проверьте соединение и попробуйте ещё раз» / «Повторить»; 131-132 «Ботов пока нет» / «Создайте первого из шаблона «Первичный приём» — он поздоровается и соберёт контакт, пока менеджеры заняты»; 145-150 колонки «Бот», «Вкл», «Аккаунты», «Расписание», «Диалогов/7д»; 171-172 `{N} шаг(ов)` + « · база знаний есть» / « · без базы знаний»; 178 aria `Включить бота {…}`; 214 «Дублировать»; 224-226 confirm «Удалить бота «{…}»? Сценарий и база знаний пропадут без возврата. Если бот привязан к каналам, система откажет и назовёт их.»; 233 «Удалить».

### Редактор
`frontend/src/features/settings/bots/BotEditor.tsx`
149 «Не удалось переключить бота»; 161 «Сценарий сохранён» / `Бот «{…}» обновлён`; 170 «Не удалось сохранить»; 190-194 «Бот не найден» / «Возможно, его удалили» / «К списку ботов»; 212 «← К списку ботов»; 218 «Имя бота»; 224/227 «Включён»; 233-234 «Аккаунты Авито» / «Аккаунт может быть привязан только к одному боту»; 245 `{…} — сейчас за другим ботом, при сохранении перепривяжется`; 254-255 «База знаний ai_answer» / «Текст целиком уходит в промпт модели на шаге ai_answer. Пишите фактами: услуга — цена от — срок»; 271 «Сценарий»; 282/297 «+ добавить шаг» / «+ Добавить шаг»; 308 «Сохранить с предупреждениями» / «Сохранить»; 311 «🧪 Протестировать»; 314 «Отменить изменения»; 318 «Есть несохранённые изменения»; 345 «Ошибки блокируют сохранение — почините отмеченные шаги».

### Типы шагов и словари
`frontend/src/features/settings/bots/scenario.ts:30-38` «Сообщение» (Отправить текст клиенту), «Вопрос» (Задать вопрос и ждать ответ), «Меню» (Вопрос с вариантами ответа), «Условие» (Ветвление без вопроса), «Ответ AI» (Ответ Claude по базе знаний), «Оператору» (Передать диалог человеку), «Закрыть» (Закрыть диалог), «Тег» (Повесить теги на диалог), «Заметка» (Внутренняя заметка для менеджера).
55-61 дни «пн»…«вс»; 66-72 таймауты «30 минут», «1 час», «2 часа», «12 часов», «24 часа», «3 дня», «без лимита»; 76-80 условия «рабочее время», «переменная существует», «переменная равна», «текст содержит», «текст по regex»; 84-86 валидаторы «любой ответ», «телефон», «число».
125-188 значения по умолчанию: «Текст сообщения», «Ваш вопрос клиенту?», «Выберите вариант:\n1. Первый\n2. Второй», «Первый»/«Второй», «Ответьте, пожалуйста, цифрой 🙂», «Заметка для менеджера».
340-390 сводки шага: `рабочее время {…}`, `есть {…}`, `текст содержит {…}`, `текст по regex {…}`, `ask → {…} · таймаут {…}`, `menu: {N} вариант(ов) · «{…}»`, `→ иначе {…}`, `ai_answer · порог {…}`, `close · молча`, «никогда», «МСК», «пн–вс».

### Формы шагов
`components/StepForms/SendForm.tsx:12-19` «Текст» / «1–1000 символов. Доставка асинхронная — бот не ждёт подтверждения Авито» / «далее →».
`AskForm.tsx:27` «Регулярка не компилируется»; 36-37 «Вопрос» / «Можно оставить пустым — тогда вопрос уже задан предыдущим шагом send»; 45-49 «Переменная» / «Латиница, с буквы: phone, problem» / «Только латиница — это имя ключа в bot_vars»; 54 «Валидатор»; 68-69 «Регулярное выражение» / «re.search по ответу клиента, до 200 символов»; 79-80 «Retry-текст» / «Отправляется, когда ответ не прошёл валидацию»; 89 «Попыток»; 96 «Таймаут»; 107 «Без таймаута бот может ждать ответа вечно»; 112/118/125 «ответ →» / «по таймауту →» / «попытки исчерпаны →».
`MenuForm.tsx:23-24` «Текст вопроса» / «Варианты перечисляйте цифрами — клиент отвечает номером или словом»; 31-32 «Переменная (необязательно)» / «Сюда запишется id выбранного варианта»; 41 `Варианты ({N}/10)`; 47-56 «Вариант», `Вариант {N}: название`, «Ключевые слова через запятую», `Вариант {N}: ключевые слова`; 71-72 «переход →»; 81 `Удалить вариант {N}`; 100 «Новый вариант»; 108 «+ вариант»; 113/122/129 «Retry-текст» / «Попыток» / «Таймаут»; 140-141 «мимо вариантов →» / «Несовпадение считается «мимо сценария» (условие handoff №4)»; 148 «по таймауту →».
`ConditionForm.tsx:30-45` aria «Рабочее время с» / «Рабочее время до» / «Часовой пояс»; 70 «Интервал через полночь — день относится к его началу»; 78-124 aria «Имя переменной», «Значение переменной», «Ключевые слова через запятую», «Регулярное выражение»; 143 «Условия проверяются сверху вниз, побеждает первое истинное»; 149 «если»; 152/175/189 `Вид условия {N}` / `Условие {N}: переход` / `Удалить условие {N}`; 208 «+ условие»; 212-213 «иначе →» / «Обязательная ветка: сюда бот идёт, если ни одно условие не сработало».
`AiAnswerForm.tsx:21` `Порог уверенности: {…}`; 30 aria «Порог уверенности»; 34 «Ниже порога (или если модель сама просит человека) — переход «при низкой уверенности»»; 40-41 «Макс. длина ответа» / «символов»; 48-49 «Глубина контекста» / «последних сообщений диалога»; 58-59 «Приватность: перед отправкой в модель телефоны в тексте заменяются на {PHONE} — найденный номер остаётся в переменных бота и в карточке клиента»; 63/69 «далее →» / «при низкой уверенности →».
`HandoffForm.tsx:18-37` «завершает сценарий», «Причина» / «Попадёт в bot_vars.handoff.reason и в журнал аудита», «Комментарий менеджеру» / «Ляжет заметкой в диалог — её видят только сотрудники», «Теги» / «Добавятся к conversations.tags».
`CloseForm.tsx:18-35` «завершает сценарий», «Закрыть молча», «Прощальное сообщение», «В ленту добавится заметка «Диалог закрыт ботом»».
`TagForm.tsx:11-19` «Теги» / «До 50 символов каждый; дубликаты схлопываются» / «далее →».
`NoteForm.tsx:12-19` «Текст заметки» / «Видна только сотрудникам — жёлтая вставка в ленте» / «далее →».
`StepRefSelect.tsx:11` «— дефолт: передать оператору —»; 39 `{id} (нет такого шага)`.
`VariableTextarea.tsx:60-67` «Вставить переменную», `Вставить переменную: {…}`, «Переменные».
`StepCard.tsx:80` aria `Идентификатор шага {…}`; 98 «старт»; 108/113 «Ошибка в шаге» / «Предупреждение в шаге»; 117-144 «Выше», «Ниже», «Удалить шаг», `Переместить выше/ниже: {…}`, `Удалить шаг {…}`; 158 «Тип шага»; 172 «Сделать стартовым»; 196-217 «Удалить шаг?» / «Сменить тип шага?» + «Шаг {…} будет удалён. Переходы других шагов на него придётся поправить вручную.» / «Параметры шага сбросятся на значения по умолчанию нового типа.» + «Отмена» / «Удалить» / «Сменить тип».
`ScheduleEditor.tsx:45-52` «Расписание», «Круглосуточно», «По расписанию»; 75/86 `Интервал {N}: начало` / `: конец`; 94 «через полночь»; 101 `Удалить интервал {N}`; 118 «+ интервал»; 121 `Время московское ({tz})`.

### Валидация сценария (текст ошибок в интерфейсе)
`frontend/src/features/settings/bots/validation.ts`
54/60 `Шаг {…}: регулярка длиннее 200 символов` / `…не компилируется`; 68 `Идентификатор «{…}»: только латиница в нижнем регистре, цифры и _`; 75/78 `текст обязателен` / `текст длиннее {N} символов`; 88 `имя переменной — латиница, с буквы, до 32 символов (например phone)`; 96 `без таймаута бот может ждать вечно`; 102/107/117 `у меню нет ни одного варианта` / `у варианта «{…}» нет ключевых слов` / `слово «{…}» есть в нескольких вариантах`; 124/129/132 `нет ни одного условия` / `у условия «текст содержит» нет слов` / `в условии не указана переменная`; 138 `не выбрано ни одного тега`; 230 «В сценарии нет ни одного шага»; 237 `Идентификатор «{…}» встречается дважды`; 245 `Стартовый шаг «{…}» не найден в сценарии`; 251 `переход на несуществующий шаг «{…}»`; 261 `Шаг {…} недостижим из стартового шага`; 268 `из него нельзя дойти до handoff или close — сценарий повиснет`; 274 `цикл без вопроса клиенту — бот зациклится`; 291 `Переменную {…} пишут несколько шагов: {…}`; 299 `переменная {…} нигде не заполняется`; 309 `сценарий стартует молчаливым ожиданием ответа`; 315 «База знаний пуста — шаг ai_answer ответит хуже»; 340-347 «Имя бота не может быть пустым», `Имя длиннее {N} символов`, `База знаний длиннее {N} символов`; 356-363 «Расписание без интервалов не включит бота никогда», `Интервал {N}: начало и конец совпадают`, `Интервал {N}: не выбран ни один день недели`.

### Песочница
`frontend/src/features/settings/bots/SandboxDrawer.tsx`
28-29 «бот вне расписания — в диалог не входит» / «бот сейчас ничего не ждёт — таймаут промотать нечему»; 43-112 трасса: `→ шаг {…}`, «жду ответ», `дедлайн {…}`, «ответ принят», «🔀 условие», «↻ переспрашиваю (…, попытка N)», «↯ сообщение мимо сценария: N», «🔎 сработал детектор: клиент просит человека», «⏩ таймаут шага {…}», «таймаут не в счёт — клиент успел ответить», « · модель просит оператора», «🏷 теги: …», «Диалог закрыт ботом»; 129-130 демо-значения «Иван» / «Ремонт iPhone»; 177 «⏩ таймаут»; 222 `🧪 Песочница: {…} (черновик)`; 227-230 «Время» / «сейчас» / «задать»; 236 aria «Время симуляции»; 244-245 «заглушка» / «настоящий»; 252/259 «Клиент» / «Объявление»; 266 «Сбросить»; 272 «Настоящий режим делает живые вызовы Claude и расходует токены. Для отладки графа хватает заглушки»; 277 «Сначала почините сценарий»; 293-296 «Типовой чек перед включением: дневной сценарий → «сейчас ночь» → невалидный телефон дважды → «позовите оператора» → «ужасный сервис!». Так за минуту проверяются все шесть условий передачи оператору»; 311 «Состояние»; 314 `Шаг: {…}` + бейдж «WAITING»; 318-319 `Ждёт: {…}` / ` · до {…}`; 338-345 «счётчики», `шагов: N/M · подряд бота:`, `мимо сценария: N/M · вызовов AI:`; 357 «⏩ Промотать таймаут»; 364-365 aria «Ответ клиента» / placeholder «ответ клиента…»; 387 aria «Отправить ответ клиента».

### Сценарий по умолчанию (тексты, которые бот шлёт клиенту)
`frontend/src/features/settings/bots/defaultScenario.ts:18` «Здравствуйте, {client_name}! Это сервис Lead Partner 👋\nПодскажите, что случилось с техникой — модель и проблему?»; 62-63 комментарий «Клиент описал проблему, бот дал предварительный ответ» + тег «первичный-приём»; 69 «Мастер ответит утром. Оставьте телефон — перезвоним первыми ✔»; 79 «Кажется, это не номер телефона 🙂 Напишите в формате +7 900 000-00-00»; 90 тег «контакт собран»; 96 «🤖 Бот собрал контакт: {phone}\nПроблема со слов клиента: {problem}»; 104-105 «Ночной диалог: проблема зафиксирована, перезвонить утром первыми» + тег «ночной-лид»; 118-120 база знаний «Замена экрана iPhone — ориентировочно от 8900 ₽, срок 1–2 часа.» / «Диагностика бесплатная, точная стоимость — после неё.» / «Работаем ежедневно 10:00–20:00, приём техники без записи.»; 124 имя по умолчанию «Новый бот».

## 1.21. `/settings/distribution`
`frontend/src/features/settings/distribution/DistributionTab.tsx`
66 «Сохранено»; 70-71 «Не удалось сохранить» / «Попробуйте ещё раз»; 101/103 «Не удалось загрузить настройки» / «Повторить»; 118-119 «Распределение диалогов» / «Обычно новое обращение ждёт во «Входящих», пока его кто-нибудь не примет. Можно раздавать автоматически — тому, кто сейчас свободнее всех»; 127 «Раздавать диалоги автоматически»; 133-134 «Новое обращение сразу уходит свободному оператору» / «Всё как сейчас: диалоги принимают вручную из «Входящих»»; 140-144 «Сколько диалогов держать на одном операторе» + «Больше этого числа система ему не отдаст — обращение подождёт во «Входящих», где его видят все.»; 153 aria «Потолок диалогов на оператора»; 163 «Без ограничения»; 186 «Сохранить»; 190 «Есть несохранённые изменения»; 196-197 «Диалог уходит только тому, кто сейчас в сети и назначен на этот канал. Если подходящих нет — обращение остаётся во «Входящих».»
`WorkHoursBlock.tsx:59` «Сохранено» / «Отчёты пересчитаются в течение часа»; 63-64 «Не удалось сохранить»; 73 «Рабочие часы»; 76-77 «Окно, по которому считается скорость первого ответа. Время вне его в расчёт не идёт: ночная пауза не портит цифру, но и не украшает её.»; 83/93 «С» / «До»; 111-112 «Конец не позже начала — рабочих часов не останется, и скорость ответа будет нулевой.»; 130 «Сохранить часы»; 134 «Есть несохранённые изменения»; 140 «Отчёты пересчитываются раз в час — новые цифры появятся не мгновенно.»

## 1.22. `/settings/team`
`frontend/src/features/settings/team/TeamPage.tsx:27/33` вкладки «Сотрудники» / «Журнал аудита»; 47 «Команда» / «Сотрудники, их роли и журнал действий»; 51 «Для вашей роли здесь пока нет разделов»; 54 aria «Разделы команды».
`roles.ts:6-9` «Администратор», «Руководитель», «Менеджер», «Наблюдатель»; 13-16 подсказки «Весь интерфейс: сотрудники, аккаунты Авито, боты, статистика, журнал» / «Все диалоги без отправки, статистика всех сотрудников, журнал аудита» / «Переписка с клиентами, личные шаблоны и своя статистика за день» / «Только чтение диалогов: без ответов, заметок и смены статусов».
`TeamMembersTab.tsx`
45-47 «отключён», «ждёт пароля», «активен»; 63-66 «отошёл», «в сети», «не в сети»; 109 «Нельзя оставить систему без администратора»; 113 «Не получилось выполнить действие. Попробуйте ещё раз»; 222 aria «Сотрудники»; 227-228 «Поиск сотрудника» / «Имя или email»; 236-237 «Фильтр по роли» / «Роль: любая»; 248 «Показывать отключённых»; 256 «+ Пригласить»; 275/279 «Не получилось загрузить список сотрудников» / «Повторить»; 289 «Никого не нашлось» / «Пригласите первого сотрудника»; 301-315 колонки «Сотрудник», «Роль», «Отдел», «Ведёт диалоги», «Статус», «Онлайн», «Действия»; 331/347/365/396 aria `Роль: {…}`, `Отдел: {…}`, `Ведёт диалоги: {…}`, `Действия: {…}`; 411 «Выслать новую ссылку» / «Сбросить пароль»; 420 «Задать пароль»; 431 «Отключить (нельзя себя)» / «Отключить»; 440 «Включить»; 452 «Удалить (нельзя себя)» / «Удалить»; 466 `{от}–{до} из {всего}`; 475/483 «Назад» / «Вперёд»; 494 `{имя} приглашён` / «Ссылка установки пароля»; 505-510 «Сменить роль» / «Отключить сотрудника» / «Удалить сотрудника» / «Включить сотрудника»; 521 `{имя} станет: {роль}. Права применятся немедленно.`; 526-527 «{имя} потеряет доступ прямо сейчас — открытые окна перестанут работать. Открытые диалоги останутся назначенными на него: переназначьте их через фильтр «Менеджер» в Чатах.»; 532-535 «{имя} исчезнет из всех списков, доступ оборвётся сразу, а незакрытые диалоги вернутся в «Входящие» — их разберёт смена. В журнале аудита человек останется: иначе на вопрос «кто это сделал» ответить будет нечем. Отменить удаление нельзя.»; 540 «{имя} снова сможет войти с прежним паролем.»; 545 «Отмена»; 553-558 «Сменить роль» / «Отключить» / «Удалить» / «Включить»; 576 «Задать пароль сотруднику»; 585-586 «{имя} сможет войти с этим паролем сразу — ссылку присылать не нужно. Все открытые окна этого сотрудника закроются.»; 589-590 «Новый пароль» / «Минимум 10 знаков»; 606/615 «Отмена» / «Задать пароль».
`InviteModal.tsx:51` «Сотрудник с таким email уже существует»; 55 «Не получилось пригласить. Попробуйте ещё раз»; 71 «Пригласить сотрудника»; 80 «Имя»; 98 «Роль»; 119/122 «Отмена» / «Пригласить».
`OneTimeLinkModal.tsx:38` «Передайте сотруднику ссылку установки пароля:»; 44 «Ссылка показывается один раз и действует 72 часа. Потеряли — перевыпустите в меню сотрудника.»; 48 «Скопировано» / «📋 Скопировать»; 50 «Готово».
`AuditLogTab.tsx:19-23` периоды; 43 «система»; 58 «скрыть» / «детали»; 120 aria «Журнал аудита»; 125/145-146 aria «Период журнала» / «Произвольный период журнала» + «Выберите даты»; 164-165 «Сотрудник» / «Сотрудник: все»; 179-180 «Действие» / «Действие: любое»; 201/205 «Не получилось загрузить журнал» / «Повторить»; 210 «За выбранный период записей нет»; 217-221 колонки «Время», «Сотрудник», «Действие», «Объект», «Детали»; 234 `{от}–{до} из {всего}`; 243/251 «Назад» / «Вперёд».
`auditActions.ts:8-28` подписи действий: «Вход в систему», «Выход из системы», «Приглашение сотрудника», «Пароль установлен по приглашению», «Смена роли», «Сотрудник отключён», «Сотрудник включён», «Аккаунт Авито подключён/отключён/включён», «Смена статуса диалога», «Назначение диалога», «Диалог открыт заново», «Получен телефон клиента», «Бот передал диалог оператору», «Бот выключен вмешательством оператора», «Изменён сценарий бота», «Создан/Изменён/Удалён шаблон», «Выгрузка статистики»; 37-45 типы объектов: «Сотрудник», «Аккаунт», «Диалог», «Клиент», «Сообщение», «Шаблон», «Бот», «Статистика».

## 1.23. `/settings/profile`
`frontend/src/features/settings/profile/ProfilePage.tsx`
15-18 подписи ролей (ТРЕТЬЯ копия); 22-25 ВТОРАЯ, ИНАЯ версия подсказок ролей: «полный доступ, включая аккаунты Авито и команду» / «видит все диалоги, передаёт их и пишет заметки — отвечать клиенту не может» / «отвечает клиентам, ведёт свои диалоги и передаёт их коллегам» / «только чтение диалогов»; 60/104/116 вкладки «Учётная запись», «Интерфейс», «Быстрые ответы»; 80 `Роль: {…} — {…}`; 83-84 «Пароль меняет администратор — напишите ему формой ниже или в поддержку Telegram @example_dev»; 120-121 «Личные шаблоны видите только вы. Вставляются в диалоге по «/» или кнопке быстрых ответов»; 136 «Профиль» / «Ваши данные, каналы и личные настройки»; 138 aria «Разделы профиля».
`ContactAdminForm.tsx:24` «Сообщение отправлено администратору»; 71 «Написать администратору»; 82 «Сообщение не отправлено. Проверьте соединение и попробуйте ещё раз»; 87 «Слишком много сообщений — это не отправлено. Попробуйте {…}»; 91-92 «Тема» / «Коротко: о чём вопрос»; 100-101 «Сообщение» / «Что случилось и что нужно»; 113 «Отправить».
`MyChannelsBlock.tsx:41` «Мои каналы»; 44 «Обращения этих каналов попадают к вам во «Входящие». Состав настраивает администратор.»; 51 «Загружаем…»; 56 «Не получилось загрузить список каналов»; 63-64 «Каналы не назначены» + «Новые обращения во «Входящие» к вам не попадут. Напишите администратору формой выше.»; 76/80 «открыт всем» / «назначен»; 88 «Открыт всем» — на канал не назначен ни один оператор, поэтому его обращения видят все.»
`AppearanceBlock.tsx:34` «Оформление»; 37 «Тема запоминается для этого компьютера.»; 43-45 «Тёмная», «Светлая», «Как в системе»; 47 aria «Тема оформления»; 52 «Звук новых сообщений».
`AboutAppBlock.tsx` (только в Windows-приложении): 55 «О приложении»; 58 `LeadChat для Windows · версия {…} · канал обновлений`; 64 «Проверить обновления»; 68 `Обновить до {…}`; 73 «У вас последняя версия»; 82 «Запускать при входе в Windows».

## 1.24. `/updates` — «Что нового»
`frontend/src/features/updates/UpdatesPage.tsx:22-24` метки «Новое», «Улучшено», «Исправлено»; 28-29 месяцы; 46 «Что нового»; 49-50 «Здесь появляется всё, что меняется в вашей работе. Технические работы сюда не попадают — только то, что видно на экране.»
`frontend/src/features/updates/changelog.ts` — крупный блок пользовательской копирайтерской прозы: 4 релиза, 41-212. Заголовки релизов: 41 «Передача с подтверждением, чёрный список и разгрузка очереди», 103 «Разбор диалогов и автоматическая раздача», 135 «Новый вид и порядок в списке», 197 «Очередь обращений и каналы». Внутри — пары `what`/`why` (45-46, 50-51, 55-56, 60-61, 65-66, 70-71, 75-76, 80-81, 85-86, 90-91, 95-96, 107-108, 112-113, 117-118, 122-123, 127-128, 139-140, 144-145, 149-150, 154-155, 159-160, 164-165, 169-170, 174-175, 179-180, 184-185, 189-190, 201-202, 206-207, 211-212).

## 1.25. `/ui-kit` — витрина компонентов
`frontend/src/features/uikit/UiKitPage.tsx` 43-47 выдуманные данные («! Парт - 7 / Ист - В43 МНЧ !», «Алексей Смирнов», «Николай Петров», «Ремонт холодильника Bosch», «Здравствуйте! Холодильник не морозит»); 139-140 «Живой каталог: компоненты настоящие, данные выдуманные. Переключите тему — всё ниже обязано остаться читаемым.»; 147 «Светлая тема»/«Тёмная тема»; 151-425 разделы «Цвета», «Типографика», «Форма и глубина», «Кнопки», «Поля ввода», «Бейджи и подсказки», «Карточка диалога», «Пузыри переписки», «Пустые состояния», «Иконки» + все демо-подписи («Обычная», «Недоступна», «Загрузка», «Принять», «Отклонить», «Так нельзя», «Флажок», «Тумблер», «По умолчанию», «Успех», «Ошибка», «Внимание», «Мягкий», «Контурный», «Наведите», «Меню», «Передать», «В архив», «Заблокировать», «Модальное окно», тосты и т. д.); 408-415 таблица образцов пустых состояний.

## 1.26. Десктоп-оболочка (Windows, Tauri)
`frontend/src/platform/UpdateBanner.tsx:25` aria «Доступно обновление»; 28 `Доступна версия {…}`; 42 «Перезапустить»; 45 «Позже».
`frontend/src/platform/toast.ts:34` «Новое сообщение»; 54 «Вложение»; 85 «Откройте центр уведомлений»; 98 «Диалог передан вам».
`frontend/src/platform/tauri/notifier.ts:100-104` `{N} новое сообщение/новых сообщения/новых сообщений в {M} диалоге/диалогах/диалогах` (локальная копия `plural`, строка 114).
`frontend/src/platform/tauri/updater.ts:55` «Не удалось установить обновление».
`frontend/src/platform/bridge.ts:117` «Офлайн-очередь доступна только в приложении для Windows».
Нативная часть (Rust):
`desktop/src-tauri/src/tray.rs:124-128` «На месте», «Отошёл», «Развернуть LeadChat», «Выход»; 236-256 «Новых чатов нет», `Новые чаты: {N}`, ` — новых: {N}`, ` — отошёл».
`desktop/src-tauri/src/lib.rs:390-391` «LeadChat продолжает работать в трее» / «Сообщения продолжают приходить. Значок в трее — развернуть, «Выход» — закрыть приложение.»
`desktop/src-tauri/src/notify.rs:858-896` placeholder «Быстрый ответ…», кнопки «Ответить», «Открыть», «Открыть LeadChat»; 911-914 сводка `{N} новых сообщений в {M} диалогах` (склонения реализованы).
`desktop/src-tauri/tauri.conf.json:48-49` «LeadChat — диалоги Авито в одном окне» / «Рабочее место менеджера: все диалоги Авито в одном окне, уведомления, офлайн-режим.»

## 1.27. Общие форматтеры и словари
`frontend/src/shared/lib/plural.ts:6-12` — канонический склонитель.
`frontend/src/shared/lib/formatTime.ts:12-25` месяцы кратко/полно; 49 «вчера»; 57-58 «Сегодня» / «Вчера».
`frontend/src/shared/lib/period.ts:97-103` «Сегодня», «Вчера», «7 дней», «30 дней», «Этот месяц», «Прошлый месяц», «Произвольный».
`frontend/src/shared/lib/waiting.ts:28-30` `{N} мин` / `{N} ч` / `{N} дн`.
`frontend/src/shared/api/http.ts:43` `Запрос завершился с кодом {N}`; 69/147 «Сервер недоступен. Проверьте соединение»; 215 «Сервер недоступен».
`frontend/src/shared/api/rateLimit.ts:28-33` «позже», `через N секунду/секунды/секунд`, `через N минуту/минуты/минут`, `через N час/часа/часов`.
`frontend/src/shared/realtime/applyWsEvent.ts:595-596` «Диалог передан вам» / `Передал(а): {имя} — «{комментарий}»`; 608-609 «Аккаунт Авито требует переподключения» / `«{канал}»: приём сообщений остановлен`.

---

# ЧАСТЬ 2. ТЕКСТЫ СЕРВЕРА

## 2.1. Конверт ошибок — `app/core/errors.py:37-53`
Общие: «Запрос не прошёл валидацию» (400), «Требуется авторизация» (401), «Недостаточно прав» (403), «Не найдено» (404), «Конфликт данных» (409), «Тело запроса больше лимита» (413), «Операция неприменима к текущему состоянию» (422), «Слишком много запросов, попробуйте позже» (429), «Внутренняя ошибка сервера» (500), «Внешний сервис временно недоступен» (503).
Именные: «Неверный email или пароль», «Слишком много неудачных попыток входа, попробуйте позже», «Ссылка недействительна или устарела», «Режим просмотра — назначьте менеджера или передайте диалог».
Коды БЕЗ русского сообщения (берут дефолт): `app/api/deps.py:44,51,54,79,80`; `app/core/security.py:87`; `app/api/routes/auth.py:159,167,175,215,219,223,268,271,284,287`; `app/api/routes/webhooks.py:39,52,56,59`; `app/api/routes/internal.py:203,211`; `app/api/routes/support.py:105`; `app/api/routes/avito_accounts.py:156`; `app/services/notifications.py:904`; `app/services/support.py:242,288`.
Замечание: `app/services/media.py:331` бросает код `"gone"`, которого нет ни в `_STATUS_CODES`, ни в `_DEFAULT_MESSAGES` — сообщение задано явно («Ссылка истекла, обновите диалог»), поэтому видно, но каталог кодов о нём не знает.

## 2.2. ApiError с русским текстом (полный список)
`app/api/routes/avito_accounts.py:159` «Сотрудник не найден»; :188 «Аккаунт не найден»; :196 «Авито сейчас не отвечает — попробуйте позже».
`app/api/routes/avito_connect.py:170` «Аккаунт Авито не найден»; :337 «Ключи не заданы»; :341 «Не удалось связаться с Авито — проверьте адрес»; :344 «настоящий Авито»/«встроенный имитатор»; :424 «Авито не принял эти ключи. Проверьте Client ID и Client Secret»; :436 «Не удалось связаться с Авито — попробуйте ещё раз через минуту»; :444-445 «Авито принял ключи, но отказал в доступе к мессенджеру. Проверьте, что приложению выданы права messenger:read и messenger:write»; :751 «Подписаться можно только на включённом канале»; :767 «Авито не принял подписку — попробуйте ещё раз через минуту»; :854 «Доступ отозван — обновлять нечего, нужно переподключение»; :867 «Авито отозвал доступ — требуется переподключение»; :873 «Токен уже обновляется — подождите несколько секунд»; :901 «Аккаунт требует переподключения через OAuth»; :913 «Токены аккаунта истекли — требуется переподключение».
`app/api/routes/bots.py:117` «Бот не найден»; :167 «Сессия песочницы истекла — начните заново»; :169 `AI недоступен: {…}`; :332 «PUT заменяет бота целиком — поле scenario обязательно»; :424 «Аккаунт Авито не найден»; :462 «Бот привязан к аккаунтам — сначала отвяжите их».
`app/api/routes/clients.py:57` «Клиент не найден».
`app/api/routes/conversations.py:140` «Эти фильтры неприменимы к вкладке «Входящие»»; :147 «неприменим при tab=inbox»; :370 «Сотрудник не найден».
`app/api/routes/media.py:93,97` «Файл не найден».
`app/api/routes/templates.py:86,99` «Поле не может быть пустым»; :125,136 «Шаблон не найден»; :130 «Общие шаблоны меняет только администратор или руководитель»; :218 «Создавать общие шаблоны может только администратор или руководитель».
`app/api/routes/auth.py:183` «Учётная запись отключена, обратитесь к администратору»; :292 «Пароль уже установлен»; :354 «Текущий пароль не подходит»; :361 «Новый пароль совпадает с текущим»; :405 «Имя не может быть пустым».
`app/api/routes/presence.py:70` «Состояние может быть «на месте» или «отошёл»».
`app/api/routes/notifications.py:89` «Неизвестная важность»; :98 «Начало периода позже его конца».
`app/api/routes/audit.py:49` «Начало периода позже его конца».
`app/api/routes/internal.py:250` «Неизвестный вид события».
`app/services/account_operators.py:208` «Сотрудник отключён»; :210 «Роль не отвечает клиентам»; :331 «Канал не найден»; :352 «Сотрудник не найден»; :360 «Сотрудник отключён — сначала включите его, потом назначайте на канал: …»; :369 «Эти сотрудники не отвечают клиентам и не могут вести канал: …»; :475 «Диалог другого канала — принять его может назначенный оператор».
`app/services/conversations.py:386` «Недопустимое значение tab»; :619,634 «Диалог не найден»; :805 «Недопустимый статус»; :817 «Диалог уже в этом статусе»; :827 «Закрытый диалог возвращается только в работу — или сам, когда клиент напишет»; :836 «Неизвестный результат обращения»; :850 «Результат обращения ставится только при закрытии»; :860 «Сумма указывается только у выезда»; :922 «Сотрудник не найден»; :929 «Сотрудник отключён»; :936 «Этому сотруднику нельзя передать диалог — он не отвечает клиентам»; :967 «Диалог уже назначен на этого сотрудника»; :969 «У диалога и так нет ответственного»; :1106 «Невалидный курсор»; :1125 «before и after одновременно нельзя».
`app/services/inbox.py:524` «Принять диалог может только тот, кто отвечает клиентам — назначьте менеджера»; :568 «Диалог закрыт — принимать нечего»; :577 «Диалог не ждёт принятия»; :585 «Вы уже приняли этот диалог»; :649,680,704 «Диалог не найден»; :1039 «Диалог и так никем не принят»; :1047 «Вернуть диалог в очередь может тот, кто его принял».
`app/services/transfer.py:94` «Диалог уже ведёт этот сотрудник»; :103 «Взять диалог себе можно без передачи»; :117 «Этот диалог вам не передавали»; :127 «Диалог передан другому сотруднику».
`app/services/participants.py:70` «Этот сотрудник и так ведёт диалог».
`app/services/pins.py:76` «Закреплять можно только свои диалоги».
`app/services/messages.py:149` «Тот же client_message_id с другим текстом — проверьте генерацию идентификатора»; :194,374,514 «Диалог не найден»; :203 «Пустое сообщение»; :229 «Диалог закрыт — верните его в работу, чтобы ответить»; :237 «Аккаунт Авито требует переподключения — обратитесь к администратору»; :639 «Сообщение не найдено»; :642 «Повторить отправку может только автор сообщения или админ»; :655 «Повторить можно только неотправленное сообщение».
`app/services/media.py:139` «Ожидается multipart/form-data с полем 'file'»; :153 «В Content-Type не найден boundary»; :248 «Хранилище вложений недоступно — сообщите администратору»; :259 «Файл пустой»; :274 «Допустимы только изображения (jpeg, png, webp) и pdf»; :318 «Ссылка без подписи»; :322,329 «Ссылка с некорректной подписью»; :331 «Ссылка истекла, обновите диалог»; :416 «Вложение не найдено или устарело — загрузите файл заново».
`app/services/users.py:125` «Сотрудник не найден»; :156 «Сначала назначьте администратором кого-то ещё.»; :229,251 «Сотрудник с таким email уже существует»; :282 «Пароль уже установлен — нужна не новая ссылка, а сброс пароля»; :371 «Нельзя сменить роль самому себе — попросите другого администратора»; :440 «Нельзя отключить самого себя…»; :489 «Свой пароль меняют в профиле — с вводом текущего»; :538 «Нельзя удалить самого себя…»; :624 «Заявка не привязана к сотруднику — сбросьте пароль на экране «Команда»».
`app/services/stats.py:152` «date_from должен быть не позже date_to»; :1034 «order — только asc или desc»; :1297 «У вас уже выполняется экспорт — дождитесь его завершения»; :1370 «Очередь задач недоступна — повторите позже»; :1385 «Задача выгрузки не найдена»; :1582 «XLSX-экспорт недоступен: не установлен openpyxl».
`app/services/conversation_table.py:124` «По этой колонке сортировать нельзя»; :147 «Слишком глубокая страница — уточните фильтр».
`app/services/app_settings.py:119` «Ожидалось «да» или «нет»»; :132 «Ожидалось целое число или «без ограничения»»; :159 «Ожидался час суток»; :166 «Час суток — от 0 до 23»; :180 «Ожидалось число дней».
`app/services/avito_app.py:216` «Укажите Client ID приложения Авито»; :219 «Адрес должен начинаться с http:// или https://»; :224 «Укажите Client Secret приложения Авито».
`app/services/notifications.py:819` «Уведомление не найдено»; :900 «У этого уведомления нет действия»; :912 «Действие пока недоступно — сделайте это вручную в настройках».
`app/services/avito_accounts.py:1062` «Уведомление не привязано к аккаунту — переподключите его в настройках»; :1071 «Аккаунт Авито не найден — возможно, он уже удалён»; :1079 «Откройте страницу Авито и подтвердите доступ — ссылка действует 10 минут».
`app/schemas/users.py:99` «Некорректный адрес почты»; :101,121 «Имя не может быть пустым»; :130 «Нужно передать хотя бы одно изменяемое поле».
`app/schemas/bots.py:54` «Начало и конец интервала совпадают — интервал пустой»; :79 «Без интервалов расписание не включит бота никогда»; :151 «Имя бота не может быть пустым».
`app/api/routes/support.py:32-33` «Заявка принята. Если такая учётная запись есть, администратор получит её и вышлет новую ссылку для входа.»; :35 «Сообщение отправлено администратору.»; :51 «нужен адрес вида имя@домен»; :66 «пустое значение».
`app/integrations/avito/errors.py:66` «Не удалось связаться с Авито»; :74 «Авито: refresh-токен не принят (400)». Клиент Авито (`client.py:158-362`) формирует `Авито: {контекст} -> HTTP {код}` и т. п. — это уходит в лог и в детали доставки.
`app/workers/deliver.py:57-59` «Аккаунт Авито требует переподключения», «Авито: сообщение не доставлено после 5 попыток», «Отправка вложений в Авито пока недоступна» — эти строки видит оператор в пузыре «Не доставлено: {…}».

## 2.3. Центр уведомлений — `app/services/notifications.py`
Кнопки (`ACTIONS`): :129 «Переподключить», :135 «Перерегистрировать», :141 «Выслать новую ссылку».
Заголовки каталога `KINDS` (:184-350): «Аккаунт Авито требует переподключения», «Приём сообщений остановился», «Резервное копирование не выполнилось», «Планировщик не подаёт признаков жизни», «Система не отвечает снаружи», «Очередь разгружена автоматически», «Очередь входящих не разбирается», «Сообщения не уходят клиентам», «На диске мало места», «Сертификат скоро истекает», «AI временно недоступен», «Сотрудник не может войти — просит новый пароль», «Сообщение администратору», «Учётная запись заблокирована после неудачных попыток входа», «Клиент недоволен», «Клиент ждёт вашего ответа», «Ваш ответ не дошёл до клиента», «Сообщения от клиентов не разбираются», «Канал отобрали: подписка на события пропала», «Диалог больше 30 минут без ответа», «Клиент вернулся в закрытый диалог», «Вам передали диалог», «Диалог никто не принял».
Тела уведомлений в точках порождения:
`app/scheduler/jobs/watchdog.py:260-264` «Приём сообщений остановился» + `Последнее сообщение от клиента пришло {N} мин назад, хотя подключённых аккаунтов Авито: {M}. В рабочее время такой тишины не бывает — скорее всего, приём встал и клиенты пишут в пустоту.`; :306-309 «Очередь входящих не разбирается» + `В очереди {N} сообщений, самое старое ждёт {M} мин. Сообщения от клиентов приходят, но до менеджеров не доезжают.`; :345-357 `Диск заполнен на {N}%` + «Занято…, свободно…»; :413-426 «Сертификат сайта истёк» / `Сертификат сайта истекает через {N} дн.` + пояснения; :460-467 «Проверка живости планировщика не работает» + детали; :504-515 «Резервное копирование не выполнилось» + «Успешных копий не было ни разу с момента запуска» / «Отметка о последней копии испорчена»; :581-585 «Сообщения от клиентов не разбираются» + `За последний час не разобрано {N} сообщений от Авито…`; :652-657 «Канал отобрали: подписка на события пропала» + «У аккаунтов {…} наша подписка… Лечится кнопкой перерегистрации вебхука на карточке канала.»
`app/api/routes/internal.py:80-145` — тексты внешнего наблюдателя: «Резервное копирование не выполнилось», «Копия не уехала в облако», «Резервная копия вдвое меньше вчерашней», «Резервная копия не читается», «Пробное восстановление не прошло», «Диск заполняется», «Планировщик не подаёт признаков жизни», «Система не отвечает снаружи» + развёрнутые тела.
`app/scheduler/jobs/cleanup.py:82-84` `Закрыто обращений: {N}. Их никто не взял, и клиент молчал дольше {M} дн. Если клиент напишет снова, диалог вернётся в очередь сам.`
`app/scheduler/jobs/awaiting.py:209` `Клиент ждёт ответа {N} мин`; :254-255 `{Сотрудник} не отвечает клиенту {N} мин`.
`app/services/inbox.py:980-993` «Диалог никто не принял» + `Все операторы отказались от диалога — {клиент} ждёт {…}. Назначьте ответственного вручную. Последняя причина отказа: {…}`.
`app/api/routes/conversations.py:399-402` «Вас позвали в диалог» + `{имя} просит посмотреть: {причина}`; :711-715 `{имя} передаёт вам диалог. {комментарий}`.
`app/workers/deliver.py:235-237` `message.undelivered` с телом = текстом ошибки доставки (см. 2.2).
`app/api/routes/auth.py:149-153` `Вход заблокирован: {имя}` + «Учётная запись {email} временно заблокирована после серии неудачных попыток входа. Если это сам сотрудник — вышлите ему новую ссылку для входа.»
`app/services/support.py:210-223` `Не может войти: {имя}` + «…Ссылка одноразовая, действует трое суток.» / «ОСТОРОЖНО: учётная запись … отключена, а доступ просят. Сначала решите, работает ли человек у вас, — и только потом высылайте ссылку.»
`app/services/avito_accounts.py:447-448` «Аккаунт Авито требует переподключения» + `«{канал}»: ключи приложения больше не принимаются`; :905 «История загружена частично»/«История загружена» + `Аккаунт «{…}»: загружено {N} диалогов, не удалось загрузить {M} — подробности в журнале сервера`; :926-929 «Загрузка истории сорвалась» + «Уже загруженное на месте, повтор продолжит с того же места.»

## 2.4. Журнал аудита — `app/services/audit.py`
Реестр действий :38-124 (57 подписей): «Вход в систему», «Выход из системы», «Смена статуса диалога», «Назначение диалога», «Диалог переоткрыт — клиент вернулся», «Оператор отказался от диалога», «Оператор забрал отказ обратно», «Система распределила диалог оператору», «Диалог предложен другому сотруднику», «Сотрудник принял переданный диалог», «Сотрудник отказался от переданного диалога», «Передача отменена — никто не ответил», «В диалог позвали коллегу», «Диалог возвращён в очередь — оператор не в сети», «Массово закрыты зависшие диалоги во «Входящих»», «Получен телефон клиента», «Клиент помечен как нежелательный», «С клиента снята пометка», «Приглашён сотрудник», «Приглашение принято — пароль установлен», «Сотрудник сменил свой пароль», «Сотрудник изменил своё имя», «Изменена роль сотрудника», «Изменено участие сотрудника в работе с диалогами», «Сотрудник отключён/удалён/включён», «Обновлён сценарий бота», «Бот передал диалог оператору», «Бот отключён в диалоге — вмешался оператор», «Подключён/отключён/включён аккаунт Авито», «Канал Авито удалён/переименован», «Ключи приложения Авито изменены/сброшены к настройкам сервера», «Подписка на входящие обновлена / обновить не удалось», «Токен канала обновлён вручную», «Изменён состав операторов канала», «Создан/Изменён/Удалён шаблон», «Выгрузка статистики», «Изменены настройки распределения диалогов», «Изменены рабочие часы статистики», «Перерегистрирована подписка на события Авито», «Созданы служебные сущности smoke-регрессии».
Словари для описаний :156-173: статусы «Новый/В работе/Закрыт»; роли строчными «администратор/руководитель/менеджер/наблюдатель»; «оператор/бот/система»; источники телефона «из текста сообщения/получен ботом/внесён вручную»; «Диалог взят в работу», «Диалог передан коллеге», «Диалог переназначен руководителем», «Диалог назначен автоматически».
Особые формулировки :193 «Неудачная попытка входа»; :205 «Диалог возвращён во «Входящие»»; :207 «С диалога снят ответственный»; :210 «Диалог принят из очереди»; `Статус диалога: {A} → {B} ({кто})`.

## 2.5. Экспорт статистики (тексты в файле выгрузки) — `app/services/stats.py`
Лист «Диалоги» :1213-1240: «Первое сообщение (МСК)», «Аккаунт», «Клиент», «Телефон», «Объявление», «Статус», «Ответственный», «Автор первого ответа», «FRT оператора, с», «FRT оператора (раб.), с», «FRT бота, с», «Сообщений вх.», «Сообщений исх. (оператор)», «Сообщений исх. (бот)», «Закрыт кем», «Закрыт когда (МСК)», «Результат», «Сумма, ₽».
Лист «Менеджеры» :1245-1252: «Менеджер», «Принято», «Отвечено», «Закрыто», «FRT ср., с», «FRT мед., с», «FRT мед. (раб.), с», «Отправлено».
Лист «Сводка» :1427-1453: «Период», «Аккаунт»/«все», «Менеджеры»/«все», «Выгружено», «Диалогов новых», «Диалогов закрыто», «В работе сейчас», «Ждут ответа сейчас», «FRT оператора, медиана, с», «FRT оператора, медиана», «FRT оператора, среднее, с», «FRT оператора (раб. часы), медиана, с», «…медиана», «Отвечено оператором», «Без ответа», «FRT бота, медиана, с», «Закрыто ботом без оператора, %», «Закрыто ботом без оператора», «Собрано телефонов», «  из них ботом», «  из них автоизвлечением», «  из них вручную», «Переоткрытий», «Повторных клиентов»; :1713 «Сводка»/«Показатель»/«Значение»; :1731 «Итого»; :1741 «Менеджеры»; :1745 «Диалоги».
CSV-выгрузка таблицы диалогов — `app/services/conversation_table.py:219-235`: «Клиент», «Телефон», «Канал», «Статус», «Оператор», «Объявление», «Метки», «Сообщений», «Первый ответ, сек», «Длительность, сек», «Последнее сообщение» + статусы.

## 2.6. Тексты ботов (уходят клиенту / в ленту)
`app/bots/engine.py:59-60` «🤖 AI недоступен, передал оператору», «🤖 Диалог закрыт ботом (шаг {…})».
`app/bots/handoff.py:39-52` причины передачи: «негатив», «клиент попросил живого человека», «AI не уверен в ответе», «негатив или жалоба клиента», «клиент пишет мимо сценария», «сценарий дошёл до передачи оператору», «клиент не ответил в отведённое время», «не удалось получить корректный ответ», «сработала защита от зацикливания», «AI недоступен», «сценарий изменился во время диалога»; :58-62 подписи собранного «Телефон», «Проблема», «Тип техники», «Бренд», «Модель»; :189/207/213 `🤖 Бот передал диалог оператору. Причина: {…}`, «Собрано ботом:», `Шаг сценария: {…}`.
`app/bots/ai.py:128-224` — системные промпты и описания инструментов на русском (не UI, но пользовательского вида); :353 фолбэк-ответ «Здравствуйте! Ориентировочно ремонт от 1500 ₽, точная цена после диагностики.»; :368 «Передаю ваш вопрос мастеру, он ответит в ближайшее время.»; :355-356 ключевые слова детекторов («ужасн», «отвратительн», «кошмар», «хамств», «жалоб», «верните деньги», «оператор», «менеджер», «живой человек», «позовите человека», «мастера»).
`app/bots/steps.py:377-385` те же ключевые слова детектора «просит человека».
`app/bots/validator.py:589-603` серверные тексты правил валидации (дубль фронтового набора, формулировки другие: «Дубль id шага», «Стартовый шаг entry не найден среди steps», «Ссылка на несуществующий шаг», «Шаг недостижим из стартового», «Из шага нет пути к handoff или close — сценарий повиснет», «Цикл без вопроса клиенту — бот зациклится сам на себе», «Переменная не объявлена ни одним ask/menu до этого шага», «Два шага пишут в одну переменную», «Регулярное выражение не компилируется», «Ключевое слово встречается в нескольких вариантах меню», «Шаг ai_answer при пустой базе знаний», «Шаг ждёт ответ бесконечно (timeout: null)», «Сценарий стартует молчаливым ожиданием», «Сценарий не соответствует схеме», «Сценарий должен быть JSON-объектом»); :521-548 «обязательное поле», «неизвестное поле», «не подходит ни под один вариант», «это значение»; :1025 «Сценарий не прошёл валидацию».
`app/bots/scenarios/__init__.py:24` «Первичный приём».
`app/bots/schedule.py:148,155` «никогда», «пн–вс».
`app/bots/sandbox.py:58` «[AI-ответ по базе знаний]»; :97 «заглушка песочницы»; :110 «модуль app.bots.ai недоступен»; :114 «ANTHROPIC_API_KEY не задан»; :223 «Песочница»; :241 «Черновик в песочнице».

## 2.7. Плейсхолдеры вложений (то, что видит оператор вместо файла)
`app/integrations/avito/adapter.py:179-186` «Фотография», «Голосовое сообщение», «Видео», «Файл», «Геопозиция», «Ссылка», «Звонок», «Объявление».

## 2.8. Массовое закрытие — запись в ленту
`app/services/queue_cleanup.py:188-190` `Закрыт автоматически: клиент молчал дольше {N} дн и обращение никто не взял` / `Закрыт массово: в переписке тихо дольше {N} дн. {имя}`.

## 2.9. CLI и конфиг (видит администратор сервера)
`app/cli.py:50,89-417` — тексты сервисных команд и вывода.
`app/core/config.py:257-260` — фатальное предупреждение про `MEDIA_SIGN_KEY`.
`app/services/crypto.py:52,78,82` — ошибки ключа шифрования.

---

# ЧАСТЬ 3. ТЕРМИНОЛОГИЯ

## 3.1. «диалог» / «обращение» / «чат» / «переписка» — четыре слова на одну сущность
Счёт по видимым строкам фронта: «диалог*» ≈163, «обращени*» ≈35, «чат*» ≈19, «переписк*» ≈12.

**«Диалог» — рабочий термин интерфейса** (доминирует): ChatListPane, ChatThreadPane, ThreadActions, InboxDecisionBar, useInbox, audit, notifications.

**«Обращение» — в тех же местах, где могло быть «диалог»**:
- `ChatListPane.tsx:93` «Новые **обращения** появятся здесь» — а рядом :125 «Все **диалоги** разобраны», :132 «**Диалогов** пока нет».
- `OutcomeModal.tsx:39` «Чем закончилось **обращение**?» — при этом карточка клиента называет тот же блок `ClientCardPane.tsx:353` «Чем закончилось» без слова, а `conversations.py:836` — «Неизвестный результат **обращения**».
- `DistributionTab.tsx:119,133,143,197` — «**обращение** ждёт во «Входящих»», «Новое **обращение** сразу уходит…» — и тут же :127 «Раздавать **диалоги** автоматически», :140 «Сколько **диалогов** держать на одном операторе», :196 «**Диалог** уходит только тому…». Один абзац — оба слова.
- `MyChannelsBlock.tsx:44,64,88` — только «обращения».
- `AccountsPage.tsx:610` «Каналы, **обращения** которых приходят в «Чаты»»; :160 «**обращения** канала видят все операторы».
- `WeekBars.tsx:89` «{N} **обращение/обращения/обращений** за неделю».
- `PresenceMenuItems.tsx:43` «Отошёл — новых **обращений** не давать».
- `TablePage.tsx:320` «Разбор **обращений**…» — при заголовке :317 «**Диалоги**» и подписи в рельсе `AppLayout.tsx:276` «Разбор **диалогов**».
- Сервер: `cleanup.py:82` «Закрыто **обращений**: N», `inbox.py:524` «Принять **диалог**…».

**«Чат»**:
- `AppLayout.tsx:245` тултип рельсы «**Чаты**», `badges.ts:62` «**Чаты** — …»;
- `ChatListPane.tsx:414` заголовок колонки «**Чаты**» при aria-метке :411 «Список **диалогов**»;
- `AccountsPage.tsx:395,412` «Загружаем историю… {N} **чатов**», «…сорвалась на {N} **чатах**» — единственное место, где сущность названа «чат» с числом;
- `AccountsPage.tsx:567` «Загружаем историю **чатов**…»;
- `TeamMembersTab.tsx:527` «…через фильтр «Менеджер» в **Чатах**»;
- десктоп-трей `desktop/src-tauri/src/tray.rs:236,238` «Новых **чатов** нет» / «Новые **чаты**: N» — и тут же `notify.rs:914` «в N **диалогах**». Внутри одного приложения трей говорит «чаты», тост — «диалоги».

**«Переписка»**:
- `ChatThreadPane.tsx:287` aria «**Переписка**»;
- `AccountsPage.tsx:211,453,499` «**переписка** удалена», «удалить его **переписку**»;
- `ConnectChannelWizard.tsx:219` «**переписку** не отдаст»;
- `roles.ts:15` «**Переписка** с клиентами…»;
- `TablePage.tsx:320` «объём **переписки**»;
- `UiKitPage.tsx:353` «Пузыри **переписки**».

## 3.2. «оператор» / «менеджер» — обе роли-слова живут параллельно
Формально роль в системе — `manager` = «Менеджер» (`roles.ts:8`), а «оператор» ролью не является нигде. Но «оператор» используется как имя того же человека:
- `TablePage.tsx:203,367,368` колонка «**Оператор**», фильтр «Фильтр по **оператору**» / «**Оператор**: все» — против `ChatListPane.tsx:327,328` «Фильтр по **менеджеру**» / «**Менеджер**: все» и `StatsFilters.tsx:112` «**Менеджеры**». Один и тот же фильтр по человеку назван двумя словами на двух экранах.
- `ManagersTable.tsx:31` колонка «**Менеджер**» с подсказкой :64 «…до первого ответа **оператора**» — в одной колонке оба слова.
- `DistributionTab.tsx:133,140,153` «свободному **оператору**», «на одном **операторе**», «Потолок диалогов на **оператора**» — при :127 «Раздавать **диалоги**…» и общем контексте роли «менеджер».
- `AssignOperatorsModal.tsx:98,230` «**оператор**», «Только сотрудники, назначенные **операторами**…» — назначение всегда «операторы», хотя роль — `manager`/`admin` (`roles.ts:26-28 canAnswer`).
- `MessageBubble.tsx:173` фолбэк автора «**Оператор**»; `ClientCardPane.tsx:150` фолбэк автора заметки «**Сотрудник**»; `AuditLogTab.tsx` колонка «**Сотрудник**». Три слова для автора.
- `BotsPage.tsx:99` в одном предложении: «…пока **менеджеры** заняты. Как только пишет **оператор** — бот замолкает…».
- `scenario.ts:35,38` шаг «**Оператору**» / шаг «Заметка» с подсказкой «Внутренняя заметка для **менеджера**».
- Сервер: `errors.py:52` «Режим просмотра — назначьте **менеджера**…»; `inbox.py:524` «…назначьте **менеджера**»; `audit.py:48,50,62` «**Оператор** отказался…», «Система распределила диалог **оператору**», «…**оператор** не в сети»; `stats.py:1219` «**Ответственный**», :1245 «**Менеджер**», `conversation_table.py:223` «**Оператор**» — три слова на одно поле в трёх выгрузках.
- В карточке клиента поле называется четвёртым словом: `ClientCardPane.tsx:369,374` «**Ответственный**».
- Подписи ролей продублированы в 4 файлах, из них одна — строчными: `AppLayout.tsx:33-38`, `ProfilePage.tsx:14-19`, `settings/team/roles.ts:5-10`, `PeoplePicker.tsx:7-12` (строчные: «администратор», «руководитель», «менеджер», «наблюдатель»). Сервер держит пятую копию строчными: `app/services/audit.py:158-161`.
- Описания ролей существуют в ДВУХ РАЗНЫХ редакциях: `ProfilePage.tsx:21-25` и `settings/team/roles.ts:12-16` (см. §5).

## 3.3. «канал» / «аккаунт» — оба слова на одну сущность, часто в одном абзаце
- Раздел называется `SettingsLayout.tsx:17` «**Аккаунты** Авито», а его подзаголовок `AccountsPage.tsx:610` — «**Каналы**, обращения которых приходят в «Чаты»». Заголовок и подзаголовок одного экрана расходятся.
- `ConnectChannelWizard.tsx:133-135`: «Ключи принадлежат **каналу** «{…}» — они обновлены, новый **канал** не появился. Для другого **аккаунта** Авито нужны его собственные Client ID и Client Secret…» — оба слова в одном сообщении.
- `AssignOperatorsModal.tsx:185-186`: «**Канал** «{…}» доступен всем операторам» / «На **аккаунт** «{…}» назначено: N» — две ветки одного тоста, разные слова.
- Фильтры: `ChatListPane.tsx:313-314` «Фильтр по **каналу**» / «**Канал**: все»; `TablePage.tsx:354-355` то же; но `StatsFilters.tsx:124-125` — «**Аккаунт** Авито» / «**Аккаунт**: все». Один и тот же фильтр — два слова на соседних экранах.
- `MyChannelsBlock.tsx:41` «Мои **каналы**» — у менеджера сущность только «канал»; у администратора та же сущность только «аккаунт» (`AccountsPage`).
- `BotEditor.tsx:233` «**Аккаунты** Авито» против `BotsPage.tsx:66` «Сначала отвяжите **каналы**: {…}» и :226 «Если бот привязан к **каналам**…» — редактор и список ботов расходятся.
- `notifications/catalog.ts:76` «**Аккаунт** требует переподключения» и :124 «**Канал** отобрали: подписка пропала» — соседние строки одного словаря.
- Сервер: `account_operators.py:331` «**Канал** не найден» vs `avito_accounts.py:188` «**Аккаунт** не найден»; `audit.py:94-102` — «Подключён **аккаунт** Авито», «**Канал** Авито удалён», «**Канал** Авито переименован», «Токен **канала** обновлён вручную», «Изменён состав операторов **канала**» — в одном реестре оба слова.
- Третье слово в API/типах: `stats.py:1214` «**Аккаунт**», `conversation_table.py:221` «**Канал**».

## 3.4. Прочие двоящиеся термины (замечено попутно)
- «Быстрые ответы» (`SettingsLayout.tsx:22`, `TemplatesPage.tsx:15`, `Composer.tsx:392`) vs «шаблон» (`TemplatesManager.tsx:61,164,224,363`, `auditActions.ts:25-27`, `templates.py:125`) — раздел и его содержимое названы по-разному; в одном окне: `TemplatesManager.tsx:73` «Новый **быстрый ответ**» и :139 кнопка «Сохранить» + :363 «**Шаблон** «{…}» исчезнет из пикера».
- «Пометить как нежелательного» (`ThreadActions.tsx:129`) / «Нежелательный клиент» (`ClientCardPane.tsx:291`) / «чёрный список» (`ConversationListItem.tsx:243` — только в комментарии) / «Заблокировать» (`UiKitPage.tsx:283`) / `client.blocked` в аудите `audit.py:70` «Клиент помечен как нежелательный».
- «Закрыть без ответа» (`OutcomeModal.tsx:89`) читается как «закрыть, не ответив клиенту», хотя означает «не указывать результат»; рядом в статистике «Без ответа» (`stats.py:1444`, `WeekBars.tsx:102`) значит именно «клиенту не ответили». Одно словосочетание — два смысла.
- «Разбор диалогов» (рельса, `AppLayout.tsx:276`) vs «Диалоги» (заголовок страницы, `TablePage.tsx:317`) vs «Разбор обращений» (подзаголовок, :320) — три названия одного экрана.

---

# ЧАСТЬ 4. ЧИСЛИТЕЛЬНЫЕ

## 4.1. Где склонения ЕСТЬ
Канонический склонитель — `frontend/src/shared/lib/plural.ts:6-12`. Используется в:
- `shared/api/rateLimit.ts:29,31,33` — «через N секунду/секунды/секунд», «…минуту/минуты/минут», «…час/часа/часов».
- `features/notifications/time.ts:19,30` — «N минуту/минуты/минут назад», «повторялось N раз/раза/раз».
- `features/settings/accounts/WeekBars.tsx:89` — «N обращение/обращения/обращений за неделю» (но через ЛОКАЛЬНУЮ копию функции, `WeekBars.tsx:34-40`).
- `platform/tauri/notifier.ts:100-104` — «N новое сообщение/новых сообщения/новых сообщений в M диалоге/диалогах/диалогах» (ещё одна ЛОКАЛЬНАЯ копия, :114).
- `features/stats/lib/heatmap.ts:60-65` — `pluralIncoming`: «входящее / входящих / входящих» (четвёртая копия правила; ветка `few` и ветка `many` возвращают ОДНО И ТО ЖЕ — ветка мертва, но результат верный).
- Нативная часть: `desktop/src-tauri/src/notify.rs:911-914` — собственная реализация `plural`, покрыта тестами :1190-1194.

Итого: **четыре независимые реализации правила во фронте** (`shared/lib/plural.ts`, `WeekBars.tsx:34`, `heatmap.ts:60`, `notifier.ts:114`) + одна в Rust. Условие ветки `few` в них записано по-разному (`mod100 < 10 || mod100 >= 20` против `mod100 < 12 || mod100 > 14`), результат совпадает, но правило продублировано пять раз.

## 4.2. Где склонений НЕТ (число + фиксированная форма)
Фронт:
- `features/settings/accounts/AccountsPage.tsx:395` — `Загружаем историю… {N} чатов` → «1 чатов».
- `features/settings/accounts/AccountsPage.tsx:412` — `Загрузка истории сорвалась на {N} чатах.` → «1 чатах».
- `features/templates/TemplatesManager.tsx:94` — `{N} символов · вставить:` → «1 символов».
- `features/settings/bots/BotsPage.tsx:171` — `{N} шаг(ов)` — уход от склонения скобкой.
- `features/settings/bots/scenario.ts:360` — `menu: {N} вариант(ов)` — то же.
- `features/chats/inbox/UnloadQueueDialog.tsx:80` — `· молчит {N} дн` — сокращение, склонение не нужно, но соседняя строка :51 подписывает поле «Тихо дольше, **дней**».
- `features/chats/components/card/ClientCardPane.tsx:412` — title «Позван(а) в диалог» (скобочная форма рода).
- `shared/realtime/applyWsEvent.ts:596` — `Передал(а): {имя}` (то же).

Бэкенд:
- `app/scheduler/jobs/watchdog.py:308` — `В очереди {N} сообщений` → «1 сообщений».
- `app/scheduler/jobs/watchdog.py:583` — `За последний час не разобрано {N} сообщений от Авито.` → «1 сообщений».
- `app/services/avito_accounts.py:897` — `загружено {N} диалогов` → «1 диалогов».
- `app/scheduler/jobs/watchdog.py:263` — `подключённых аккаунтов Авито: {N}` — форма через двоеточие, склонение обойдено.

## 4.3. Приём «двоеточие вместо склонения» (используется системно и работает)
`UnloadQueueDialog.tsx:73` «Закроется диалогов: N»; `useInbox.ts:282` «Закрыто диалогов: N»; `ChatListPane.tsx:574` «Найдено: N»; :672 «Никто не берёт: N»; `AccountsPage.tsx:120` «Операторы: N»; `AssignOperatorsModal.tsx:186` «…назначено: N», :282 «Выбрано: N из M»; `cleanup.py:82` «Закрыто обращений: N»; `badges.ts:60-61` «в очереди: N», «непрочитанных: N»; `ConversationListItem.tsx:136` «непрочитанных: N».

## 4.4. Числа-константы, где форма верна по факту
`validation.ts:78,341,347` «длиннее {TEXT_MAX/NAME_MAX/KNOWLEDGE_BASE_MAX} символов»; `InvitePage.tsx:184,192` «мин. 10 символов» / «Минимум 10 символов»; `AskForm.tsx:69` «до 200 символов»; `deliver.py:58` «после 5 попыток»; `useStatsExport.tsx:30` «Период больше 366 дней»; `TeamMembersTab.tsx:590` «Минимум 10 знаков»; `notifications` «действует 72 часа».

---

# ЧАСТЬ 5. ОБРЕЗКА МНОГОТОЧИЕМ И ТУЛТИПЫ

Механизмы обрезки: класс `.lc-truncate` (`app/lc-base.css:385-390`), локальные `text-overflow: ellipsis` и проп Mantine `truncate`.

## 5.1. Обрезка ЕСТЬ, тултип/`title` ЕСТЬ
| Где | Обрезка | Подсказка |
|---|---|---|
| `/dialogs`, колонка «Клиент» | `.dt__ell--wide`, `table.css:81-92` | `TablePage.tsx:184` `title={r.client_name}` |
| `/dialogs`, «Оператор» | `.dt__ell` | `TablePage.tsx:205` `title` |
| `/dialogs`, «Канал» | `.dt__ell` | `TablePage.tsx:214` `title` |
| `/dialogs`, «Объявление» | `.dt__ell--wide` | `TablePage.tsx:249` `title` |
| Строка списка, чип состояния | — | `ConversationListItem.tsx:221` `title={chip.title}` |

Комментарий в `table.css:78-80` прямо фиксирует правило: «Обрезанное показывается целиком в подсказке — иначе «Ист - 41 / Сев…» не отличить от «Ист - 41 / Север-2»». В `/dialogs` правило соблюдено полностью.

## 5.2. Обрезка ЕСТЬ, подсказки НЕТ
| Где | Файл:строка | Что обрезается |
|---|---|---|
| Имя клиента в строке списка | `ConversationListItem.tsx:188` (`.conv-card__name lc-truncate`) | имя клиента |
| Вторая строка карточки списка | `ConversationListItem.tsx:200-202` (`.conv-card__line2 lc-truncate`) | `{канал} · {объявление} · {ответственный}` — три поля в одной обрезаемой строке |
| Превью последнего сообщения | `ConversationListItem.tsx:206` (`.conv-card__preview lc-truncate`) | текст сообщения |
| Имя клиента в шапке ленты | `ChatThreadPane.tsx:303` (`.thread-header__name lc-truncate`) | имя клиента |
| Название канала в шапке ленты | `ChatThreadPane.tsx:316` (`lc-truncate`) | `conv.account.title` |
| Ответственный в шапке ленты | `ChatThreadPane.tsx:322` (`lc-truncate`) | `conv.assignee.full_name` |
| Пикер быстрых ответов — название | `TemplatePickerPopover.tsx:170` + `template-picker.css:85-92` (`width: 140px`) | `t.title` |
| Пикер быстрых ответов — тело | `TemplatePickerPopover.tsx:171` + `template-picker.css:96-102` | `t.body` |
| Таблица шаблонов — «Название» | `TemplatesManager.tsx:311` + `templates.css:128-134` | `t.title` |
| Таблица шаблонов — «Текст» | `TemplatesManager.tsx:314` + `templates.css:136-144` | `t.body` |
| Название аккаунта на карточке | `AccountsPage.tsx:324` (Mantine `truncate`) | `account.title` |
| Сводка шага бота | `StepCard.tsx:102` (Mantine `truncate`) | `stepSummary(step)` (плюс сама сводка уже урезана функцией `clip`, `scenario.ts:360,371`) |
| Имя вложения в композере | `Composer.tsx:339` + `composer.css:275-280` | имя файла |

## 5.3. Пограничный случай: `title` есть, но НЕ про обрезанный текст
`ClientCardPane.tsx:409-416`: `<li className="card-participant" title={p.reason ?? "Позван(а) в диалог"}>` — обрезается ИМЯ (`.card-participant__name`, `client-card.css:469-474`), а в `title` лежит ПРИЧИНА приглашения. Наведение на обрезанное имя показывает не имя, а причину.

## 5.4. Обрезка на уровне данных (до CSS)
`scenario.ts:360` `clip(step.params.text, 28)`, :371 `clip(…, 32)`, :348 `clip(cond.regex, 20)` — текст режется в строке сводки шага, и потом ещё раз обрезается CSS в `StepCard.tsx:102`. Двойная обрезка без подсказки.
`AccountsPage.tsx:138-139` — список назначенных операторов обрезается по количеству: `Назначены: {первые} и ещё {N}` (это не многоточие, а честное «и ещё N»).
`CriticalBanners.tsx:88` — `и ещё {N}`.

## 5.5. Известный случай обрезки БЕЗ многоточия (зафиксирован в коде)
`OutcomeModal.tsx:56-59` — комментарий описывает уже исправленную поломку: подсказка справа от названия «на 390px «Не наш профиль» обрезалось: 107 пикселей текста в 97-пиксельной коробке с `nowrap` и `overflow: hidden` — то есть без многоточия, просто обрубленное слово». Сейчас подсказка перенесена в столбик (`.outcome-option__hint`).

### Есть в коде, но не выведено (13)

* **Тип уведомления «Клиент недоволен» (conversation.negative) объявлен в каталоге сервера и полностью описан в интерфейсе (иконка 😠, подпись, важность warning), но НИ ОДНА строка боевого кода его не порождает: вне app/services/notifications.py ссылок нет вообще, только тесты.**
  * где: app/services/notifications.py:254-257 (KindSpec), frontend/src/features/notifications/catalog.ts:93 (иконка+подпись), frontend/src/shared/api/types.ts:1154 (тип). Producer: не нашёл (grep 'conversation.negative' по app/ вне notifications.py — 0 совпадений; есть только tests/unit/test_notifications.py:441,538,649 и tests/integration/test_notifications_pg.py:398)
  * почему важно: Подпись «Клиент недоволен» ВСЕГДА присутствует в выпадающем фильтре «Тип: любой» на /notifications (список строится из всех ключей KIND_CATALOG — frontend/src/features/notifications/NotificationsPage.tsx:170-176). Руководитель выбирает фильтр, получает пустой список и не может отличить «сегодня никто не жаловался» от «эта тревога не работает». Детектор негатива в системе есть (app/bots/handoff.py:39 «негатив», app/bots/ai.py:355), тег «негатив» доезжает до строки списка (ConversationListItem.tsx:118) — то есть событие вычисляется, но в центр уведомлений не уходит.
* **Тип уведомления «Клиент вернулся в закрытый диалог» (conversation.reopened) объявлен в каталоге и в интерфейсе, но notify(kind="conversation.reopened") не вызывается нигде. Все пять совпадений строки в app/ — это ДРУГАЯ сущность: одноимённое действие журнала аудита.**
  * где: app/services/notifications.py:326-329 (KindSpec, адресное, «ответственному менеджеру»), frontend/src/features/notifications/catalog.ts:95 (иконка 🔁 + подпись). Реальные употребления строки — аудит и статистика: app/services/inbound.py:309, app/services/avito_accounts.py:666, app/services/audit.py:43,135, app/services/stats.py:565
  * почему важно: Менеджер, у которого клиент вернулся в закрытый диалог, уведомления не получит — а каталог и фильтр журнала обещают, что получит. Плюс одна и та же строка «conversation.reopened» означает в коде две разные вещи (вид уведомления и действие аудита), что маскирует пропажу: поверхностный grep находит «пять употреблений» и выглядит живым.
* **Тип уведомления «Сообщения не уходят клиентам» (delivery.failures) объявлен в каталоге сервера и в интерфейсе, но за пределами notifications.py на него нет НИ ОДНОЙ ссылки — даже в тестах.**
  * где: app/services/notifications.py:223-224 (KindSpec, severity=warning, audience=admin), frontend/src/features/notifications/catalog.ts:82 (иконка 📤 + подпись «Сообщения не уходят клиентам»). Producer: не нашёл (0 совпадений по app/ вне notifications.py)
  * почему важно: Это единственная агрегированная тревога про массовый сбой доставки. Индивидуальные провалы шлются автору сообщения как message.undelivered (app/workers/deliver.py:235-237), но администратор про «отвал доставки в целом» не узнаёт никогда, хотя подпись висит в фильтре журнала.
* **Страница /ui-kit («Живой каталог» компонентов) собрана целиком, лежит в бандле отдельным чанком и доступна любому вошедшему пользователю без проверки прав — но ссылки на неё нет нигде в интерфейсе.**
  * где: frontend/src/app/router.tsx:107-112 (маршрут, без RequirePermission), frontend/src/features/uikit/UiKitPage.tsx (628 строк). Единственные совпадения по 'ui-kit' во всём frontend/src — сам маршрут и импорт в нём.
  * почему важно: Любой менеджер или наблюдатель, набрав /ui-kit, попадёт на витрину с выдуманными данными («Алексей Смирнов», «Ремонт холодильника Bosch», «! Парт - 7 / Ист - В43 МНЧ !»), рабочими кнопками тостов и текстами вроде «Заблокировать». В брифе такого раздела нет, а с точки зрения пользователя это неотличимо от сломанного экрана с чужими клиентами.
* **Описания ролей существуют в двух независимых редакциях с разными формулировками, и пользователь видит обе — на /settings/profile одну, на /settings/team другую.**
  * где: frontend/src/features/settings/profile/ProfilePage.tsx:21-25 (ROLE_HINTS: «полный доступ, включая аккаунты Авито и команду» / «видит все диалоги, передаёт их и пишет заметки — отвечать клиенту не может» / «отвечает клиентам, ведёт свои диалоги и передаёт их коллегам» / «только чтение диалогов») против frontend/src/features/settings/team/roles.ts:12-16 (ROLE_HINTS: «Весь интерфейс: сотрудники, аккаунты Авито, боты, статистика, журнал» / «Все диалоги без отправки, статистика всех сотрудников, журнал аудита» / «Переписка с клиентами, личные шаблоны и своя статистика за день» / «Только чтение диалогов: без ответов, заметок и смены статусов»)
  * почему важно: Администратор описывает роль сотруднику по одному тексту, сотрудник читает у себя другой. Вторая редакция (ProfilePage) — локальная копия, ниоткуда не импортируется и никаким тестом с первой не сверяется; расхождение будет расти при каждой правке прав.
* **Подписи ролей (ROLE_LABELS) продублированы в четырёх файлах фронта и пятом на сервере, причём в двух копиях они строчными, в трёх — с прописной.**
  * где: frontend/src/app/AppLayout.tsx:33-38 (копия), frontend/src/features/settings/profile/ProfilePage.tsx:14-19 (копия), frontend/src/features/chats/components/card/PeoplePicker.tsx:7-12 (копия, СТРОЧНЫМИ), frontend/src/features/settings/team/roles.ts:5-10 (единственная экспортируемая), app/services/audit.py:158-161 (сервер, строчными)
  * почему важно: В окне «Передать диалог» роль показана как «менеджер», в шапке приложения — «Менеджер», в журнале аудита — «менеджер». Добавление новой роли потребует правки пяти мест; забытое место даст либо TS-ошибку (Record<Role,…> её поймает), либо тихо разное написание — как сейчас.
* **Правило русских числительных реализовано четырьмя независимыми копиями во фронте (плюс пятой в Rust), при этом канонический модуль shared/lib/plural.ts используют только три вызова.**
  * где: frontend/src/shared/lib/plural.ts:6-12 (канон, потребители: shared/api/rateLimit.ts:29,31,33 и features/notifications/time.ts:19,30) — против локальных копий frontend/src/features/settings/accounts/WeekBars.tsx:34-40, frontend/src/platform/tauri/notifier.ts:114, frontend/src/features/stats/lib/heatmap.ts:60-66; в heatmap.ts:64 и :65 ветки few и many возвращают ОДНУ И ТУ ЖЕ строку «входящих» — ветка мертва
  * почему важно: Функция, которую нужно звать, физически не позвана из трёх мест — там её переписали. Из-за этого в новых местах её тоже не находят и пишут либо шестую копию, либо форму без склонения (см. «1 чатов» в AccountsPage.tsx:395, «1 символов» в TemplatesManager.tsx:94, «1 сообщений» в watchdog.py:308).
* **Подсказка по горячим клавишам обещает срезы списка «Мои, Все, Новые, Закрытые» по Alt+1…4, но вкладок «Новые» и «Закрытые» в интерфейсе больше нет — это значения отдельного фильтра состояния.**
  * где: frontend/src/features/hotkeys/catalog.ts:31 («Alt + 1…4», «Срезы списка: Мои, Все, Новые, Закрытые»); фактические вкладки — frontend/src/features/chats/components/list/ChatListPane.tsx:46-47 («Мои», «Все») плюс «Входящие» (:471); значения фильтра — ChatListPane.tsx:58-60 («Новые», «В работе», «Закрытые»). Реализация: frontend/src/features/hotkeys/useChatHotkeys.ts:41-44 — Alt+3/Alt+4 ставят вкладку «Все» + статус.
  * почему важно: Текст справки, который читают в первый рабочий день при переезде с Jivo, описывает интерфейс предыдущей версии. Человек ищет вкладку «Новые» глазами и не находит; собственный комментарий в catalog.ts:6-9 предупреждает ровно об этом риске («шпаргалка, обещающая несуществующее сочетание, заставляет человека решить, что сломалось приложение»).
* **В тексте профиля зашит личный телеграм-ник как канал поддержки продукта.**
  * где: frontend/src/features/settings/profile/ProfilePage.tsx:83-84: «Пароль меняет администратор — напишите ему формой ниже или в поддержку Telegram @example_dev»
  * почему важно: Строка видна ВСЕМ ролям на каждом заходе в профиль. Это единственное место во всём интерфейсе, где названа внешняя точка поддержки, и она указывает на личный аккаунт, а не на роль/канал — при смене исполнителя текст останется, а адресат исчезнет. Плюс рядом (ContactAdminForm) уже есть штатная внутренняя форма обращения, то есть предлагается два разных пути для одной задачи.
* **Дежурный ответ центра уведомлений «Действие пока недоступно — сделайте это вручную в настройках» недостижим: реестр незаписанных целей PENDING_ACTIONS/ACTIONLESS-механизма пуст, все кнопки каталога реализованы.**
  * где: app/services/notifications.py:912 (текст ответа 503), app/services/notifications.py:156-157 (реестр пуст: «Пусто: обе кнопки каталога написаны»); аналогичный пустой реестр в журнале — app/services/audit.py:146-151
  * почему важно: Строка выглядит как живой пользовательский текст и попадёт в любой перевод/вычитку, хотя показать её сейчас нечем. Обратная сторона: если завтра в ACTIONS добавят цель без реализации, пользователь получит именно этот текст — так что удалять его нельзя, но и считать его частью действующего интерфейса тоже нельзя.
* **Тексты ACTIONLESS_CRITICAL написаны человеческим языком и выглядят как объяснения для администратора, но пользователю не показываются никогда — их читает только тест.**
  * где: app/services/notifications.py:357-373 (например «Бэкап живёт вне приложения (14 §4) — нажать на него из UI нечего», «Сертификат перевыпускается на сервере (certbot), из браузера нажать нечего»); единственные потребители — tests/unit/test_notifications.py:154-158, tests/unit/test_watchdog.py:641-646, tests/unit/test_support.py:400
  * почему важно: Администратор, получивший критичное уведомление без кнопки, видит только «Подтвердить» (frontend/src/features/notifications/catalog.ts:157) и не понимает, почему чинить нечем. Готовое объяснение, почему кнопки нет, в коде уже написано — и не выведено.
* **Комментарий в звуковом модуле утверждает, что тумблера отключения звука ещё нет, хотя он уже есть и работает — в том же файле десятью строками ниже стоит правильная ссылка на него.**
  * где: frontend/src/shared/realtime/notify.ts:13 («Тумблер muted появится в профиле позже; каркас уже готов к нему») против notify.ts:60 («тумблер в /settings/profile (11 §4.3)») и реального переключателя frontend/src/features/settings/profile/AppearanceBlock.tsx:52 («Звук новых сообщений»)
  * почему важно: Мелочь, но она относится к инвентаризации: при чтении кода фича «отключение звука» выглядит незаконченной, и её могут заново «реализовать».
* **Заголовки уведомлений в каталоге сервера и подписи типов в каталоге фронта расходятся текстуально по семи видам — в журнале /notifications колонка «Событие» и колонка «Что произошло» показывают разные формулировки одного и того же.**
  * где: Пары (сервер app/services/notifications.py → фронт frontend/src/features/notifications/catalog.ts): :204 «Планировщик не подаёт признаков жизни» → :79 «Планировщик не отвечает»; :233 «Сотрудник не может войти — просит новый пароль» → :88 «Запрос на сброс пароля»; :248 «Учётная запись заблокирована после неудачных попыток входа» → :90 «Учётная запись заблокирована»; :322 «Диалог больше 30 минут без ответа» → :94 «Диалог без ответа»; :314 «Канал отобрали: подписка на события пропала» → :124 «Канал отобрали: подписка пропала»; :188 «Аккаунт Авито требует переподключения» → :76 «Аккаунт требует переподключения»; :287 «Ваш ответ не дошёл до клиента» → :116 «Ответ не дошёл до клиента»
  * почему важно: Расхождение спроектировано (комментарий catalog.ts:14 прямо говорит, что фронт не сочиняет заголовки, а только подписи типов), но в таблице журнала обе строки стоят В ОДНОЙ СТРОКЕ рядом — NotificationsPage.tsx:103 рисует kindLabel, :106 рисует notification.title. Пользователь видит «Планировщик не отвечает | Планировщик не подаёт признаков жизни» и вправе решить, что это два разных события.

### Расхождения с брифом и чего не нашёл

## РАСХОЖДЕНИЯ С БРИФОМ ЗАКАЗЧИКА

### 1. Разделы: бриф называет 8 экранов, в коде их 14 (+1 скрытый)

**Совпадает с брифом:**
- `/chats` — есть, `frontend/src/app/router.tsx:70` (+ deep-link `/chats/:id`, :113)
- `/dialogs` — есть, router.tsx:94, но в интерфейсе называется тремя разными именами: в рельсе «Разбор диалогов» (`AppLayout.tsx:276`), заголовком страницы «Диалоги» (`TablePage.tsx:317`), подзаголовком «Разбор обращений» (`TablePage.tsx:320`)
- `/stats` — есть, router.tsx:132
- `/settings/accounts` — есть, router.tsx:144
- `/settings/bots` — есть, router.tsx:158 (+ редактор `/settings/bots/:id`, :162 — брифом не назван)
- `/settings/distribution` — есть, router.tsx:175
- `/settings/team` — есть, router.tsx:189
- `/settings/profile` — есть, router.tsx:141

**НЕ СОВПАДАЕТ — брифовского пути `/settings/quick-replies` в коде нет.**
Фактический путь — `/settings/templates` (`router.tsx:148`), компонент `TemplatesPage`. Строки «quick-replies» / «quick_replies» не встречаются ни в `app/`, ни в `frontend/src/`, ни в `docs/` — проверено grep'ом, 0 совпадений. При этом ПОДПИСЬ пункта в меню и заголовок страницы — именно «Быстрые ответы» (`SettingsLayout.tsx:22`, `TemplatesPage.tsx:15`). То есть бриф описал видимое название, а адрес назвал по нему же; в коде адрес назван по сущности («templates»), а внутренности сущности зовутся то «быстрый ответ», то «шаблон» (см. §3.4 инвентаря).

**Есть в коде, брифом не названо (7 маршрутов):**
- `/login` — router.tsx:61
- `/invite/:token` — router.tsx:62
- `/notifications` — router.tsx:120, журнал уведомлений; вход только через ссылку «Показать все» в панели колокольчика (`NotificationPanel.tsx:115`)
- `/updates` — router.tsx:102, «Что нового»; вход только из меню аватара (`AppLayout.tsx:171`)
- `/ui-kit` — router.tsx:108, витрина компонентов; входа в интерфейсе НЕТ вовсе (см. список мёртвого/скрытого)
- `/settings` — редирект на `/settings/profile`, router.tsx:140
- `*` — страница «Страница не найдена», router.tsx:200

### 2. Роли: бриф называет две, в коде их четыре

Бриф: «Администратор, Менеджер».
Код: `admin | head | manager | observer` — `frontend/src/shared/auth/usePermissions.ts:4` и `app/core/rbac.py:7`, матрица прав `app/core/rbac.py:31-60`.

Русские подписи всех четырёх: `frontend/src/features/settings/team/roles.ts:5-10` — «Администратор», «Руководитель», «Менеджер», «Наблюдатель».

Две роли, которых нет в брифе, не декоративны — под них написан интерфейс:
- **head («Руководитель»)** — открывает `/dialogs`, `/stats` и вкладку журнала аудита (`router.tsx:74,127,186`), не может отправлять клиенту (нет права `messages:send`, `rbac.py:33-45`), из-за чего существует отдельный экранный текст `ThreadFooter.tsx:69` «👁 Режим просмотра — назначьте менеджера или передайте диалог» и серверный `app/core/errors.py:52` с тем же текстом.
- **observer («Наблюдатель»)** — только `conversations:read` (`rbac.py:58`); под него написаны отдельные пустые состояния и ветки: `TeamPage.tsx:51` «Для вашей роли здесь пока нет разделов», `NotificationsPage.tsx:187` «Вашей роли уведомления не приходят», вкладка «Быстрые ответы» в профиле у него не собирается (`ProfilePage.tsx:112`).

Кроме ролей, у сотрудника есть ОРТОГОНАЛЬНЫЙ признак «ведёт диалоги» и поле «Отдел» (`TeamMembersTab.tsx:310-311,362-365`), которых бриф не упоминает вовсе. То есть модель доступа в коде двумерная (роль × «отвечает клиентам»), а бриф описывает одномерную из двух значений.

### 3. Прочие расхождения формулировок брифа и кода
- Бриф говорит «/dialogs» как о разделе; в интерфейсе это управленческий экран под правом `stats:all` (`router.tsx:74`) — менеджер его не видит и в рельсе (`AppLayout.tsx:275`).
- Бриф не упоминает десктоп-приложение, а в коде есть целая оболочка со своими текстами (трей, тосты Windows, «О приложении») — `desktop/src-tauri/src/tray.rs`, `frontend/src/features/settings/profile/AboutAppBlock.tsx`.

---

## ЧЕГО НЕ НАШЁЛ / ГРАНИЦЫ ЭТОГО СРЕЗА

1. **Файлов локализации нет.** Ни i18n-словаря, ни ключей, ни библиотеки перевода — все русские строки зашиты в местах употребления (проверено: `frontend/src/` не содержит `locales/`, `i18n`, `t(`-обёрток; `package.json` фронта я не разбирал построчно, но ни один модуль перевода в коде не импортируется). Инвентарь выше — это и есть единственный «словарь».

2. **Не проверял тексты в `fake_avito/` и `docs/`** — они за пределами заданного среза (интерфейс + сервер). Отмечу только, что банер имитатора виден в бою: `AccountsPage.tsx:637-638`, `ConnectChannelWizard.tsx:195-198`.

3. **Не нашёл текстов писем.** Восстановление пароля идёт заявкой администратору, а не письмом (`LoginPage.tsx:215-216` — комментарий это фиксирует), почтовых шаблонов в `app/` нет.

4. **Не нашёл единого места с текстами ошибок HTTP на фронте.** `ApiError.message` приходит с сервера и печатается как есть в десятках мест (`e instanceof ApiError ? e.message : "Попробуйте ещё раз"` — паттерн встречается в BlockClientButton.tsx:55, TransferBar.tsx:52, AccountsPage.tsx:290, ConnectChannelWizard.tsx:101,154, BotEditor, DistributionTab.tsx:71, WorkHoursBlock.tsx:64, TablePage.tsx:284 и др.). Это значит, что часть видимых пользователю текстов физически живёт только в Python — их полный список я дал в части 2, но проверить, какие из них реально доезжают до экрана, статически нельзя.

5. **Не смог статически подтвердить полноту списка «мёртвых» уведомлений для случаев динамического kind.** Проверял поиском строковых литералов; если где-то kind собирается из переменной, я это пропустил. Для трёх найденных (`conversation.negative`, `conversation.reopened`, `delivery.failures`) проверил вручную по всему `app/` — производителя нет.

6. **Тултипы, реализованные атрибутом `title`, не проверял на мобильной раскладке.** На тач-экранах `title` не показывается вовсе; это касается всех четырёх колонок `/dialogs` (TablePage.tsx:184,205,214,249), чипа состояния (ConversationListItem.tsx:221) и счётчика колонки (ChatListPane.tsx:425). Отмечаю как факт, не как оценку.

7. **`frontend/dev/preview.tsx`** лежит вне `src/` и в инвентарь не попал — это dev-харнесс, в бандл не входит.

8. **Не сверял тексты с `docs/`** (16-DESIGN-SYSTEM, 11-UI, 14-NOTIFICATIONS и т. п.), на которые массово ссылаются комментарии. Если аудит предполагает сверку «текст в коде против текста в спецификации», это отдельный проход — здесь его нет.

## Дополнение 22.08.2026 — `conversations:close_queued`

Право заведено по решению владельца после разбора живого случая: он закрывал
диалоги пачкой и обнаружил, что обращение, которое висит во «Входящих» и ещё
никем не взято, закрытие убирает из очереди НАСОВСЕМ — условие очереди
(`inbox.queue_condition`) требует `status != closed`, а матрица переходов
разрешает `new → closed`. Клиент при этом ждёт, а обращения больше нет ни у кого.

Решение владельца дословно: «во входящих можно только "Отклонить" — так они
возвращаются в очередь; закрыть диалог из очереди тоже хорошая идея, но такое
нужно оставить только админам».

Разница между двумя действиями принципиальная:

* **«Отклонить»** — «я сейчас занят»: диалог уходит с глаз ТОГО, кто нажал, на
  три минуты (`inbox.DECLINE_TTL`) и возвращается в общую очередь к коллегам;
* **«Закрыть»** — «с этим обращением покончено»: диалог исчезает у всех и не
  возвращается, пока клиент не напишет снова.

Оператору нужно первое; второе оставлено администратору, который разбирает спам
и отвечает за то, что обращение действительно не нужно.

Право ОТДЕЛЬНОЕ от `conversations:manage` намеренно: вести свой диалог и
хоронить чужой, ещё не начатый, — разные полномочия.

Запрет держит сервер (`services/conversations.change_status` → 403
`queued_close_forbidden`), интерфейс лишь не показывает заведомо отказной пункт
(`ThreadActions.tsx`). Спрятанная кнопка защищает от промаха, но не от горячей
клавиши, повторного запроса и чужого клиента.
