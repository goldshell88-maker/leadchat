/**
 * НЕ СТОРОЖ. Снималка разметки раздела «Настройки» для стенда адаптива.
 *
 * Довод тот же, что у `__снимок-разметки.dump.tsx`: jsdom раскладку не считает
 * вовсе, а все 334 сторожа проекта утверждают про НАМЕРЕНИЕ (какое свойство
 * выставлено), тогда как едет РЕЗУЛЬТАТ. Поэтому разметку снимаем React'ом,
 * а меряем её в настоящем браузере с настоящим (собранным) CSS.
 *
 * ⚠ КАЖДЫЙ ЭКРАН СНИМАЕТСЯ ВНУТРИ НАСТОЯЩЕГО КАРКАСА `SettingsLayout`.
 * Без него в снимке не оказалось бы колонки разделов на 220px, а именно она
 * съедает ширину у содержимого: медиазапросы в CSS считают от ОКНА, а
 * содержимому достаётся окно минус рельс минус эта колонка.
 *
 * ⚠ РАЗВИЛКА `useMediaQuery` СНИМАЕТСЯ В ОБОИХ СОСТОЯНИЯХ. В
 * `TeamMembersTab` справка о ролях ниже 1560 рисуется свёрнутой
 * (`<details>`), выше — колонкой сбоку (`<aside>`). Тег выбирает React, и в
 * одном снимке живёт только одна ветка: снимаем обе, иначе половина
 * диапазона осталась бы непроверенной.
 *
 * Запуск руками:
 *   npx vitest run --config vitest.dump.config.ts --reporter=basic
 */
import { expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// так же поступают соседние сторожа, читающие исходники.
import { writeFileSync } from "node:fs";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { SettingsLayout } from "@/features/settings/SettingsLayout";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { OperatorsGridPage } from "@/features/settings/accounts/OperatorsGridPage";
import { TemplatesPage } from "@/features/settings/templates/TemplatesPage";
import { BotsPage } from "@/features/settings/bots/BotsPage";
import { LeadbotTab } from "@/features/settings/leadbot/LeadbotTab";
import { LeadsTab } from "@/features/settings/leads/LeadsTab";
import { DistributionTab } from "@/features/settings/distribution/DistributionTab";
import { TeamPage } from "@/features/settings/team/TeamPage";
import { ProfilePage } from "@/features/settings/profile/ProfilePage";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const КАТАЛОГ = "./.dump";
const DAY = 86_400_000;

/** Все права: снимаем самый населённый вид раздела, а не урезанный ролью. */
const ВСЕ_ПРАВА = [
  "conversations:read", "messages:send", "conversations:manage",
  "notes:read", "notes:write", "templates:own", "templates:shared",
  "stats:own", "stats:all", "accounts:read", "accounts:manage",
  "bots:manage", "settings:manage", "users:manage", "audit:read",
];

/**
 * Ширина окна для `useMediaQuery`. jsdom медиазапросы не считает — разметка
 * решается ими, и без подмены обе ветки дают одну и ту же (умолчание).
 */
function ширинаОкна(width: number) {
  vi.stubGlobal("matchMedia", (query: string) => {
    const max = Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? Infinity);
    const min = Number(/min-width:\s*(\d+)px/.exec(query)?.[1] ?? 0);
    const matches = width <= max && width >= min;
    return {
      matches,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    };
  });
}

/**
 * Ширина ЛЮБОГО блока для замера через `getBoundingClientRect`.
 *
 * jsdom раскладку не считает вовсе и отдаёт нули на любой блок, а справка о
 * ролях выбирает тег по ширине СВОЕЙ сетки (`useInlineSize`). Нули оставляют
 * снимок в одной ветке из двух, поэтому ширину подставляем руками. Остальные
 * поля прямоугольника — те же нули, что и у jsdom: подменяется ровно то, что
 * решает дело.
 */
const роднойRect = Element.prototype.getBoundingClientRect;

function ширинаБлока(width: number | null) {
  if (width === null) {
    Element.prototype.getBoundingClientRect = роднойRect;
    return;
  }
  Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0, width, height: 0 } as DOMRect;
  };
}

