/**
 * СТЕНД ДЛЯ ПРОВЕРКИ ВЁРСТКИ. Только разработка, в сборку не входит.
 *
 * ЗАЧЕМ. Бриф редизайна требует проверять КАЖДЫЙ блок правок в двух цветовых
 * схемах и на трёх ширинах (1920 / 1280 / 390). Сделать это на живом
 * приложении нельзя без входа под сотрудником, а вход требует пароля, сервера
 * и базы — то есть проверка вёрстки упирается в бэкенд, к вёрстке отношения
 * не имеющий.
 *
 * Стенд рендерит НАСТОЯЩИЕ экраны с настоящими стилями, подменяя ровно две
 * вещи: сессию (кто вошёл) и сетевые ответы. Ничего специально для стенда в
 * самих экранах нет — если стенд показывает поломку, она есть и в приложении.
 *
 * ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ: логики. Как только стенд начнёт «чинить» экран
 * своими данными, он перестанет быть измерительным прибором и станет вторым
 * приложением, которое расходится с первым.
 *
 * Запуск:  npm run dev  →  http://localhost:5173/dev-preview.html?screen=profile
 * Экраны:  ?screen=<ключ из SCREENS>, тема — ?theme=light|dark
 * Состояние сети: ?state=ok|empty|many|slow|timeout|offline|error401|error403|error500
 *
 * ЧЕГО `?w=` НЕ УМЕЕТ, И ЭТО ВАЖНЕЕ ТОГО, ЧТО УМЕЕТ.
 *
 * Параметр задаёт ширину БЛОКА, а медиазапросы смотрят на ширину ОКНА. То
 * есть `?w=390` показывает узкий блок на широком экране — раскладка при этом
 * остаётся настольной, и ни одно мобильное правило не включается. Проверка
 * «на 390px» через `?w=` мобильную вёрстку не проверяет ВООБЩЕ.
 *
 * Разница не косметическая. У шапки ленты на `max-width: 767px` включается
 * перенос в две строки; без него имени клиента доставалось ноль пикселей.
 * Через `?w=390` виден именно нулевой вариант — то есть поломка, которой в
 * жизни нет, — а настоящий мобильный вид не виден никогда.
 *
 * Поэтому три ширины из брифа проверяются НАСТОЯЩИМ размером окна, а `?w=`
 * годится ровно для одного: посмотреть, как экран ведёт себя в узкой колонке
 * при настольной раскладке.
 *
 * (Отдельно: до 9 августа `?w=` не работал совсем — `.settings-content`
 * объявлен `flex: 1`, и `flex-grow` бил заданную ширину. Стенд молча
 * показывал ширину окна на любом значении параметра.)
 */

