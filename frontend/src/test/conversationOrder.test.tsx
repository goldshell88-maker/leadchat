import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { useTogglePin } from "@/features/chats/hooks/usePins";
import { compareConversationRows } from "@/shared/lib/conversationOrder";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ПОРЯДОК СПИСКА ДИАЛОГОВ — ОДИН НА ВЕСЬ ФРОНТ (разбор от 12 августа).
 *
 * Порядок считался в двух местах и по-разному: сервер отдавал страницу в
 * своём порядке, а локальные правки кэша (новое сообщение, патч строки,
 * закрепление) пересортировывали её собственным компаратором — «непрочитанные
 * сверху, затем свежее сверху», без закреплённых и без полного ключа. Пока по
 * вкладке не проехало ни одного события, список стоял так, как прислал
 * сервер; первое же входящее перестраивало его в другой порядок, и
 * закреплённые уезжали вниз.
 *
 * Здесь проверяется, что оба пути дают ОДНО И ТО ЖЕ, и что закрепление
 * обходится без перезапроса списка.
 */

function row(id: string, lastAt: string | null, extra: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: lastAt ? { body: "Здравствуйте", direction: "in", created_at: lastAt } : null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: lastAt,
    ...extra,
  };
}

describe("Порядок списка диалогов", () => {
  it("закреплённые выше всех, даже самых свежих", () => {
    const pinnedOld = row("a", "2026-08-01T10:00:00Z", { pinned: true });
    const freshest = row("b", "2026-08-12T10:00:00Z");
    expect([freshest, pinnedOld].sort(compareConversationRows).map((r) => r.id)).toEqual(["a", "b"]);
  });

  it("дальше — по последнему сообщению УБЫВАЮЩЕ", () => {
    const rows = [
      row("old", "2026-08-01T10:00:00Z"),
      row("new", "2026-08-12T10:00:00Z"),
      row("mid", "2026-08-06T10:00:00Z"),
    ];
    expect(rows.sort(compareConversationRows).map((r) => r.id)).toEqual(["new", "mid", "old"]);
  });

  it("диалог без сообщений уходит в конец, а не в начало", () => {
    const rows = [row("silent", null), row("talking", "2020-01-01T00:00:00Z")];
    expect(rows.sort(compareConversationRows).map((r) => r.id)).toEqual(["talking", "silent"]);
  });

  /*
   * Служебные события Авито приходят пачками с одинаковой секундой. Без
   * третьего ключа порядок внутри пачки задаёт исходный порядок массива —
   * и меняется после каждой локальной правки кэша.
   */
  it("одинаковая секунда: порядок задаёт id и не зависит от порядка на входе", () => {
    const same = "2026-08-12T10:00:00Z";
    const forward = [row("c-1", same), row("c-2", same), row("c-3", same)];
    const backward = [row("c-3", same), row("c-2", same), row("c-1", same)];
    expect(forward.sort(compareConversationRows).map((r) => r.id)).toEqual(["c-1", "c-2", "c-3"]);
    expect(backward.sort(compareConversationRows).map((r) => r.id)).toEqual(["c-1", "c-2", "c-3"]);
  });

  it("непрочитанное на порядок НЕ влияет: сервер по нему сортировать не может", () => {
    // Пер-юзерный счётчик проставляется уже поверх готовой страницы
    // (`read_markers.apply_unread_counts`), ORDER BY его не видит. Учитывай мы
    // его здесь — клиентский порядок снова разошёлся бы с серверным.
    const loudOld = row("old", "2026-08-01T10:00:00Z", { unread_count: 9, tags: ["негатив"] });
    const quietNew = row("new", "2026-08-12T10:00:00Z", { unread_count: 0 });
    expect([loudOld, quietNew].sort(compareConversationRows).map((r) => r.id)).toEqual([
      "new",
      "old",
    ]);
  });
});

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

const PINNED = row("p-1", "2026-08-01T09:00:00Z", { pinned: true });
const FRESH = row("f-1", "2026-08-12T09:00:00Z");
const STALE = row("s-1", "2026-08-05T09:00:00Z");
const SERVER_ORDER = [PINNED, FRESH, STALE];

function page(items: ConversationDto[]): ConversationsPage {
  return { items, page: { limit: 50, offset: 0, total: items.length } };
}

/**
 * Идентификаторы диалогов в том порядке, в котором строки сейчас нарисованы.
 * Берём из видимого имени клиента — специальных атрибутов ради теста в разметке
 * заводить незачем, а имя в фикстуре несёт id.
 */
function renderedIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>(".conv-card__name")].map((el) =>
    (el.textContent ?? "").replace("Клиент ", ""),
  );
}

