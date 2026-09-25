/**
 * Кнопка «Следующий (N)» обещает ровно то, что может дать.
 *
 * ЧТО БЫЛО. Число бралось из общего размера очереди (`inboxStore.count`), и
 * открытый диалог в него входил. При четырёх ждущих, один из которых открыт,
 * кнопка обещала «Следующий (4)» вместо трёх. На границе выходило хуже: в
 * очереди один диалог, он же открыт — кнопка показывала «Следующий (1)»,
 * `nextInboxId` возвращал `null`, и нажатие уводило на пустой экран.
 *
 * Правильный расчёт (`useInboxAhead`) был написан вместе с этим доводом и всё
 * это время не был подключён ни к чему: его держал только собственный тест.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessagesPage } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders } from "./render";

function строка(id: string): ConversationDto {
  return {
    id,
    status: "new",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `cl-${id}`, name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: null,
    item: null,
    last_message: null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-23T10:00:00Z",
    in_inbox: true,
    offered_at: "2026-08-23T09:00:00Z",
  };
}

/** Очередь в сторе и её строки в кэше — как после `GET /inbox`. */
function очередь(ids: string[], count = ids.length) {
  useInboxStore.setState({
    count,
    escalated: 0,
    ids: Object.fromEntries(ids.map((id) => [id, true])),
  });
  queryClient.setQueryData(qk.inbox.list, {
    pages: [{ items: ids.map(строка), page: { limit: ids.length, offset: 0, total: count } }],
    pageParams: [0],
  });
}

function открыть(convId: string) {
  queryClient.setQueryData(qk.messages.list(convId), {
    pages: [
      {
        items: [],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
  queryClient.setQueryData(qk.conversations.list(useChatUiStore.getState().filters), {
    pages: [{ items: [строка(convId)], page: { limit: 1, offset: 0, total: 1 } }],
    pageParams: [0],
  });
  return renderWithProviders(<ChatThreadPane convId={convId} />);
}

describe("Кнопка «Следующий» в шапке ленты", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ inboxOpen: true });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("открытый диалог в обещание не входит: в очереди четверо, один открыт — обещаем троих", () => {
    очередь(["q-1", CONV_ID, "q-3", "q-4"]);
    открыть(CONV_ID);
    expect(screen.getByRole("button", { name: "Следующий (3)" })).toBeInTheDocument();
  });

  it("в очереди один диалог, он же открыт — кнопки нет вовсе", () => {
    очередь([CONV_ID]);
    открыть(CONV_ID);
    // Раньше здесь стояло «Следующий (1)», а нажатие уводило на пустой экран.
    expect(screen.queryByRole("button", { name: /Следующий/ })).toBeNull();
  });

  it("открыт диалог не из очереди — обещаем всю очередь", () => {
    очередь(["q-1", "q-2"]);
    открыть(CONV_ID);
    expect(screen.getByRole("button", { name: "Следующий (2)" })).toBeInTheDocument();
  });

  it("число берётся из счётчика сервера, а не из загруженной страницы", () => {
    // Первая страница из двух, в очереди сто двадцать: обещаем 119, а не 1.
    очередь(["q-1", CONV_ID], 120);
    открыть(CONV_ID);
    expect(screen.getByRole("button", { name: "Следующий (119)" })).toBeInTheDocument();
  });
});