import { StrictMode, Suspense, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { Button, MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { cssVariablesResolver, theme } from "@/app/theme";
import { useSessionStore } from "@/shared/stores/sessionStore";
import type { SessionUser } from "@/shared/api/types";

import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "@/app/lc-vars.css";
import "@/app/lc-base.css";
import "@/app/lc-table-cards.css";
import "@/app/app-layout.css";
/*
 * Стили раздела настроек тянет SettingsLayout, которого в стенде нет. Без них
 * `.settings-page` остаётся обычным блоком без заданной высоты — а половина
 * поломок раскладки видна ТОЛЬКО при заданной высоте (колонки с `columns`
 * начинают уходить вбок, а не вниз). Подключаем явно, иначе стенд врёт в
 * безопасную сторону, что хуже, чем не мерить вовсе.
 */
import "@/features/settings/settings.css";

/* --------------------------------------------------------- кто «вошёл» --- */

const USER: SessionUser = {
  id: "0d4f0a1e-6b7c-4b1e-9a2d-1f3e5c7a9b0d",
  // Длинная почта намеренно: короткая прячет ровно те переполнения, ради
  // которых стенд и заводился.
  email: "ekaterina.vinogradova-dispatcher@partner-lead-centre.ru",
  full_name: "Екатерина Виноградова",
  role: "admin",
  is_active: true,
};

const ALL_PERMISSIONS = [
  "conversations:read",
  "conversations:manage",
  "messages:send",
  "notes:read",
  "notes:write",
  "templates:own",
  "templates:shared",
  "stats:own",
  "stats:all",
  "accounts:manage",
  "users:manage",
  "bots:manage",
  "distribution:manage",
  "audit:read",
  "notifications:read",
];

/* ------------------------------------------------------- ответы сервера --- */

/**
 * Сеть отвечает пустыми, но ПРАВИЛЬНОЙ ФОРМЫ данными.
 *
 * Пустой ответ — не бедность стенда, а самый строгий случай для вёрстки:
 * пустые состояния и скелетоны проверяются именно на нём. Экраны, которым
 * нужны непустые данные, получают их точечно ниже.
 */
const FIXTURES: Record<string, unknown> = {
  "/auth/me": { ...USER, permissions: ALL_PERMISSIONS },
  "/avito-accounts": { items: [], page: { limit: 50, offset: 0, total: 0 } },
  "/avito/app": {
    client_id: "",
    secret_set: false,
    api_base: "https://api.avito.ru",
    auth_url: "https://www.avito.ru/oauth",
    live: true,
    source: "db",
    redirect_uri: "https://example.test/api/v1/avito/callback",
  },
  // Шаблоны — НЕ пустые: именно длинные тексты ответов раздували раскладку
  // профиля вширь, и на пустом списке эта поломка не видна вовсе.
  "/templates/folders": { folders: ["Диагностика", "Цены и сроки", "Отказы"] },
  "/templates": {
    items: [
      {
        id: "t-1",
        title: "Цена на диагностику",
        body:
          "Здравствуйте! Выезд мастера и диагностика — 0 ₽ при последующем ремонте. " +
          "Если от ремонта откажетесь, диагностика 500 ₽. Мастер приедет в удобное вам время, " +
          "работаем ежедневно с 8:00 до 22:00. Подскажите, пожалуйста, адрес и марку техники?",
        folder: "Цены и сроки",
        shared: true,
        created_at: "2026-08-01T10:00:00Z",
      },
      {
        id: "t-2",
        title: "Не наш профиль",
        body:
          "К сожалению, этот вид техники мы не ремонтируем — рекомендуем обратиться " +
          "в специализированный сервис. Извините за неудобство!",
        folder: "Отказы",
        shared: false,
        created_at: "2026-08-02T10:00:00Z",
      },
      {
        id: "t-3",
        title: "Уточнение модели",
        body:
          "Подскажите, пожалуйста, полное название модели — оно на шильдике сзади или " +
          "на внутренней стороне дверцы. Можно просто сфотографировать: так мастер приедет " +
          "сразу с нужной деталью и ремонт займёт один визит.",
        folder: "Диагностика",
        shared: true,
        created_at: "2026-08-03T10:00:00Z",
      },
    ],
  },
  // Сотрудники — НЕ пустые: карточный режим таблицы виден только на данных,
  // а он и есть предмет проверки адаптива.
  "/users": {
    // ТРИНАДЦАТЬ ЧЕЛОВЕК — столько их у заказчика. На двух строках не видно
    // ни того, занимает ли таблица высоту, ни того, как ведут себя длинные
    // почты и названия отделов: стенд показывал бы удобную выдумку.
    //
    // Ключ поля — `handles_conversations`, и это не мелочь. В прежней
    // фикстуре стояло `takes_conversations`, такого поля у ответа нет, и
    // ВСЕ переключатели «Ведёт диалоги» рисовались выключенными. То есть
    // стенд показывал команду, которая целиком не берёт обращения, — и на
    // этой картинке нельзя было судить ни о столбце, ни о его ширине.
    items: [
      {
            "id": "u-1",
            "email": "ekaterina.vinogradova-dispatcher@partner-lead-centre.ru",
            "full_name": "Екатерина Виноградова",
            "role": "admin",
            "is_active": true,
            "handles_conversations": true,
            "department": "Диспетчерская",
            "presence": "online",
            "is_online": true,
            "invite_pending": false,
            "last_seen_at": "2026-08-09T09:00:00Z"
      },
      {
            "id": "u-2",
            "email": "petr@partner-lead-centre.ru",
            "full_name": "Пётр Ковалёв",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 1",
            "presence": null,
            "is_online": false,
            "invite_pending": false,
            "last_seen_at": null
      },
      {
            "id": "u-3",
            "email": "olga.kovaleva@partner-lead-centre.ru",
            "full_name": "Ольга Ковалёва",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 1",
            "presence": "away",
            "is_online": true,
            "invite_pending": false,
            "last_seen_at": "2026-08-09T09:00:00Z"
      },
      {
            "id": "u-4",
            "email": "sergey.kim@partner-lead-centre.ru",
            "full_name": "Сергей Ким",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 2",
            "presence": null,
            "is_online": false,
            "invite_pending": false,
            "last_seen_at": null
      },
      {
            "id": "u-5",
            "email": "marina.egorova@partner-lead-centre.ru",
            "full_name": "Марина Егорова",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 2",
            "presence": "online",
            "is_online": true,
            "invite_pending": false,
            "last_seen_at": "2026-08-09T09:00:00Z"
      },
      {
            "id": "u-6",
            "email": "anna.solovyova@partner-lead-centre.ru",
            "full_name": "Анна Соловьёва",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 3",
            "presence": null,
            "is_online": false,
            "invite_pending": false,
            "last_seen_at": null
      },
      {
            "id": "u-7",
            "email": "dmitry.orlov@partner-lead-centre.ru",
            "full_name": "Дмитрий Орлов",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 3",
            "presence": "online",
            "is_online": true,
            "invite_pending": false,
            "last_seen_at": "2026-08-09T09:00:00Z"
      },
      {
            "id": "u-8",
            "email": "irina.polyakova@partner-lead-centre.ru",
            "full_name": "Ирина Полякова",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 4",
            "presence": null,
            "is_online": false,
            "invite_pending": false,
            "last_seen_at": null
      },
      {
            "id": "u-9",
            "email": "alexey.zhukov@partner-lead-centre.ru",
            "full_name": "Алексей Жуков",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 5",
            "presence": "online",
            "is_online": true,
            "invite_pending": true,
            "last_seen_at": "2026-08-09T09:00:00Z"
      },
      {
            "id": "u-10",
            "email": "natalya.guseva@partner-lead-centre.ru",
            "full_name": "Наталья Гусева",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 6",
            "presence": null,
            "is_online": false,
            "invite_pending": false,
            "last_seen_at": null
      },
      {
            "id": "u-11",
            "email": "viktor.lebedev@partner-lead-centre.ru",
            "full_name": "Виктор Лебедев",
            "role": "manager",
            "is_active": true,
            "handles_conversations": true,
            "department": "Дисп 7",
            "presence": "online",
            "is_online": true,
            "invite_pending": false,
            "last_seen_at": "2026-08-09T09:00:00Z"
      },
      {
            "id": "u-12",
            "email": "yulia.tarasova@partner-lead-centre.ru",
            "full_name": "Юлия Тарасова",
            "role": "head",
            "is_active": true,
            "handles_conversations": true,
            "department": "СТАРШИЕ - ЧАТЫ",
            "presence": null,
            "is_online": false,
            "invite_pending": false,
            "last_seen_at": null
      },
      {
            "id": "u-13",
            "email": "roman.belov@partner-lead-centre.ru",
            "full_name": "Роман Белов",
            "role": "observer",
            "is_active": true,
            "handles_conversations": false,
            "department": "ОКК",
            "presence": "online",
            "is_online": true,
            "invite_pending": false,
            "last_seen_at": "2026-08-09T09:00:00Z"
      }
],
    page: { limit: 25, offset: 0, total: 13 },
  },
  "/conversations": { items: [], page: { limit: 50, offset: 0, total: 0 } },
  "/inbox": { items: [], count: 0 },
  "/me/accounts": {
    items: [
      { id: "a-1", title: "Парт - 7 / Ист - В43 МНЧ", avito_user_id: 990100 },
      { id: "a-2", title: "Партнёр Лид Центр — Юго-Запад", avito_user_id: 990200 },
    ],
  },
  "/settings/work-hours": { start_hour: 10, end_hour: 20 },
  "/notifications": { items: [], unread: 0 },
};

function fixtureFor(pathname: string): unknown {
  const key = Object.keys(FIXTURES).find((k) => pathname.includes(k));
  return key ? FIXTURES[key] : {};
}

/**
 * СОСТОЯНИЕ СЕТИ — `?state=`.
 *
 * ЗАЧЕМ. Бриф аудита требует проверить на КАЖДОМ экране пустоту, первую
 * загрузку, ошибку сети, 401, 403, 500, таймаут и «много данных». На живой
 * системе такие состояния ловятся случайно: чтобы увидеть 500, надо дождаться
 * поломки. То есть проверка «а как это выглядит, когда всё плохо» никогда не
 * делается — и потому именно там копятся «Упс, что-то пошло не так» и пустые
 * экраны без объяснений.
 *
 * Стенд умеет показать любое из них по требованию. Это не подмена настоящих
 * проверок, а способ увидеть ветку кода, до которой иначе не добраться.
 *
 * Значения: ok (по умолчанию), empty, many, slow, timeout, offline,
 * error401, error403, error500.
 */
type NetState =
  | "ok"
  | "empty"
  | "many"
  | "slow"
  | "timeout"
  | "offline"
  | "error401"
  | "error403"
  | "error500";

/** Пустой ответ ПРАВИЛЬНОЙ формы: список пуст, но поля на месте. */
function emptied(body: unknown): unknown {
  if (Array.isArray(body)) return [];
  if (body && typeof body === "object") {
    const out: Record<string, unknown> = { ...(body as Record<string, unknown>) };
    for (const [k, v] of Object.entries(out)) {
      if (Array.isArray(v)) out[k] = [];
      if (k === "total" || k === "count" || k === "unread") out[k] = 0;
    }
    return out;
  }
  return body;
}

/** Размножение списков до 500 строк — проверка виртуализации и прокрутки. */
function multiplied(body: unknown): unknown {
  if (!body || typeof body !== "object") return body;
  const out: Record<string, unknown> = { ...(body as Record<string, unknown>) };
  for (const [k, v] of Object.entries(out)) {
    if (!Array.isArray(v) || v.length === 0) continue;
    const big: unknown[] = [];
    for (let i = 0; i < 500; i += 1) {
      const src = v[i % v.length] as Record<string, unknown>;
      big.push({ ...src, id: `${String(src.id ?? "row")}-${i}` });
    }
    out[k] = big;
    if ("total" in out) out.total = big.length;
  }
  return out;
}

function installFakeNetwork(state: NetState): void {
  const fail = (status: number) =>
    ({
      ok: false,
      status,
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => ({
        error: { code: status === 401 ? "unauthorized" : "internal_error", message: "" },
      }),
      text: async () => "",
    }) as unknown as Response;

  window.fetch = (async (input: RequestInfo | URL) => {
    const url = new URL(String(input), window.location.origin);

    // Сессию не ломаем даже в «плохих» состояниях: иначе стенд покажет экран
    // входа вместо разбираемого экрана, и проверять будет нечего.
    const isMe = url.pathname.includes("/auth/me");

    if (!isMe) {
      if (state === "offline") throw new TypeError("Failed to fetch");
      if (state === "timeout") await new Promise(() => {}); // навсегда — вид «висит»
      if (state === "slow") await new Promise((r) => setTimeout(r, 4000));
      if (state === "error401") return fail(401);
      if (state === "error403") return fail(403);
      if (state === "error500") return fail(500);
    }

    let body = fixtureFor(url.pathname);
    if (!isMe && state === "empty") body = emptied(body);
    if (!isMe && state === "many") body = multiplied(body);

    return {
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as typeof fetch;
}

/* ---------------------------------------------------------------- экраны --- */

const SCREENS: Record<string, () => Promise<{ default: () => JSX.Element }>> = {
  profile: async () => ({
    default: (await import("@/features/settings/profile/ProfilePage")).ProfilePage,
  }),
  accounts: async () => ({
    default: (await import("@/features/settings/accounts/AccountsPage")).AccountsPage,
  }),
  team: async () => ({
    default: (await import("@/features/settings/team/TeamPage")).TeamPage,
  }),
  distribution: async () => ({
    default: (await import("@/features/settings/distribution/DistributionTab"))
      .DistributionTab,
  }),
  // Список диалогов — главный экран продукта: на нём живут выбранная строка,
  // счётчик непрочитанных и полосы срочности, то есть всё, что трогает
  // перекраска акцента. Рендерим одну карточку напрямую: поднимать
  // виртуализированный список ради проверки цвета незачем.
  chats: async () => {
    const { ConversationListItem } = await import(
      "@/features/chats/components/list/ConversationListItem"
    );
    await import("@/features/chats/components/list/chat-list.css");
    await import("@/features/chats/components/list/wait-gauge.css");
    const now = 1_754_640_000_000;
    const base = {
      id: "c-1",
      status: "in_progress" as const,
      channel: "avito" as const,
      account: { id: "a-1", title: "Парт - 7 / Ист" },
      client: { id: "cl-1", name: "Иван Петров", phone: null, avito_rating: 4.9 },
      assignee: null,
      item: { title: "Ремонт стиральной машины Bosch", url: null, price: null },
      last_message: { body: "А сколько будет стоить замена насоса?", direction: "in" as const, created_at: "2026-08-08T09:40:12Z" },
      unread_count: 3,
      bot_active: false,
      tags: [] as string[],
      transferred_to_me: false,
      last_message_at: "2026-08-08T09:40:12Z",
    };
    const rows = [
      { row: base, active: true, подпись: "выбранный, 3 непрочитанных" },
      { row: { ...base, id: "c-2", unread_count: 0, client: { ...base.client, id: "cl-2", name: "Ольга Ковалёва" }, undelivered: true }, active: false, подпись: "ответ не ушёл" },
      { row: { ...base, id: "c-3", unread_count: 0, client: { ...base.client, id: "cl-3", name: "Сергей Ким" }, tags: ["негатив"] }, active: false, подпись: "негатив" },
      { row: { ...base, id: "c-4", unread_count: 0, client: { ...base.client, id: "cl-4", name: "Марина Егорова" }, pinned: true }, active: false, подпись: "закреплён" },
    ];
    /*
     * ⚠ СТЕНД НЕ ПЕРЕДАВАЛ `showChannel` — И ПОТОМУ НЕ ВИДЕЛ ГЛАВНОГО (25.08).
     *
     * В жизни это флаг «список не сужен до одного канала», то есть состояние по
     * умолчанию: диспетчер открывает Пульт и видит строки С НАЗВАНИЕМ КАНАЛА,
     * тремя строками. Здесь флаг не передавался вовсе, третья строка не
     * рисовалась, и прибор показывал двухстрочную карточку — ту, которой у
     * владельца на экране нет. Проверка вёрстки 23 августа прошла мимо наложения
     * строк именно поэтому: смотрели не на тот вариант.
     *
     * И вторая слепота: карточки лежали потоком, а в приложении их раскладывает
     * виртуализатор — АБСОЛЮТНО, шагом ровно ROW_HEIGHT. Поток прячет
     * переполнение (соседа отталкивает вниз), абсолютная раскладка — показывает
     * (сосед остаётся на месте, и содержимое ложится поверх). Повторяем раскладку
     * приложения, иначе прибор врёт в безопасную сторону.
     */
    const ROW_HEIGHT = 84;   // ChatListPane.ROW_HEIGHT
    return {
      default: () => (
        <div style={{ maxWidth: 420, background: "var(--lc-bg-panel)", padding: 8 }}>
          <div style={{ position: "relative", height: rows.length * ROW_HEIGHT }}>
            {rows.map((r, i) => (
              <div
                key={r.row.id}
                style={{ position: "absolute", top: 0, left: 0, width: "100%",
                         height: ROW_HEIGHT, transform: `translateY(${i * ROW_HEIGHT}px)` }}
              >
                <ConversationListItem
                  row={r.row as never}
                  active={r.active}
                  now={now}
                  showChannel
                  onOpen={() => {}}
                />
              </div>
            ))}
          </div>
        </div>
      ),
    };
  },
  // ЗДЕСЬ БЫЛ ЭКРАН `outcome` — окно «Чем закончилось обращение?» (пять кнопок
  // и сумма). Снято 12 августа решением владельца вместе с самим окном.
  templates: async () => {
    const { TemplatesManager } = await import("@/features/templates/TemplatesManager");
    return { default: () => <TemplatesManager /> };
  },
  /*
   * ЖИВАЯ ЛЕНТА. Экран, который нельзя посмотреть иначе: он наполняется
   * кадрами WebSocket, то есть на живом стенде показывает пустоту, пока в
   * системе что-нибудь не произойдёт. Здесь строки положены в хранилище
   * руками — ровно те, что бывают в жизни: клиент написал, ответ ушёл, ответ
   * не дошёл, обращение встало в очередь, канал отвалился, связь пропадала.
   *
   * Проверять тут надо ДВЕ вещи, невидимые в тестах (там `css: false`):
   * выравнивание трёх столбцов строки (время, событие, канал) на 1920 и на
   * 390, и различимость тонов — полоса слева у «не доставлено» обязана
   * читаться и в светлой теме, и в тёмной.
   */
  feed: async () => {
    const { FeedPage } = await import("@/features/feed/FeedPage");
    const { useFeedStore } = await import("@/features/feed/store");
    const at = (s: number) => new Date(Date.UTC(2026, 7, 12, 9, 14, s)).toISOString();
    useFeedStore.setState({
      nextSeq: 8,
      pausedAtSeq: null,
      filters: { accountId: null, group: null },
      channels: [
        { id: "a-1", title: "Парт - 7 / Ист" },
        { id: "a-2", title: "В43 МНЧ" },
      ],
      entries: [
        { seq: 7, at: at(58), group: "channels", tone: "bad", text: "Канал «В43 МНЧ» отвалился: нужен повторный вход, приём сообщений остановлен", accountId: "a-2", accountTitle: "В43 МНЧ", conversationId: null },
        { seq: 6, at: at(51), group: "delivery", tone: "bad", text: "Борис: ответ не доставлен — Авито вернул 429", accountId: "a-1", accountTitle: "Парт - 7 / Ист", conversationId: "c-2" },
        { seq: 5, at: at(44), group: "channels", tone: "warn", text: "Связь с сервером пропадала на 2 минуты — что было в это время, лента не знает", accountId: null, accountTitle: null, conversationId: null, gap: true },
        { seq: 4, at: at(30), group: "queue", tone: "good", text: "Наталья: диалог взял Иван Петров (ждал 12 секунд)", accountId: "a-1", accountTitle: "Парт - 7 / Ист", conversationId: "c-1" },
        { seq: 3, at: at(18), group: "queue", tone: "warn", text: "Наталья: обращение встало в очередь — ждёт, кто возьмёт", accountId: "a-1", accountTitle: "Парт - 7 / Ист", conversationId: "c-1" },
        { seq: 2, at: at(12), group: "messages", tone: "good", text: "Ольга Ковалёва: ответил бот", accountId: "a-2", accountTitle: "В43 МНЧ", conversationId: "c-3" },
        { seq: 1, at: at(4), group: "messages", tone: "neutral", text: "Наталья: новое сообщение", accountId: "a-1", accountTitle: "Парт - 7 / Ист", conversationId: "c-1" },
      ],
    });
    return { default: () => <FeedPage /> };
  },
  // Ряд действий в шапке ленты. Заведён, когда к пяти иконкам добавилась
  // шестая — «вернуть в очередь» (#33). Шапка узкая: рядом стоят имя клиента,
  // объявление и время ожидания, и на 390px именно этот ряд первым начинает
  // выдавливать соседей. Проверять такое на глаз нельзя.
  "thread-actions": async () => {
    const { ThreadActions } = await import(
      "@/features/chats/components/thread/ThreadActions"
    );
    await import("@/features/chats/components/thread/thread-actions.css");
    await import("@/features/chats/components/thread/chat-thread.css");
    const conv = {
      id: "c-1",
      status: "in_progress" as const,
      channel: "avito" as const,
      account: { id: "a-1", title: "Парт - 7 / Ист" },
      client: { id: "cl-1", name: "Иван Петров", phone: null, avito_rating: 4.9, blocked: false },
      assignee: { id: USER.id, full_name: USER.full_name },
      participants: [],
      item: { title: "Ремонт стиральной машины Bosch", url: null, price: "от 1500 ₽" },
      last_message: null,
      unread_count: 0,
      bot_active: false,
      tags: [] as string[],
      transferred_to_me: false,
      last_message_at: null,
      pinned: false,
    };
    return {
      // Разметка повторяет ChatThreadPane ДОСЛОВНО, включая имена классов.
      // Придуманные классы — это стенд, который врёт: ряд отрисуется без
      // раскладки, растянется во всю ширину и покажет запас там, где его нет.
      // Шапка ЦЕЛИКОМ, со всеми соседями ряда: кнопка «назад», аватар,
      // «Следующий (N)», «Закрыть» и «Клиент». Без них стенд показывал бы
      // ряду простор, которого в жизни нет, — а тесно здесь именно из-за
      // соседей.
      default: () => (
        <header className="thread-header">
          <Button
            className="thread-header__back"
            variant="subtle"
            size="compact-sm"
            aria-label="Назад к списку"
          >
            ‹
          </Button>
          <div className="client-avatar" style={{ width: 40, height: 40, flex: "none" }}>
            ИП
          </div>
          <div className="thread-header__info">
            <p className="thread-header__name lc-truncate">Иван Петров</p>
            <div className="thread-header__sub">
              <span className="lc-truncate">Парт - 7 / Ист</span>
              <a href="tel:+79001112241" className="thread-header__phone lc-num">
                +7 900 111-22-41
              </a>
              <span className="lc-truncate">Ольга Ковалёва</span>
              <span className="thread-header__wait" title="Клиент ждёт ответа">
                ждёт 12 мин
              </span>
            </div>
          </div>
          <Button size="compact-sm" variant="subtle">
            Следующий (7)
          </Button>
          <ThreadActions
            conversation={conv as never}
            onInvite={() => {}}
            onTransfer={() => {}}
            onBlock={() => {}}
          />
          <Button size="compact-sm">Закрыть</Button>
          <button type="button" className="thread-header__card-toggle" aria-label="Карточка клиента">
            Клиент
          </button>
        </header>
      ),
    };
  },
  /*
   * ГРАФИК СТАТИСТИКИ. Экран, поломку которого видно только глазами: подписи
   * оси налезали друг на друга («21:0017 авг., 18:0018 авг.» на скриншоте
   * владельца от 22.08), а тесты меряют расстояния и картинки не рисуют.
   *
   * Неделя по часам — 168 точек, самый плотный ряд, который вообще бывает:
   * почасовая группировка разрешена ровно до семи дней (06 §4.2). Если
   * подписи не налезают здесь, они не налезут нигде.
   *
   * Данные собираются формулой, а не берутся с сервера: стенду нужен
   * ХУДШИЙ случай, а не типичный, и суточная волна с длинными подписями
   * («16 сент., 06:00») задаётся тут точнее, чем ловится в бою.
   */
  chart: async () => {
    const { MetricChart } = await import("@/features/stats/components/MetricChart");
    await import("@/features/stats/stats.css");
    const points = Array.from({ length: 168 }, (_, i) => {
      const at = new Date(Date.UTC(2026, 7, 16, 0) + i * 3_600_000);
      const hour = at.getUTCHours();
      // Суточная волна: ночью пусто, днём пик — как в жизни.
      const value = hour < 6 ? 0 : Math.round(6 + 20 * Math.sin(((hour - 6) / 18) * Math.PI));
      return { ts: at.toISOString().slice(0, 19), value };
    });
    return {
      default: () => (
        <div style={{ padding: 16, background: "var(--lc-bg-page)" }}>
          <MetricChart
            data={{
              metric: "conversations_new",
              group: "hour",
              refreshed_at: "2026-08-22T23:00:00Z",
              points,
            }}
            isPending={false}
            isError={false}
            onRetry={() => {}}
            metric="conversations_new"
            onMetricChange={() => {}}
            group="hour"
            onGroupChange={() => {}}
            hourAllowed
            dayLabel="16 – 22 авг."
            onDayShift={() => {}}
            onWholeWeek={() => {}}
          />
        </div>
      ),
    };
  },
};

/* ------------------------------------------------------------------ стенд --- */

const params = new URLSearchParams(window.location.search);
const screen = params.get("screen") || "profile";
const scheme = params.get("theme") === "light" ? "light" : "dark";
/**
 * Ширина колонки содержимого меряется НЕ от края окна: слева стоят иконочный
 * рельс (58px) и меню разделов настроек (220px). При окне 1920 содержимому
 * достаётся 1642 — та самая ширина, на которой владелец намерил переполнение.
 * Без поправки стенд показал бы 1920 и поломку бы не поймал.
 *
 * `?w=` перекрывает значение, `?w=full` снимает ограничение.
 */
const CHROME_WIDTH = 58 + 220;
const widthParam = params.get("w");
const contentWidth =
  widthParam === "full" ? undefined : Number(widthParam) || window.innerWidth - CHROME_WIDTH;

const load = SCREENS[screen];
const Screen = load
  ? (await load()).default
  : () => (
      <div style={{ padding: 24, color: "var(--lc-text-1)" }}>
        <p>Неизвестный экран: {screen}</p>
        <p>Доступны: {Object.keys(SCREENS).join(", ")}</p>
      </div>
    );

installFakeNetwork((params.get("state") as NetState) || "ok");
useSessionStore.setState({
  user: USER,
  permissions: ALL_PERMISSIONS as never,
  accessToken: "dev-preview",
  bootstrapped: true,
});

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

function Stand() {
  // Схему ставим так же, как это делает приложение, — атрибутом на корне.
  useEffect(() => {
    document.documentElement.setAttribute("data-mantine-color-scheme", scheme);
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider
        theme={theme}
        forceColorScheme={scheme}
        cssVariablesResolver={cssVariablesResolver}
      >
        <Notifications position="top-right" autoClose={4000} limit={4} />
        <MemoryRouter initialEntries={[`/settings/${screen}`]}>
          {/* `settings-page`/`settings-content` — та же обёртка, что даёт
              настоящий раздел настроек: без неё меряли бы не то. */}
          <div className="settings-page">
            {/*
              `flex: none` — не украшение, без него ширина не держалась ВООБЩЕ.

              `.settings-content` объявлен `flex: 1`, а `flex-grow` бьёт
              заданную ширину: элемент растягивался на всё окно, сколько бы ни
              стояло в `?w=`. Стенд при этом молчал и показывал картинку —
              то есть на любой запрошенной ширине мерилась ширина окна.
              Прибор, который врёт, хуже отсутствующего: он выдаёт запас там,
              где его нет, и «проверено на 390px» означало «проверено на том,
              что было в окне».
            */}
            <div
              className="settings-content"
              id="stand"
              style={
                contentWidth === undefined
                  ? undefined
                  : // `flex: none` — ТОЛЬКО вместе с заданной шириной.
                    //
                    // Без ширины он превращает блок в «по содержимому», и
                    // таблица с `width: 100%` внутри упирается в круговое
                    // определение: при `?w=full` она раздувалась до 500000px,
                    // а страница молчала — `.settings-content` прячет
                    // переполнение, так что ни прокрутки, ни обрезанного края.
                    { width: contentWidth, flex: "none" }
              }
            >
              <Suspense fallback={<div style={{ padding: 24 }}>загрузка…</div>}>
                <Screen />
              </Suspense>
            </div>
          </div>
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Stand />
  </StrictMode>,
);