describe("Список после локальных правок кэша", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const listCalls = () =>
    fetchMock.mock.calls
      .map((c) => decodeURIComponent(String(c[0])))
      .filter((u) => u.includes("/conversations?"));

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

    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations")) return jsonResponse(200, page(SERVER_ORDER));
      if (url.pathname.endsWith("/inbox")) return jsonResponse(200, page([]));
      if (url.pathname.endsWith("/inbox/count")) return jsonResponse(200, { count: 0, escalated: 0 });
      if (url.pathname.includes("/pin")) return jsonResponse(200, {});
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

  it("входящее сообщение НЕ перетасовывает список по своим правилам", async () => {
    renderWithProviders(<ChatListPane />);
    await screen.findByText("Клиент f-1");
    expect(renderedIds()).toEqual(["p-1", "f-1", "s-1"]);

    // Клиент написал в самый старый диалог: он обязан подняться на второе
    // место — под закреплённый. Прежний компаратор поднял бы его на ПЕРВОЕ
    // (непрочитанные сверху), уронив закреплённый.
    act(() => {
      applyWsEvent({
        type: "message:new",
        ts: "2026-08-12T12:00:00Z",
        data: {
          conversation_id: "s-1",
          message: {
            id: "m-1",
            conversation_id: "s-1",
            direction: "in",
            sender_type: "client",
            body: "Ну что там?",
            attachments: [],
            delivery_status: "delivered",
            created_at: "2026-08-12T12:00:00Z",
            sender: null,
          },
          // Сервер везёт патч строки вместе с сообщением; для порядка важно
          // только новое `last_message_at`, его проставляет сам обработчик.
          conversation_patch: {},
        },
      });
    });

    await waitFor(() => expect(renderedIds()).toEqual(["p-1", "s-1", "f-1"]));
  });

  it("строки живут по ключу-идентификатору: при перестановке узел уезжает вместе со своим диалогом", async () => {
    renderWithProviders(<ChatListPane />);
    await screen.findByText("Клиент f-1");

    const nodeOfFresh = screen.getByText("Клиент f-1").closest(".conv-card") as HTMLElement;
    expect(nodeOfFresh).not.toBeNull();

    act(() => {
      applyWsEvent({
        type: "message:new",
        ts: "2026-08-12T12:00:00Z",
        data: {
          conversation_id: "s-1",
          message: {
            id: "m-2",
            conversation_id: "s-1",
            direction: "in",
            sender_type: "client",
            body: "Ну что там?",
            attachments: [],
            delivery_status: "delivered",
            created_at: "2026-08-12T12:00:00Z",
            sender: null,
          },
          // Сервер везёт патч строки вместе с сообщением; для порядка важно
          // только новое `last_message_at`, его проставляет сам обработчик.
          conversation_patch: {},
        },
      });
    });

    await waitFor(() => expect(renderedIds()).toEqual(["p-1", "s-1", "f-1"]));
    // Ключ по индексу переиспользовал бы ЭТОТ узел под другой диалог: строка
    // осталась бы на месте, а содержимое подменилось — клик уходил бы не туда.
    expect(document.body.contains(nodeOfFresh)).toBe(true);
    expect(nodeOfFresh.textContent).toContain("Клиент f-1");
    expect(nodeOfFresh.textContent).not.toContain("Клиент s-1");
  });

  it("закреп поднимает строку в кэше и НЕ перезапрашивает список", async () => {
    const user = userEvent.setup();

    function PinButton() {
      const pin = useTogglePin("s-1");
      return (
        <button type="button" onClick={() => pin.mutate(true)}>
          закрепить
        </button>
      );
    }

    renderWithProviders(
      <>
        <ChatListPane />
        <PinButton />
      </>,
    );
    await screen.findByText("Клиент s-1");
    const before = listCalls().length;

    await user.click(screen.getByRole("button", { name: "закрепить" }));

    // Закреплённых стало двое: они идут первым ключом, между собой — по
    // последнему сообщению (s-1 свежее p-1).
    await waitFor(() => expect(renderedIds()).toEqual(["s-1", "p-1", "f-1"]));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/pin"))).toBe(true),
    );
    expect(listCalls().length).toBe(before);
  });

  it("сервер отказал в закрепе — строка возвращается на своё место", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.includes("/pin")) {
        return jsonResponse(422, {
          error: { code: "unprocessable", message: "максимум", details: { reason: "too_many_pins" } },
        });
      }
      if (url.pathname.endsWith("/conversations")) return jsonResponse(200, page(SERVER_ORDER));
      if (url.pathname.endsWith("/inbox/count")) return jsonResponse(200, { count: 0, escalated: 0 });
      return jsonResponse(200, { items: [] });
    });

    function PinButton() {
      const pin = useTogglePin("s-1");
      return (
        <button type="button" onClick={() => pin.mutate(true)}>
          закрепить
        </button>
      );
    }

    renderWithProviders(
      <>
        <ChatListPane />
        <PinButton />
      </>,
    );
    await screen.findByText("Клиент s-1");

    await user.click(screen.getByRole("button", { name: "закрепить" }));

    // Тост говорит «не получилось» — список обязан говорить то же самое.
    await waitFor(() => expect(renderedIds()).toEqual(["p-1", "f-1", "s-1"]));
  });
});
