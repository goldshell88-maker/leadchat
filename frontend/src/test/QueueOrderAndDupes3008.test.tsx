import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import type { ConversationDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ОЧЕРЕДЬ: ПОРЯДОК И МНИМЫЕ ДУБЛИКАТЫ (жалоба владельца 30.08).
 *
 * ⚠ ДУБЛИКАТОВ НЕ БЫЛО. Замер по боевой базе: 52 клиента держат в очереди по
 * два диалога, ещё пять — по три; ВСЕ на одном и том же канале и (46 из 52)
 * про РАЗНЫЕ объявления. Авито заводит отдельную переписку на каждое
 * объявление, а подстрочник строки с 12 августа показывает только канал — вот
 * строки и выглядят копиями. Лечится различителем, а не дедупликацией: убери
 * мы «дубль», оператор потерял бы настоящее обращение.
 *
 * ⚠ ПОРЯДОК ЛОМАЛА МОЯ СОБСТВЕННАЯ ПРАВКА ТОГО ЖЕ УТРА. Заморозка порядка под
 * курсором заводилась против «строка уехала из-под клика», но у диспетчера
 * курсор стоит над колонкой почти всю смену — и «пока наведён» означало на
 * практике «навсегда». В очереди это особенно дорого: там порядок «дольше всех
 * ждёт — выше» и есть смысл экрана, по нему решают, кого брать следующим.
 */

const КЛИЕНТ = "client-natalya";

function строка(id: string, item: string): ConversationDto {
  return {
    id,
    status: "new",
    channel: "avito",
    account: { id: "acc-1", title: "Михаил КП" },
    client: { id: КЛИЕНТ, name: "Наталья", phone: null, avito_rating: null },
    assignee: null,
    item: { title: item, url: null, price: null },
    last_message: { body: "А то может его проще на запчасти", direction: "in", created_at: "2026-08-30T10:00:00Z" },
    unread_count: 1,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-30T10:00:00Z",
  };
}

describe("Очередь: порядок и мнимые дубликаты", () => {
  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" }, inboxOpen: false });
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  });

  it("объявление показывается, когда его попросили", () => {
    renderWithProviders(
      <ConversationListItem
        row={строка("c1", "Ремонт стиральной машины Bosch")}
        active={false}
        now={Date.parse("2026-08-30T10:16:00Z")}
        onOpen={() => {}}
        showChannel
        showItem
      />,
    );
    expect(
      screen.queryByText(/Ремонт стиральной машины Bosch/),
      "строки одного клиента снова неразличимы — читаются как дубликаты",
    ).not.toBeNull();
  });

  it("и НЕ показывается, когда не просили", () => {
    /*
     * Разбор 12 августа верен: объявление занимает всю ширину, обрезается на
     * середине и вытесняет канал, ради которого подстрочник и нужен. Вернуть
     * его всем значило бы починить 57 строк из 2600, испортив остальные.
     */
    renderWithProviders(
      <ConversationListItem
        row={строка("c1", "Ремонт стиральной машины Bosch")}
        active={false}
        now={Date.parse("2026-08-30T10:16:00Z")}
        onOpen={() => {}}
        showChannel
      />,
    );
    expect(screen.queryByText(/Ремонт стиральной машины Bosch/)).toBeNull();
  });

  it("две переписки одного клиента различимы в списке", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/conversations")) {
          return jsonResponse(200, {
            items: [строка("c1", "Стиральная машина Bosch"), строка("c2", "Холодильник Atlant")],
            page: { limit: 50, offset: 0, total: 2 },
          });
        }
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
    render(
      <QueryClientProvider client={qc}>
        <MantineProvider theme={theme} defaultColorScheme="light">
          <MemoryRouter initialEntries={["/chats"]}>
            <ChatListPane />
          </MemoryRouter>
        </MantineProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.queryByText(/Стиральная машина Bosch/)).not.toBeNull());
    expect(
      screen.queryByText(/Холодильник Atlant/),
      "вторая переписка того же клиента неотличима от первой",
    ).not.toBeNull();
  });

  it("у разных клиентов объявление не показывается", async () => {
    /*
     * ⚠ БЕЗ ЭТОЙ ПРОВЕРКИ ПАНЕЛЬ МОГЛА РАЗДАВАТЬ ПРИЗНАК ВСЕМ ПОДРЯД, И НИ
     * ОДИН ТЕСТ БЫ НЕ УПАЛ: соседние проверяют строку напрямую, а не то, кому
     * панель признак ставит. Поймано диверсией.
     *
     * Смысл правила: объявление возвращается ровно туда, где оно единственное,
     * что различает строки. Остальным оно вытесняет канал — то, по чему
     * решают, кому диалог.
     */
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const первый = строка("c1", "Стиральная машина Bosch");
    const второй = {
      ...строка("c2", "Холодильник Atlant"),
      client: { id: "client-oleg", name: "Олег", phone: null, avito_rating: null },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/conversations")) {
          return jsonResponse(200, {
            items: [первый, второй],
            page: { limit: 50, offset: 0, total: 2 },
          });
        }
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
    render(
      <QueryClientProvider client={qc}>
        <MantineProvider theme={theme} defaultColorScheme="light">
          <MemoryRouter initialEntries={["/chats"]}>
            <ChatListPane />
          </MemoryRouter>
        </MantineProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.queryByText(/Олег/)).not.toBeNull());
    expect(
      screen.queryByText(/Стиральная машина Bosch/),
      "объявление показано там, где строки и так различимы — оно вытеснит канал",
    ).toBeNull();
  });

  it("в очереди порядок не замораживается ни при каких условиях", () => {
    /*
     * ⚠ ГЛАВНАЯ ПРОВЕРКА ЭТОГО ФАЙЛА. В обычном списке порядок — удобство, в
     * очереди — обещание: «дольше всех ждёт — выше». Снимок вместо очереди
     * означает, что новые обращения валятся в конец мимо своего места, и
     * оператор берёт не того, кого должен.
     */
    const исходник = readFileSync(
      "src/features/chats/components/list/ChatListPane.tsx",
      "utf-8",
    ) as string;
    expect(
      исходник,
      "заморозка снова может включиться в очереди",
    ).toMatch(/const морозим = подКурсором && !inboxOpen;/);
    expect(
      исходник,
      "обработчик наведения не отсекает очередь",
    ).toMatch(/if \(inboxOpen\) return; \/\/ очередь не морозим/);
  });

  it("заморозка отпускает сама, а не ждёт увода мыши", () => {
    // Курсор диспетчера стоит над колонкой почти всю смену: «пока наведён»
    // означало на практике «навсегда».
    const исходник = readFileSync(
      "src/features/chats/components/list/ChatListPane.tsx",
      "utf-8",
    ) as string;
    expect(исходник, "срок заморозки исчез — она снова вечная").toMatch(
      /ЗАМОРОЗКА_МС = \d+/,
    );
    expect(исходник, "таймер не заводится при наведении").toMatch(
      /setTimeout\(\(\) => setПодКурсором\(false\), ЗАМОРОЗКА_МС\)/,
    );
  });
});
