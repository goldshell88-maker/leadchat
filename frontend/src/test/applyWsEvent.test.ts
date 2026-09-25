import { useSessionStore } from "@/shared/stores/sessionStore";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type {
  ConversationDetailDto,
  ConversationDto,
  ConversationsPage,
  MessageDto,
  MessagesPage,
} from "@/shared/api/types";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { notifyNewMessage } from "@/shared/realtime/notify";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";

vi.mock("@/shared/realtime/notify", () => ({ notifyNewMessage: vi.fn() }));

const FILTERS = { tab: "all" } as const;

function makeConv(id: string, overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: "Иван Петров", phone: null, avito_rating: null },
    assignee: null,
    item: { title: "Ремонт iPhone 13", url: null, price: null },
    last_message: { body: "старое", direction: "in", created_at: "2026-08-04T09:00:00Z" },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-04T09:00:00Z",
    ...overrides,
  };
}

function makeMsg(id: string, convId: string, overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id,
    conversation_id: convId,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "А сколько будет стоить замена экрана?",
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-08-04T10:00:00Z",
    ...overrides,
  };
}

function seedList(rows: ConversationDto[]) {
  queryClient.setQueryData<InfiniteData<ConversationsPage>>(qk.conversations.list(FILTERS), {
    pages: [{ items: rows, page: { limit: 50, offset: 0, total: rows.length } }],
    pageParams: [0],
  });
}

function seedMessages(convId: string, items: MessageDto[]) {
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId), {
    pages: [
      {
        items,
        page: { prev_cursor: "prev", next_cursor: "next", has_more_before: false, has_more_after: false },
      },
    ],
    pageParams: [null],
  });
}

function listRows(): ConversationDto[] {
  const data = queryClient.getQueryData<InfiniteData<ConversationsPage>>(qk.conversations.list(FILTERS));
  return data?.pages.flatMap((p) => p.items) ?? [];
}

/** Непрочитанные КОНКРЕТНОГО диалога — без правил бейджа (чей, живой ли). */
function unreadOf(convId: string): number {
  return useUnreadStore.getState().byConversation[convId]?.count ?? 0;
}

function messageItems(convId: string): MessageDto[] {
  const data = queryClient.getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId));
  return data?.pages.flatMap((p) => p.items) ?? [];
}

/**
 * ⚠ КТО «Я» В ЭТИХ ПРОВЕРКАХ. С 02.09 звук нового сообщения играет ТОЛЬКО по
 * своим диалогам: диспетчер выключил его совсем, потому что звенело на каждое
 * входящее всей компании (замер: около шестисот сигналов за смену, своих —
 * меньше десятой части). Значит у кадра, который обязан звенеть, должен быть
 * хозяин — и это я.
 */
const МОЙ_ID = "11111111-1111-4111-8111-111111111111";

