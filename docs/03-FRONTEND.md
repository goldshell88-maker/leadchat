# 03 — Архитектура фронтенда (React 18 + TypeScript + Vite)

> Документ детализирует разделы 2 (стек), 3 (дизайн интерфейсов), 5 (роли) и 1.7 (десктоп)
> [DESIGN.md](../DESIGN.md). Ничего из принятых там решений не меняет — только раскрывает
> до уровня «завтра пишем код». Контракты API/WS, на которые ссылается документ
> (`/api/v1/ws/ticket`, догон после реконнекта, retry отправки), зафиксированы в
> [01-API-SPEC](01-API-SPEC.md) — это единые контракты, здесь они описаны со стороны клиента.

**Фиксированные версии (package.json):**

```jsonc
{
  "dependencies": {
    "react": "^18.3", "react-dom": "^18.3",
    "react-router-dom": "^6.26",
    "@tanstack/react-query": "^5.51",
    "@tanstack/react-virtual": "^3.8",
    "zustand": "^4.5",
    "@mantine/core": "^7.12", "@mantine/hooks": "^7.12",
    "@mantine/notifications": "^7.12", "@mantine/dates": "^7.12",
    "dayjs": "^1.11"
  },
  "devDependencies": {
    "typescript": "^5.5", "vite": "^5.4", "@vitejs/plugin-react": "^4.3",
    "vitest": "^2.0", "@testing-library/react": "^16.0"
  }
}
```

Tauri-плагины (`@tauri-apps/api`, `@tauri-apps/plugin-notification`, `@tauri-apps/plugin-sql`,
`@tauri-apps/plugin-autostart`, `@tauri-apps/plugin-global-shortcut`) ставятся в тот же
package.json — в веб-сборку они не попадают благодаря динамическому импорту (раздел 7).

`vite.config.ts` — ключевое:

```ts
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": "/src" } },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", ws: true }, // FastAPI dev
    },
  },
  build: { target: "es2022", sourcemap: true },
});
```

Одна кодовая база, две сборки Vite (04 §1.1): web (`dist/` отдаётся nginx'ом на
`chat.partner-lead-centre.ru`) и `vite build --mode desktop` для упаковки в Tauri —
отличие только в `VITE_API_BASE=https://chat.partner-lead-centre.ru` (в Tauri origin —
`tauri://localhost`, поэтому база API абсолютная) и `VITE_IS_DESKTOP=1` (tree-shaking
десктоп-модулей из веб-бандла). Нативные фичи включаются рантайм-детектом `isTauri()`.

---

## 1. Структура каталогов и дерево компонентов `/chats`

### 1.1. Каталоги `src/` (feature-based)

```
src/
├── app/                        # сборка приложения, ничего доменного
│   ├── App.tsx                 # <Providers><RouterProvider/></Providers>
│   ├── providers.tsx           # QueryClientProvider, MantineProvider, Notifications
│   ├── router.tsx              # все роуты + guard'ы (раздел 6)
│   ├── theme.ts                # тема Lead Partner (раздел 8)
│   └── queryClient.ts          # QueryClient с дефолтами (раздел 2.4)
│
├── features/                   # по одному каталогу на фичу; фичи не импортируют друг друга
│   ├── auth/                   # /login, сессия, refresh JWT
│   │   ├── LoginPage.tsx
│   │   ├── api.ts              # login/logout/refresh/me
│   │   └── sessionStore.ts     # zustand: user, accessToken (в памяти, не в localStorage)
│   ├── chats/                  # ядро продукта, самая большая фича
│   │   ├── ChatsPage.tsx       # трёхколоночный layout + адаптив
│   │   ├── api.ts              # все fetch-функции фичи
│   │   ├── hooks/              # useConversations, useMessages, useSendMessage, ...
│   │   ├── chatUiStore.ts      # zustand: фильтры, draft'ы (раздел 2.2)
│   │   ├── unreadStore.ts      # zustand: счётчики непрочитанных (раздел 3.5)
│   │   └── components/
│   │       ├── list/           # левая колонка
│   │       ├── thread/         # центр
│   │       └── client-card/    # правая колонка
│   ├── stats/                  # /stats: карточки метрик, тепловая карта, таблица, экспорт
│   ├── settings/
│   │   ├── accounts/           # /settings/accounts (admin)
│   │   ├── team/               # /settings/team (admin)
│   │   ├── bots/               # /settings/bots (admin)
│   │   ├── templates/          # /settings/templates (admin, head)
│   │   └── profile/            # /settings/profile (все)
│   └── templates-picker/       # «⚡ быстрые ответы» — используется из chats, поэтому отдельно
│
├── shared/                     # можно импортировать откуда угодно
│   ├── api/
│   │   ├── http.ts             # fetch-обёртка: baseURL `${VITE_API_BASE ?? ""}/api/v1` (01 §1.1;
│   │   │                       # VITE_API_BASE задаёт desktop-сборка, 04 §1.1), JWT, 401→refresh→retry
│   │   ├── queryKeys.ts        # ЕДИНСТВЕННОЕ место объявления ключей кэша (раздел 2.1)
│   │   └── types.ts            # DTO: ConversationDto, MessageDto, ... (генерятся из OpenAPI)
│   ├── realtime/
│   │   ├── WsClient.ts         # WebSocket-клиент (раздел 3.1)
│   │   ├── applyWsEvent.ts     # события → кэш TanStack Query (раздел 3.3)
│   │   └── connectionStore.ts  # zustand: status, lastEventAt
│   ├── auth/
│   │   └── usePermissions.ts   # права из GET /auth/me (раздел 5)
│   ├── ui/                     # UserAvatar, RelativeTime, EmptyState, ConfirmModal, ...
│   └── lib/                    # formatPhone, plural, insertTemplateVars({имя},{менеджер})
│
├── platform/                   # мост web/Tauri (раздел 7) — единственное десктоп-осознанное место
│   ├── bridge.ts               # интерфейс PlatformBridge + isTauri() + getBridge()
│   ├── web.ts                  # веб-заглушка
│   └── tauri/                  # реализация на Tauri-плагинах (динамический импорт);
│                               # внутри — notifier / offline / updater (04 §1.1)
│
└── main.tsx
```

Правила зависимостей (проверяются eslint-plugin-boundaries):
`features/* → shared, platform`; `shared → shared`; `app → всё`. Кросс-импорты между
фичами запрещены — общее выносится в `shared/` или отдельную фичу
(так `templates-picker` вынесен, потому что нужен и в чатах, и в настройках).

### 1.2. Дерево компонентов экрана `/chats`

Соответствует макету DESIGN 3.3 (три колонки). Зоны ответственности — в комментариях;
компонент не лезет в чужую зону: данные ходят через TanStack Query, UI-состояние — через
`chatUiStore`, никаких prop-drilling цепочек глубже двух уровней.

