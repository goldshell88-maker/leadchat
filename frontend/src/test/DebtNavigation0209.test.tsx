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
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * НАВИГАЦИЯ ПО СВОЕЙ РАБОТЕ (жалоба владельца 02.09).
 *
 * «Нет интуитивно понятной навигации, сколько у меня диалогов взято, сколько
 * диалогов не отвечено… всё сливается с общим фоном».
 *
 * Замер боя того же дня: 27 диалогов, ждут ответа 11, и восемь из этих
 * одиннадцати уже прочитаны — то есть бейджа непрочитанного на них нет и от
 * сделанных они не отличались ничем.
 */

function строка(id: string, over: Partial<ConversationDto> = {}): ConversationDto {
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
    ...over,
  };
}

function страница(items: ConversationDto[]): ConversationsPage {
  return { items, page: { limit: 50, offset: 0, total: items.length } };
}

const оригВысота = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const оригШирина = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("сколько взято и сколько не отвечено", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const адреса = () => fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));

  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "mine" } });
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations/counts")) {
        return jsonResponse(200, { mine: 27, mine_waiting: 11 });
      }
      if (url.pathname.endsWith("/conversations")) {
        const только = url.searchParams.get("waiting_only") === "true";
        return jsonResponse(
          200,
          страница(
            только
              ? [строка("ждёт", { waiting_since: "2026-09-02T09:00:00Z" })]
              : [строка("ждёт", { waiting_since: "2026-09-02T09:00:00Z" }), строка("отвечен", { waiting_since: null })],
          ),
        );
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
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

  it("оба числа приходят от сервера, а не считаются по загруженным строкам", async () => {
    /*
     * ⚠ ЭТО ГЛАВНАЯ ПРОВЕРКА ФАЙЛА. В списке ДВЕ строки, а на экране обязаны
     * стоять 27 и 11: прежний бейдж «Моих» сняли ровно за то, что он показывал
     * число загруженных, и оно росло от прокрутки.
     */
    нарисовать();
    await waitFor(() => {
      expect(адреса().some((u) => u.includes("/conversations/counts"))).toBe(true);
    });
    expect(await screen.findByText("27")).toBeInTheDocument();
    const долг = await screen.findByRole("button", { name: /Ждут ответа: 11/ });
    expect(долг).toHaveTextContent("11");
  });

  it("нажатие на долг сужает список до ждущих", async () => {
    нарисовать();
    const долг = await screen.findByRole("button", { name: /Ждут ответа: 11/ });
    expect(долг).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(долг);

    await waitFor(() => {
      expect(адреса().some((u) => u.includes("waiting_only=true"))).toBe(true);
    });
    expect(useChatUiStore.getState().filters.waitingOnly).toBe(true);
    // Показатель считает МОЙ долг — значит и открыть обязан мои диалоги.
    expect(useChatUiStore.getState().filters.tab).toBe("mine");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /показаны только они/ })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    });
  });

  it("с «Всех» нажатие переводит на «Мои»: число про мой долг, и список тоже", async () => {
    /*
     * ⚠ ИНАЧЕ «НАЖАЛ НА 11 — УВИДЕЛ СОРОК». На вкладке «Все» ждут ответа
     * диалоги всей компании, а показатель считает `mine_waiting`. Число и то,
     * что открывается по нажатию, обязаны быть одним множеством.
     */
    useChatUiStore.setState({ filters: { tab: "all" } });
    нарисовать();
    await userEvent.click(await screen.findByRole("button", { name: /Ждут ответа: 11/ }));
    await waitFor(() => {
      const f = useChatUiStore.getState().filters;
      expect(f.tab).toBe("mine");
      expect(f.waitingOnly).toBe(true);
    });
  });

  it("повторное нажатие снимает сужение", async () => {
    нарисовать();
    const долг = await screen.findByRole("button", { name: /Ждут ответа: 11/ });
    await userEvent.click(долг);
    await waitFor(() => expect(useChatUiStore.getState().filters.waitingOnly).toBe(true));
    await userEvent.click(await screen.findByRole("button", { name: /Ждут ответа: 11/ }));
    await waitFor(() => expect(useChatUiStore.getState().filters.waitingOnly).toBeUndefined());
  });

  it("долга нет — чипа нет: пустая кнопка «ждут 0» звала бы в никуда", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations/counts")) {
        return jsonResponse(200, { mine: 27, mine_waiting: 0 });
      }
      return jsonResponse(200, страница([строка("отвечен", { waiting_since: null })]));
    });
    нарисовать();
    expect(await screen.findByText("27")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Ждут ответа/ })).not.toBeInTheDocument();
  });

  it("строка ждущего и строка отвеченного помечены по-разному", async () => {
    /*
     * ⚠ ПРОВОДКА, БЕЗ КОТОРОЙ ВСЁ ОСТАЛЬНОЕ ЗЕЛЕНЕЕТ ВПУСТУЮ. Предикат можно
     * написать, покрыть тестами и не повесить на строку ни разу — этот проект
     * на такой дыре попадался много раз.
     */
    const { container } = нарисовать();
    await screen.findByText("Клиент ждёт");
    const ждущая = container.querySelector('[data-awaiting]');
    expect(ждущая).not.toBeNull();
    expect(ждущая).not.toHaveAttribute("data-answered");

    const отвеченная = container.querySelector('[data-answered]');
    expect(отвеченная).not.toBeNull();
    expect(отвеченная).not.toHaveAttribute("data-awaiting");
  });

  it("стили состояний есть в таблице, а не только в разметке", async () => {
    // @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
    const { readFileSync } = await import("node:fs");
    const css = readFileSync(
      "src/features/chats/components/list/chat-list.css",
      "utf-8",
    ) as string;
    // Полоса у левого края — глаз начинает строку слева, а состояние жило
    // только в ячейке 48px у правого.
    expect(css).toContain('.conv-card[data-awaiting][data-urgency="none"]::before');
    expect(css).toContain(".conv-card[data-awaiting] .conv-card__name");
  });
});
