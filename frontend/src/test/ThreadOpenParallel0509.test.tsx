/**
 * ОТКРЫТИЕ ДИАЛОГА — ОДИН КРУГ ДО СЕРВЕРА, А НЕ ДВА (просьба владельца 05.09:
 * «сделай моментальную подгрузку диалога, есть небольшой тайминг»).
 *
 * ЗАМЕР БОЯ ЗА ШЕСТЬ ЧАСОВ, ради которого этот сторож и написан:
 *   GET /conversations/{id}           n=123, среднее 55 мс, максимум 436
 *   GET /conversations/{id}/messages  n=130, среднее 42 мс, максимум 283
 *
 * Пока оба запроса уходят разом, человек ждёт 55 мс. Свяжи их в цепочку —
 * например `enabled: Boolean(detail.data)` у ленты, что выглядит совершенно
 * невинно, — и он ждёт 97 мс, а на хвосте распределения 719. Сломать это одной
 * строкой легко, а заметить глазом нельзя: экран выглядит целым, просто
 * «немножко тормозит». Отсюда сторож.
 *
 * ВТОРАЯ ПОЛОВИНА — ПОВТОРНОЕ ОТКРЫТИЕ. Диспетчер мечется между двумя-тремя
 * диалогами постоянно, и второй заход в тот же диалог обязан рисоваться из
 * кэша, без единого запроса: `staleTime` 30 секунд из `app/queryClient.ts`
 * ровно про это.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

function строкаСписка(): ConversationDto {
  return {
    id: CONV_ID,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: "client-1", name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-05T10:00:00Z",
  } as ConversationDto;
}

function пустаяСтраницаЛенты(): MessagesPage {
  return {
    items: [],
    page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
  };
}

/** Что и каким глаголом ушло на сервер. */
let запросы: Array<{ url: string; method: string }> = [];

/** Только чтение диалога: `POST /read` — не ожидание человека, он ничего не рисует. */
function чтенияДиалога(): string[] {
  return запросы
    .filter((з) => з.method === "GET" && з.url.includes(`/conversations/${CONV_ID}`))
    .map((з) => з.url);
}

describe("открытие диалога — один круг до сервера", () => {
  beforeEach(() => {
    queryClient.clear();
    запросы = [];
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, activeConversationId: null });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
      pages: [{ items: [строкаСписка()], page: { limit: 1, offset: 0, total: 1 } }],
      pageParams: [0],
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  /**
   * Сервер МОЛЧИТ — и это здесь главное условие, а не удобство. Пока ни один
   * ответ не пришёл, всё, что успело уйти, ушло независимо друг от друга.
   * Отвечай сервер сразу, цепочка «второе после первого» выглядела бы точно так
   * же, как параллель, и проверка не значила бы ничего.
   */
  it("деталь и лента уходят вместе — второе не ждёт первого", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        запросы.push({ url: String(url), method: init?.method ?? "GET" });
        return new Promise<Response>(() => {});
      }),
    );

    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    const ушло = чтенияДиалога();
    expect(ушло.some((u) => u.endsWith(`/conversations/${CONV_ID}`))).toBe(true);
    expect(ушло.some((u) => u.includes(`/conversations/${CONV_ID}/messages`))).toBe(true);
  });

  it("уже открытый диалог со свежими данными не ходит на сервер заново", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        запросы.push({ url: String(url), method: init?.method ?? "GET" });
        return Promise.resolve(
          new Response(JSON.stringify(makeConversation()), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }),
    );

    // Данные лежат в кэше и им ноль секунд — ровно то, что остаётся после
    // первого открытия (общий staleTime 30 с).
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    queryClient.setQueryData(qk.messages.list(CONV_ID), {
      pages: [пустаяСтраницаЛенты()],
      pageParams: [null],
    });

    const { unmount } = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await act(async () => {
      await Promise.resolve();
    });
    unmount();

    expect(чтенияДиалога()).toEqual([]);
  });

  /**
   * ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ОБЯЗАНА ПАДАТЬ ПО СВОЕЙ ПРИЧИНЕ. Та же панель с
   * ПУСТЫМ кэшем обязана на сервер сходить — иначе проверка выше зеленела бы и
   * от сломанного экрана, который не спрашивает вообще ничего.
   */
  it("а с пустым кэшем — ходит: проверка выше не зеленеет от мёртвой панели", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        запросы.push({ url: String(url), method: init?.method ?? "GET" });
        return new Promise<Response>(() => {});
      }),
    );

    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(чтенияДиалога().length).toBeGreaterThan(0);
  });
});