/*
 * ⚠ ДЛИННЫЕ ЗНАЧЕНИЯ ВЗЯТЫ НАРОЧНО — это смысл всего набора. «Бригада Андрея
 * Владиславовича КП · Б6» — длиной с настоящее название отдела (37 знаков,
 * самое длинное живое), почта партнёрского домена — 43 знака. Имена людей и
 * отделов выдуманы, длины — боевые. Стенд, посеянный короткими именами,
 * покажет, что всё влезает, и соврёт.
 */
const ЛЮДИ = [
  { id: "u-1", full_name: "Константинопольский Вячеслав", email: "konstantinopolskiy@partner-lead-centre.ru", role: "admin", department: "Бригада Андрея Владиславовича КП · Б6", color: "#22c55e", is_online: true },
  { id: "u-2", full_name: "Анна Смирнова", email: "anna.smirnova@partner-lead-centre.ru", role: "head", department: "Диспетчерская МНЧ · смена 2", color: "#3b82f6", is_online: false },
  { id: "u-3", full_name: "Кузнецов Арсений (Чатер)", email: "kuznetsov.arseniy.chater@partner-lead-centre.ru", role: "manager", department: "Бригада Петра Иванова КП", color: "#a855f7", is_online: true },
  { id: "u-4", full_name: "Дмитрий Соколов", email: "d.sokolov@partner-lead-centre.ru", role: "manager", department: null, color: null, is_online: false },
  { id: "u-5", full_name: "Марина Егорова", email: "m.egorova@partner-lead-centre.ru", role: "observer", department: "Наблюдение · Санкт-Петербург", color: "#f97316", is_online: false },
  { id: "u-6", full_name: "Григорий Воронцов", email: "vorontsov.grigoriy@partner-lead-centre.ru", role: "manager", department: "Бригада Артура БТ/МНЧ", color: "#14b8a6", is_online: true },
];

function человек(i: number) {
  const ч = ЛЮДИ[i];
  return {
    ...ч,
    is_active: true,
    invite_pending: i === 4,
    handles_conversations: ч.role !== "observer",
    presence: i === 1 ? "away" : "active",
    created_at: "2026-08-01T08:00:00Z",
  };
}

const КОМАНДА = ЛЮДИ.map((_, i) => человек(i));

const КАНАЛЫ = [
  { title: "Бригада Петра Иванова КП", origin: "В43" },
  { title: "Вячеслав БТ/МНЧ · Санкт-Петербург", origin: "В65" },
  { title: "Анатолий МНЧ", origin: null },
  { title: "Марк БТ/МНЧ", origin: "В95" },
  { title: "Никита КП", origin: "В12" },
];

function канал(i: number) {
  const к = КАНАЛЫ[i];
  const now = Date.now();
  return {
    id: `acc-${i + 1}`,
    title: к.title,
    avito_user_id: 100200300 + i,
    status: i === 4 ? "disabled" : "active",
    disabled_reason: i === 4 ? "Отключён администратором 06.09: закончилась подписка Авито у партнёра" : null,
    own_keys: i === 0,
    lead_origin: к.origin,
    lead_partner_number: k(i),
    lead_src_key: i === 0 ? "kp" : null,
    review_url: null,
    is_service: false,
    token_expires_at: new Date(now + (i === 1 ? 2 : 30) * DAY).toISOString(),
    created_at: new Date(now - 40 * DAY).toISOString(),
    token: i === 1
      ? { state: "warning", message: "Токен истекает через 2 дня — нужно переподключить канал", last_refresh_at: null, action: "reconnect" }
      : { state: "ok", message: "Токен активен", last_refresh_at: new Date(now - 3600_000).toISOString(), action: null },
    webhook: i === 2
      ? { status: "stale", url: "https://leadchat.partner-lead-centre.ru/api/v1/webhooks/avito", last_event_at: new Date(now - 7 * 3600_000).toISOString(), state: "warning", message: "События не приходят больше 6 часов — проверьте подписку", action: "resubscribe" }
      : { status: "ok", url: "https://leadchat.partner-lead-centre.ru/api/v1/webhooks/avito", last_event_at: new Date(now - 60_000).toISOString(), state: "ok", message: "События приходят", action: null },
    backfill: { status: "idle" },
    operators: { count: i === 3 ? 0 : 4, preview: i === 3 ? [] : [{ id: "u-1", full_name: "Константинопольский Вячеслав" }, { id: "u-3", full_name: "Кузнецов Арсений (Чатер)" }] },
    stats: { total: 1512 + i * 37, missed: 61 + i, daily: [104, 220, 318, 407, 512, 66, 78], stale_minutes: 60 },
  };
}

