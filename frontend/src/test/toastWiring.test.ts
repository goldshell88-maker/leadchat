import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useNotificationStore } from "@/features/notifications/store";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { notifyNewMessage } from "@/shared/realtime/notify";
import type { MessageDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { resetSessionStore } from "./helpers";

/**
 * Нативные тосты должны кто-то ЗВАТЬ (04 §4.1). Функции `toastForMessage` /
 * `toastForHandoff` жили без единого вызова — десктоп не показывал ни одного
 * уведомления. Тест фиксирует обе точки подключения в общем коде.
 */

function inbound(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: "m1",
    conversation_id: "conv-1",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "Здравствуйте, актуально?",
    attachments: [],
    created_at: new Date().toISOString(),
    delivery_status: "sent",
    delivery_error: null,
    client_message_id: null,
    ...overrides,
  } as MessageDto;
}

describe("Тосты подключены к потоку WS (04 §4.1)", () => {
  let calls: ReturnType<typeof makeFakeTauriBridge>["calls"];

  beforeEach(() => {
    const fake = makeFakeTauriBridge();
    calls = fake.calls;
    enterTauriRuntime(fake.bridge);
    resetSessionStore();
    useChatUiStore.setState({ soundEnabled: false }); // звук в jsdom не нужен
  });

  afterEach(() => {
    leaveTauriRuntime();
  });

  it("входящее от клиента доходит до моста тостом kind=message", () => {
    notifyNewMessage(inbound());

    expect(calls.notify).toHaveBeenCalledTimes(1);
    expect(calls.notify.mock.calls[0][0]).toMatchObject({
      conversationId: "conv-1",
      kind: "message",
      direction: "in",
      senderType: "client",
    });
  });

  it("сообщения бота и свои исходящие тостов не дают (04 §4.1)", () => {
    notifyNewMessage(inbound({ sender_type: "bot" }));
    notifyNewMessage(inbound({ direction: "out", sender_type: "operator" }));

    expect(calls.notify).not.toHaveBeenCalled();
  });

  it("«диалог передан вам» даёт приоритетный тост kind=handoff", () => {
    applyWsEvent({
      type: "conversation:assigned",
      ts: new Date().toISOString(),
      data: {
        conversation_id: "conv-1",
        assignee: { id: "u1", full_name: "Я" },
        assigned_by: { id: "u2", full_name: "Пётр Сидоров" },
        comment: null,
        is_for_you: true,
      },
    });

    expect(calls.notify).toHaveBeenCalledTimes(1);
    expect(calls.notify.mock.calls[0][0]).toMatchObject({
      conversationId: "conv-1",
      kind: "handoff",
      isForYou: true,
    });
  });

  it("передача НЕ мне тоста не даёт", () => {
    applyWsEvent({
      type: "conversation:assigned",
      ts: new Date().toISOString(),
      data: {
        conversation_id: "conv-1",
        assignee: { id: "u3", full_name: "Коллега" },
        assigned_by: { id: "u2", full_name: "Пётр Сидоров" },
        comment: null,
        is_for_you: false,
      },
    });

    expect(calls.notify).not.toHaveBeenCalled();
  });

  /**
   * 14 §4: «при открытом десктоп-клиенте уведомление превращается в нативное
   * окно Windows». Проверяем именно критичное — обычное остаётся тостом внутри
   * приложения и ядро Windows не дёргает.
   */
  describe("Критичное уведомление центра → нативное окно (14 §4)", () => {
    beforeEach(() => {
      // Уведомления приходят ролям с `conversations:manage`; рассылка admin —
      // по `users:manage` (services/notifications.AUDIENCE_PERMISSION).
      resetSessionStore({ permissions: ["conversations:manage", "users:manage", "audit:read"] });
      useNotificationStore.getState().clear();
    });

    afterEach(() => {
      useNotificationStore.getState().clear();
    });

    it("критичное системное событие уходит в мост kind=system и без диалога", () => {
      applyWsEvent({
        type: "notify",
        ts: new Date().toISOString(),
        data: {
          id: "n-1",
          level: "error",
          severity: "critical",
          kind: "disk.space",
          title: "На диске мало места",
          text: "Свободно 4%",
          audience_hint: "admin",
        },
      });

      expect(calls.notify).toHaveBeenCalledTimes(1);
      const req = calls.notify.mock.calls[0][0];
      expect(req).toMatchObject({ kind: "system", title: "На диске мало места" });
      // Диалога у серверного события нет — клик по тосту никуда не ведёт.
      expect(req.conversationId).toBeUndefined();
    });

    it("связанный диалог попадает в тост — по клику открывается переписка", () => {
      applyWsEvent({
        type: "notify",
        ts: new Date().toISOString(),
        data: {
          id: "n-2",
          level: "error",
          severity: "critical",
          kind: "conversation.negative",
          title: "Клиент недоволен",
          text: "«верните деньги»",
          entity: { type: "conversation", id: "conv-9" },
        },
      });

      expect(calls.notify.mock.calls[0][0]).toMatchObject({
        kind: "system",
        conversationId: "conv-9",
      });
    });

    it("некритичное нативного окна не открывает — только тост внутри приложения", () => {
      applyWsEvent({
        type: "notify",
        ts: new Date().toISOString(),
        data: {
          id: "n-3",
          level: "warning",
          severity: "warning",
          kind: "conversation.no_reply",
          title: "Диалог без ответа",
          text: "40 минут",
        },
      });

      expect(calls.notify).not.toHaveBeenCalled();
    });
  });
});
