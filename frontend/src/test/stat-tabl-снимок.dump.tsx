/**
 * НЕ СТОРОЖ. Снималка разметки пяти экранов для стенда адаптива:
 * «Статистика», «Разбор диалогов», «Живая лента», «Диалоги бота», «Уведомления».
 *
 * ⚠ ЗАЧЕМ. Проверить «ничего не съезжает и не обрезается» сторожами нельзя:
 * jsdom раскладку не считает вовсе. Все 334 проверки проекта утверждают про
 * НАМЕРЕНИЕ (какое свойство выставлено), а едет РЕЗУЛЬТАТ — ширина, перенос,
 * переполнение. Поэтому разметка снимается здесь, а меряется в браузере с
 * боевым CSS (см. .dump/stand-stat.html).
 *
 * ⚠ ЗНАЧЕНИЯ ДЛИННЫЕ НАРОЧНО. «Бригада Петра Иванова КП», «Васильцова 7000300»,
 * «Селезнёва-Горская Анастасия» — длиной с настоящие строки боя (сами имена
 * выдуманы). Стенд, посеянный короткими именами, покажет, что всё влезает, и
 * соврёт.
 *
 * Запуск руками:
 *   npx vitest run --config vitest.dump.config.ts --reporter=basic
 */
import { expect, it } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]);
// так же поступают соседние сторожа, читающие исходники.
import { writeFileSync } from "node:fs";
import { vi } from "vitest";
import { cleanup, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { StatsPage } from "@/features/stats/StatsPage";
import { TablePage } from "@/features/table/TablePage";
import { FeedPage } from "@/features/feed/FeedPage";
import { BotDialogsPage } from "@/features/bot-dialogs/BotDialogsPage";
import { NotificationsPage } from "@/features/notifications/NotificationsPage";
import { useFeedStore } from "@/features/feed/store";
import type { HeatmapCell } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const КАТАЛОГ = "./.dump";

/** Канал длиной с самое длинное настоящее название — им сеются все пять экранов. */
const ДЛИННЫЙ_КАНАЛ = "Бригада Петра Иванова КП";

const АККАУНТЫ = {
  items: [
    { id: "acc-1", title: ДЛИННЫЙ_КАНАЛ, source: "Б6", is_active: true, status: "ok" },
    { id: "acc-2", title: "Вячеслав БТ/МНЧ", source: "Б2", is_active: true, status: "ok" },
    { id: "acc-3", title: "Анатолий МНЧ", source: "Б1", is_active: true, status: "ok" },
  ],
  page: { limit: 50, offset: 0, total: 3 },
};

const СОТРУДНИКИ = {
  items: [
    { id: "u-1", full_name: "Селезнёва-Горская Анастасия", department: "ОКК", is_active: true },
    { id: "u-2", full_name: "Кузнецов Арсений (Чатер)", department: "ОКК", is_active: true },
    { id: "u-3", full_name: "Анна Смирнова", department: null, is_active: true },
  ],
};

/* ----------------------------------------------------------- статистика */

const SUMMARY = {
  period: { date_from: "2026-08-08", date_to: "2026-09-07", tz: "Europe/Moscow" },
  prev_period: { date_from: "2026-07-08", date_to: "2026-08-07" },
  refreshed_at: "2026-09-07T11:05:12Z",
  period_live: true,
  work_hours: { start_hour: 9, end_hour: 21 },
  cards: {
    conversations_new: { value: 3124, prev: 2801, delta_pct: 11.4 },
    conversations_closed: { value: 2903, prev: 3010, delta_pct: -3.7 },
    in_progress_now: { value: 147, prev: null, delta_pct: null },
    waiting_now: { value: 64, prev: null, delta_pct: null },
    queue_now: { value: 231, prev: null, delta_pct: null },
    frt_operator: {
      median_sec: 954,
      avg_sec: 3402,
      median_biz_sec: 881,
      avg_biz_sec: 2104,
      answered: 2604,
      unanswered: 520,
      prev_median_sec: 1203,
      delta_pct: -20.8,
    },
    frt_bot: { median_sec: 33, avg_sec: 47, answered: 2951 },
    bot_closed: { pct: 18.6, closed_by_bot: 541, closed_total: 2903, prev_pct: 15.2, delta_pct: 3.4 },
    phones_collected: { value: 784, by_source: { bot: 412, regex: 305, manual: 67 }, prev: 651, delta_pct: 20 },
    repeat_contacts: { reopened: 253, repeat_clients: 194, prev_reopened: 217 },
  },
};

/** 30 дней по дням — обычный вид графика; подписи оси самые длинные («16 сент.»). */
const ТОЧКИ = Array.from({ length: 30 }, (_, i) => ({
  ts: `2026-08-${String(9 + i).padStart(2, "0")}`.replace("2026-08-3", "2026-09-0"),
  value: 40 + ((i * 37) % 120),
}));

const HEAT_CELLS: HeatmapCell[] = [];
for (let dow = 1; dow <= 7; dow += 1) {
  for (let hour = 0; hour < 24; hour += 1) HEAT_CELLS.push({ dow, hour, value: (hour * dow) % 40 });
}

const MANAGERS = {
  period: { date_from: "2026-08-08", date_to: "2026-09-07" },
  refreshed_at: "2026-09-07T11:05:12Z",
  // Подпись источника рядом с заголовком (09.09) — на стенде она обязана быть
  // видна, иначе сверка раскладки её не меряет вовсе.
  period_live: true,
  rows: [
    // Фамилия длинная нарочно: на 900 и уже строка становится карточкой, и
    // именно длинное неразрывное слово распирало её изнутри.
    { manager_id: "u-1", full_name: "Селезнёва-Горская Анастасия", is_active: true, taken: 341, answered: 318, closed: 289, frt_avg_sec: 2103, frt_median_sec: 744, frt_median_biz_sec: 712, messages_sent: 4127 },
    { manager_id: "u-2", full_name: "Кузнецов Арсений (Чатер)", is_active: true, taken: 274, answered: 261, closed: 233, frt_avg_sec: 1210, frt_median_sec: 1004, frt_median_biz_sec: 902, messages_sent: 3441 },
    { manager_id: "u-3", full_name: "Анна Смирнова", is_active: false, taken: 44, answered: 33, closed: 22, frt_avg_sec: null, frt_median_sec: null, frt_median_biz_sec: null, messages_sent: 128 },
    { manager_id: "u-4", full_name: "Григорий Воронцов", is_active: true, taken: 190, answered: 177, closed: 160, frt_avg_sec: 880, frt_median_sec: 640, frt_median_biz_sec: 601, messages_sent: 2210 },
    { manager_id: "u-5", full_name: "Светлана Морозова", is_active: true, taken: 121, answered: 111, closed: 98, frt_avg_sec: 1440, frt_median_sec: 980, frt_median_biz_sec: 900, messages_sent: 1502 },
  ],
  totals: { taken: 970, answered: 900, closed: 802, frt_median_sec: 954, frt_median_biz_sec: 881, messages_sent: 11408 },
};

/* ------------------------------------------------------ разбор диалогов */

const ТАБЛИЦА_СТРОКИ = [
  {
    id: "c-1",
    status: "in_progress",
    client_name: "Васильцова 7000300",
    client_phone: "+7 908 123-45-08",
    assignee_id: "u-1",
    assignee_name: "Селезнёва-Горская Анастасия",
    assignee_department: "ОКК",
    item_title: "Телемастер на дом / Ремонт телевизоров / Все бренды / ЖК LED oled / Выезд в день обращения",
    account_id: "acc-1",
    account_title: ДЛИННЫЙ_КАНАЛ,
    tags: ["срочно", "повторный", "ночной-лид"],
    bot_active: false,
    unread_count: 0,
    last_message_at: "2026-09-07T14:24:00Z",
    messages_count: 34,
    first_response_sec: 41 * 60,
    duration_sec: 52 * 60,
  },
  {
    id: "c-2",
    status: "new",
    client_name: "Григорий Воронцов",
    client_phone: null,
    assignee_id: null,
    assignee_name: null,
    item_title: null,
    account_id: "acc-2",
    account_title: "Вячеслав БТ/МНЧ",
    tags: ["ночной-лид"],
    bot_active: true,
    unread_count: 1,
    last_message_at: "2026-09-07T14:20:00Z",
    messages_count: 1,
    first_response_sec: null,
    duration_sec: 0,
  },
  {
    id: "c-3",
    status: "closed",
    client_name: "Светлана Морозова",
    client_phone: "+7 900 111-22-51",
    assignee_id: "u-2",
    assignee_name: "Кузнецов Арсений (Чатер)",
    assignee_department: "ОКК",
    item_title: "Ремонт стиральных машин Indesit / Выезд мастера сегодня",
    account_id: "acc-1",
    account_title: ДЛИННЫЙ_КАНАЛ,
    tags: [],
    bot_active: false,
    unread_count: 0,
    last_message_at: "2026-09-06T09:11:00Z",
    messages_count: 12,
    first_response_sec: 95,
    duration_sec: 3600 * 3,
  },
  {
    id: "c-4",
    status: "waiting",
    client_name: "Анатолий МНЧ",
    client_phone: "+7 900 000-00-00",
    assignee_id: "u-3",
    assignee_name: "Анна Смирнова",
    assignee_department: null,
    item_title: "Медиаприставки и планшеты — диагностика",
    account_id: "acc-3",
    account_title: "Анатолий МНЧ",
    tags: ["негатив"],
    bot_active: true,
    unread_count: 7,
    last_message_at: "2026-09-07T13:02:00Z",
    messages_count: 8,
    first_response_sec: 12,
    duration_sec: 640,
  },
];

/* --------------------------------------------------------- диалоги бота */

const БОТ = {
  items: [
    { id: "b-1", status: "in_progress", client_name: "Васильцова 7000300", account_title: ДЛИННЫЙ_КАНАЛ, assignee_name: "Селезнёва-Горская Анастасия", messages_count: 14, last_message_at: "2026-09-07T13:00:00Z", first_response_sec: 4200, outcome: { group: "loop", label: "сработала защита от зацикливания" } },
    { id: "b-2", status: "new", client_name: "Григорий Воронцов", account_title: "Вячеслав БТ/МНЧ", assignee_name: null, messages_count: 3, last_message_at: "2026-09-07T12:40:00Z", first_response_sec: null, outcome: { group: "refuse", label: "бот отказал по регламенту, человек не подключился" } },
    { id: "b-3", status: "closed", client_name: "Светлана Морозова", account_title: "Анатолий МНЧ", assignee_name: "Кузнецов Арсений (Чатер)", messages_count: 22, last_message_at: "2026-09-06T18:20:00Z", first_response_sec: 60, outcome: { group: null, label: "бот закрыл сам" } },
  ],
  page: { limit: 50, offset: 0, total: 3 },
  counters: [
    { group: "loop", label: "Зациклился на одном вопросе", count: 41, kind: "failure" },
    { group: "refuse", label: "Отказал и не передал человеку", count: 12, kind: "failure" },
    { group: "stuck", label: "Завис и замолчал", count: 7, kind: "failure" },
    { group: "wrong_diag", label: "Поставил диагноз по фотографии", count: 3, kind: "failure" },
    { group: "unsure", label: "Не смог сам — позвал человека", count: 210, kind: "capacity" },
  ],
};

const БОТ_ЖИВОЕ = {
  count: 3,
  items: [
    { id: "l-1", client_name: "Васильцова 7000300", account_title: ДЛИННЫЙ_КАНАЛ, since: "2026-09-07T14:00:00Z", last_message_at: "2026-09-07T14:20:00Z", bot_replies: 4, last_bot_text: "Подскажите, куда к вам подъехать (квартира, подъезд, этаж) и ваш номер телефона, запишу вас" },
    { id: "l-2", client_name: "Григорий Воронцов", account_title: "Вячеслав БТ/МНЧ", since: "2026-09-07T14:10:00Z", last_message_at: "2026-09-07T14:22:00Z", bot_replies: 2, last_bot_text: "Замена подсветки у нас идёт от 1000 рублей, точнее мастер сориентирует на месте" },
    { id: "l-3", client_name: "Светлана Морозова", account_title: "Анатолий МНЧ", since: "2026-09-07T14:18:00Z", last_message_at: "2026-09-07T14:23:00Z", bot_replies: 1, last_bot_text: "Здравствуйте! Расскажите, что случилось с техникой?" },
  ],
};

/* ----------------------------------------------------------- уведомления */

const УВЕДОМЛЕНИЯ = {
  items: [
    { id: "n-1", kind: "webhook.lost", severity: "critical", title: "Канал отобрали: подписка на события пропала", body: `Канал «${ДЛИННЫЙ_КАНАЛ}» перестал получать вебхуки Авито — сообщения клиентов в LeadChat не приходят с 14:02`, entity: { kind: "account", id: "acc-1", title: ДЛИННЫЙ_КАНАЛ }, action: { code: "reconnect", label: "Переподключить" }, repeat_count: 14, is_read: false, created_at: "2026-09-07T11:02:00Z", last_seen_at: "2026-09-07T14:02:00Z" },
    { id: "n-2", kind: "inbound.stalled", severity: "critical", title: "Приём сообщений остановился", body: "За последние 30 минут не пришло ни одного входящего ни по одному каналу", entity: null, action: null, repeat_count: 112, is_read: false, created_at: "2026-09-07T10:00:00Z", last_seen_at: "2026-09-07T13:58:00Z" },
    { id: "n-3", kind: "conversation.no_reply", severity: "warning", title: "Диалог без ответа больше часа", body: `Клиент «Васильцова 7000300» в канале «${ДЛИННЫЙ_КАНАЛ}» ждёт ответа 1 ч 12 мин`, entity: { kind: "conversation", id: "c-1", title: "Васильцова 7000300" }, action: { code: "open", label: "Открыть диалог", to: "/chats/c-1" }, repeat_count: 1, is_read: false, created_at: "2026-09-07T12:50:00Z", last_seen_at: "2026-09-07T12:50:00Z" },
    { id: "n-4", kind: "support.password_reset", severity: "info", title: "Сотрудник просит новый пароль", body: "Селезнёва-Горская Анастасия не может войти: ссылка установки пароля истекла", entity: { kind: "user", id: "u-1", title: "Селезнёва-Горская Анастасия" }, action: { code: "invite", label: "Выдать ссылку" }, repeat_count: 1, is_read: true, created_at: "2026-09-07T09:10:00Z", last_seen_at: "2026-09-07T09:10:00Z" },
    { id: "n-5", kind: "message.undelivered", severity: "warning", title: "Ответ не дошёл до клиента", body: "Авито отклонило отправку: «u2i chat is blocked by the recipient»", entity: { kind: "conversation", id: "c-3", title: "Светлана Морозова" }, action: { code: "open", label: "Открыть диалог", to: "/chats/c-3" }, repeat_count: 3, is_read: true, created_at: "2026-09-06T18:00:00Z", last_seen_at: "2026-09-06T19:30:00Z" },
  ],
  page: { limit: 50, offset: 0, total: 5 },
  unread: 3,
};

/* --------------------------------------------------------------- лента */

const ЛЕНТА = [
  { seq: 12, at: "2026-09-07T14:23:11Z", group: "messages", tone: "neutral", who: "Васильцова 7000300", text: "новое сообщение", accountId: "acc-1", accountTitle: ДЛИННЫЙ_КАНАЛ, conversationId: "c-1" },
  { seq: 11, at: "2026-09-07T14:22:48Z", group: "delivery", tone: "bad", who: "Светлана Морозова", text: "ответ не доставлен: чат заблокирован получателем", accountId: "acc-1", accountTitle: ДЛИННЫЙ_КАНАЛ, conversationId: "c-3" },
  { seq: 10, at: "2026-09-07T14:22:03Z", group: "queue", tone: "warn", who: "Григорий Воронцов", text: "встал в очередь и ждёт уже 12 мин — никто не принял", accountId: "acc-2", accountTitle: "Вячеслав БТ/МНЧ", conversationId: "c-2" },
  { seq: 9, at: "2026-09-07T14:21:40Z", group: "dialogs", tone: "good", who: "Анатолий МНЧ", text: "принял Кузнецов Арсений (Чатер) через 12 с", accountId: "acc-3", accountTitle: "Анатолий МНЧ", conversationId: "c-4" },
  { seq: 8, at: "2026-09-07T14:20:00Z", group: "channels", tone: "bad", who: null, text: `канал «${ДЛИННЫЙ_КАНАЛ}» перестал получать события: подписка на вебхуки пропала`, accountId: "acc-1", accountTitle: ДЛИННЫЙ_КАНАЛ, conversationId: null },
  { seq: 7, at: "2026-09-07T14:18:20Z", group: "people", tone: "neutral", who: null, text: "Селезнёва-Горская Анастасия отошла — статус «не на месте»", accountId: null, accountTitle: null, conversationId: null },
  { seq: 6, at: "2026-09-07T14:17:00Z", group: "channels", tone: "warn", who: null, text: "связь с сервером обрывалась 1 мин 40 с — часть событий в ленту не попала", accountId: null, accountTitle: null, conversationId: null, gap: true },
  { seq: 5, at: "2026-09-07T14:15:12Z", group: "messages", tone: "good", who: "Васильцова 7000300", text: "ответил бот", accountId: "acc-1", accountTitle: ДЛИННЫЙ_КАНАЛ, conversationId: "c-1" },
];

/* ------------------------------------------------------------- снималка */

function сеть() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const p = url.pathname;
    if (p.endsWith("/stats/summary")) return jsonResponse(200, SUMMARY);
    if (p.endsWith("/stats/timeseries"))
      return jsonResponse(200, { metric: "conversations_new", group: "day", refreshed_at: SUMMARY.refreshed_at, points: ТОЧКИ });
    if (p.endsWith("/stats/heatmap")) return jsonResponse(200, { tz: "Europe/Moscow", metric: "messages_in", cells: HEAT_CELLS });
    if (p.endsWith("/stats/managers")) return jsonResponse(200, MANAGERS);
    if (p.endsWith("/users/assignable")) return jsonResponse(200, СОТРУДНИКИ);
    if (p.endsWith("/avito-accounts")) return jsonResponse(200, АККАУНТЫ);
    if (p.endsWith("/conversations/table")) return jsonResponse(200, { items: ТАБЛИЦА_СТРОКИ, page: { limit: 50, offset: 0, total: 4 } });
    if (p.endsWith("/bot-dialogs/live")) return jsonResponse(200, БОТ_ЖИВОЕ);
    if (p.endsWith("/bot-dialogs")) return jsonResponse(200, БОТ);
    if (p.endsWith("/notifications")) return jsonResponse(200, УВЕДОМЛЕНИЯ);
    if (p.endsWith("/notifications/unread-count")) return jsonResponse(200, { unread: 3, critical: 2, warning: 1, info: 0 });
    return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
  });
}

