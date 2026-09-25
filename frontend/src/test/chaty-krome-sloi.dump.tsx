/**
 * НЕ СТОРОЖ. Снималка ОТКРЫТЫХ СЛОЁВ и ПОЛОС рабочего места для стенда адаптива.
 *
 * ⚠ ЗАЧЕМ. Снимок главного кадра застаёт экран в покое: все меню закрыты, полос
 * нет, режим ввода обычный. А едет вёрстка как раз в слоях — там, где на
 * готовую раскладку кладут ещё один прямоугольник с текстом заранее неизвестной
 * длины: меню статуса, «Действия с диалогом», фильтры списка, панель быстрых
 * ответов, полоса передачи, полоса «диалог ведёт бот».
 *
 * Слои Mantine живут в ПОРТАЛЕ, то есть вне `container`, — поэтому снимается
 * `document.body.innerHTML` целиком.
 *
 * ⚠ ЧЕГО ЭТОТ СНИМОК НЕ ЗНАЕТ. Координаты выпадающих панелей считает
 * floating-ui в момент открытия, по НАСТОЯЩИМ размерам якоря; в jsdom они
 * нулевые, и в снимке панель окажется у левого верхнего угла. Значит, на стенде
 * честно меряется ТОЛЬКО собственная ширина панели и обрезка текста внутри
 * неё, а не то, вылезет ли она за край окна. Про это сказано в отчёте.
 *
 * Запуск: npx vitest run --config vitest.dump.config.ts
 */
import { expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { writeFileSync } from "node:fs";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { ChatsPage } from "@/features/chats/ChatsPage";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessageDto } from "@/shared/api/types";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

const ДРУГОЙ = { id: "u-2", full_name: "Кузнецов Арсений Константинопольский" };

function строка(): ConversationDto {
  const когда = "2026-09-07T19:25:00Z";
  return {
    id: "conv-1",
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-0", title: "Бригада Петра Иванова КП" },
    client: { id: "cl-0", name: "Татьяна Константинопольская", phone: null, avito_rating: 4.9 },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Телемастер на дом / Ремонт телевизоров / Все бренды / ЖК LED oled", url: null, price: "360 ₽" },
    last_message: { id: "m-0", body: "Лесная, 118", direction: "in", sender_type: "client", created_at: когда },
    unread_count: 3,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: когда,
    waiting_since: когда,
    status_since: когда,
  } as unknown as ConversationDto;
}

const РЕПЛИКИ: MessageDto[] = [
  { id: "m2", direction: "in", sender_type: "client", body: "Нет, не Билли просто он выключился, я фонариком просветила, работает экран, нету подсветки.", attachments: [], created_at: "2026-09-07T15:49:00Z", status: "delivered" },
  { id: "m4", direction: "out", sender_type: "operator", sender_name: "Кузнецов Арсений (Чатер)", body: "Подскажите, куда к вам подъехать (квартира, подъезд, этаж) и ваш номер телефона, запишу вас", attachments: [], created_at: "2026-09-07T16:15:00Z", status: "delivered" },
] as unknown as MessageDto[];

/** Ширина окна для `useMediaQuery`: без неё ChatsPage считает экран широким. */
function ширинаОкна(width: number) {
  /*
   * ⚠ ОКНО ЗАДАЁТСЯ ДВАЖДЫ, И ЭТО НЕ ИЗБЫТОК (08.09). Раньше хватало подмены
   * `matchMedia`: ширину читал `useMediaQuery`. После перевода порогов на
   * ширину МЕСТА (заход 2) решение принимает `useWorkspaceWidth`, а он в
   * jsdom не может измерить элемент (раскладки нет) и честно откатывается
   * на `window.innerWidth − рельса`. В jsdom это 1024 − 218 = 806, то есть
   * «узко» при любой запрошенной ширине: карточка клиента уезжала в оверлей
   * и слой «Статус диалога» не снимался вовсе. Один экран из-за этого
   * молча выпал из проверки раскладки — см. layout/известные.ts.
   */
  vi.stubGlobal("innerWidth", width);
  vi.stubGlobal("matchMedia", (query: string) => {
    const max = Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? Infinity);
    return {
      matches: width <= max,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  });
}