```
ChatsPage                                  # layout 3 колонок, адаптив (раздел 8.2),
│                                          # синхронизация :id из URL → активный диалог
├── ChatListPane                           # ЛЕВАЯ КОЛОНКА
│   ├── ChatListHeader                     # заголовок «ЧАТЫ», общий счётчик, поиск (debounce 300мс)
│   ├── ChatFilterTabs                     # Мои | Все | Новые | Закрытые → chatUiStore.filters.tab
│   ├── ChatFilterBar                      # селекты: аккаунт Авито; менеджер (виден только head/admin)
│   └── ConversationList                   # TanStack Virtual + useInfiniteQuery (раздел 4.1)
│       └── ConversationListItem           # чистый презентационный: аватар, имя, объявление,
│                                          # последнее сообщение, время, бейджи ● / ⚑ / 🤖;
│                                          # чипы тегов (conversations.tags), «негатив» — акцентный;
│                                          # onClick → navigate(`/chats/${id}`)
│
├── ChatThreadPane                         # ЦЕНТР; если :id нет — <EmptyThreadPlaceholder/>
│   ├── ThreadHeader                       # клиент · объявление · «аккаунт: LP-Москва» · статус;
│   │                                      # на мобильном — кнопка «назад» к списку
│   ├── MessageList                        # TanStack Virtual, обратная лента (раздел 4.2)
│   │   ├── DateDivider                    # «Сегодня», «Вчера», «28 июля»
│   │   ├── MessageBubble                  # in — слева, out — справа (+имя оператора),
│   │   │                                  # 🤖 у сообщений бота, system — серым по центру
│   │   │   ├── AttachmentPreview          # фото/файлы из messages.attachments (jsonb)
│   │   │   └── DeliveryStatusIcon         # pending ⏳ / delivered ✓ / failed ✗ + «повторить»
│   │   ├── NoteBubble                     # direction='note': жёлтая вставка
│   │   │                                  # «видно только сотрудникам»
│   │   └── NewMessagesButton              # «↓ новые сообщения», если скролл не внизу
│   ├── ReadOnlyBanner                     # head: «Режим просмотра — назначьте менеджера или
│   │                                      # передайте диалог»; observer: просто без ввода
│   └── Composer                           # видим при can('messages:send') или can('notes:write')
│       ├── ComposerToolbar                # ⚡ шаблоны | 📎 вложения | 🗒 режим заметки
│       │   ├── TemplatePickerPopover      # из features/templates-picker: поиск, папки,
│       │   │                              # подстановка {имя} {менеджер} перед вставкой
│       │   └── AttachmentButton           # выбор файла → upload → attachment_id в отправку
│       ├── MessageInput                   # textarea: Enter=отправить, Shift+Enter=перенос;
│       │                                  # draft ↔ chatUiStore.drafts[convId]
│       └── SendButton
│
└── ClientCardPane                         # ПРАВАЯ КОЛОНКА (на мобильном — Drawer)
    ├── ClientInfoCard                     # имя, ★рейтинг Авито, «на Авито с 2021»,
    │                                      # телефон (извлечён бэкендом из текста — DESIGN 3.3)
    ├── ItemCard                           # item_title, item_price, [фото], item_url ↗
    ├── TagChips                           # чипы тегов диалога (conversations.tags, 01 §5.1),
    │                                      # «негатив» — акцентный
    ├── ConversationControls               # селект ответственного (conversations:manage; head/admin),
    │                                      # селект статуса new/in_progress/closed,
    │                                      # кнопка «→ Передать» → TransferDialog
    │   └── TransferDialog                 # выбор сотрудника + комментарий (уйдёт как note)
    ├── ClientHistoryList                  # прошлые диалоги этого клиента, клик → /chats/:id
    └── NotesList                          # внутренние заметки карточки; добавление при can('notes:write')
```

Ответственность одним предложением на колонку:

| Колонка | Отвечает за | НЕ отвечает за |
|---|---|---|
| `ChatListPane` | какие диалоги показать (фильтры+поиск), навигацию к диалогу, бейджи | содержимое переписки, мутации диалога |
| `ChatThreadPane` | ленту сообщений, отправку/заметки, статусы доставки | список, данные клиента |
| `ClientCardPane` | карточку клиента/объявления, управление диалогом (статус, ответственный, передача) | ленту и отправку сообщений |

---

## 2. Состояние

Жёсткое правило: **всё, что пришло с сервера, живёт только в кэше TanStack Query;
Zustand — только UI-состояние, которого нет на сервере.** Дублирование серверных данных
в Zustand запрещено (ревью-чеклист).

### 2.1. TanStack Query — фабрика ключей

Единственное место объявления ключей — `shared/api/queryKeys.ts`. Никаких литеральных
массивов в компонентах.

```ts
// shared/api/queryKeys.ts
export type ConversationFilters = {
  tab: "mine" | "all" | "new" | "closed";   // вкладки DESIGN 3.3
  search?: string;                          // имя / текст / телефон
  accountId?: string;                       // фильтр по аккаунту Авито
  assigneeId?: string;                      // фильтр «по менеджеру» (head/admin)
  tag?: string;                             // фильтр по тегу (tag= в 01 §5.1), напр. «негатив»
};

export type StatsFilters = {
  from: string; to: string;                 // ISO-даты периода
  managerIds?: string[];
  accountId?: string;
};

export const qk = {
  me: ["me"] as const,

  conversations: {
    root: ["conversations"] as const,
    list: (f: ConversationFilters) => ["conversations", "list", f] as const,  // infinite
    detail: (id: string) => ["conversations", "detail", id] as const,
  },

  messages: {
    root: ["messages"] as const,
    list: (conversationId: string) => ["messages", conversationId] as const,  // infinite, cursor
  },

  clients: {
    detail: (id: string) => ["clients", id] as const,
    history: (id: string) => ["clients", id, "history"] as const,  // прошлые диалоги клиента
  },

  stats: {
    root: ["stats"] as const,
    summary: (f: StatsFilters) => ["stats", "summary", f] as const,   // карточки метрик
    timeline: (f: StatsFilters) => ["stats", "timeline", f] as const, // график + тепловая карта
    managers: (f: StatsFilters) => ["stats", "managers", f] as const, // таблица по менеджерам
  },

  templates: {
    root: ["templates"] as const,
    list: (scope: "shared" | "personal") => ["templates", scope] as const,
  },

  accounts: ["accounts"] as const,   // /settings/accounts
  team: ["team"] as const,           // /settings/team
  bots: ["bots"] as const,           // /settings/bots
  audit: (page: number) => ["audit", page] as const,
} as const;
```

Замечания:

- `conversations.list` и `messages.list` — `useInfiniteQuery` (пагинация — раздел 4:
  диалоги offset-based, лента сообщений — cursor, по 01 §1.4). Фильтры входят в ключ целиком: смена вкладки = другой ключ = свой кэш,
  переключение вкладок мгновенное (данные из кэша + фоновый refetch).
- `conversations.detail` нужен отдельно от списка: deep-link `/chats/:id` обязан работать,
  когда диалога ещё нет ни на одной загруженной странице списка.
- `search` перед попаданием в ключ — trim + debounce 300 мс (иначе кэш замусоривается
  промежуточными строками).

### 2.2. Zustand — только UI