function k(i: number): string | null {
  return i % 2 === 0 ? "7788" : null;
}

const ШАБЛОНЫ = [
  { id: "t-1", owner_id: null, title: "Приветствие и уточнение модели техники", body: "Здравствуйте! Уточните, пожалуйста, модель техники и что именно случилось — подскажу стоимость выезда", folder: "Первый контакт" },
  { id: "t-2", owner_id: null, title: "Цена выезда мастера по телевизорам", body: "Выезд мастера 500 ₽, диагностика бесплатно при согласии на ремонт. Замена подсветки от 1000 рублей", folder: "Цены" },
  { id: "t-3", owner_id: null, title: "Запись на визит", body: "Подскажите, куда к вам подъехать (квартира, подъезд, этаж) и ваш номер телефона, запишу вас", folder: "Первый контакт" },
  { id: "t-4", owner_id: fakeUser.id, title: "Мой ответ про запчасти", body: "Точнее по запчастям смогу сориентировать на месте", folder: null },
];

const БОТЫ = {
  items: [
    { id: "bot-1", name: "Первичный приём обращений с Авито", is_enabled: true, accounts: [{ id: "acc-1", title: "Бригада Петра Иванова КП" }, { id: "acc-2", title: "Вячеслав БТ/МНЧ · Санкт-Петербург" }], schedule: null, dialogs_7d: 154, updated_at: "2026-09-06T10:00:00Z" },
    { id: "bot-2", name: "Ночной дежурный", is_enabled: false, accounts: [{ id: "acc-3", title: "Анатолий МНЧ" }], schedule: { days: [1, 2, 3, 4, 5, 6, 7], start: "20:00", end: "10:00", tz: "Europe/Moscow" }, dialogs_7d: 12, updated_at: "2026-09-05T10:00:00Z" },
  ],
  page: { limit: 50, offset: 0, total: 2 },
};

const ЛИДБОТ = {
  connection: { url: "http://10.10.0.2:8790", token_set: true, source: "db" },
  is_ready: true,
  enabled: true,
  mode: "suggest",
  context_messages: 30,
  account_ids: ["acc-1"],
  accounts: КАНАЛЫ.map((к, i) => ({ id: `acc-${i + 1}`, title: к.title, lead_origin: к.origin, busy_with_other_bot: i === 1 })),
};

const ЛИДБОТ_ЗВОНКИ = {
  items: [
    { id: "c1", at: "2026-09-07T16:26:00Z", conversation_id: "7f3a19c2-1111-2222-3333-444455556666", account_id: "acc-1", request_id: "900123", question: "Вы мне скажите, сколько будет стоить, а то мне тут сказали 10000?", reply: "Замена подсветки у меня идёт от 1000 рублей, точнее по запчастям смогу вас на месте сориентировать", layer: "model", flag: null, confidence: 0.82, needs_operator: false, outcome: "answered", outcome_label: "Ответил клиенту", escalation: null, lead_ready: true, warnings: [], ms: 2140, error: null },
    { id: "c2", at: "2026-09-07T16:20:00Z", conversation_id: "8a4b20d3-2222-3333-4444-555566667777", account_id: "acc-2", request_id: null, question: "Нам удобней самим привезти телевизор", reply: null, layer: null, flag: "timeout", confidence: null, needs_operator: true, outcome: "failed", outcome_label: "Передал человеку", escalation: { reason: "ai_unsure", label: "AI не уверен в ответе", deadline_min: 15 }, lead_ready: false, warnings: ["Таймаут ожидания ответа лид-бота: сервер 10.10.0.2:8790 не ответил за 30 секунд"], ms: 30_000, error: "Таймаут ожидания ответа лид-бота: сервер 10.10.0.2:8790 не ответил за 30 секунд" },
  ],
};

