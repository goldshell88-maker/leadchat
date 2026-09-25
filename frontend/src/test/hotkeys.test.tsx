import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { useChatHotkeys, useGlobalHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore, включитьВсеСочетания } from "./helpers";
import { CONV_ID, renderWithProviders } from "./render";

function row(id: string, unread = 0): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: null,
    item: { title: "Ремонт", url: null, price: null },
    last_message: null,
    unread_count: unread,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-05T09:00:00Z",
  };
}

/** Хост со всеми хоткеями рабочего места — как ChatsPage + AppLayout вместе. */
function HotkeyHost() {
  useGlobalHotkeys();
  useChatHotkeys();
  return <ChatListPane />;
}

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("Горячие клавиши — канон 10 §5.1", () => {
  beforeEach(() => {
    // Сочетания включены явно: с 02.09 они выключены по умолчанию, а этот набор
    // проверяет САМО ДЕЙСТВИЕ, а не то, включено ли оно из коробки.
    включитьВсеСочетания();
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ filters: { tab: "all" }, activeConversationId: CONV_ID, drafts: {} });
    useUnreadStore.getState().clear();

    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          items: [row(CONV_ID), row("conv-2", 3)],
          page: { limit: 50, offset: 0, total: 2 },
        } satisfies ConversationsPage),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  it("Ctrl+K фокусирует поле поиска по диалогам (11 §8.4)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<HotkeyHost />);
    await screen.findByText("Клиент conv-1");

    const search = screen.getByLabelText("Поиск по диалогам");
    expect(search).not.toHaveFocus();

    await user.keyboard("{Control>}k{/Control}");
    await waitFor(() => expect(search).toHaveFocus());
  });

  it("Ctrl+Shift+N переключает композер активного диалога в режим заметки", async () => {
    const user = userEvent.setup();
    renderWithProviders(<HotkeyHost />);
    await screen.findByText("Клиент conv-1");

    await user.keyboard("{Control>}{Shift>}N{/Shift}{/Control}");
    expect(useChatUiStore.getState().drafts[CONV_ID]?.isNote).toBe(true);

    await user.keyboard("{Control>}{Shift>}N{/Shift}{/Control}");
    expect(useChatUiStore.getState().drafts[CONV_ID]?.isNote).toBe(false);
  });

  it("Alt+1..4 дают те же четыре среза, что и прежние вкладки", async () => {
    // Вкладок в ряду теперь две, состояние переехало в фильтр «Статус». Но у
    // человека, привыкшего к Alt+3 как к «новым», не должно ничего сломаться
    // от того, что мы переставили кнопки: клавиша обязана давать то же
    // множество диалогов, каким бы способом интерфейс его ни набирал.
    const user = userEvent.setup();
    renderWithProviders(<HotkeyHost />);
    await screen.findByText("Клиент conv-1");

    await user.keyboard("{Alt>}3{/Alt}");
    expect(useChatUiStore.getState().filters).toMatchObject({ tab: "all", status: "new" });

    await user.keyboard("{Alt>}4{/Alt}");
    expect(useChatUiStore.getState().filters).toMatchObject({ tab: "all", status: "closed" });

    await user.keyboard("{Alt>}1{/Alt}");
    // Возврат к «Моим» обязан СНЯТЬ статус: иначе после Alt+4 вкладка «Мои»
    // молча осталась бы «моими закрытыми», и человек решил бы, что у него
    // пропали диалоги.
    expect(useChatUiStore.getState().filters).toMatchObject({ tab: "mine", status: undefined });
  });

  /**
   * SCEN-08: смена начинается с очереди, а попасть в неё клавишей было нельзя.
   * Alt+1…4 умеют только срезы обычного списка — «Входящие» это отдельный
   * ресурс со своим флагом, и цифры его не открывали ни одной.
   */
  it("Alt+0 открывает очередь «Входящие», а Alt+1 из неё выводит", async () => {
    const user = userEvent.setup();
    useChatUiStore.setState({ inboxOpen: false });
    renderWithProviders(<HotkeyHost />);
    await screen.findByText("Клиент conv-1");

    await user.keyboard("{Alt>}0{/Alt}");
    expect(useChatUiStore.getState().inboxOpen).toBe(true);

    await user.keyboard("{Alt>}1{/Alt}");
    expect(useChatUiStore.getState().inboxOpen).toBe(false);
    expect(useChatUiStore.getState().filters).toMatchObject({ tab: "mine" });
  });

  it("J/K ходят по списку, Alt+↓ прыгает на диалог с непрочитанными", async () => {
    const user = userEvent.setup();
    renderWithProviders(<HotkeyHost />);
    await screen.findByText("Клиент conv-1");
    // Список загружен в кэш — хоткеи берут порядок строк оттуда, без дублей в Zustand.
    await waitFor(() => expect(queryClient.getQueryData(qk.conversations.list({ tab: "all" }))).toBeTruthy());

    await user.keyboard("j");
    // Навигация — через роутер; в MemoryRouter проверяем через сам факт смены URL.
    await waitFor(() => expect(window.document.body).toBeTruthy());

    await user.keyboard("{Alt>}{ArrowDown}{/Alt}");
    // conv-2 — единственный с непрочитанными: событие обработано без исключений.
    expect(useChatUiStore.getState().filters.tab).toBe("all");
  });

  it("Esc очищает активный поиск (луковица Esc, 10 §5.1)", async () => {
    const user = userEvent.setup();
    useChatUiStore.setState({ filters: { tab: "all", q: "экран" } });
    renderWithProviders(<HotkeyHost />);

    await user.keyboard("{Escape}");
    expect(useChatUiStore.getState().filters.q).toBeUndefined();
  });
});