describe("applyWsEvent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient.clear();
    useUnreadStore.getState().clear();
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" } });
    useSessionStore.setState({
      user: { id: МОЙ_ID, full_name: "Я", email: "me@leadchat.local", role: "manager" } as never,
    });
  });

  it("message:new appends to the open thread cache and bumps the list row to the top", () => {
    // convA свежее и стоит первым; сообщение прилетает в convB
    seedList([makeConv("conv-a", { last_message_at: "2026-08-04T09:30:00Z" }), makeConv("conv-b")]);
    seedMessages("conv-b", [makeMsg("m1", "conv-b", { created_at: "2026-08-04T09:00:00Z" })]);

    const msg = makeMsg("m2", "conv-b");
    applyWsEvent({
      type: "message:new",
      ts: "2026-08-04T10:00:00.100Z",
      data: {
        conversation_id: "conv-b",
        message: msg,
        // ⚠ ХОЗЯИН ДИАЛОГА — Я, И ЭТО ЧАСТЬ ПРОВЕРКИ (02.09). С этого дня звук
        // играет ТОЛЬКО по своим диалогам: диспетчер выключил его совсем,
        // потому что звенело на каждое входящее всей компании — около
        // шестисот сигналов за смену, из них его меньше десятой части.
        // Оставь здесь чужой диалог — и проверка звука ниже стала бы
        // проверять тишину, а не сигнал.
        conversation_patch: {
          last_message_at: msg.created_at,
          unread_delta: 1,
          assignee_id: МОЙ_ID,
        },
      },
    });

    // Лента: append в последнюю страницу
    expect(messageItems("conv-b").map((m) => m.id)).toEqual(["m1", "m2"]);

    // Список: строка обновлена и поднята наверх (непрочитанные — первыми)
    const rows = listRows();
    expect(rows[0].id).toBe("conv-b");
    expect(rows[0].unread_count).toBe(1);
    expect(rows[0].last_message?.body).toBe(msg.body);
    expect(rows[0].last_message_at).toBe(msg.created_at);

    // Непрочитанные и звук. Спрашиваем счётчик САМОГО ДИАЛОГА, а не сумму по
    // бейджу: сумма отвечает на другой вопрос — «сколько работы у меня» — и
    // зависит от того, мой ли это диалог и жив ли он
    // (`unreadStore.selectMineUnread`). Здесь же проверяется, что входящее
    // вообще посчиталось.
    expect(unreadOf("conv-b")).toBe(1);
    expect(notifyNewMessage).toHaveBeenCalledTimes(1);
  });

  it("системная запись не занимает превью строки и не двигает её наверх", () => {
    /*
     * ЭТО И БЫЛО ВИДНО В СПИСКЕ ЧАТОВ 12 августа: вместо слов клиента строка
     * показывала «Отказ отменён: Ад…». Каждая смена статуса, принятие, отказ и
     * его отмена кладут в ленту системную запись и публикуют её тем же кадром
     * `message:new` — коллеги обязаны видеть событие. Но в СТРОКЕ СПИСКА место
     * одно, и занимать его служебным текстом нельзя: список читают ради того,
     * что сказал клиент.
     *
     * Проверяются обе половины беды сразу — и текст, и порядок: сервер
     * `last_message_at` системной записью не двигает вовсе
     * (`conversations.add_system_message`), а фронт двигал, и строка прыгала
     * наверх без повода.
     */
    seedList([
      makeConv("conv-a", { last_message_at: "2026-08-04T09:30:00Z" }),
      makeConv("conv-b", { last_message_at: "2026-08-04T09:00:00Z" }),
    ]);
    seedMessages("conv-b", []);

    applyWsEvent({
      type: "message:new",
      ts: "2026-08-04T10:00:00.100Z",
      data: {
        conversation_id: "conv-b",
        message: makeMsg("sys-1", "conv-b", {
          direction: "system",
          sender_type: "system",
          body: "Отказ отменён: Анна Смирнова",
        }),
        conversation_patch: { escalated: false },
      },
    });

    const rowB = listRows().find((r) => r.id === "conv-b");
    expect(rowB?.last_message?.body).toBe("старое");
    expect(rowB?.last_message_at).toBe("2026-08-04T09:00:00Z");
    expect(listRows()[0].id).toBe("conv-a");
    // В ленте запись при этом есть — иначе коллеги не увидели бы события.
    expect(messageItems("conv-b").map((m) => m.id)).toEqual(["sys-1"]);
  });

  it("внутренняя заметка тоже не попадает в превью строки", () => {
    // Заметку клиент не видел; в списке, где показывают последнее сказанное
    // клиенту или клиентом, ей делать нечего.
    seedList([makeConv("conv-b", { last_message_at: "2026-08-04T09:00:00Z" })]);
    seedMessages("conv-b", []);

    applyWsEvent({
      type: "message:new",
      ts: "2026-08-04T10:00:00.100Z",
      data: {
        conversation_id: "conv-b",
        message: makeMsg("note-1", "conv-b", {
          direction: "note",
          sender_type: "operator",
          body: "Постоянный клиент, дать скидку",
        }),
        conversation_patch: {},
      },
    });

    const rowB = listRows()[0];
    expect(rowB.last_message?.body).toBe("старое");
    expect(rowB.last_message_at).toBe("2026-08-04T09:00:00Z");
  });

  it("message:new deduplicates by message id (WS + догон)", () => {
    seedList([makeConv("conv-b")]);
    seedMessages("conv-b", [makeMsg("m1", "conv-b")]);

    const event = {
      type: "message:new",
      ts: "2026-08-04T10:00:00.100Z",
      data: {
        conversation_id: "conv-b",
        message: makeMsg("m2", "conv-b"),
        conversation_patch: { unread_delta: 1 },
      },
    } as const;

    applyWsEvent(event);
    applyWsEvent(event);

    expect(messageItems("conv-b")).toHaveLength(2); // m2 не задублировалось
  });

  it("message:new в открытом видимом диалоге не считает непрочитанные и не звучит", () => {
    seedList([makeConv("conv-b")]);
    seedMessages("conv-b", []);
    useChatUiStore.setState({ activeConversationId: "conv-b" }); // jsdom: visibilityState = visible

    applyWsEvent({
      type: "message:new",
      ts: "2026-08-04T10:00:00.100Z",
      data: {
        conversation_id: "conv-b",
        message: makeMsg("m2", "conv-b"),
        conversation_patch: { unread_delta: 1 },
      },
    });

    expect(listRows()[0].unread_count).toBe(0);
    expect(unreadOf("conv-b")).toBe(0);
    expect(notifyNewMessage).not.toHaveBeenCalled();
  });

  it("conversation:updated patches the detail cache and the list row", () => {
    const conv = makeConv("conv-a");
    seedList([conv]);
    const detail: ConversationDetailDto = {
      ...conv,
      bot_vars: {},
      external_chat_id: "u2i-abc",
      first_client_at: null,
      client_conversations_count: 1,
    };
    queryClient.setQueryData(qk.conversations.detail("conv-a"), detail);

    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-08-04T10:05:00Z",
      data: {
        conversation_id: "conv-a",
        patch: { status: "closed", tags: ["негатив"], client: { phone: "+79261234567" } },
      },
    });

    const patched = queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail("conv-a"));
    expect(patched?.status).toBe("closed");
    expect(patched?.tags).toEqual(["негатив"]);
    expect(patched?.client.phone).toBe("+79261234567");
    expect(patched?.client.name).toBe("Иван Петров"); // вложенный merge не потерял поля
    expect(patched?.external_chat_id).toBe("u2i-abc");

    expect(listRows()[0].status).toBe("closed");
  });
});