const ЛИДБОТ_МОЛЧАНИЕ = {
  channels: КАНАЛЫ.map((к, i) => ({ id: `acc-${i + 1}`, title: к.title, bot_attached: i < 2, status: "active" })),
  in_progress: [
    { conversation_id: "7f3a19c2-1111-2222-3333-444455556666", status: "Ждёт ответа клиента, третья реплика", last_message_at: "2026-09-07T16:26:00Z" },
  ],
  not_taken: [
    { conversation_id: "8a4b20d3-2222-3333-4444-555566667777", status: "in_progress", reason: "operator_answered", reason_label: "В диалоге уже отвечал человек — бот не входит", last_message_at: "2026-09-07T15:10:00Z" },
    { conversation_id: "9b5c31e4-3333-4444-5555-666677778888", status: "new", reason: "channel_without_bot", reason_label: "Канал «Марк БТ/МНЧ» не подключён к лид-боту", last_message_at: "2026-09-07T14:02:00Z" },
  ],
};

const ЛИДЫ_СОСТОЯНИЕ = {
  configured: true,
  stats: { total: 412, unacked: 7, created: 388, held: 18, failed: 6 },
};

const ЛИДЫ_ВЫДАЧИ = {
  items: [
    { conversation_id: "conv-1", handed_at: "2026-09-07T16:30:00Z", src_key: "kp", src_label: "Лид-центр КП (Комплексные Партнёры)", acked: true, decision: "visit", decision_label: "Выезд мастера", created: true, request_id: "900123", message: null },
    { conversation_id: "conv-2", handed_at: "2026-09-07T15:10:00Z", src_key: "bt", src_label: "Лид-центр БТ/МНЧ", acked: false, decision: "visit", decision_label: "Выезд мастера", created: false, request_id: null, message: "CRM не отдала справочник видов работ — заявка не создана, повторите вручную" },
  ],
  held_back: [
    { conversation_id: "conv-3", client_name: "Григорий Воронцов", account_title: "Анатолий МНЧ", reason: "Нет номера телефона клиента и не задан адрес визита" },
  ],
};

const РЕШЁТКА = {
  accounts: КАНАЛЫ.map((к, i) => ({ id: `acc-${i + 1}`, title: к.title, lead_origin: к.origin, status: i === 4 ? "disabled" : "active", is_service: false })),
  users: КОМАНДА.map((ч) => ({
    id: ч.id,
    full_name: ч.full_name,
    role: ч.role,
    can_be_operator: ч.role !== "observer",
    reason: ч.role === "observer" ? "Роль не отвечает клиентам" : null,
    department: ч.department,
  })),
  assigned: [
    { account_id: "acc-1", user_id: "u-1" },
    { account_id: "acc-2", user_id: "u-1" },
    { account_id: "acc-1", user_id: "u-3" },
    { account_id: "acc-3", user_id: "u-6" },
  ],
};

const ЖУРНАЛ = {
  items: [
    { id: 90211, user: { id: "u-1", full_name: "Константинопольский Вячеслав" }, action: "user.role_changed", description: "Смена роли: Кузнецов Арсений (Чатер) → Менеджер", entity: "user", entity_id: "7f3a19c2-1111-2222-3333-444455556666", details: { role: "manager" }, created_at: "2026-09-07T11:32:00Z" },
    { id: 90210, user: { id: "u-2", full_name: "Анна Смирнова" }, action: "account.operators_changed", description: "Канал «Вячеслав БТ/МНЧ · Санкт-Петербург»: назначено 4 оператора вместо 2", entity: "avito_account", entity_id: "acc-2", details: { added: ["u-3", "u-6"] }, created_at: "2026-09-07T10:04:00Z" },
    { id: 90209, user: { id: "u-1", full_name: "Константинопольский Вячеслав" }, action: "settings.distribution_changed", description: "Автораспределение включено, потолок активных диалогов снят", entity: "settings", entity_id: "distribution", details: { enabled: true, max_active: null }, created_at: "2026-09-06T18:41:00Z" },
  ],
  page: { limit: 50, offset: 0, total: 3 },
};

