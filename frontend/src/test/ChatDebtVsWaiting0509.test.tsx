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
 * ГРАНИЦА МЕЖДУ ПИЛЮЛЕЙ ДОЛГА И НОВЫМ ПОЛЕМ «ЖДУТ ОТВЕТА».
 *
 * У признака `waitingOnly` теперь два входа, и это НЕ два способа сделать одно
 * и то же — множества разные:
 *
 *  · пилюля показывает МОЙ долг (`mine_waiting`) и вместе с сужением переводит
 *    на «Мои»; сервер в этом случае берёт ожидание И «мои» одной связкой;
 *  · поле на вкладке «Все» значит «кого-то заждались» по всей компании — там
 *    ни счётчика, ни обещания сходимости нет.
 *
 * Что стережётся: на «Моих» срез показан ровно ОДИН раз (пилюлей, без чипа), а
 * вне «Моих» пилюля не притворяется нажатой — её число про мой долг, и «нажал
 * на 3 — увидел 40» разрушило бы доверие сразу к обоим.
 */

function строка(id: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "Стас КП" },
    client: { id: `cl-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: { body: "текст", direction: "in", created_at: "2026-09-05T09:00:00Z" },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-05T09:00:00Z",
  };
}

function нарисовать(filters: ConversationFilters) {
  resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
  useChatUiStore.setState({ activeConversationId: null, filters, inboxOpen: false });

  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/conversations/counts")) {
      return jsonResponse(200, { mine: 27, mine_waiting: 3 });
    }
    if (url.pathname.endsWith("/conversations")) {
      return jsonResponse(200, {
        items: [строка("c-1")],
        page: { limit: 50, offset: 0, total: 1 },
      });
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

describe("Пилюля долга и поле «ждут ответа»", () => {
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

  /**
   * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА С ПОДОБРАННЫМИ ДАННЫМИ: строка чипов на экране
   * ЕСТЬ (стоит канал), значит «чипа „Ждут ответа“ нет» означает именно его
   * отсутствие. Заведи мы чип и здесь — он встал бы рядом с каналом, и
   * проверка покраснела бы на нём, а не на пустом экране.
   */
  it("на «Моих» срез показан пилюлей и НЕ продублирован чипом", async () => {
    нарисовать({ tab: "mine", waitingOnly: true, accountId: "acc-1" });

    const пилюля = await screen.findByRole("button", { name: /Ждут ответа: 3/ });
    expect(пилюля).toHaveAttribute("aria-pressed", "true");

    expect(screen.getByText("Канал: Стас КП")).toBeInTheDocument();
    expect(screen.queryByText("Ждут ответа", { selector: ".chat-list-pane__chip-label" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Снять сужение «ждут ответа»" })).toBeNull();
  });

  it("на «Всех» пилюля не нажата — её число про мой долг, а список про общий", async () => {
    нарисовать({ tab: "all", waitingOnly: true });

    const пилюля = await screen.findByRole("button", { name: /Ждут ответа: 3/ });
    // Горела бы «показаны только они» — и врала бы: показаны диалоги всей
    // компании, а число рядом считает мои три.
    expect(пилюля).toHaveAttribute("aria-pressed", "false");
    expect(пилюля.getAttribute("aria-label")).not.toContain("показаны только они");
  });

  it("вне «Моих» срез называет и снимает чип — иначе снять его нечем", async () => {
    const user = userEvent.setup();
    нарисовать({ tab: "all", waitingOnly: true });

    expect(
      await screen.findByText("Ждут ответа", { selector: ".chat-list-pane__chip-label" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Снять сужение «ждут ответа»" }));
    expect(useChatUiStore.getState().filters.waitingOnly).toBeUndefined();
  });

  it("нажатие пилюли с «Всех» по-прежнему уводит на «Мои» с её срезом", async () => {
    const user = userEvent.setup();
    нарисовать({ tab: "all" });

    await user.click(await screen.findByRole("button", { name: /Ждут ответа: 3/ }));

    const f = useChatUiStore.getState().filters;
    expect({ tab: f.tab, waitingOnly: f.waitingOnly }).toEqual({ tab: "mine", waitingOnly: true });
  });
});

/**
 * ПУСТО ПОД СРЕЗОМ «ЖДУТ ОТВЕТА» — ЭТО ЦЕЛЬ, А НЕ ОТСУТСТВИЕ РАБОТЫ.
 *
 * Правка 02.09 была половинчатой: заметили, что пилюля исчезнет вместе с
 * последним ждущим диалогом, и оставили её видимой при включённом сужении, — а
 * экран под ней разбирать не стали. Диспетчер с двадцатью семью взятыми
 * диалогами отвечал последнему ждущему и читал «У вас пока нет диалогов».
 */
describe("Пусто под срезом «ждут ответа»", () => {
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

  function пусто(filters: ConversationFilters) {
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null, filters, inboxOpen: false });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/conversations/counts")) {
          return jsonResponse(200, { mine: 27, mine_waiting: 0 });
        }
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
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

  it("на «Моих» экран не выдаёт срез за отсутствие работы", async () => {
    пусто({ tab: "mine", waitingOnly: true });

    expect(await screen.findByText("Никто не ждёт ответа")).toBeInTheDocument();
    // Двадцать семь взятых диалогов никуда не делись — их скрывает срез.
    expect(screen.queryByText("У вас пока нет диалогов")).toBeNull();
  });

  it("выход одной кнопкой: она снимает именно срез", async () => {
    const user = userEvent.setup();
    пусто({ tab: "mine", waitingOnly: true });

    await user.click(await screen.findByRole("button", { name: "Показать остальные" }));
    expect(useChatUiStore.getState().filters.waitingOnly).toBeUndefined();
  });

  it("на «Всех» срез назван раньше безымянного «ничего не подходит»", async () => {
    /*
     * Здесь сужение ЕСТЬ и попадает в общий счёт (на «Всех» у него свой чип),
     * то есть безымянная ветка перехватила бы его, будь порядок другим.
     */
    пусто({ tab: "all", waitingOnly: true });

    expect(await screen.findByText("Никто не ждёт ответа")).toBeInTheDocument();
    expect(screen.queryByText("Ничего не подходит под фильтры")).toBeNull();
  });
});
