import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ОДИН ДИАЛОГ — ОДНА СТРОКА (снимок владельца от 25 августа).
 *
 * На экране строки наложились друг на друга: имена читались вперемешку
 * («Дмитрий» поверх «Андрея»), под ними в одном месте стояли две разные
 * подписи. Обновление вкладки лечило.
 *
 * ПРИЧИНА — НЕ ВЁРСТКА. Карточка ростом 78px внутри шага 84px, содержимое в
 * неё влезает при любом размере шрифта: размеры в пикселях, а не в rem
 * (замерено в стенде). Ломается ПОРЯДОК ДАННЫХ.
 *
 * Страницы тянутся ПО СМЕЩЕНИЮ (`?offset=` считается как «сколько уже
 * загружено»), а сервер отдаёт список по времени последнего сообщения. Клиент
 * написал в диалог со второй страницы — диалог поднялся на первую, всё ниже
 * сдвинулось на позицию, и пограничный диалог приезжает ВТОРЫМ разом.
 *
 * Дальше дубль ломает отрисовку: ключ виртуальной строки — это id диалога,
 * два индекса дают ОДИН ключ, и React получает список с повторяющимся key.
 * Класть такие узлы в разные места он не обязан — карточки оказываются в одной
 * позиции. Обновление вкладки лечит, потому что запрос начинается со страницы 0.
 *
 * ⚠ Сортировку это НЕ проверяет — ей посвящён conversationOrder.test.tsx.
 * Здесь ровно одно утверждение: одна беседа не может стоять в списке дважды,
 * какие бы страницы ни приехали с сервера.
 */

function row(id: string, lastAt: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: { body: "Здравствуйте", direction: "in", created_at: lastAt },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: lastAt,
  };
}

/** Строки в том порядке, в каком нарисованы: имя в фикстуре несёт id. */
function renderedIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>(".conv-card__name")].map((el) =>
    (el.textContent ?? "").replace("Клиент ", ""),
  );
}

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

/*
 * ЖИВАЯ ГОНКА, ЗАПИСАННАЯ ДВУМЯ СТРАНИЦАМИ.
 *
 * Первая страница уехала клиенту такой: [a, b, c]. Пока диспетчер листал,
 * клиент написал в «c» — он поднялся наверх, и вторая страница, запрошенная с
 * offset=3, начинается с «c» ЕЩЁ РАЗ. Всего сервер знает 6 бесед, значит
 * страница вторая не последняя и загрузится.
 */
const СТРАНИЦА_1 = [row("a", "2026-08-12T10:00:00Z"), row("b", "2026-08-12T09:00:00Z"),
                    row("c", "2026-08-12T08:00:00Z")];
const СТРАНИЦА_2 = [row("c", "2026-08-12T08:00:00Z"), row("d", "2026-08-12T07:00:00Z"),
                    row("e", "2026-08-12T06:00:00Z")];

function page(items: ConversationDto[], offset: number, total: number): ConversationsPage {
  return { items, page: { limit: 3, offset, total } };
}

describe("Список диалогов: дубль страницы не рисует вторую строку", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    queryClient.clear();
    useUnreadStore.getState().clear();
    useInboxStore.getState().clear();
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" }, inboxOpen: false });
    resetSessionStore({
      user: fakeUser,
      accessToken: "test-token",
      bootstrapped: true,
      permissions: ["conversations:read", "messages:send", "conversations:manage"],
    });
    // Виртуализатор считает по размерам узла; в jsdom их нет.
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 900 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations")) {
        const offset = Number(url.searchParams.get("offset") || 0);
        return jsonResponse(200, offset >= 3 ? page(СТРАНИЦА_2, 3, 6) : page(СТРАНИЦА_1, 0, 6));
      }
      if (url.pathname.endsWith("/inbox")) return jsonResponse(200, page([], 0, 0));
      if (url.pathname.endsWith("/inbox/count")) return jsonResponse(200, { count: 0, escalated: 0 });
      return jsonResponse(200, { items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight)
      Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth)
      Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  it("беседа с границы страниц рисуется один раз, а не двумя карточками", async () => {
    renderWithProviders(<ChatListPane />);
    await screen.findByText("Клиент a");
    // вторая страница подтягивается сама: строку-лоадер видит виртуализатор
    await waitFor(() => expect(renderedIds()).toContain("e"));

    const ids = renderedIds();
    expect(ids.filter((id) => id === "c")).toHaveLength(1);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("остальные беседы обеих страниц на месте — дедуп не съедает лишнего", async () => {
    renderWithProviders(<ChatListPane />);
    await screen.findByText("Клиент a");
    await waitFor(() => expect(renderedIds()).toContain("e"));
    expect(renderedIds()).toEqual(["a", "b", "c", "d", "e"]);
  });
});