/**
 * Один ответчик на все экраны раздела: путей немного, а разложить их по
 * восьми копиям стаба значило бы восемь раз забыть один и тот же.
 */
function поднятьСеть() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const p = url.pathname;
      const method = init?.method ?? "GET";
      if (method !== "GET") return jsonResponse(200, {});

      if (p.includes("/avito-accounts/operators/grid")) return jsonResponse(200, РЕШЁТКА);
      if (p.includes("/avito-accounts")) {
        return jsonResponse(200, { items: КАНАЛЫ.map((_, i) => канал(i)), page: { limit: 50, offset: 0, total: КАНАЛЫ.length } });
      }
      if (p.endsWith("/templates")) {
        const личные = url.searchParams.get("scope") === "personal";
        const items = личные ? ШАБЛОНЫ.filter((t) => t.owner_id) : ШАБЛОНЫ.filter((t) => !t.owner_id);
        return jsonResponse(200, { items, page: { limit: 200, offset: 0, total: items.length } });
      }
      if (p.endsWith("/bots")) return jsonResponse(200, БОТЫ);
      if (p.includes("/leadbot/calls")) return jsonResponse(200, ЛИДБОТ_ЗВОНКИ);
      if (p.includes("/leadbot/silence")) return jsonResponse(200, ЛИДБОТ_МОЛЧАНИЕ);
      if (p.includes("/leadbot")) return jsonResponse(200, ЛИДБОТ);
      if (p.includes("/settings/leads/handouts")) return jsonResponse(200, ЛИДЫ_ВЫДАЧИ);
      if (p.includes("/settings/leads")) return jsonResponse(200, ЛИДЫ_СОСТОЯНИЕ);
      if (p.endsWith("/settings/distribution")) return jsonResponse(200, { enabled: true, max_active: 5 });
      if (p.endsWith("/settings/work-hours")) return jsonResponse(200, { start_hour: 8, end_hour: 22 });
      if (p.endsWith("/settings/phone-detect")) return jsonResponse(200, { enabled: true, min_digits: 10 });
      if (p.endsWith("/settings/thread")) return jsonResponse(200, { page_size: 50 });
      if (p.endsWith("/audit-log")) return jsonResponse(200, ЖУРНАЛ);
      if (p.endsWith("/users/assignable")) return jsonResponse(200, { items: КОМАНДА });
      if (p.endsWith("/users")) return jsonResponse(200, { items: КОМАНДА, page: { limit: 50, offset: 0, total: КОМАНДА.length } });
      if (p.endsWith("/me/channels")) {
        return jsonResponse(200, {
          items: КАНАЛЫ.slice(0, 3).map((к, i) => ({ id: `acc-${i + 1}`, title: к.title, lead_origin: к.origin, muted: i === 2 })),
          all_channels: false,
        });
      }
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    }),
  );
}

/**
 * Снимает один экран внутри настоящего каркаса настроек и кладёт на диск.
 *
 * `ждём` — текст, по которому видно, что данные приехали: снимок до ответа
 * сети — это скелетон, и стенд померил бы серые прямоугольники вместо строк.
 */
