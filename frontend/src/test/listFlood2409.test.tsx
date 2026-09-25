/**
 * ЛИСТАНИЕ СТРЕЛКАМИ НЕ ВЫБИВАЕТ ЛИЧНУЮ ПЛАНКУ NGINX (проверка 24.09).
 *
 * Каждый шаг Ctrl+↓ открывал диалог целиком — около девяти запросов, — а
 * зажатая стрелка давала до тридцати шагов в секунду. Планка nginx (30 запросов
 * в секунду на человека, запас 60) кончалась за секунды: 5 583 отказа 429 за
 * 08.09–24.09, на диалоге, где человек остановился, — «Диалог недоступен».
 *
 * Стережём две половины починки:
 *   1. зажатая стрелка шагает не чаще раза в 150 мс, лишний автоповтор гасится;
 *   2. пока человек листает, отметка прочтения, история клиента и подсказки
 *      объединения ждут, а после остановки уходят сами.
 */
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen } from "@testing-library/react";
import { useLocation } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { markKeyboardStep, STEP_SETTLE_MS } from "@/features/chats/keyboardStepping";
import { useChatHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

function row(id: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `cl-${id}`, name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: null,
    unread_count: 2,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-24T10:00:00Z",
    in_inbox: false,
    offered_at: null,
  };
}

function seedList(ids: string[]): void {
  queryClient.setQueryData(qk.conversations.list(useChatUiStore.getState().filters), {
    pages: [{ items: ids.map(row), page: { limit: 50, offset: 0, total: ids.length } }],
    pageParams: [0],
  });
}

function seedThread(id: string): void {
  queryClient.setQueryData(qk.conversations.detail(id), row(id));
  queryClient.setQueryData(qk.messages.list(id), {
    pages: [
      {
        items: [],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

function LocationProbe() {
  const { pathname } = useLocation();
  useEffect(() => {
    const id = pathname.startsWith("/chats/") ? pathname.slice("/chats/".length) : null;
    useChatUiStore.getState().setActive(id || null);
  }, [pathname]);
  return <div data-testid="address">{pathname}</div>;
}

function HotkeyHost() {
  useChatHotkeys();
  return null;
}

let requests: string[] = [];

async function wait(ms: number): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

function ctrlDown(repeat: boolean): boolean {
  return fireEvent.keyDown(document.body, { key: "ArrowDown", code: "ArrowDown", ctrlKey: true, repeat });
}

describe("Листание не выбивает планку nginx", () => {
  beforeEach(() => {
    queryClient.clear();
    requests = [];
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, inboxOpen: false, drafts: {} });
    useInboxStore.setState({ count: 0, escalated: 0, ids: {} });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        requests.push(String(url));
        return new Promise(() => {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("зажатая Ctrl+↓ шагает не чаще раза в 150 мс, лишний автоповтор гасится", async () => {
    seedList(["m-1", "m-2", "m-3", "m-4", "m-5"]);
    seedThread("m-1");
    useChatUiStore.setState({ activeConversationId: "m-1" });
    renderWithProviders(
      <>
        <HotkeyHost />
        <LocationProbe />
      </>,
      { route: "/chats/m-1" },
    );

    ctrlDown(false);
    const swallowed = [ctrlDown(true), ctrlDown(true), ctrlDown(true)];
    expect(screen.getByTestId("address")).toHaveTextContent("/chats/m-2");
    // `false` — событие отменено: браузеру стрелка тоже не досталась.
    expect(swallowed).toEqual([false, false, false]);

    await wait(160);
    ctrlDown(true);
    expect(screen.getByTestId("address")).toHaveTextContent("/chats/m-3");
  });

  it("отдельные нажатия не притормаживаются", () => {
    seedList(["m-1", "m-2", "m-3", "m-4"]);
    seedThread("m-1");
    useChatUiStore.setState({ activeConversationId: "m-1" });
    renderWithProviders(
      <>
        <HotkeyHost />
        <LocationProbe />
      </>,
      { route: "/chats/m-1" },
    );

    ctrlDown(false);
    ctrlDown(false);
    ctrlDown(false);
    expect(screen.getByTestId("address")).toHaveTextContent("/chats/m-4");
  });

  it("пролистанный диалог: прочтение, история и подсказки ждут остановки", async () => {
    seedList(["m-1"]);
    seedThread("m-1");
    useChatUiStore.setState({ activeConversationId: "m-1" });
    markKeyboardStep();
    renderWithProviders(
      <>
        <ChatThreadPane convId="m-1" />
        <ClientCardPane convId="m-1" />
      </>,
      { route: "/chats/m-1" },
    );
    await wait(0);

    const secondary = () =>
      requests.filter((u) => /\/read$|client-history|merge-candidates/.test(u)).map((u) => u.replace(/^.*\/api\/v1/, ""));
    expect(secondary()).toEqual([]);

    await wait(STEP_SETTLE_MS + 50);
    expect(secondary().sort()).toEqual([
      "/clients/cl-m-1/merge-candidates",
      "/conversations/m-1/client-history",
      "/conversations/m-1/read",
    ]);
  });

  it("открытый мышью диалог отмечается прочитанным сразу", async () => {
    seedList(["m-1"]);
    seedThread("m-1");
    useChatUiStore.setState({ activeConversationId: "m-1" });
    await wait(STEP_SETTLE_MS + 10);
    renderWithProviders(<ChatThreadPane convId="m-1" />, { route: "/chats/m-1" });
    await wait(0);

    expect(requests.some((u) => u.endsWith("/conversations/m-1/read"))).toBe(true);
  });
});
