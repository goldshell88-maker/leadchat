import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { collectDialogsForCache, installCachePersist, warmupFromCache } from "@/platform/warmup";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";

/**
 * Мгновенный старт из кэша и запись серверных ответов обратно (04 §5.4):
 * снапшот рисуется до ответа API, но кладётся протухшим — первый же ответ
 * сервера его перезаписывает (кэш — проекция сервера, 04 §5.1).
 */

function row(id: string, overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: "Иван Петров", phone: null, avito_rating: null },
    assignee: null,
    item: null,
    last_message: null,
    unread_count: 2,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function listData(): InfiniteData<ConversationsPage> | undefined {
  return queryClient.getQueryData(qk.conversations.list(DEFAULT_FILTERS));
}

describe("Локальный кэш диалогов (04 §5.4)", () => {
  beforeEach(() => {
    queryClient.clear();
    useUnreadStore.getState().clear();
    useChatUiStore.setState({ filters: DEFAULT_FILTERS });
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
  });

  afterEach(() => {
    leaveTauriRuntime();
  });

  it("warmup рисует список до ответа сервера и засеивает счётчики", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    calls.warmup.mockResolvedValue({
      dialogs: [row("c1"), row("c2")],
      savedAt: "2026-08-05T09:59:00Z",
    });
    enterTauriRuntime(bridge);

    await expect(warmupFromCache()).resolves.toBe(true);

    expect(listData()?.pages[0].items.map((r) => r.id)).toEqual(["c1", "c2"]);
    expect(useUnreadStore.getState().byConversation["c1"].count).toBe(2);

    // Снапшот протух сразу: экран уходит за свежими данными, сервер прав.
    const state = queryClient.getQueryState(qk.conversations.list(DEFAULT_FILTERS));
    expect(state?.dataUpdatedAt).toBe(0);
  });

  it("если сервер уже ответил, снапшот не подставляется", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    calls.warmup.mockResolvedValue({ dialogs: [row("c1")], savedAt: null });
    enterTauriRuntime(bridge);
    queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
      pages: [{ items: [row("srv")], page: { limit: 1, offset: 0, total: 1 } }],
      pageParams: [0],
    });

    await expect(warmupFromCache()).resolves.toBe(false);
    expect(listData()?.pages[0].items[0].id).toBe("srv");
  });

  it("в вебе warmup — пустая операция", async () => {
    await expect(warmupFromCache()).resolves.toBe(false);
  });

  it("ответ сервера уходит в cache_apply_sync (топ-200 по last_message_at)", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    enterTauriRuntime(bridge);
    const stop = installCachePersist(0);

    queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
      pages: [
        {
          items: [row("c1", { last_message_at: "2026-08-05T10:00:00Z" }), row("c2", { last_message_at: "2026-08-05T11:00:00Z" })],
          page: { limit: 2, offset: 0, total: 2 },
        },
      ],
      pageParams: [0],
    });

    expect(collectDialogsForCache().map((r) => r.id)).toEqual(["c2", "c1"]); // свежие первыми
    await new Promise((r) => setTimeout(r, 5));
    expect(calls.persist).toHaveBeenCalledTimes(1);
    expect(calls.persist.mock.calls[0][0].dialogs.map((d) => d.id)).toEqual(["c2", "c1"]);

    stop();
  });
});