```ts
// features/chats/chatUiStore.ts
interface ChatUiState {
  // Активный диалог. Источник истины — URL (/chats/:id); стор — зеркало для
  // не-роутерных потребителей (WsClient решает, звучать ли звуку; unreadStore).
  activeConversationId: string | null;

  filters: ConversationFilters;              // вкладка, поиск, аккаунт, менеджер

  // Черновики: и текст, и режим (сообщение/заметка) — переживают переключение диалогов.
  drafts: Record<string, { text: string; isNote: boolean }>;

  soundEnabled: boolean;                     // настройка пользователя
  clientCardOpen: boolean;                   // мобильный Drawer правой колонки

  setActive(id: string | null): void;
  setFilters(p: Partial<ConversationFilters>): void;
  setDraft(convId: string, d: { text: string; isNote: boolean }): void;
  clearDraft(convId: string): void;
}

export const useChatUiStore = create<ChatUiState>()(
  persist(
    (set) => ({ /* ... */ }),
    {
      name: "leadchat-chat-ui",
      // Персистим ТОЛЬКО черновики и звук. Фильтры и активный диалог — сессионные.
      partialize: (s) => ({ drafts: s.drafts, soundEnabled: s.soundEnabled }),
    },
  ),
);
```

Остальные сторы:

| Стор | Содержимое | Персист |
|---|---|---|
| `features/auth/sessionStore` | `user` (id, роль, имя), `accessToken` — **только в памяти**; refresh-cookie httpOnly делает всё остальное (DESIGN 9) | нет |
| `shared/realtime/connectionStore` | `status: 'connecting'\|'open'\|'reconnecting'`, `lastEventAt` | нет |
| `features/chats/unreadStore` | `byConversation: Record<id, number>`, селектор `total()` | нет (сервер отдаёт счётчики при загрузке списка) |

### 2.3. Правила инвалидации

| Событие | Действие с кэшем |
|---|---|
| WS `message:new` | `setQueryData` в `qk.messages.list(convId)` (append) **и** точечное обновление строки в активном списке (раздел 3.3). Никаких `invalidateQueries` — это refetch на каждое сообщение, при 10 000 входящих/сутки недопустимо |
| WS `message:status` (delivered/failed) | `setQueryData`: найти сообщение по id, заменить `delivery_status` |
| WS `conversation:updated` | `setQueryData`: слить `data.patch` в `qk.conversations.detail(id)`; строку в списках обновить, если ключ совпал по фильтру — иначе `invalidateQueries(qk.conversations.root, { refetchType: 'active' })` (смена статуса/ответственного может переместить диалог между вкладками — честнее спросить сервер) |
| Успешная своя мутация статуса/ответственного | оптимистично `detail`, затем как в предыдущей строке; сервер всё равно пришлёт `conversation:updated` — обработчик идемпотентен |
| CRUD шаблонов | `invalidateQueries(qk.templates.root)` |
| Подключили/отключили аккаунт Авито | `invalidateQueries(qk.accounts)` и `qk.conversations.root` |
| Реконнект WS | догон через REST (раздел 3.2, 01 §11.7): `GET /conversations?updated_since=…` + `GET /conversations/{id}/messages?after=…`; офлайн дольше 30 минут — жёсткий `invalidateQueries` на `conversations.root` + `messages.list(activeId)` |
| Смена пользователя (logout/login) | `queryClient.clear()` + очистка zustand-сторов |

### 2.4. Дефолты QueryClient

```ts
// app/queryClient.ts
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,          // realtime доставляет изменения сам — refetch агрессивный не нужен
      gcTime: 30 * 60_000,
      retry: 2,
      refetchOnWindowFocus: false, // фокус-refetch заменён догоном по WS (раздел 3.2)
    },
  },
});
```

Точечные отклонения: `qk.stats.*` — `staleTime: 60_000` (живая точность не нужна);
`qk.me`, `qk.accounts` — `staleTime: 5 * 60_000`.

---

## 3. Realtime-слой

### 3.1. WebSocket-клиент

Подключение по короткоживущему тикету, потому что нативный `WebSocket` не умеет
`Authorization`-заголовок, а класть долгоживущий JWT в query string нельзя (URL логируются).

Контракт (01 §11.1/§11.5):
1. `POST /api/v1/ws/ticket` (с обычным JWT) → `{ "ticket": "<random>", "expires_in": 60 }` — одноразовый.
2. `new WebSocket("wss://chat.partner-lead-centre.ru/api/ws?ticket=<ticket>")`.
3. Heartbeat: **клиент** шлёт `{"type":"ping"}` каждые 25 с, сервер отвечает `{"type":"pong"}`.
   Нет pong'а 10 с → считаем соединение мёртвым, закрываем и реконнектимся
   (ловит «полумёртвые» соединения за NAT). Сервер со своей стороны закрывает сокет
   кодом `4408`, если 60 с не было ни одного кадра; серверные коды закрытия
   `4401` (битый тикет) / `4403` (пользователь деактивирован — уйти на `/login`) /
   `4408`/`1012` обрабатываются в `onclose`.

```ts
// shared/realtime/WsClient.ts
export class WsClient {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closedByUser = false;
  private pingTimer?: ReturnType<typeof setInterval>;
  private pongWatchdog?: ReturnType<typeof setTimeout>;

  constructor(private onEvent: (e: WsEvent) => void) {}

  async connect() {
    this.closedByUser = false;
    connectionStore.setState({ status: this.attempt ? "reconnecting" : "connecting" });
    try {
      const { ticket } = await http.post<{ ticket: string }>("/ws/ticket");
      // В Tauri origin = tauri://localhost — база берётся из VITE_API_BASE (04 §1.1)
      const base = import.meta.env.VITE_API_BASE ?? location.origin;
      const url = `${base.replace(/^http/, "ws")}/api/ws?ticket=${ticket}`;
      this.ws = new WebSocket(url);
      this.ws.onopen = () => {
        this.attempt = 0;
        connectionStore.setState({ status: "open" });
        this.startHeartbeat();                   // клиент пингует каждые 25 с (01 §11.5)
        void catchUpAfterReconnect();            // раздел 3.2 — ВСЕГДА, даже при первом коннекте
      };
      this.ws.onmessage = (ev) => {
        const e = JSON.parse(ev.data) as WsEvent;
        if (e.type === "pong") { clearTimeout(this.pongWatchdog); return; }
        connectionStore.setState({ lastEventAt: e.ts });
        this.onEvent(e);
      };
      this.ws.onclose = (ev) => {
        if (ev.code === 4403) { useSessionStore.getState().logout(); return; } // деактивирован
        this.scheduleReconnect();                // 4401/4408/1012 и любой обрыв — обычный reconnect
      };
      this.ws.onerror = () => this.ws?.close();
    } catch {
      this.scheduleReconnect();                  // не выдали тикет (нет сети/401) — тоже backoff
    }
  }

  private startHeartbeat() {
    clearInterval(this.pingTimer);
    this.pingTimer = setInterval(() => {
      this.ws?.send('{"type":"ping"}');
      // нет pong'а за 10 с → соединение мёртвое (01 §11.5), закрываем — onclose реконнектит
      this.pongWatchdog = setTimeout(() => this.ws?.close(), 10_000);
    }, 25_000);
  }

  private scheduleReconnect() {
    if (this.closedByUser) return;
    clearInterval(this.pingTimer);
    clearTimeout(this.pongWatchdog);
    connectionStore.setState({ status: "reconnecting" });
    // Exponential backoff: 1s, 2s, 4s, 8s, 16s, дальше 30s; full jitter,
    // чтобы 50 клиентов не ломились одновременно после рестарта api.
    const base = Math.min(1000 * 2 ** this.attempt++, 30_000);
    setTimeout(() => this.connect(), Math.random() * base);
  }

  close() { this.closedByUser = true; clearInterval(this.pingTimer); this.ws?.close(); }
}
```

