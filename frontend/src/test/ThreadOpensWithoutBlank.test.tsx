/**
 * ОТКРЫТИЕ ДИАЛОГА НЕ ГАСИТ ЭКРАН (жалоба владельца 22.08: «при открытии и
 * закрытии есть задержка, диалог визуально пропадает»).
 *
 * ЧТО БЫЛО. Панель целиком висела на `GET /conversations/{id}`. От клика по
 * строке до ответа сервера шапка показывала «Загрузка…», лента — скелет, низ
 * панели был пуст: диалог, который человек только что видел в списке и по
 * которому щёлкнул, исчезал и собирался заново. На закрытии то же самое ещё
 * раз — закрыл, ушли к следующему, снова пустая панель.
 *
 * ЧТО СТАЛО. Пока едет деталь, шапка рисуется по СТРОКЕ СПИСКА: те же поля
 * сервера, просто приехавшие другим запросом и уже лежащие в кэше. Придумывать
 * ничего не приходится — строки нет, значит и показать нечего, и остаётся
 * прежнее «Загрузка…».
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders } from "./render";

function строкаСписка(over: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id: CONV_ID,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: "client-1", name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт стиральных машин", url: null, price: null },
    last_message: null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-22T10:00:00Z",
    ...over,
  };
}

function положитьВСписок(row: ConversationDto) {
  queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
    pages: [{ items: [row], page: { limit: 1, offset: 0, total: 1 } }],
    pageParams: [0],
  });
}

/** Пустая, но существующая лента: скелет ленты к делу не относится. */
function пустаяЛента() {
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [
      {
        items: [],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

describe("открытие диалога", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ filters: DEFAULT_FILTERS });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    пустаяЛента();
    // Деталь НЕ отвечает: ровно те доли секунды, в которые экран и гас.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("имя клиента и статус видны сразу — до ответа сервера", () => {
    положитьВСписок(строкаСписка());
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(screen.getByText("Ольга Никитина")).toBeInTheDocument();
    expect(screen.queryByText("Загрузка…")).toBeNull();
  });

  it("канал и объявление в подзаголовке — тоже из строки", () => {
    положитьВСписок(строкаСписка());
    const { container } = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    const sub = container.querySelector(".thread-header__sub") as HTMLElement;
    expect(sub.textContent).toContain("LP-Москва");
    expect(sub.textContent).toContain("Ремонт стиральных машин");
  });

  it("статус читается из строки, а не выдумывается", () => {
    положитьВСписок(строкаСписка({ status: "waiting_client" }));
    const { container } = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    const chip = container.querySelector(".thread-header__status") as HTMLElement;
    expect(chip.dataset.status).toBe("waiting_client");
  });

  /*
   * Меню «…» остаётся на детали намеренно: ему нужен состав позванных, которого
   * в строке нет. Половина меню, дорисованная через миг, хуже меню, пришедшего
   * на миг позже.
   */
  it("меню действий ждёт деталь, а не собирается из половины данных", () => {
    положитьВСписок(строкаСписка());
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(screen.queryByRole("button", { name: /Действия/i })).toBeNull();
  });

  it("строки в кэше нет — честное «Загрузка…», а не выдуманный диалог", () => {
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(screen.getByText("Загрузка…")).toBeInTheDocument();
  });

  it("диалог, открытый из очереди, тоже находится: очередь — такой же кэш строк", () => {
    queryClient.setQueryData(qk.inbox.list, {
      pages: [{ items: [строкаСписка({ in_inbox: true })], page: { limit: 1, offset: 0, total: 1 } }],
      pageParams: [0],
    });
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(screen.getByText("Ольга Никитина")).toBeInTheDocument();
  });
});