function посеять(деталь: Record<string, unknown> = {}) {
  queryClient.clear();
  resetSessionStore({
    user: fakeUser,
    permissions: [
      "conversations:read",
      "messages:send",
      "conversations:manage",
      "notes:read",
      "notes:write",
      "templates:own",
      "templates:shared",
    ] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  useInboxStore.setState({ ids: {}, escalatedIds: {}, count: 25, escalated: 0, claimedNotice: null } as never);

  const items = [строка()];
  for (const вариант of [{ tab: "mine" as const }, { tab: "mine" as const, q: "" }]) {
    queryClient.setQueryData([...CONVERSATIONS_LIST_KEY, вариант], {
      pages: [{ items, page: { limit: 50, offset: 0, total: items.length } }],
      pageParams: [0],
    });
  }
  queryClient.setQueryData(
    qk.conversations.detail("conv-1"),
    makeConversation({
      client: { id: "cl-0", name: "Татьяна Константинопольская", phone: "+7 900 000-00-00", avito_rating: 4.9 },
      account: { id: "acc-0", title: "Бригада Петра Иванова КП" },
      item: {
        title: "Телемастер на дом / Ремонт телевизоров / Все бренды / ЖК LED oled / Выезд в день обращения",
        url: "https://avito.ru/x",
        price: "360 ₽",
      },
      // `as never` ломал разбор типов: из него нельзя разложить объект, и
      // на этом падала вся сборка (tsc идёт первым шагом `npm run build`).
      ...(деталь as Record<string, unknown>),
    }),
  );
  queryClient.setQueryData(qk.messages.list("conv-1"), {
    pages: [{ items: РЕПЛИКИ, page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false } }],
    pageParams: [null],
  });
}

function снять(имя: string, html: string) {
  expect(html.length, `снимок ${имя} пуст`).toBeGreaterThan(1500);
  writeFileSync(`./.dump/${имя}.html`, html, "utf8");
}

function нарисовать() {
  return renderWithProviders(
    <Routes>
      <Route path="/chats/:id" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats/conv-1" },
  );
}

/**
 * Слои по одному: два открытых меню сразу — состояние, которого на экране не
 * бывает, и мерить его значило бы искать беду там, где её некому увидеть.
 */
const СЛОИ: Array<{ имя: string; кнопка: string }> = [
  { имя: "sloi-status-menu", кнопка: "Статус диалога" },
  { имя: "sloi-more-actions", кнопка: "Действия с диалогом" },
  { имя: "sloi-list-filters", кнопка: "Фильтры диалогов" },
  { имя: "sloi-quick-replies", кнопка: "Быстрые ответы" },
];

for (const слой of СЛОИ) {
  it(`снимает открытый слой: ${слой.кнопка}`, async () => {
    ширинаОкна(1920);
    посеять();
    const user = userEvent.setup();
    нарисовать();
    const btn = screen.getAllByLabelText(слой.кнопка)[0];
    await user.click(btn);
    снять(слой.имя, document.body.innerHTML);
    vi.unstubAllGlobals();
  });
}

it("снимает режим заметки в поле ввода", async () => {
  ширинаОкна(1920);
  посеять();
  const user = userEvent.setup();
  нарисовать();
  await user.click(screen.getByRole("button", { name: /Заметка/ }));
  снять("sloi-note-mode", document.body.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает полосы: передача диалога, бот ведёт диалог, «в работе у»", () => {
  ширинаОкна(1920);
  посеять({
    bot_active: true,
    participants: [
      { id: fakeUser.id, full_name: fakeUser.full_name, department: "ОКК" },
      { id: ДРУГОЙ.id, full_name: ДРУГОЙ.full_name, department: "Контроль качества" },
    ],
    transfer: {
      to: { id: fakeUser.id, full_name: fakeUser.full_name },
      by: { id: ДРУГОЙ.id, full_name: ДРУГОЙ.full_name },
      note: "Клиент просит мастера сегодня до 18:00, я на выезде — передаю",
      created_at: "2026-09-07T19:20:00Z",
    },
  });
  const { container } = нарисовать();
  снять("sloi-polosy", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает те же полосы на узком экране (карточка оверлеем)", () => {
  ширинаОкна(1280);
  посеять({
    bot_active: true,
    participants: [
      { id: fakeUser.id, full_name: fakeUser.full_name, department: "ОКК" },
      { id: ДРУГОЙ.id, full_name: ДРУГОЙ.full_name, department: "Контроль качества" },
    ],
    transfer: {
      to: { id: fakeUser.id, full_name: fakeUser.full_name },
      by: { id: ДРУГОЙ.id, full_name: ДРУГОЙ.full_name },
      note: "Клиент просит мастера сегодня до 18:00, я на выезде — передаю",
      created_at: "2026-09-07T19:20:00Z",
    },
  });
  const { container } = нарисовать();
  снять("sloi-polosy-1280", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает те же полосы на телефоне (стек из двух экранов)", () => {
  ширинаОкна(390);
  посеять({
    bot_active: true,
    participants: [
      { id: fakeUser.id, full_name: fakeUser.full_name, department: "ОКК" },
      { id: ДРУГОЙ.id, full_name: ДРУГОЙ.full_name, department: "Контроль качества" },
    ],
    transfer: {
      to: { id: fakeUser.id, full_name: fakeUser.full_name },
      by: { id: ДРУГОЙ.id, full_name: ДРУГОЙ.full_name },
      note: "Клиент просит мастера сегодня до 18:00, я на выезде — передаю",
      created_at: "2026-09-07T19:20:00Z",
    },
  });
  const { container } = нарисовать();
  снять("sloi-polosy-mobile", container.innerHTML);
  vi.unstubAllGlobals();
});

/**
 * Модальные окна — отдельно от выпадающих панелей, и это важно для доверия к
 * замеру. Позицию `Menu`/`Popover` считает floating-ui по НАСТОЯЩИМ размерам
 * якоря, которых в jsdom нет, — такой снимок мерить бессмысленно. Модальное
 * окно Mantine раскладывает CSS (центрирование флексом, ширина от `size`), то
 * есть на стенде оно встаёт ровно туда же, куда встало бы у человека.
 */
const ОКНА: Array<{ имя: string; пункт: RegExp; ширина: number }> = [
  { имя: "okno-transfer-1920", пункт: /Передать диалог/, ширина: 1920 },
  { имя: "okno-transfer-390", пункт: /Передать диалог/, ширина: 390 },
  { имя: "okno-invite-1920", пункт: /Позвать коллегу/, ширина: 1920 },
  { имя: "okno-invite-390", пункт: /Позвать коллегу/, ширина: 390 },
];

for (const окно of ОКНА) {
  it(`снимает модальное окно: ${окно.имя}`, async () => {
    ширинаОкна(окно.ширина);
    посеять();
    const user = userEvent.setup();
    нарисовать();
    await user.click(screen.getAllByLabelText("Действия с диалогом")[0]);
    await user.click(await screen.findByText(окно.пункт));
    снять(окно.имя, document.body.innerHTML);
    vi.unstubAllGlobals();
  });
}