Жизненный цикл: singleton, `connect()` после логина, `close()` при logout.
`document.visibilitychange` в hidden **не** отключает сокет (менеджер должен слышать звук
со свёрнутой вкладкой); мобильный браузер сам заморозит вкладку — на это отвечает догон.

### 3.2. Догон после реконнекта

Пока сокета не было, события потеряны. WS-хаб их не буферизирует — догоняем через REST
(контракт — 01 §11.7):

```
GET /conversations?updated_since=<lastEventAt − 30 c>&limit=200
→ изменившиеся диалоги (обновить строки списков; unread_count приходит в них же)

GET /conversations/{id}/messages?after=<next_cursor>      // только для открытого диалога
→ хвост ленты (курсор сохранён из последнего ответа §4.2 / последнего message:new)
```

```ts
// shared/realtime/applyWsEvent.ts
export async function catchUpAfterReconnect() {
  const since = connectionStore.getState().lastEventAt;
  if (!since) return;                            // первый коннект сессии — кэш и так свежий
  if (Date.now() - Date.parse(since) > 30 * 60_000) {
    // офлайн дольше 30 минут (01 §11.7 п.4): дифф не догоняем — полный refetch дешевле
    queryClient.invalidateQueries({ queryKey: qk.conversations.root });
    queryClient.invalidateQueries({ queryKey: qk.messages.root });
    return;
  }
  // перекрытие −30 с безопасно: applyNewMessage дедуплицирует по message.id
  const overlap = new Date(Date.parse(since) - 30_000).toISOString();
  const diff = await http.get<ConversationsPage>(
    `/conversations?updated_since=${encodeURIComponent(overlap)}&limit=200`);
  diff.items.forEach(applyConversationRow);      // те же функции, что для WS
  const { activeConversationId } = useChatUiStore.getState();
  const cursor = activeConversationId && lastKnownCursor(activeConversationId);
  if (activeConversationId && cursor) {
    const tail = await http.get<MessagesPage>(
      `/conversations/${activeConversationId}/messages?after=${cursor}`);
    tail.items.forEach((m) => applyNewMessage(m.conversation_id, m));
  }
}
```

### 3.3. События → кэш TanStack Query

Типы событий — по каталогу 01 §11.2–11.3 (он полный и нормативный); формат кадра единый:
`{ type, ts, data: {...} }`.

```ts
type UserRef = { id: string; full_name: string };

type WsEvent =
  | { type: "pong"; ts: string; data: { n: number } }
  | { type: "message:new"; ts: string;
      data: { conversation_id: string; message: MessageDto;
              conversation_patch: Partial<ConversationDto> } }
  | { type: "message:status"; ts: string;
      data: { conversation_id: string; message_id: string;
              delivery_status: "delivered" | "failed"; error?: string } }
  | { type: "conversation:updated"; ts: string;
      data: { conversation_id: string; patch: Partial<ConversationDto> } }
  | { type: "conversation:assigned"; ts: string;
      data: { conversation_id: string; assignee: UserRef; assigned_by: UserRef;
              comment: string | null; is_for_you: boolean } }
  | { type: "typing"; ts: string;
      data: { conversation_id: string; source: "client" | "operator"; user?: UserRef } }
  | { type: "presence:online"; ts: string;
      data: { user_id: string; status: "online" | "away" | "offline" } }
  | { type: "account:needs_reauth"; ts: string;
      data: { account_id: string; title: string } }
  | { type: "notify"; ts: string;
      data: { level: "info" | "warning" | "error"; title: string; text: string } };
```

```ts
export function applyNewMessage(convId: string, msg: MessageDto) {
  // 1) Лента. setQueryData на infinite-кэш: append в последнюю страницу.
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId), (old) => {
    if (!old) return old;                                    // лента не открывалась — не создаём кэш
    if (old.pages.some((p) => p.items.some((m) => m.id === msg.id))) return old; // дубль (догон+WS)
    const pages = old.pages.slice();
    const last = pages[pages.length - 1];
    pages[pages.length - 1] = { ...last, items: [...last.items, msg] };
    return { ...old, pages };
  });

  // 2) Строка в списках диалогов: последнее сообщение, время, пересортировка.
  //    Обновляем ВСЕ закэшированные варианты фильтров, где диалог присутствует.
  queryClient.setQueriesData<InfiniteData<ConversationsPage>>(
    { queryKey: [...qk.conversations.root, "list"] },
    (old) => old && bumpConversationRow(old, convId, msg),
  );

  // 3) Непрочитанные + звук — только для чужих входящих не в активном открытом диалоге.
  const { activeConversationId } = useChatUiStore.getState();
  const isActiveAndVisible =
    activeConversationId === convId && document.visibilityState === "visible";
  if (msg.direction === "in" && !isActiveAndVisible) {
    unreadStore.getState().increment(convId);
    notifyNewMessage(msg);                                   // раздел 3.5
  }
}
```

Если диалога нет ни в одном закэшированном списке (совсем новый) —
`invalidateQueries(qk.conversations.root, { refetchType: 'active' })`: одна строка честнее
собирается сервером, чем клиентской реконструкцией DTO.

### 3.4. Оптимистичная отправка: pending → delivered/failed

Серверный контракт (01 §6.2/§1.6, DESIGN 8.2): `POST /api/v1/conversations/{id}/messages`
сразу возвращает `MessageOut` со `delivery_status='pending'`; доставку в Авито делает воркер;
итог прилетает событием `message:status`. Итого у сообщения три фазы в UI: локальный temp →
серверный pending → delivered/failed. Заметки уходят отдельным endpoint'ом
`POST .../notes` (01 §6.4), идемпотентность обоих — `client_message_id` в теле (01 §1.6).

```ts
// features/chats/hooks/useSendMessage.ts
export function useSendMessage(convId: string) {
  return useMutation({
    mutationFn: (v: { text: string; isNote: boolean; tempId: string }) =>
      http.post<MessageDto>(
        v.isNote
          ? `/conversations/${convId}/notes`        // заметки — отдельный endpoint (01 §6.4)
          : `/conversations/${convId}/messages`,
        { text: v.text, client_message_id: v.tempId },   // идемпотентность (01 §1.6)
      ),

    onMutate: async (v) => {
      await queryClient.cancelQueries({ queryKey: qk.messages.list(convId) });
      const user = useSessionStore.getState().user!;
      const temp: MessageDto = {
        id: v.tempId,                       // crypto.randomUUID() — он же client_message_id
        conversation_id: convId,
        direction: v.isNote ? "note" : "out",
        sender_type: "operator",
        sender: { id: user.id, full_name: user.full_name },  // MessageOut.sender (01 §6.1)
        body: v.text,
        attachments: [],
        delivery_status: "pending",
        created_at: new Date().toISOString(),
      };
      applyNewMessage(convId, temp);        // мгновенно в ленте с ⏳
      useChatUiStore.getState().clearDraft(convId);
      return { temp };
    },

    onSuccess: (serverMsg, v) => {
      replaceMessageInCache(convId, v.tempId, serverMsg);   // temp → серверный id, всё ещё pending
    },

    onError: (_e, v) => {
      // Сеть/5xx: НЕ убираем пузырь — помечаем failed, показываем «✗ повторить».
      patchMessageInCache(convId, v.tempId, { delivery_status: "failed" });
      // Черновик возвращаем, чтобы текст не потерялся при уходе со страницы.
      useChatUiStore.getState().setDraft(convId, { text: v.text, isNote: v.isNote });
    },
  });
}
```