function начать() {
  queryClient.clear();
  resetSessionStore({
    user: { ...fakeUser, role: "head" },
    permissions: [
      "conversations:read",
      "conversations:manage",
      "stats:all",
      "accounts:read",
      "accounts:manage",
      "users:manage",
      "audit:read",
      "settings:manage",
    ] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  vi.stubGlobal("fetch", сеть());
}

async function снять(имя: string, узел: HTMLElement, признак: string) {
  await waitFor(() => {
    if (!узел.querySelector(признак)) throw new Error(`нет ${признак}`);
  }, { timeout: 5000 });
  writeFileSync(`${КАТАЛОГ}/${имя}.html`, узел.innerHTML, "utf8");
  // Снималка обязана падать на пустоте: пустой файл на стенде выглядел бы как
  // «всё влезает», то есть врал бы в самую удобную сторону.
  expect(узел.innerHTML.length).toBeGreaterThan(2000);
}

it("снимает разметку «Статистики»", async () => {
  начать();
  const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
  await снять("stat-tabl-stats", container, ".managers__row");
  vi.unstubAllGlobals();
});

/**
 * ⚠ ГРАФИК СНИМАЕТСЯ ЕЩЁ РАЗ НА КАЖДУЮ ШИРИНУ, И ЭТО НЕ ДУБЛЬ (08.09).
 *
 * Система координат графика считается по фактической ширине блока
 * (`useInlineSize` в MetricChart), а jsdom раскладку не считает и отдаёт ноль —
 * снимок выше уходит на стенд с запасным `viewBox="0 0 720 240"`. Стенд,
 * собранный только из него, показал бы прежнюю растянутую картинку и объявил
 * бы правку несделанной.
 *
 * Числа — не выдумка: это ширины `.stats-chart__plot`, ЗАМЕРЕННЫЕ на стенде
 * для окон 1024…2560 при обеих рельсах (218 — умолчание, 72 — свёрнутая).
 * Снимок под каждую ширину стенд кладёт в окно того же размера с той же
 * рельсой — и меряет уже настоящий конец пути, а не намерение.
 */
const ШИРИНЫ_ГРАФИКА: Array<[метка: string, блок: number]> = [
  // рельса развёрнута (218) — умолчание, вся матрица
  ["218-1024", 724], ["218-1180", 880], ["218-1181", 482], ["218-1280", 539],
  ["218-1366", 589], ["218-1440", 631], ["218-1600", 723], ["218-1920", 907],
  ["218-2560", 1275],
  // рельса свёрнута (72) — края матрицы и самый узкий двухколоночный случай
  ["72-1024", 870], ["72-1181", 566], ["72-2560", 1359],
];

const роднойRect = Element.prototype.getBoundingClientRect;

it("снимает график на каждой ширине матрицы", async () => {
  for (const [метка, блок] of ШИРИНЫ_ГРАФИКА) {
    начать();
    Element.prototype.getBoundingClientRect = function () {
      return { x: 0, y: 0, top: 0, left: 0, right: блок, bottom: 0, width: блок, height: 0 } as DOMRect;
    };
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await снять(`stat-grafik-${метка}`, container, ".managers__row");
    Element.prototype.getBoundingClientRect = роднойRect;
    vi.unstubAllGlobals();
    cleanup();
  }
});

it("снимает разметку «Разбора диалогов»", async () => {
  начать();
  const { container } = renderWithProviders(<TablePage />, { route: "/dialogs" });
  await снять("stat-tabl-table", container, ".dt__row");
  vi.unstubAllGlobals();
});

it("снимает разметку «Живой ленты»", async () => {
  начать();
  useFeedStore.setState({
    entries: ЛЕНТА as never,
    channels: [
      { id: "acc-1", title: ДЛИННЫЙ_КАНАЛ },
      { id: "acc-2", title: "Вячеслав БТ/МНЧ" },
      { id: "acc-3", title: "Анатолий МНЧ" },
    ],
    nextSeq: 13,
    filters: { accountId: null, group: null },
    pausedAtSeq: null,
  } as never);
  const { container } = renderWithProviders(<FeedPage />, { route: "/feed" });
  await снять("stat-tabl-feed", container, ".lc-feed__item");
  vi.unstubAllGlobals();
});

it("снимает разметку «Диалогов бота»", async () => {
  начать();
  const { container } = renderWithProviders(<BotDialogsPage />, { route: "/bot-dialogs" });
  await снять("stat-tabl-bot", container, ".bot-row");
  vi.unstubAllGlobals();
});

it("снимает разметку «Уведомлений»", async () => {
  начать();
  const { container } = renderWithProviders(<NotificationsPage />, { route: "/notifications" });
  await снять("stat-tabl-notify", container, ".lc-journal__row");
  vi.unstubAllGlobals();
});
