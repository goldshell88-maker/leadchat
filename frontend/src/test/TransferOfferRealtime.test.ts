import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { useActionBus } from "@/features/hotkeys/actionBus";
import { runAction } from "@/features/hotkeys/dispatch";
import { qk } from "@/shared/api/queryKeys";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { resyncNow } from "@/shared/realtime/quietResync";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { resetSessionStore } from "./helpers";

/**
 * Передача диалога у получателя (жалоба владельца 24.09 «когда передаёшь,
 * тебе диалог не приходит»): полоса «Принять» и строка во «Входящих» обязаны
 * появиться сразу, Ctrl+R — принять передачу, решение из уведомления —
 * исполниться один раз.
 */

const Я = { id: "me", full_name: "Я Получатель" } as const;

function assigned(offer: boolean | undefined) {
  applyWsEvent({
    type: "conversation:assigned",
    ts: new Date().toISOString(),
    data: {
      conversation_id: "conv-t",
      assignee: Я,
      assigned_by: { id: "u2", full_name: "Пётр Сидоров" },
      comment: null,
      is_for_you: true,
      ...(offer === undefined ? {} : { offer }),
    },
  });
}

describe("Кадр передачи у получателя", () => {
  let calls: ReturnType<typeof makeFakeTauriBridge>["calls"];

  beforeEach(() => {
    const fake = makeFakeTauriBridge();
    calls = fake.calls;
    enterTauriRuntime(fake.bridge);
    resetSessionStore({ user: { ...Я, role: "manager" } as never });
    queryClient.clear();
    // jsdom не умеет играть звук, а сигнал получателю здесь не проверяется.
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  });

  afterEach(() => {
    leaveTauriRuntime();
    queryClient.clear();
    vi.restoreAllMocks();
  });

  it("предложение перечитывает деталь и «Входящие» и даёт карточку с кнопками", () => {
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    assigned(true);

    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(qk.conversations.detail("conv-t")));
    expect(keys).toContain(JSON.stringify(qk.inbox.list));
    expect(calls.notify.mock.calls[0][0]).toMatchObject({
      title: "Диалог передан вам",
      requireInteraction: true,
      actions: [
        { action: "accept", title: "Принять" },
        { action: "decline", title: "Отклонить" },
      ],
    });
  });

  it("прямое назначение не предлагает «Принять», которое ничего бы не сделало", () => {
    assigned(false);

    const card = calls.notify.mock.calls[0][0];
    expect(card).toMatchObject({ title: "Вам назначили диалог", requireInteraction: false });
    expect(card.actions).toBeUndefined();
  });

  it("развязка предложения мне перечитывает «Входящие» и снимает ⚑", () => {
    const предложение = { to: Я, by: { id: "u2", full_name: "Пётр" }, at: null, comment: null };
    queryClient.setQueryData(qk.conversations.detail("conv-t"), {
      id: "conv-t",
      transfer: предложение,
    });
    queryClient.setQueryData(qk.inbox.list, {
      pages: [{ items: [{ id: "conv-t", transferred_to_me: true, transfer: предложение }] }],
      pageParams: [null],
    });
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    applyWsEvent({
      type: "conversation:updated",
      ts: new Date().toISOString(),
      data: {
        conversation_id: "conv-t",
        patch: { transfer: null, assignee: { id: "u2", full_name: "Пётр" } },
      },
    });

    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(qk.inbox.list));
    const строка = queryClient.getQueryData<{
      pages: Array<{ items: Array<{ transferred_to_me: boolean }> }>;
    }>(qk.inbox.list)?.pages[0].items[0];
    expect(строка?.transferred_to_me).toBe(false);
  });
});

describe("Ctrl+R на переданном мне диалоге", () => {
  beforeEach(() => {
    resetSessionStore({ user: { ...Я, role: "manager" } as never });
    queryClient.clear();
    useActionBus.setState({ transferDecision: null });
    useChatUiStore.setState({ activeConversationId: "conv-t" });
  });

  afterEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ activeConversationId: null });
  });

  it("принимает передачу, а не берёт первого из очереди", () => {
    queryClient.setQueryData(qk.conversations.detail("conv-t"), {
      id: "conv-t",
      in_inbox: false,
      assignee: { id: "u2" },
      transfer: { to: { id: Я.id } },
    });

    const handled = runAction("claim", { navigate: vi.fn(), rows: () => [], can: () => true });

    expect(handled).toBe(true);
    expect(useActionBus.getState().transferDecision).toMatchObject({
      convId: "conv-t",
      kind: "accept",
    });
  });
});

describe("Решение из уведомления исполняется один раз", () => {
  it("снятое с шины решение не достаётся следующей панели", () => {
    useActionBus.getState().requestTransferDecision("conv-t", "decline");
    const просьба = useActionBus.getState().transferDecision;

    useActionBus.getState().transferDecisionHandled(просьба);

    expect(useActionBus.getState().transferDecision).toBeNull();
  });

  it("чужая просьба не снимает текущую", () => {
    useActionBus.getState().requestTransferDecision("conv-t", "accept");
    const текущая = useActionBus.getState().transferDecision;

    useActionBus.getState().transferDecisionHandled({ convId: "x", kind: "accept", в: 0 });

    expect(useActionBus.getState().transferDecision).toBe(текущая);
  });
});

describe("Тихая сверка", () => {
  afterEach(() => useChatUiStore.setState({ activeConversationId: null }));

  it("перечитывает деталь открытого диалога", () => {
    useChatUiStore.setState({ activeConversationId: "conv-t" });
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    resyncNow(true);

    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(qk.conversations.detail("conv-t")));
    spy.mockRestore();
  });
});