«Повторить»: для temp-сообщений (POST не дошёл) — повторный `mutate` с тем же `tempId`
(= тот же `client_message_id` — сервер вернёт уже созданное сообщение, дубля не будет);
для серверных `failed` (воркер исчерпал 5 ретраев, DESIGN 8.2) —
`POST /api/v1/messages/{id}/retry`, сервер переводит в `pending`, дальше обычный
`message:status`. Enter в пустом поле, двойной Enter — блокируются в `Composer`
(кнопка disabled, пока `isPending` **не** ставим — очередь из нескольких сообщений
подряд легальна, каждое со своим tempId).

`message:status` может обогнать ответ POST (воркер быстрее сети клиента):
`patchMessageInCache` по неизвестному id складывает патч в буфер `pendingPatches`,
`replaceMessageInCache` применяет буфер после подстановки серверного id.

В Tauri `mutationFn` не шлёт HTTP сам: `useSendMessage` делегирует в
`bridge.offlineQueue.push` (outbox — единственный путь отправки в десктопе,
и онлайн, и офлайн, 04 §5.3); `client_message_id` = `client_msg_id`,
который вернул `outbox_push`.

### 3.5. Звук и бейдж в title

```ts
// features/chats/notify.ts
const audio = new Audio("/sounds/new-message.mp3");   // лежит в public/, ~20КБ
let lastSound = 0;

export function notifyNewMessage(msg: MessageDto) {
  const { soundEnabled } = useChatUiStore.getState();
  if (soundEnabled && Date.now() - lastSound > 2000) {  // не строчить очередью
    lastSound = Date.now();
    audio.play().catch(() => {});   // autoplay-policy до первого клика — молча глотаем
  }
  void getBridge().notify({        // web: no-op/Notification API; Tauri: нативный тост (раздел 7)
    title: msg.sender?.full_name ?? "Новое сообщение",   // MessageOut.sender (01 §6.1)
    body: msg.body ?? "Вложение",
    conversationId: msg.conversation_id,
  });
}

// app/providers.tsx — подписка на суммарный счётчик
useEffect(() =>
  unreadStore.subscribe((s) => {
    const total = s.total();
    document.title = total > 0 ? `(${total}) LeadChat` : "LeadChat";  // DESIGN 3.3
    void getBridge().setBadge(total);                                  // трей-бейдж в Tauri
  }), []);
```

Обнуление счётчика диалога: при открытии диалога и при `visibilitychange → visible`
с открытым диалогом клиент шлёт `POST /api/v1/conversations/{id}/read`, локально
`unreadStore.reset(convId)`; сервер разошлёт `conversation:updated` остальным вкладкам/устройствам.

---

## 4. Виртуализация (TanStack Virtual)

Обе длинные поверхности виртуализируются — при 10 000 сообщений/сутки список из
нескольких тысяч строк без виртуализации убьёт вкладку (DESIGN прямо называет это критичным).

### 4.1. Список диалогов

Прямой случай: прокрутка вниз, подгрузка следующих страниц.

```ts
// API: GET /api/v1/conversations?tab=&q=&account_id=&assignee_id=&tag=&limit=50&offset=0
// Пагинация offset-based (01 §1.4): ответ несёт page.{limit, offset, total};
// сортировка серверная: с непрочитанными наверху (внутри группы непрочитанных
// диалоги с тегом «негатив» — первыми, DESIGN §4.3), затем по last_message_at DESC.

const q = useInfiniteQuery({
  queryKey: qk.conversations.list(filters),
  queryFn: ({ pageParam }) => fetchConversations(filters, pageParam),  // pageParam = offset
  initialPageParam: 0,
  getNextPageParam: (last, all) => {
    const loaded = all.reduce((n, p) => n + p.items.length, 0);
    return loaded < last.page.total ? loaded : undefined;              // undefined = конец
  },
});
const rows = useMemo(() => q.data?.pages.flatMap((p) => p.items) ?? [], [q.data]);

const virtualizer = useVirtualizer({
  count: rows.length + (q.hasNextPage ? 1 : 0),   // +1 — строка-лоадер
  getScrollElement: () => scrollRef.current,
  estimateSize: () => 76,                          // фикс. высота строки — measure не нужен
  overscan: 8,
  getItemKey: (i) => rows[i]?.id ?? "loader",
});

// Достигли лоадера — тянем следующую страницу
useEffect(() => {
  const last = virtualizer.getVirtualItems().at(-1);
  if (last && last.index >= rows.length - 1 && q.hasNextPage && !q.isFetchingNextPage)
    q.fetchNextPage();
}, [virtualizer.getVirtualItems(), rows.length]);
```

Высота строки фиксированная (76px, текст обрезается `line-clamp`), поэтому без
`measureElement` — дешевле. Пересортировка при `message:new` не дёргает скролл:
ключи стабильны (`getItemKey` по id), виртуализатор пересчитает позиции сам.

### 4.2. Лента сообщений: обратная прокрутка

Требования: открытие внизу (последнее сообщение), скролл вверх подгружает старые,
позиция не прыгает при prepend'е, новые сообщения приклеивают вниз, только если
пользователь и так внизу.

```ts
// API: GET /api/v1/conversations/{id}/messages?limit=50&before=<cursor>   (01 §1.4)
// before — сообщения СТАРЕЕ курсора: страница = 50 сообщений до него (по created_at,id DESC),
// в ответе items уже отсортированы ASC + prev_cursor для следующей (более старой) страницы;
// after=<cursor> используется догоном после реконнекта (раздел 3.2).

const q = useInfiniteQuery({
  queryKey: qk.messages.list(convId),
  queryFn: ({ pageParam }) => fetchMessages(convId, pageParam),
  initialPageParam: null as string | null,
  getNextPageParam: () => null,                      // вниз ничего не догружаем — низ живой (WS)
  getPreviousPageParam: (first) => first.prev_cursor, // вверх — старые
  select: (d) => ({ ...d, flat: d.pages.flatMap((p) => p.items) }), // ASC: старые → новые
});

const virtualizer = useVirtualizer({
  count: q.data?.flat.length ?? 0,
  getScrollElement: () => scrollRef.current,
  estimateSize: () => 64,
  overscan: 12,
  getItemKey: (i) => q.data!.flat[i].id,
  measureElement: (el) => el.getBoundingClientRect().height, // высоты пузырей переменные
});
```

Три механики:

