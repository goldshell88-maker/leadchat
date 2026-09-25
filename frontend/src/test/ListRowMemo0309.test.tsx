import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * СТРОКА СПИСКА ПЕРЕРИСОВЫВАЕТСЯ ТОЛЬКО КОГДА ИЗМЕНИЛАСЬ САМА (03.09).
 *
 * `ChatListPane` держит десятки единиц состояния — наведение, черновик поиска,
 * открытые меню, тик часов. Пока `ConversationListItem` не был мемоизирован,
 * каждое их изменение перерисовывало все видимые строки: два десятка
 * компонентов по четыреста строк разметки. Человек это чувствует как заминку
 * прокрутки и запаздывание нажатия — ровно то, на что жаловался владелец
 * («всё должно работать мгновенно»).
 *
 * ⚠ ФАЙЛ СТОРОЖИТ ОБЕ ПОЛОВИНЫ, И ВТОРАЯ ВАЖНЕЕ ПЕРВОЙ. `memo` без стабильной
 * ссылки на обработчик не делает НИЧЕГО: родитель передавал стрелку, созданную
 * прямо в разметке, то есть новую на каждый рендер. Обёртка стояла бы, а
 * работы не делала — самый частый дефект этого проекта.
 */

const записанные: Array<Record<string, unknown>> = [];

vi.mock("@/features/chats/components/list/ConversationListItem", () => ({
  ConversationListItem: (props: Record<string, unknown>) => {
    записанные.push(props);
    return <div data-testid="строка">{String((props.row as ConversationDto).id)}</div>;
  },
}));

const { ChatListPane } = await import("@/features/chats/components/list/ChatListPane");

function строка(id: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `cl-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт iPhone 13", url: null, price: null },
    last_message: { body: "текст", direction: "in", created_at: "2026-09-02T09:40:12Z" },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-02T09:40:12Z",
  };
}

const страница = (items: ConversationDto[]): ConversationsPage => ({
  items,
  page: { limit: 50, offset: 0, total: items.length },
});

const оригВысота = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const оригШирина = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("перерисовка строк списка", () => {
  beforeEach(() => {
    записанные.length = 0;
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "mine" } });
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
        if (url.pathname.endsWith("/conversations/counts")) {
          return jsonResponse(200, { mine: 2, mine_waiting: 0 });
        }
        if (url.pathname.endsWith("/conversations")) {
          return jsonResponse(200, страница([строка("a"), строка("b")]));
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

  it("ссылка на обработчик открытия не меняется от постороннего состояния", async () => {
    /*
     * ⚠ ГЛАВНАЯ ПРОВЕРКА ФАЙЛА. Набор текста в поиске — состояние родителя, к
     * строкам отношения не имеющее. Если `onOpen` при этом станет другой
     * функцией, `memo` на строке перестанет держать что бы то ни было, и
     * обёртка останется декорацией.
     */
    нарисовать();
    await waitFor(() => expect(screen.getAllByTestId("строка")).toHaveLength(2));
    const доНабора = записанные.at(-1)?.onOpen;
    expect(доНабора, "строки не отрисовались").toBeTypeOf("function");

    const поиск = screen.getByPlaceholderText(/поиск/i);
    await userEvent.type(поиск, "оптика");

    await waitFor(() => expect(записанные.length).toBeGreaterThan(2));
    expect(
      записанные.at(-1)?.onOpen,
      "обработчик пересоздан на постороннем наборе текста — мемоизация строки обесценена",
    ).toBe(доНабора);
  });

  it("строка списка обёрнута в memo", async () => {
    /*
     * Проверка структурная намеренно, и это НЕ греп по тексту исходника:
     * читается значение самого экспорта в рантайме. Снимут `memo` — здесь
     * упадёт, чем бы файл при этом ни выглядел.
     */
    const модуль = await vi.importActual<
      typeof import("@/features/chats/components/list/ConversationListItem")
    >("@/features/chats/components/list/ConversationListItem");
    expect(
      (модуль.ConversationListItem as unknown as { $$typeof?: symbol }).$$typeof,
      "строка списка не мемоизирована — любое состояние панели перерисовывает все видимые",
    ).toBe(Symbol.for("react.memo"));
  });
});
