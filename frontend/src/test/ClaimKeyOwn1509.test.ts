/**
 * Клавиша приёма и «уже мой» диалог (замер боя 15.09 + ревью).
 *
 * Второе нажатие по привычке на своём диалоге давало 409 «вы уже приняли».
 * Признак «мой» — только `assignee`; слово сервера `in_inbox` перевешивает
 * всё: диалог, вернувшийся в очередь, принимается клавишей, а не уводит к
 * постороннему (ревью 15.09: устаревший `claimed_by` в кэше).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { runAction } from "@/features/hotkeys/dispatch";
import { useActionBus } from "@/features/hotkeys/actionBus";
import { qk } from "@/shared/api/queryKeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeUser, resetSessionStore } from "./helpers";

const взятьПервого = vi.fn(async () => undefined);
vi.mock("@/features/chats/inbox/claimFromQueue", () => ({
  claimFromQueue: vi.fn(async () => undefined),
  взятьПервогоССервера: () => взятьПервого(),
}));

const ctx = { navigate: vi.fn(), rows: () => [], can: () => true };

describe("Клавиша приёма на открытом диалоге", () => {
  beforeEach(() => {
    queryClient.clear();
    взятьПервого.mockClear();
    useInboxStore.setState({ ids: {} } as never);
    useChatUiStore.setState({ activeConversationId: "d", drafts: {} } as never);
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
  });

  it("свой диалог (assignee — я) без слова сервера — принятие не зовётся", () => {
    queryClient.setQueryData(qk.conversations.detail("d"), {
      id: "d",
      assignee: fakeUser,
    });
    useInboxStore.setState({ ids: { d: true } } as never);
    const было = useActionBus.getState().claimNonce;
    runAction("claim", ctx);
    expect(useActionBus.getState().claimNonce).toBe(было);
    expect(взятьПервого).toHaveBeenCalledTimes(1); // поле пустое — к следующему
  });

  it("вернулся в очередь (in_inbox от сервера) — принимается открытый, не посторонний", () => {
    queryClient.setQueryData(qk.conversations.detail("d"), {
      id: "d",
      in_inbox: true,
      assignee: null,
      claimed_by: fakeUser,
    });
    const было = useActionBus.getState().claimNonce;
    runAction("claim", ctx);
    expect(useActionBus.getState().claimNonce).toBe(было + 1);
    expect(взятьПервого).not.toHaveBeenCalled();
  });

  it("чужой в очереди по стору — принимается", () => {
    queryClient.setQueryData(qk.conversations.detail("d"), {
      id: "d",
      assignee: null,
    });
    useInboxStore.setState({ ids: { d: true } } as never);
    const было = useActionBus.getState().claimNonce;
    runAction("claim", ctx);
    expect(useActionBus.getState().claimNonce).toBe(было + 1);
  });
});
