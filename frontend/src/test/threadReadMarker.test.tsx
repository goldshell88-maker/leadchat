import { waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, MessageDto, MessagesPage } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * ОТМЕТКА ПРОЧТЕНИЯ ПОВТОРЯЕТСЯ, ПОКА ДИАЛОГ ОТКРЫТ (находка 23.08 №4).
 *
 * Было: маркер уходил ровно один раз, при смене диалога (`[convId]`). Клиент
 * писал в открытый диалог, оператор читал — а серверный маркер оставался на
 * месте. Локально всё выглядело прочитанным, но первый же перезапрос списка
 * приносил серверное число, и у диалога, на который человек СМОТРИТ, загорался
 * бейдж «3», имя жирнело, а во вкладке появлялось «(3) LeadChat».
 *
 * Проверяем поведение, а не форму: считаем обращения к `POST /read`.
 */

const ЧИТАНО: string[] = [];

vi.mock("@/features/chats/api", async () => {
  const настоящий = await vi.importActual<typeof import("@/features/chats/api")>(
    "@/features/chats/api",
  );
  return {
    ...настоящий,
    markConversationRead: vi.fn(async (id: string) => {
      ЧИТАНО.push(id);
    }),
  };
});

function сообщение(id: string, при: string): MessageDto {
  return {
    id,
    conversation_id: CONV_ID,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: `текст-${id}`,
    attachments: [],
    delivery_status: "delivered",
    client_message_id: null,
    created_at: при,
  } as MessageDto;
}

/** Что лежит в ленте СЕЙЧАС — и это же отдаёт сеть при перезапросе. */
let лента: MessageDto[] = [];

function страница(items: MessageDto[]): MessagesPage {
  return {
    items,
    page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
  } satisfies MessagesPage;
}

function положитьВЛенту(items: MessageDto[]): void {
  лента = items;
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [страница(items)],
    pageParams: [null],
  });
}

function открыть(): void {
  const conv = makeConversation() as ConversationDetailDto;
  queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
  // ⚠ ЗАГЛУШКА ОБЯЗАНА РАЗЛИЧАТЬ АДРЕСА. Одна на всё («вернуть диалог») ломала
  // сам тест: лента перезапрашивается при монтировании, получала объект диалога
  // вместо страницы сообщений, и `flat` оставался пустым ВСЕГДА — проверка
  // «пришло новое сообщение» была бы зелёной при любом коде.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown) =>
      String(url).includes("/messages") ? jsonResponse(200, страница(лента)) : jsonResponse(200, conv),
    ),
  );
  renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
}

describe("Маркер прочтения открытого диалога", () => {
  beforeEach(() => {
    ЧИТАНО.length = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    положитьВЛенту([сообщение("m-1", "2026-08-23T10:00:00Z")]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("уходит при открытии диалога", () => {
    открыть();
    expect(ЧИТАНО).toEqual([CONV_ID]);
  });

  it("уходит ЗАНОВО, когда клиент пишет в открытый диалог", async () => {
    открыть();
    expect(ЧИТАНО).toHaveLength(1);

    положитьВЛенту([
      сообщение("m-1", "2026-08-23T10:00:00Z"),
      сообщение("m-2", "2026-08-23T10:01:00Z"),
    ]);

    // ⚠ ЖДЁМ, А НЕ ПРОВЕРЯЕМ СРАЗУ. TanStack Query уведомляет наблюдателей
    // асинхронно (notifyManager), и синхронная проверка ловила состояние ДО
    // перерисовки — то есть краснела бы и на исправном коде.
    await waitFor(() =>
      expect(
        ЧИТАНО.length,
        "новое сообщение в открытом диалоге не отметилось прочитанным — бейдж вернётся с первым же перезапросом списка",
      ).toBe(2),
    );
  });

  it("не уходит, пока вкладка скрыта: читать некому", () => {
    const было = Object.getOwnPropertyDescriptor(Document.prototype, "visibilityState");
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    try {
      открыть();
      expect(ЧИТАНО).toEqual([]);
    } finally {
      if (было) Object.defineProperty(Document.prototype, "visibilityState", было);
      else delete (document as unknown as Record<string, unknown>).visibilityState;
    }
  });
});
