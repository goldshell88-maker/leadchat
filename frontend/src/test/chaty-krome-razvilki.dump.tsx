/**
 * НЕ СТОРОЖ. Снималка РАЗВИЛОК рабочего места для стенда адаптива.
 *
 * ⚠ ЗАЧЕМ ОТДЕЛЬНО ОТ `__снимок-разметки.dump.tsx`. Тот снимает ОДНО
 * состояние React — трёхколоночное, с закрытой карточкой. Но раскладку
 * выбирает `useMediaQuery` в самом ChatsPage, и в снимке этот выбор УЖЕ
 * застыл: сколько ни меняй ширину стенда, разметка останется трёхколоночной.
 * То есть весь диапазон 768–1359 (карточка оверлеем) и ≤767 (стек из двух
 * экранов) остался бы непроверенным — а это ровно те ширины, где вёрстка и
 * едет.
 *
 * Здесь `matchMedia` подменяется на заданную ширину ДО отрисовки, и снимки
 * снимаются по одному на каждую ветку.
 *
 * Запуск: npx vitest run --config vitest.dump.config.ts
 */
import { expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { writeFileSync } from "node:fs";
import { act } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { ChatsPage } from "@/features/chats/ChatsPage";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessageDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

const СТРОКИ = [
  { имя: "Анатолий МНЧ", канал: "Анатолий МНЧ", текст: "Доброе утро! Во сколько вы сможете подъехать сегодня?", ждёт: 60, непроч: 0 },
  { имя: "Пользователь", канал: "Марк БТ/МНЧ", текст: "1-аяКленовская 12, телефон 8 (900) 000-00-00", ждёт: 1, непроч: 3 },
  { имя: "Светлана Морозова", канал: "Бригада Петра Иванова КП", текст: "Нам удобней самим привезти телевизор", ждёт: 5, непроч: 1 },
  { имя: "Васильцова 7000300", канал: "Вячеслав БТ/МНЧ", текст: "Лесная, 118", ждёт: 8, непроч: 3 },
];

function строка(i: number): ConversationDto {
  const с = СТРОКИ[i];
  const когда = new Date(Date.UTC(2026, 8, 7, 19, 30) - с.ждёт * 60_000).toISOString();
  return {
    id: i === 0 ? "conv-1" : `conv-${i + 1}`,
    status: "in_progress",
    channel: "avito",
    account: { id: `acc-${i}`, title: с.канал },
    client: { id: `cl-${i}`, name: с.имя, phone: null, avito_rating: 4.9 },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Телемастер на дом / Ремонт телевизоров / Все бренды / ЖК LED oled / Выезд в день обращения", url: null, price: "360 ₽" },
    last_message: { id: `m-${i}`, body: с.текст, direction: "in", sender_type: "client", created_at: когда },
    unread_count: с.непроч,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: когда,
    waiting_since: когда,
    status_since: когда,
  } as unknown as ConversationDto;
}

const РЕПЛИКИ: MessageDto[] = [
  { id: "m2", direction: "in", sender_type: "client", body: "Нет, не Билли просто он выключился, и он работает, все нету подсветки, я фонариком просветила, работает экран, нету подсветки.", attachments: [], created_at: "2026-09-07T15:49:00Z", status: "delivered" },
  { id: "m4", direction: "out", sender_type: "operator", sender_name: "Кузнецов Арсений (Чатер)", body: "Подскажите, куда к вам подъехать (квартира, подъезд, этаж) и ваш номер телефона, запишу вас", attachments: [], created_at: "2026-09-07T16:15:00Z", status: "delivered" },
  { id: "m5", direction: "in", sender_type: "client", body: "Вы мне скажите, сколько будет стоить, а то мне тут сказали 10000?", attachments: [], created_at: "2026-09-07T16:16:00Z", status: "delivered" },
] as unknown as MessageDto[];

/**
 * Окно заданной ширины. Приём тот же, что в `clientCardOverlay.test.tsx`:
 * `useMediaQuery` читает `matchMedia`, и другого способа сообщить React
 * ширину у нас нет — настоящего окна в jsdom не существует.
 */
function ширинаОкна(width: number) {
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

function посеять() {
  queryClient.clear();
  resetSessionStore({
    user: fakeUser,
    permissions: ["conversations:read", "messages:send", "conversations:manage", "notes:read", "notes:write"] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  useInboxStore.setState({ ids: {}, escalatedIds: {}, count: 25, escalated: 0, claimedNotice: null } as never);

  const items = СТРОКИ.map((_, i) => строка(i));
  for (const вариант of [{ tab: "mine" as const }, { tab: "mine" as const, q: "" }]) {
    queryClient.setQueryData([...CONVERSATIONS_LIST_KEY, вариант], {
      pages: [{ items, page: { limit: 50, offset: 0, total: items.length } }],
      pageParams: [0],
    });
  }
  queryClient.setQueryData(
    qk.conversations.detail("conv-1"),
    makeConversation({
      client: { id: "cl-т", name: "Татьяна Константинопольская", phone: "+7 900 000-00-00", avito_rating: 4.9 },
      account: { id: "acc-0", title: "Бригада Петра Иванова КП" },
      item: {
        title: "Телемастер на дом / Ремонт телевизоров / Все бренды / ЖК LED oled / Выезд в день обращения",
        url: "https://avito.ru/x",
        price: "360 ₽",
      },
    }),
  );
  queryClient.setQueryData(qk.messages.list("conv-1"), {
    pages: [{ items: РЕПЛИКИ, page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false } }],
    pageParams: [null],
  });
  /*
   * ⚠ ЛИЧНОСТЬ КЛИЕНТА СЕЕТСЯ РАДИ БЛОКА ДОПОЛНИТЕЛЬНЫХ НОМЕРОВ (09.09).
   * Без неё карточка на стенде рисуется БЕЗ этого блока, и сверка раскладки
   * про него не говорит ничего — то есть сторож слеп по построению. Номеров
   * взято ПЯТЬ: столько бывает у человека после объединения карточек, и это
   * ровно тот случай, где строка «номер + кнопка» переносится и может вылезти
   * за край узкой карточки.
   *
   * Один номер — чужой (`client_id` присоединённой карточки): у такого кнопки
   * нет, и ряд обязан оставаться ровным без неё.
   */
  queryClient.setQueryData(["clients", "identity", "cl-т"], {
    id: "cl-т",
    name: "Татьяна Константинопольская",
    phone: "+79000000000",
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    external_id: "923456789",
    phones: [
      { value: "+79000000000", client_id: "cl-т", primary: true },
      { value: "+79995550188", client_id: "cl-т", primary: false },
      { value: "+79161112233", client_id: "cl-т", primary: false },
      { value: "+79001112241", client_id: "cl-т", primary: false },
      { value: "+79031234567", client_id: "cl-т", primary: false },
      { value: "+79001112242", client_id: "cl-другая", primary: false },
    ],
    avito_ids: [{ value: "923456789", client_id: "cl-т", primary: true }],
    merged_from: [],
    merged_into: null,
  });
}

function снять(имя: string, html: string) {
  // Пустой снимок на стенде выглядел бы как «всё влезает», то есть врал бы
  // в самую удобную сторону — падаем громко.
  expect(html.length, `снимок ${имя} пуст`).toBeGreaterThan(1500);
  writeFileSync(`./.dump/${имя}.html`, html, "utf8");
}

it("снимает карточку клиента ОВЕРЛЕЕМ (ширина ≤1359)", () => {
  ширинаОкна(1280);
  посеять();
  useChatUiStore.setState({ clientCardOpen: false } as never);
  const { container } = renderWithProviders(
    <Routes>
      <Route path="/chats/:id" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats/conv-1" },
  );
  // Карточку открываем ПОСЛЕ монтирования: на смену диалога оверлей гасится.
  act(() => useChatUiStore.getState().setClientCardOpen(true));
  снять("razvilki-card-overlay", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает мобильный стек: только лента (ширина ≤767)", () => {
  ширинаОкна(390);
  посеять();
  useChatUiStore.setState({ clientCardOpen: false } as never);
  const { container } = renderWithProviders(
    <Routes>
      <Route path="/chats/:id" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats/conv-1" },
  );
  снять("razvilki-mobile-thread", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает мобильный стек: только список (ширина ≤767)", () => {
  ширинаОкна(390);
  посеять();
  const { container } = renderWithProviders(
    <Routes>
      <Route path="/chats" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats" },
  );
  снять("razvilki-mobile-list", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает мобильную карточку клиента поверх ленты (ширина ≤767)", () => {
  ширинаОкна(390);
  посеять();
  useChatUiStore.setState({ clientCardOpen: false } as never);
  const { container } = renderWithProviders(
    <Routes>
      <Route path="/chats/:id" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats/conv-1" },
  );
  act(() => useChatUiStore.getState().setClientCardOpen(true));
  снять("razvilki-mobile-card", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает узкую раскладку с ЗАКРЫТОЙ карточкой (768–1359)", () => {
  ширинаОкна(1280);
  посеять();
  useChatUiStore.setState({ clientCardOpen: false } as never);
  const { container } = renderWithProviders(
    <Routes>
      <Route path="/chats/:id" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats/conv-1" },
  );
  снять("razvilki-narrow-closed", container.innerHTML);
  vi.unstubAllGlobals();
});