async function снять(
  имя: string,
  путь: string,
  экран: React.ReactNode,
  ждём: string,
  до?: () => void,
  ждём2?: string,
) {
  queryClient.clear();
  const { container } = renderWithProviders(
    <Routes>
      <Route path="/settings" element={<SettingsLayout />}>
        <Route path={путь} element={экран} />
      </Route>
    </Routes>,
    { route: `/settings/${путь}` },
  );
  /*
   * Ждём НЕ текст, а узел с данными: снимок до ответа сети — это скелетон, и
   * стенд померил бы серые прямоугольники вместо настоящих строк. Селектор
   * надёжнее подписи: подпись собирается из нескольких span'ов (название
   * канала плюс источник), и точное совпадение по строке не срабатывает.
   */
  await waitFor(
    () => expect(container.querySelector(ждём), `${имя}: не дождались ${ждём}`).toBeTruthy(),
    { timeout: 5000 },
  );
  /*
   * Раскрытая карточка, вторая вкладка, журнал — это ДРУГАЯ разметка, а не то
   * же самое чуть иначе. Снимок только первого вида оставил бы их без замера.
   */
  if (до) {
    до();
    await waitFor(
      () => expect(container.querySelector(ждём2 ?? ждём), `${имя}: не дождались ${ждём2}`).toBeTruthy(),
      { timeout: 5000 },
    );
  }
  writeFileSync(`${КАТАЛОГ}/nastroyki-${имя}.html`, container.innerHTML, "utf8");
  // Пустой снимок на стенде выглядел бы как «всё влезает», то есть врал бы в
  // самую удобную сторону.
  expect(container.innerHTML.length, `${имя}: снимок пуст`).toBeGreaterThan(3000);
  cleanup();
}

it("снимает разметку всех экранов раздела «Настройки»", async () => {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ВСЕ_ПРАВА as never,
    accessToken: "t",
    bootstrapped: true,
  });
  поднятьСеть();
  ширинаОкна(1440);

  await снять("accounts", "accounts", <AccountsPage />, "article.account-card");
  await снять("operators", "accounts/operators", <OperatorsGridPage />, ".op-grid__cell");
  await снять("templates", "templates", <TemplatesPage />, ".tpl-col--title");
  await снять("bots", "bots", <BotsPage />, ".bots-table__row");
  await снять("leadbot", "leadbot", <LeadbotTab />, ".lb-channels__item");
  await снять("leads", "leads", <LeadsTab />, ".leads-channel");
  await снять("distribution", "distribution", <DistributionTab />, ".dist__cols");
  await снять("profile", "profile", <ProfilePage />, ".prof-hero__name");

  /*
   * ⚠ КОМАНДА СНИМАЕТСЯ ДВАЖДЫ, И ЭТО НЕ ДУБЛЬ. Ниже 1560 справка о ролях —
   * свёрнутый `<details>` в потоке, выше — `<aside>` во второй колонке сетки.
   * Тег выбирает React по `useMediaQuery`, и снимок при одной ширине оставил
   * бы половину диапазона непроверенной.
   */
  /* Раскрытая карточка канала — своя сетка (`.account-card[data-open]`). */
  await снять("accounts-open", "accounts", <AccountsPage />, "article.account-card", () => {
    fireEvent.click(document.querySelectorAll(".account-row__chevron")[0].closest("button")!);
  }, "article.account-card[data-open]");
  /* Вторая вкладка профиля: там таблица сочетаний с колонкой в 190px. */
  await снять("profile-ui", "profile", <ProfilePage />, ".prof-hero__name", () => {
    fireEvent.click(screen.getByRole("tab", { name: "Интерфейс" }));
  }, ".prof-hotkeys");
  /* Журнал аудита — вторая вкладка «Команды», таблица на семь колонок. */
  await снять("team-audit", "team", <TeamPage />, ".team-members__table", () => {
    fireEvent.click(screen.getByRole("tab", { name: "Журнал аудита" }));
  }, "table.audit__table");

  /*
   * ⚠ ПОДМЕНЯЕТСЯ ШИРИНА БЛОКА, А НЕ ОКНА (правка 08.09). Ветку выбирает уже
   * не `useMediaQuery`, а замер самой сетки (`useInlineSize` в
   * TeamMembersTab): окно содержимому не достаётся целиком — слева рельса.
   * jsdom раскладку не считает и отдаёт ноль на любой блок, поэтому ширину
   * подставляем руками — 1075 и 1475 — это те самые сетки, что дают на стенде
   * окна 1561 и 1961 при развёрнутой рельсе.
   */
  ширинаБлока(1075);
  await снять("team-fold", "team", <TeamPage />, ".team-roles--fold");
  ширинаБлока(1475);
  await снять("team-aside", "team", <TeamPage />, "aside.team-roles");
  ширинаБлока(null);

  vi.unstubAllGlobals();
  expect(true).toBe(true);
});