```ts
// 1) Начальная позиция — вниз, без анимации, до отрисовки (useLayoutEffect):
useLayoutEffect(() => {
  if (q.isSuccess) virtualizer.scrollToIndex(q.data.flat.length - 1, { align: "end" });
}, [convId, q.isSuccess]);   // только при смене диалога, не на каждый рендер

// 2) Подгрузка старых при подходе к верху + сохранение позиции.
//    Prepend страницы меняет индексы всех элементов — компенсируем дельтой высоты.
const onScroll = () => {
  const el = scrollRef.current!;
  if (el.scrollTop < 400 && q.hasPreviousPage && !q.isFetchingPreviousPage) {
    const prevTotal = virtualizer.getTotalSize();
    q.fetchPreviousPage().then(() => {
      requestAnimationFrame(() => {
        // виртуализатор уже перемерил: возвращаем виewport на тот же контент
        el.scrollTop += virtualizer.getTotalSize() - prevTotal;
      });
    });
  }
};

// 3) Прилипание к низу. «Внизу» = отступ < 120px.
const stickToBottom = useRef(true);
// в onScroll: stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
useEffect(() => {
  if (!q.data) return;
  if (stickToBottom.current) {
    virtualizer.scrollToIndex(q.data.flat.length - 1, { align: "end" });
  } else {
    setShowNewButton(true);       // «↓ новые сообщения» вместо принудительного скролла
  }
}, [q.data?.flat.length]);
```

Свои исходящие — всегда скролл вниз независимо от `stickToBottom` (пользователь только
что нажал «отправить», он ждёт свой пузырь). Ограничение кэша: при уходе с диалога, если
загружено > 6 страниц, обрезаем до последних двух (`setQueryData`), чтобы возврат был
мгновенным, но память не росла бесконечно. Дата-разделители не отдельные строки
виртуализатора — рендерятся внутри `MessageBubble`, когда дата текущего сообщения
отличается от предыдущего (`flat[i-1]`): так индексация не расходится с данными.

---

## 5. Роли в UI

### 5.1. `usePermissions`

Права — плоские строки; **источник — список `permissions` из ответа `GET /auth/me`**
(01 §2.5): бэкенд отдаёт вычисленный набор, фронт роль→права не хардкодит — при изменении
матрицы прав фронт менять не придётся. Названия прав совпадают с бэкендовыми
`require_permission(...)` — каталог имён один, в 01 §12 (проекция DESIGN 5.1).

```ts
// shared/auth/usePermissions.ts
export type Role = "admin" | "head" | "manager" | "observer";

// Тип — строго каталог 01 §12; расхождение имён между фронтом и бэком запрещено
export type Permission =
  | "conversations:read"    // читать все диалоги
  | "messages:send"         // отправлять сообщения
  | "conversations:manage"  // менять статус / назначать / передавать
  | "notes:read"
  | "notes:write"           // внутренние заметки
  | "templates:own"         // личные шаблоны
  | "templates:shared"      // общие шаблоны
  | "stats:own"             // виджет «моя статистика»
  | "stats:all"             // статистика по всем
  | "bots:manage"
  | "accounts:read"
  | "accounts:manage"
  | "users:manage"
  | "audit:read";

export function usePermissions() {
  const role = useSessionStore((s) => s.user?.role);
  const permissions = useSessionStore((s) => s.user?.permissions);  // из GET /auth/me
  return useMemo(() => {
    const set = new Set<Permission>(permissions ?? []);
    return {
      role,
      can: (p: Permission) => set.has(p),
      canAny: (...ps: Permission[]) => ps.some((p) => set.has(p)),
    };
  }, [role, permissions]);
}
```

Напоминание из DESIGN 3.7/9: **UI-скрытие — это UX, а не безопасность**; каждое право
дублируется permission-check'ом на endpoint'е. Фронт не пытается быть барьером.

### 5.2. Таблица «элемент интерфейса → право»

| Элемент | Право / условие | admin | head | manager | observer |
|---|---|:-:|:-:|:-:|:-:|
| Пункт меню «Чаты», список, поиск, фильтры | `conversations:read` | ✅ | ✅ | ✅ | ✅ |
| `Composer` в режиме сообщения | `messages:send` | ✅ | ❌ | ✅ | ❌ |
| `ReadOnlyBanner` («Режим просмотра…») | `!messages:send && notes:write` | — | ✅ | — | — |
| Кнопка/режим «🗒 заметка», `NotesList`-добавление | `notes:write` | ✅ | ✅ | ✅ | ❌ |
| Селект статуса, кнопка «→ Передать» | `conversations:manage` | ✅ | ✅ | ✅ | ❌ |
| Селект «ответственный» в `ConversationControls` | `conversations:manage` + роль head/admin | ✅ | ✅ | ❌* | ❌ |
| Фильтр «по менеджеру» в `ChatFilterBar` | `stats:all` | ✅ | ✅ | ❌ | ❌ |
| Пункт меню «Статистика» (все сотрудники) | `stats:all` | ✅ | ✅ | ❌ | ❌ |
| Виджет «моя статистика за сегодня» | `stats:own` | — | — | ✅ | ❌ |
| `/settings/templates` (общие шаблоны) | `templates:shared` | ✅ | ✅ | ❌ | ❌ |
| Личные шаблоны в `/settings/profile` | `templates:own` | ✅ | ✅ | ✅ | ❌ |
| `/settings/bots` | `bots:manage` | ✅ | ❌ | ❌ | ❌ |
| `/settings/accounts` | `accounts:manage` (просмотр — `accounts:read`) | ✅ | ❌ | ❌ | ❌ |
| `/settings/team` | `users:manage` | ✅ | ❌ | ❌ | ❌ |
| Журнал аудита (вкладка в настройках) | `audit:read` | ✅ | ✅ | ❌ | ❌ |
| Кнопка «повторить» у failed-сообщения | `messages:send` | ✅ | ❌ | ✅ | ❌ |
| `TemplatePickerPopover` (⚡) | `messages:send` | ✅ | ❌ | ✅ | ❌ |

\* у менеджера право `conversations:manage` есть — он передаёт диалог через «→ Передать»;
свободный селект переназначения чужих диалогов показывается только head/admin
(UI-решение, не отдельное право).

### 5.3. Режим просмотра (head / observer)

`ChatThreadPane` выбирает низ панели одной функцией:

```tsx
function ThreadFooter({ convId }: { convId: string }) {
  const { can } = usePermissions();
  if (can("messages:send")) return <Composer convId={convId} />;          // admin, manager
  if (can("notes:write"))                                                 // head
    return (
      <>
        <ReadOnlyBanner text="Режим просмотра — назначьте менеджера или передайте диалог" />
        <Composer convId={convId} noteOnly />   {/* только жёлтые заметки, ⚡ и 📎 скрыты */}
      </>
    );
  return null;                                                            // observer: ничего
}
```

У observer скрыты также `ConversationControls` (кроме чтения), добавление заметок и
`TransferDialog` — но фильтры и поиск полноценны (DESIGN 5.2).

---

## 6. Роутинг

React Router 6.26 (data-router `createBrowserRouter`). Карта — ровно DESIGN 3.1.

