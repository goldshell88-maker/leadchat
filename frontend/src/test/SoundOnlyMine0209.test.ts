import { beforeEach, describe, expect, it, vi } from "vitest";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, resetSessionStore } from "./helpers";

const notify = vi.hoisted(() => ({ notifyNewMessage: vi.fn(), notifyAssignedToMe: vi.fn() }));
vi.mock("@/shared/realtime/notify", () => notify);

/**
 * ЗВУК ЗНАЧИТ «НУЖЕН ТЫ», А НЕ «ГДЕ-ТО ЧТО-ТО ПРОИЗОШЛО».
 *
 * ⚠ ОБРАТНАЯ СВЯЗЬ ДИСПЕТЧЕРА 02.09, дословно: «звук выключил, потому что он
 * режет сильно слух и 90% просто так оповещает, когда даже сообщений нет…
 * Без звука я не реагирую на сообщения, а звук не могу включить… крч не
 * работопригодно».
 *
 * ⚠ ЗАМЕР ПОДТВЕРДИЛ ЖАЛОБУ ПОЧТИ ДОСЛОВНО. За день на боевой: 521 входящее
 * сообщение и 81 постановка в очередь — около шестисот сигналов на каждого.
 * Звенело на ЛЮБОЕ входящее в ЛЮБОМ диалоге: тринадцать человек слышали каждое
 * сообщение всей компании по шестнадцати каналам. Своих среди них — меньше
 * десятой части.
 *
 * Это не про громкость и не про вкус: человек ВЫКЛЮЧИЛ звук и перестал видеть
 * сообщения. Шум сделал работу невозможной ровно так же, как сделала бы тишина.
 */
describe("Звук нового сообщения", () => {
  const я = fakeUser.id;
  const коллега = "99999999-9999-4999-8999-999999999999";

  function входящее(assignee_id: string | null) {
    return {
      type: "message:new" as const,
      ts: new Date().toISOString(),
      data: {
        conversation_id: "c-1",
        message: {
          id: "m-1",
          conversation_id: "c-1",
          direction: "in",
          sender_type: "client",
          body: "здравствуйте",
          attachments: [],
          delivery_status: "delivered",
          created_at: new Date().toISOString(),
        },
        conversation_patch: { unread_delta: 1, assignee_id },
      },
    };
  }

  beforeEach(() => {
    notify.notifyNewMessage.mockClear();
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
  });

  it("⚠ по МОЕМУ диалогу звенит", () => {
    applyWsEvent(входящее(я) as never);
    expect(
      notify.notifyNewMessage,
      "по своему диалогу звука нет — человек не узнает, что его клиент написал",
    ).toHaveBeenCalled();
  });

  it("⚠ по ЧУЖОМУ диалогу молчит", () => {
    applyWsEvent(входящее(коллега) as never);
    expect(
      notify.notifyNewMessage,
      "звенит по чужому диалогу — это и есть те самые «90% просто так»",
    ).not.toHaveBeenCalled();
  });

  it("по ничейному диалогу молчит: у очереди свой сигнал", () => {
    /*
     * Новый клиент в очереди звенит отдельно и дважды (`playInboxChime`), чтобы
     * его отличали не глядя. Второе сообщение того же ждущего клиента звонить
     * не должно: строка в очереди уже стоит и видна.
     */
    applyWsEvent(входящее(null) as never);
    expect(notify.notifyNewMessage).not.toHaveBeenCalled();
  });

  it("⚠ непрочитанное считается ВСЕГДА, даже когда молчим", () => {
    /*
     * Тишина не должна прятать работу: чужой диалог не звенит, но счётчик
     * растёт — видно, а не слышно. Спутай это — и правка «убрать шум»
     * превратилась бы в «спрятать поток», а руководитель перестал бы замечать
     * его вовсе.
     */
    useUnreadStore.getState().reset("c-1");
    applyWsEvent(входящее(коллега) as never);
    expect(
      useUnreadStore.getState().byConversation["c-1"]?.count,
      "по чужому диалогу пропал и счётчик — тишина превратилась в слепоту",
    ).toBe(1);
  });
});
