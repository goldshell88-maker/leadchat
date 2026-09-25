import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ИСТОЧНИК ВИДЕН В ФИЛЬТРЕ «КАНАЛ» (просьба владельца 03.09).
 *
 * ⚠ ЭТО ПРОВЕРКА «ПОДКЛЮЧЕНО ЛИ», И ОНА ВАЖНЕЕ ПРОВЕРКИ САМОГО ФОРМАТА.
 * Формат подписи сторожит `channelLabel0309.test.ts`. Но формат, который никто
 * не зовёт, — самый частый дефект этого проекта: за один день он попадался
 * четырежды. Здесь список рисуется по-настоящему.
 */

const АККАУНТЫ = [
  { id: "acc-1", title: "Александр КП", lead_origin: "В84.2" },
  { id: "acc-2", title: "Александр МНЧ", lead_origin: "М5" },
  { id: "acc-3", title: "SMOKE-ACCOUNT", lead_origin: null },
];

function строка(id: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "Александр КП", lead_origin: "В84.2" },
    client: { id: `cl-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт", url: null, price: null },
    last_message: { body: "текст", direction: "in", created_at: "2026-09-03T09:40:12Z" },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-03T09:40:12Z",
  } as unknown as ConversationDto;
}

const страница = (items: ConversationDto[]): ConversationsPage => ({
  items,
  page: { limit: 50, offset: 0, total: items.length },
});

const оригВысота = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const оригШирина = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("источник в фильтре «Канал»", () => {
  beforeEach(() => {
    // Админ намеренно: справочник каналов запрашивается только при праве
    // `accounts:read`, а без него фильтр собирается из загруженных строк и не
    // покажет каналы, по которым сейчас нет диалогов.
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      accessToken: "test-token",
      bootstrapped: true,
      permissions: ["conversations:read", "accounts:read"],
    });
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" } });
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get: () => 600,
    });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
      configurable: true,
      get: () => 320,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/avito-accounts")) {
          return jsonResponse(200, { items: АККАУНТЫ, page: { limit: 50, offset: 0, total: 3 } });
        }
        if (url.pathname.endsWith("/conversations/counts")) {
          return jsonResponse(200, { mine: 1, mine_waiting: 0 });
        }
        if (url.pathname.endsWith("/conversations")) {
          return jsonResponse(200, страница([строка("a")]));
        }
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (оригВысота) Object.defineProperty(HTMLElement.prototype, "offsetHeight", оригВысота);
    if (оригШирина) Object.defineProperty(HTMLElement.prototype, "offsetWidth", оригШирина);
  });

  function нарисовать() {
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

  it("рядом с названием канала стоит его источник", async () => {
    нарисовать();
    // Панель фильтров свёрнута по умолчанию — сначала раскрываем её, как это
    // делает человек.
    await userEvent.click(await screen.findByRole("button", { name: /фильтры/i }));
    await userEvent.click(await screen.findByPlaceholderText(/канал/i));

    await waitFor(() => expect(screen.getByText("Александр КП · В84.2")).toBeTruthy());
    expect(
      screen.getByText("Александр МНЧ · М5"),
      "два канала одного человека различает только источник",
    ).toBeTruthy();
  });

  it("канал без источника показан просто названием", async () => {
    нарисовать();
    await userEvent.click(await screen.findByRole("button", { name: /фильтры/i }));
    await userEvent.click(await screen.findByPlaceholderText(/канал/i));

    await waitFor(() => expect(screen.getByText("SMOKE-ACCOUNT")).toBeTruthy());
  });
});