```tsx
// app/router.tsx
export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },

  {
    element: <RequireAuth />,                       // guard 1: сессия
    children: [{
      element: <AppLayout />,                       // сайдбар ≡, индикатор соединения, юзер
      children: [
        { index: true, element: <Navigate to="/chats" replace /> },
        { path: "/chats", element: <ChatsPage /> },
        { path: "/chats/:id", element: <ChatsPage /> },   // тот же компонент — deep-link

        {
          element: <RequirePermission anyOf={["stats:all"]} />,       // guard 2: права
          children: [{ path: "/stats", element: <StatsPage /> }],
        },
        {
          element: <RequirePermission anyOf={["accounts:manage"]} />,
          children: [{ path: "/settings/accounts", element: <AccountsPage /> }],
        },
        {
          element: <RequirePermission anyOf={["users:manage"]} />,
          children: [{ path: "/settings/team", element: <TeamPage /> }],
        },
        {
          element: <RequirePermission anyOf={["bots:manage"]} />,
          children: [{ path: "/settings/bots", element: <BotsPage /> }],
        },
        {
          element: <RequirePermission anyOf={["templates:shared"]} />,
          children: [{ path: "/settings/templates", element: <TemplatesPage /> }],
        },
        { path: "/settings/profile", element: <ProfilePage /> },          // все роли
      ],
    }],
  },
  { path: "*", element: <NotFoundPage /> },
]);
```

Guard'ы:

```tsx
function RequireAuth() {
  const { user, bootstrapped } = useSessionStore();
  const loc = useLocation();
  if (!bootstrapped) return <FullscreenLoader />;   // идёт тихий refresh по cookie при старте
  if (!user) return <Navigate to="/login" state={{ from: loc }} replace />;
  return <Outlet />;
}

function RequirePermission({ anyOf }: { anyOf: Permission[] }) {
  const { canAny } = usePermissions();
  // Не 403-страница, а редирект в /chats: пункта меню юзер и так не видел,
  // сюда попадают только по прямой ссылке.
  return canAny(...anyOf) ? <Outlet /> : <Navigate to="/chats" replace />;
}
```

Deep-link `/chats/:id`:

```tsx
// ChatsPage.tsx
const { id } = useParams();                          // URL — источник истины
const setActive = useChatUiStore((s) => s.setActive);
useEffect(() => { setActive(id ?? null); }, [id]);   // зеркалим в стор для WS/unread

// клик по строке списка:
navigate(`/chats/${conv.id}`);                       // history работает: back возвращает список
```

Диалог по deep-link грузится через `qk.conversations.detail(id)` независимо от списка;
404 от API → нотификация «Диалог не найден» + `navigate('/chats')`. Ссылку на диалог
менеджеры кидают друг другу в мессенджеры — это штатный сценарий, deep-link обязан
работать с холодного старта. Нативное уведомление (Tauri) по клику делает
`router.navigate('/chats/' + conversationId)` — тот же механизм.

nginx: SPA-fallback `try_files $uri /index.html;` для всех не-`/api` путей (иначе
холодный `/chats/:id` отдаст 404).

### Логин

После `POST /api/v1/auth/login` — `navigate(state.from ?? '/chats')`. При старте приложения
`sessionStore.bootstrap()` делает тихий `POST /api/v1/auth/refresh` (httpOnly-cookie, DESIGN 9):
успех → пользователь сразу в кабинете, «запомнить меня» работает без localStorage-токенов.

---

## 7. Совместимость с Tauri

Принцип DESIGN 1.7: одна кодовая база, нативные фичи включаются рантайм-детектом
(для десктопа — отдельный режим сборки `vite --mode desktop` с абсолютной базой API,
см. раздел 1 и 04 §1.1). Весь Tauri-специфичный код изолирован в `src/platform/` —
фичи зовут только интерфейс.

```ts
// platform/bridge.ts
export function isTauri(): boolean {
  return "__TAURI_INTERNALS__" in window;    // инжектится Tauri 2 в WebView
}

export interface PlatformBridge {
  /** Нативный тост (Windows Action Center) или Notification API / no-op в вебе */
  notify(n: { title: string; body: string; conversationId: string }): Promise<void>;
  /** Бейдж непрочитанных: иконка трея в Tauri; в вебе — no-op (title меняется отдельно) */
  setBadge(count: number): Promise<void>;
  /** Клик по уведомлению / «Ответить» из тоста */
  onNotificationAction(cb: (a: { conversationId: string; replyText?: string }) => void): void;
  /** Офлайн-очередь исходящих (DESIGN 3.6); в вебе — заглушка «офлайна нет» */
  offlineQueue: {
    push(item: OutgoingDraft): Promise<void>;
    drain(send: (item: OutgoingDraft) => Promise<void>): Promise<void>;
    size(): Promise<number>;
  };
  /** Локальный кэш последних 200 диалогов для мгновенного старта (SQLite) */
  convCache: {
    warmup(): Promise<CachedSnapshot | null>;   // прочитать при старте
    persist(s: CachedSnapshot): Promise<void>;  // фоново обновлять
  };
  openExternal(url: string): Promise<void>;     // item_url «ссылка на Авито ↗»
}

let bridge: PlatformBridge | null = null;
export function getBridge(): PlatformBridge {
  if (!bridge) throw new Error("bridge not initialized");
  return bridge;
}

export async function initBridge(): Promise<PlatformBridge> {
  // Динамический импорт: в веб-бандле чанк tauri.js просто никогда не запрашивается.
  bridge = isTauri()
    ? (await import("./tauri")).createTauriBridge()
    : (await import("./web")).createWebBridge();
  return bridge;
}
```

Веб-заглушка — честный минимум, ничего не эмулирует:

```ts
// platform/web.ts
export function createWebBridge(): PlatformBridge {
  return {
    async notify(n) {
      // Только если вкладка скрыта и разрешение выдано; не спрашиваем разрешение сами —
      // спрашивает переключатель «уведомления браузера» в /settings/profile.
      if (document.visibilityState === "visible") return;
      if (Notification.permission !== "granted") return;
      const note = new Notification(n.title, { body: n.body, icon: "/icon-192.png" });
      note.onclick = () => { window.focus(); listeners.forEach((cb) => cb({ conversationId: n.conversationId })); };
    },
    async setBadge() {},                              // бейдж в вебе = title, см. 3.5
    onNotificationAction(cb) { listeners.push(cb); },
    offlineQueue: {
      async push() { throw new OfflineUnsupported(); }, // Composer покажет «нет соединения»
      async drain() {},
      async size() { return 0; },
    },
    convCache: { async warmup() { return null; }, async persist() {} },
    async openExternal(url) { window.open(url, "_blank", "noopener"); },
  };
}
```

Tauri-реализация (`platform/tauri/`, модули notifier/offline/updater — 04 §1.1): `plugin-notification` для тостов с action
«Ответить», `plugin-sql` (SQLite) для `offlineQueue` и `convCache`, команда Rust-ядра
для бейджа трея. Автозапуск, глобальный хоткей Ctrl+Shift+L, «закрытие → в трей»,
автообновление — целиком на Rust-стороне Tauri, фронт о них не знает (отдельный
десктоп-документ).

