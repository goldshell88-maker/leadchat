import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * СМЕНА ВЫБОРКИ НЕ ГАСИТ КОЛОНКУ (аудит 06.09).
 *
 * ЧТО БЫЛО. Вкладка, фильтр и каждая буква поиска — новый ключ запроса, а
 * новый ключ без `placeholderData` это `isPending`: на месте списка вставал
 * скелет на всё время ответа — RTT плюс 39…115 мс сервера, — и человек,
 * целившийся в строку, терял её из-под курсора. Статистика ту же беду
 * пережила 22.08 и вылечена тем же `keepPreviousData`.
 *
 * ЧТО СТАЛО. Прежние строки остаются на экране приглушёнными
 * (`data-placeholder` на прокрутке), пока не приедут новые; скелет — только
 * когда данных нет вовсе, то есть на самом первом открытии.
 *
 * ДИВЕРСИИ (обе дали красный, обе восстановлены байт в байт):
 *  1. убрать `placeholderData: keepPreviousData` у списка в `ChatListPane` —
 *     «прежние строки на месте» красный: скелет вместо строк;
 *  2. убрать `data-placeholder` с прокрутки — тот же тест красный: колонка не
 *     приглушена, человек не видит, что едет новая выборка.
 */

function строка(id: string, name: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name, phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: { body: "Здравствуйте!", direction: "in", created_at: "2026-09-06T09:40:12Z" },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-06T09:40:12Z",
    waiting_since: null,
  };
}

function страница(items: ConversationDto[]): ConversationsPage {
  return { items, page: { limit: 50, offset: 0, total: items.length } };
}

const ВСЕ = [строка("conv-all", "Иван Петров")];
const МОИ = [строка("conv-mine", "Мария Иванова")];

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("Список диалогов держит прежние строки, пока едут новые", () => {
  /** Ответ на «Мои» держим за руку: между нажатием и ответом и живёт дефект. */
  let отпуститьМои: (() => void) | null;
  let отпуститьВсе: (() => void) | null;

  beforeEach(() => {
    queryClient.clear();
    useUnreadStore.getState().clear();
    useInboxStore.getState().clear();
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" }, inboxOpen: false });
    // Без права отвечать: нет ни очереди, ни виджета «Мой день» — только список.
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    отпуститьМои = null;
    отпуститьВсе = null;

    // Виртуализатор меряет прокрутку через offsetWidth/offsetHeight — в jsdom они нулевые.
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/conversations/counts")) {
          return jsonResponse(200, { mine: 1, mine_waiting: 0 });
        }
        if (url.pathname.endsWith("/conversations")) {
          if (url.searchParams.get("tab") === "mine") {
            return new Promise<Response>((resolve) => {
              отпуститьМои = () => resolve(jsonResponse(200, страница(МОИ)));
            });
          }
          return new Promise<Response>((resolve) => {
            отпуститьВсе = () => resolve(jsonResponse(200, страница(ВСЕ)));
          });
        }
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  const прокрутка = () => document.querySelector(".chat-list-pane__scroll");
  const скелет = () => document.querySelector(".conv-skeleton");

  it("⚠ смена вкладки: прежние строки на месте и приглушены, скелета нет", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChatListPane />);
    await waitFor(() => expect(отпуститьВсе).not.toBeNull());
    отпуститьВсе?.();
    await screen.findByText("Иван Петров");

    await user.click(screen.getByRole("tab", { name: /^Мои/ }));
    await waitFor(() => expect(отпуститьМои, "запрос «Моих» не ушёл").not.toBeNull());

    expect(скелет(), "скелет вместо прежних строк — колонка погасла на время ответа").toBeNull();
    expect(screen.getByText("Иван Петров"), "прежние строки сняты с экрана").toBeInTheDocument();
    expect(прокрутка(), "колонка не приглушена — не видно, что едет новая выборка").toHaveAttribute(
      "data-placeholder",
    );

    отпуститьМои?.();
    await screen.findByText("Мария Иванова");
    expect(screen.queryByText("Иван Петров"), "прежняя выборка пережила приход новой").toBeNull();
    expect(прокрутка(), "приглушение осталось после прихода строк").not.toHaveAttribute("data-placeholder");
  });

  it("а на самом первом открытии, когда данных нет вовсе, — скелет как раньше", async () => {
    renderWithProviders(<ChatListPane />);
    await waitFor(() => expect(отпуститьВсе).not.toBeNull());

    expect(скелет(), "без данных колонка обязана показать загрузку").not.toBeNull();
    expect(прокрутка()).not.toHaveAttribute("data-placeholder");

    отпуститьВсе?.();
    await screen.findByText("Иван Петров");
    expect(скелет()).toBeNull();
  });
});
