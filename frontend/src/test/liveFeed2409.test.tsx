import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { FeedPage } from "@/features/feed/FeedPage";
import { clearFeed, matchesFilters, recordWsFrame, useFeedStore } from "@/features/feed/store";
import { TraceSwitch } from "@/features/feed/TraceSwitch";
import type { WsInboxEvent } from "@/shared/realtime/applyWsEvent";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import type { WsServerEvent } from "@/shared/realtime/wsEvents";
import { showToast } from "@/shared/ui/toast";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * ЖИВАЯ ЛЕНТА — ПРОВЕРКА 24.09.
 *
 *  1. Отбор по каналу прятал строку «новое сообщение», которую сама лента
 *     подписывала этим каналом: `message:new` приходит раньше кадра очереди и
 *     канала не несёт.
 *  2. Пауза на пустой ленте и одно пришедшее событие давали «строки вытеснены
 *     новыми», хотя вытеснять было нечего.
 *  3. «Записывать час» и «Выключить» подробного следа молчали при отказе.
 */

const CONV = "conv-feed-2409";
const ACC = "acc-7";

const inboxNew: WsInboxEvent = {
  type: "inbox:new",
  ts: "2026-09-24T09:00:00.000Z",
  data: {
    conversation_id: CONV,
    conversation: {
      id: CONV,
      status: "new",
      channel: "avito",
      account: { id: ACC, title: "Парт-7" },
      client: { id: "cli-1", name: "Наталья", phone: null, avito_rating: null },
      assignee: null,
      item: null,
      last_message: null,
      unread_count: 1,
      bot_active: false,
      tags: [],
      transferred_to_me: false,
      last_message_at: "2026-09-24T09:00:00.000Z",
    },
    can_claim: true,
  },
};

const messageNew = {
  type: "message:new",
  ts: "2026-09-24T09:00:01.000Z",
  data: {
    conversation_id: CONV,
    message: {
      id: "msg-1",
      conversation_id: CONV,
      direction: "in",
      sender_type: "client",
      sender: null,
      body: "Здравствуйте",
      attachments: [],
      delivery_status: "delivered",
      created_at: "2026-09-24T09:00:01.000Z",
    },
    conversation_patch: { unread_delta: 1 },
  },
} as WsServerEvent;

beforeEach(() => {
  queryClient.clear();
  clearFeed();
  useConnectionStore.setState({ status: "open", lastEventAt: null });
  resetSessionStore({
    user: { ...fakeUser, role: "head" },
    permissions: ["conversations:read", "notes:read", "stats:own", "stats:all"],
    accessToken: "t",
    bootstrapped: true,
  });
});

afterEach(() => {
  clearFeed();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("Отбор ленты по каналу", () => {
  it("строка о сообщении, пришедшая раньше канала, под отбором этого канала видна", () => {
    recordWsFrame(messageNew);
    recordWsFrame(inboxNew);

    const underFilter = useFeedStore
      .getState()
      .entries.filter((e) => matchesFilters(e, { accountId: ACC, group: null }))
      .map((e) => e.text);
    expect(underFilter).toHaveLength(2);
    expect(underFilter).toContain("новое сообщение");
  });
});

describe("Пауза на пустой ленте", () => {
  it("одно пришедшее событие — «на паузе, 1 новое», а не «вытеснены»", () => {
    renderWithProviders(<FeedPage />, { route: "/feed" });
    act(() => useFeedStore.getState().pause());
    act(() => recordWsFrame(inboxNew));

    expect(screen.queryByText(/вытеснены новыми/)).toBeNull();
    expect(screen.getByText("Лента на паузе")).toBeInTheDocument();
    expect(screen.getByText(/За паузой 1 новое событие/)).toBeInTheDocument();
  });
});

describe("Подробный след", () => {
  it("отказ сервера назван, а не проглочен", async () => {
    const user = userEvent.setup();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["settings:manage", "stats:all"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
        init?.method === "PATCH"
          ? jsonResponse(503, errorEnvelope("service_unavailable", "Сервер перезапускается"))
          : jsonResponse(200, { enabled: false, seconds_left: 0 }),
      ),
    );
    renderWithProviders(<TraceSwitch />, { route: "/feed" });

    await user.click(await screen.findByRole("button", { name: /Записывать час/ }));

    await waitFor(() => expect(showToast).toHaveBeenCalled());
    expect(vi.mocked(showToast).mock.calls[0][0]).toMatchObject({
      title: "Не получилось: Включить подробный след",
      message: expect.stringContaining("Сервер перезапускается"),
    });
  });
});