Интеграционные точки в общем коде — ровно четыре, все уже описаны выше:
`notifyNewMessage` (3.5), подписка на unread-total (3.5), отправка из `Composer`:
в Tauri путь **всегда** один — через `bridge.offlineQueue.push` (outbox, 04 §5.3),
и онлайн, и офлайн: `useSendMessage` (3.4) в десктопе делегирует в bridge,
`client_message_id` = `client_msg_id`, который вернул `outbox_push`, а flush делает
Rust-команда `outbox_flush` (по push, reconnect WS, `online`, таймеру — 04 §5.3);
в вебе отправка идёт напрямую HTTP через `useSendMessage`, офлайн-очереди нет —
при `navigator.onLine === false` Composer показывает «нет соединения» (заглушка
web-моста кидает `OfflineUnsupported`); `ChatsPage` на старте вызывает
`convCache.warmup()` и рисует снапшот до первого ответа API (плейсхолдер
«обновляется…», затем данные из TanStack Query замещают снапшот).

---

## 8. Тема Lead Partner

### 8.1. Токены Mantine

**Светлая тема — основная**, тёмная — опциональная (DESIGN §3.2/§6; палитра снята с CSS
partner-lead-centre.ru: сайт светлый, фирменный зелёный `#3cc13b`). Канонические токены,
полный tuple и контрасты — **docs/10-UX-DESIGN-SYSTEM.md §1** (он источник истины по теме;
ниже — структура подключения, значения tuple взять из 10 §1.5).

```ts
// app/theme.ts
import { createTheme, MantineColorsTuple } from "@mantine/core";

// Брендовый акцент Lead Partner — канонический tuple из docs/10 §1.5 (база #3cc13b)
const lp: MantineColorsTuple = [
  "#d8f3d8", "#bfe9be", "#9ee09d", "#8ada89", "#5ecf5d",
  "#3cc13b", "#2ea32d", "#1e7a1d", "#175c16", "#0c2e0c",
];

export const theme = createTheme({
  primaryColor: "lp",
  colors: {
    lp,
    dark: [   // фон/поверхности тёмной темы — переопределяем под сайт, а не дефолт Mantine
      "#C9CDD3", "#A6ACB5", "#7E8590", "#5C636E", "#3B414B",
      "#2B303A",  // dark.5 — поверхности (карточки, колонки)
      "#232833",  // dark.6 — фон панелей
      "#1B202A",  // dark.7 — основной фон приложения
      "#151922",  // dark.8 — фон списков/инпутов
      "#0F131A",  // dark.9
    ],
  },
  fontFamily: "'Proxima Nova', Roboto, 'Inter', -apple-system, 'Segoe UI', sans-serif", // стек сайта (10 §2)
  defaultRadius: "md",
  headings: { fontWeight: "600" },
  components: {
    Button: { defaultProps: { radius: "md" } },
    Tooltip: { defaultProps: { openDelay: 400 } },
  },
});

// app/providers.tsx
<MantineProvider theme={theme} defaultColorScheme="light">
```

Семантические CSS-переменные поверх Mantine (используются в чат-компонентах,
чтобы пузыри не были захардкожены на палитру):

```css
/* app/chat-vars.css */
:root[data-mantine-color-scheme="dark"] {
  --chat-bubble-in: var(--mantine-color-dark-5);
  --chat-bubble-out: var(--mantine-color-lp-9);
  --chat-bubble-note: #4d431a;            /* жёлтая заметка «видно только сотрудникам» */
  --chat-bubble-note-text: #ffe58f;
  --chat-unread-badge: var(--mantine-color-lp-5);
}
:root[data-mantine-color-scheme="light"] {
  --chat-bubble-in: var(--mantine-color-gray-1);
  --chat-bubble-out: var(--mantine-color-lp-0);
  --chat-bubble-note: #fff8dc;
  --chat-bubble-note-text: #7a6400;
  --chat-unread-badge: var(--mantine-color-lp-6);
}
```

Переключатель темы — в `/settings/profile`, значение хранит Mantine
(`localStorage: leadchat-color-scheme`). Фавикон и `<title>` — «LeadChat by Lead Partner»
(DESIGN 6). Страница `/login` — отдельный полноэкранный layout: тёмный фон, логотип,
слоган «Мы ремонтируем — Вы зарабатываете», внизу ссылка «Скачать приложение для Windows»
→ `/download`.

### 8.2. Адаптив для мобильного браузера

Десктоп — основной сценарий; мобильный браузер (DESIGN 1.1) должен быть рабочим, не
идеальным (полноценное PWA — фаза 2.0). Брейкпоинты Mantine: `sm = 768px`, `lg = 1200px`.

| Ширина | Раскладка `/chats` |
|---|---|
| ≥ 1200px | три колонки: 320px │ flex-1 │ 300px |
| 768–1199px | две колонки (список │ переписка); `ClientCardPane` → `Drawer` справа, кнопка «Клиент» в `ThreadHeader` |
| < 768px | одна колонка-«стек»: `/chats` показывает только список; `/chats/:id` — только переписку с кнопкой «← назад» (это уже даёт роутер — отдельного состояния не нужно); карточка — тот же `Drawer` на всю ширину |

```tsx
// ChatsPage.tsx — раскладка без дублирования дерева
const isMobile = useMediaQuery("(max-width: 767px)");
const isNarrow = useMediaQuery("(max-width: 1199px)");
const { id } = useParams();

if (isMobile) return id ? <ChatThreadPane /> : <ChatListPane />;
return (
  <div className={styles.threeCols}>
    <ChatListPane />
    <ChatThreadPane />
    {isNarrow ? <ClientCardDrawer /> : <ClientCardPane />}
  </div>
);
```

Мобильные обязательные мелочи: `viewport-fit=cover` + `env(safe-area-inset-*)` в паддингах
`Composer`; высота ленты через `100dvh` (не `100vh` — клавиатура iOS); тап-таргеты строк
списка ≥ 44px; `MessageInput` — `font-size: 16px` (iOS иначе зумит поле при фокусе).
Виртуализация и WS-слой от раскладки не зависят — те же компоненты.

---

## Чеклист готовности фичи «Чаты» (definition of done этапа 2 плана DESIGN 7)

- [ ] `/chats` и `/chats/:id` открываются с холодного старта (SPA-fallback в nginx)
- [ ] Сообщение приходит по WS < 1 с, звук и `(N) LeadChat` в title работают
- [ ] Обрыв сети → реконнект с backoff → догон `updated_since`/`after` (01 §11.7) возвращает
      пропущенное (проверяется выключением Wi-Fi на 2 мин)
- [ ] Отправка: пузырь мгновенно (⏳), `delivered` по WS, `failed` + «повторить» при
      остановленном воркере
- [ ] 5 000 диалогов / 10 000 сообщений в кэше — скролл 60 fps (React DevTools Profiler)
- [ ] Подгрузка старых сообщений не дёргает позицию скролла
- [ ] Черновик переживает переключение диалога и перезагрузку страницы
- [ ] head видит плашку режима просмотра, но пишет заметки и переназначает; observer —
      только читает; прямые URL настроек редиректят по правам
- [ ] Мобильная раскладка: список → диалог → назад, карточка клиента в Drawer
- [ ] Desktop-сборка (`vite --mode desktop`, 04 §1.1) запускается в Tauri: тосты, бейдж трея,
      офлайн-очередь (заглушка в вебе не ломает веб-сборку)
