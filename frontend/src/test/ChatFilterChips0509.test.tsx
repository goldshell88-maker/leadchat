import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import type { ConversationFilters } from "@/shared/api/queryKeys";
import type { ConversationDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ВКЛЮЧЁННЫЕ СУЖЕНИЯ ВИДНЫ НА ЭКРАНЕ, А НЕ ЗА ЗНАЧКОМ (жалоба владельца 05.09:
 * «при выборе фильтра по каналам не сразу понятно, как их сбросить»).
 *
 * ЧТО БЫЛО. Панель фильтров свёрнута по умолчанию. Выбрал канал — панель
 * закрылась, и от сужения на экране осталась цифра «1» на значке. Цифра
 * называет КОЛИЧЕСТВО, а человеку нужно ЗНАЧЕНИЕ: какой канал стоит и чем его
 * снять. Кнопка «Сбросить фильтры» при этом лежала внутри той же свёрнутой
 * панели — то есть за тем же значком, из-за которого её ищут.
 *
 * ЧТО СТЕРЕЖЁТСЯ ЗДЕСЬ: чип на каждое включённое сужение, подпись со
 * ЗНАЧЕНИЕМ, крестик у каждого и «Сбросить всё» при двух и более. Всё это —
 * БЕЗ единого нажатия на значок фильтров.
 */

const строка: ConversationDto = {
  id: "conv-1",
  status: "in_progress",
  channel: "avito",
  account: { id: "acc-1", title: "Стас КП" },
  client: { id: "cl-1", name: "Иван Петров", phone: null, avito_rating: null },
  assignee: null,
  item: null,
  last_message: { body: "Здравствуйте", direction: "in", created_at: "2026-09-05T09:00:00Z" },
  unread_count: 0,
  bot_active: false,
  tags: ["негатив"],
  transferred_to_me: false,
  last_message_at: "2026-09-05T09:00:00Z",
};

const ACCOUNTS = [
  { id: "acc-1", title: "Стас КП" },
  { id: "acc-2", title: "Парт - 9" },
];

function renderPane(filters: ConversationFilters, assigneeLabel: string | null = null) {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ["conversations:read", "messages:send", "conversations:manage", "stats:all", "accounts:read"],
    accessToken: "test-token",
    bootstrapped: true,
  });
  useChatUiStore.setState({ activeConversationId: null, filters, assigneeLabel, inboxOpen: false });

  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/avito-accounts")) return jsonResponse(200, { items: ACCOUNTS });
    if (url.pathname.endsWith("/users/assignable")) {
      return jsonResponse(200, { items: [{ id: "u-7", full_name: "Пётр Ковалёв", role: "manager", is_online: true }] });
    }
    if (url.pathname.endsWith("/conversations")) {
      return jsonResponse(200, { items: [строка], page: { limit: 50, offset: 0, total: 1 } });
    }
    return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
  });
  vi.stubGlobal("fetch", fetchMock);

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/chats"]}>
          <ChatListPane />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

const высота = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const ширина = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("Чипы включённых сужений", () => {
  beforeEach(() => {
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (высота) Object.defineProperty(HTMLElement.prototype, "offsetHeight", высота);
    if (ширина) Object.defineProperty(HTMLElement.prototype, "offsetWidth", ширина);
  });

  it("канал виден чипом со ЗНАЧЕНИЕМ, не открывая панель", async () => {
    renderPane({ tab: "all", accountId: "acc-1" });

    // Именно значение: имя канала, а не слово «Канал» и не число сужений.
    expect(await screen.findByText("Канал: Стас КП")).toBeInTheDocument();
    // Панель при этом закрыта — поля фильтров на экране нет.
    expect(screen.queryByLabelText("Фильтр по каналу")).toBeNull();
  });

  it("крестик чипа снимает СВОЁ сужение и не трогает соседние", async () => {
    const user = userEvent.setup();
    renderPane({ tab: "all", accountId: "acc-1", tag: "негатив" });

    await user.click(await screen.findByRole("button", { name: "Снять сужение по каналу" }));

    const f = useChatUiStore.getState().filters;
    expect(f.accountId).toBeUndefined();
    // Соседний чип обязан выжить: «снять это» — не «сбросить всё».
    expect(f.tag).toBe("негатив");
    expect(screen.getByText("Метка: негатив")).toBeInTheDocument();
  });

  it("«Сбросить всё» появляется при двух сужениях и снимает оба", async () => {
    const user = userEvent.setup();
    renderPane({ tab: "all", accountId: "acc-1", tag: "негатив" });

    await user.click(await screen.findByRole("button", { name: "Сбросить всё" }));

    const f = useChatUiStore.getState().filters;
    expect({ accountId: f.accountId, tag: f.tag }).toEqual({
      accountId: undefined,
      tag: undefined,
    });
    // Бейдж на значке считает тот же список — разъедься они, метка сужения
    // осталась бы гореть над вернувшимся полным списком.
    expect(screen.queryByLabelText(/Сужений/)).toBeNull();
  });

  /**
   * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА, И ОНА ПАДАЕТ ПО СВОЕЙ ПРИЧИНЕ: сужение здесь
   * ЕСТЬ, чип и его крестик на экране нарисованы. Убери условие
   * `сужения.length > 1` — «Сбросить всё» появится рядом с единственным
   * крестиком, и проверка покраснеет именно на этом, а не на пустом экране.
   */
  it("при одном сужении «Сбросить всё» не показывается — крестик делает то же", async () => {
    renderPane({ tab: "all", accountId: "acc-1" });

    expect(await screen.findByText("Канал: Стас КП")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Снять сужение по каналу" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Сбросить всё" })).toBeNull();
  });

  it("«Сбросить всё» гасит и подпись сотрудника, а не только его id", async () => {
    const user = userEvent.setup();
    renderPane({ tab: "all", assigneeId: "u-7", accountId: "acc-1" }, "Пётр Ковалёв");

    expect(await screen.findByText("Оператор: Пётр Ковалёв")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сбросить всё" }));

    // Подпись живёт рядом с фильтрами, а не внутри них: забудь её в патче —
    // и чип с чужим именем повис бы над полным списком.
    expect(useChatUiStore.getState().filters.assigneeId).toBeUndefined();
    expect(useChatUiStore.getState().assigneeLabel).toBeNull();
  });

  it("строка чипов переносится, а не распирает колонку 340 px", async () => {
    const { container } = renderPane({ tab: "all", accountId: "acc-1", tag: "негатив" });

    await screen.findByText("Канал: Стас КП");
    expect(container.querySelector(".chat-list-pane__applied")).not.toBeNull();
    // Правило переноса живёт в таблице стилей — проверяем её, а не разметку:
    // в jsdom ширины и переносы не считаются вовсе, и «проверка раскладки» по
    // DOM была бы зелёной при любом CSS.
    // @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
    const { readFileSync } = await import("node:fs");
    const css = readFileSync("src/features/chats/components/list/chat-list.css", "utf-8") as string;
    expect(css).toMatch(/\.chat-list-pane__applied\s*\{[^}]*flex-wrap:\s*wrap/);
    // И крестик — цель нажатия не меньше 24×24.
    expect(css).toMatch(/\.chat-list-pane__chip > button\s*\{[^}]*min-height:\s*24px/);
  });
});
