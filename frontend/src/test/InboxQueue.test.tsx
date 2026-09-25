import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { inboxRows } from "@/features/chats/inbox/api";
import { playInboxChime } from "@/features/chats/inbox/sound";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

// Звук проверяем фактом вызова: в jsdom проигрывать нечего.
vi.mock("@/features/chats/inbox/sound", () => ({ playInboxChime: vi.fn() }));

const PERMISSIONS: Record<Role, Permission[]> = {
  admin: ["conversations:read", "messages:send", "conversations:manage", "notes:write", "users:manage"],
  manager: ["conversations:read", "messages:send", "conversations:manage", "notes:write", "stats:own"],
  head: ["conversations:read", "conversations:manage", "notes:write", "stats:all", "stats:own"],
  observer: ["conversations:read"],
};

/**
 * Строка очереди — ровно то, что отдаёт `services.inbox.inbox_item`: обычный
 * `ConversationOut` плюс поля ожидания. Никакого `inbox_state` в системе нет —
 * очередь помечается булевым `in_inbox`, и фикстура обязана это повторять,
 * иначе тест зелёный, а прод пустой.
 */
function row(id: string, name: string, queued: boolean): ConversationDto {
  return {
    id,
    status: queued ? "new" : "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name, phone: null, avito_rating: null },
    assignee: queued ? null : { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: { body: "Здравствуйте!", direction: "in", created_at: "2026-08-06T09:40:12Z" },
    unread_count: 1,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-06T09:40:12Z",
    ...(queued
      ? {
          in_inbox: true,
          offered_at: "2026-08-06T09:40:12Z",
          waiting_seconds: 720,
          waiting_human: "12 мин",
          declined_count: 0,
          escalated: false,
        }
      : { in_inbox: false }),
  };
}

const QUEUE = [row("q-1", "Мария Иванова", true), row("q-2", "Сергей Орлов", true), row("q-3", "Ольга Ким", true)];
const MINE = [row("conv-1", "Иван Петров", false)];

function page(items: ConversationDto[], total = items.length): ConversationsPage {
  return { items, page: { limit: 50, offset: 0, total } };
}

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

/** Очередь «Входящие» в левой колонке (7.1, 15 §2.1). */
describe("Вкладка «Входящие»", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const urls = () => fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));

  beforeEach(() => {
    queryClient.clear();
    useUnreadStore.getState().clear();
    useInboxStore.getState().clear();
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" }, inboxOpen: false });
    vi.mocked(playInboxChime).mockClear();

    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/inbox/count")) return jsonResponse(200, { count: 3, escalated: 0 });
      if (url.pathname.endsWith("/inbox")) return jsonResponse(200, page(QUEUE));
      if (url.pathname.endsWith("/conversations")) return jsonResponse(200, page(MINE));
      if (url.pathname.endsWith("/stats/my/today")) return jsonResponse(200, { date: "2026-08-06" });
      if (url.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  function renderAs(role: Role) {
    resetSessionStore({
      user: { ...fakeUser, role },
      permissions: PERMISSIONS[role] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    return renderWithProviders(<ChatListPane />);
  }

  it("у менеджера «Входящие» стоят ПЕРВОЙ вкладкой и показывают размер очереди", async () => {
    renderAs("manager");

    // Бейдж берётся из page.total сервера — очередь может быть длиннее страницы.
    const inbox = await screen.findByRole("tab", { name: "Входящие, в очереди: 3" });
    const tabs = screen.getAllByRole("tab");
    expect(tabs[0]).toBe(inbox);
    // Ряд отвечает на ОДИН вопрос — чей диалог. Состояние («Новые»,
    // «Закрытые») переехало в фильтр «Статус»: два разных признака в одном
    // переключателе читались как повторяющие друг друга кнопки.
    expect(tabs.map((t) => t.textContent?.replace(/\d+/g, "").trim())).toEqual([
      "Входящие",
      "Мои",
      "Все",
    ]);
  });

  it("у наблюдателя вкладки «Входящие» нет вовсе и очередь не запрашивается", async () => {
    renderAs("observer");

    expect(await screen.findByRole("tab", { name: "Все" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Входящие/ })).toBeNull();
    expect(urls().some((u) => u.includes("/inbox"))).toBe(false);
  });

  it("у руководителя вкладки «Входящие» нет: он не отвечает клиентам (нет messages:send)", async () => {
    renderAs("head");

    expect(await screen.findByRole("tab", { name: "Все" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Входящие/ })).toBeNull();
    expect(urls().some((u) => u.includes("/inbox"))).toBe(false);
  });

  it("клик по «Входящие» показывает очередь, возврат на «Мои» — обычный список", async () => {
    const user = userEvent.setup();
    renderAs("manager");
    await screen.findByText("Иван Петров");

    await user.click(screen.getByRole("tab", { name: /Входящие/ }));

    expect(await screen.findByText("Мария Иванова")).toBeInTheDocument();
    expect(screen.queryByText("Иван Петров")).toBeNull();
    expect(useChatUiStore.getState().inboxOpen).toBe(true);

    await user.click(screen.getByRole("tab", { name: /^Мои/ }));

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
    expect(useChatUiStore.getState().inboxOpen).toBe(false);
    expect(useChatUiStore.getState().filters.tab).toBe("mine");
  });

  it("над очередью нет кнопки «Разгрузить» и предпросмотр не запрашивается", async () => {
    /*
     * Разгрузка очереди убрана целиком (требование владельца от 11 августа,
     * №8). Кнопка стояла ровно здесь — на вкладке очереди у того, кто из неё
     * берёт, то есть у менеджера, которым и открыт этот тест.
     *
     * Проверяется и отсутствие запроса: окно тянуло предпросмотр
     * `GET /inbox/stale` на каждое изменение срока, а ручки на сервере больше
     * нет — уцелевший вызов означал бы 404 в консоли у всех тринадцати.
     */
    const user = userEvent.setup();
    renderAs("manager");
    await screen.findByText("Иван Петров");

    await user.click(screen.getByRole("tab", { name: /Входящие/ }));
    await screen.findByText("Мария Иванова");

    expect(screen.queryByRole("button", { name: /Разгрузить/ })).toBeNull();
    expect(urls().some((u) => u.includes("/inbox/stale"))).toBe(false);
    expect(urls().some((u) => u.includes("close-stale"))).toBe(false);
  });

  it("счётчик двигают события, а не перезапрос: inbox:new +1, inbox:claimed −1", async () => {
    renderAs("manager");
    await screen.findByRole("tab", { name: "Входящие, в очереди: 3" });
    const before = urls().length;

    act(() => {
      applyWsEvent({
        type: "inbox:new",
        ts: "2026-08-06T10:00:00Z",
        data: { conversation_id: "q-4", conversation: row("q-4", "Павел Ким", true), can_claim: true },
      });
    });

    expect(await screen.findByRole("tab", { name: "Входящие, в очереди: 4" })).toBeInTheDocument();
    expect(playInboxChime).toHaveBeenCalledTimes(1); // новый в очереди звучит (7.1 п.6)

    act(() => {
      applyWsEvent({
        type: "inbox:claimed",
        ts: "2026-08-06T10:00:05Z",
        data: { conversation_id: "q-4", claimed_by: { id: "u-petr", full_name: "Пётр Ковалёв" } },
      });
    });

    expect(await screen.findByRole("tab", { name: "Входящие, в очереди: 3" })).toBeInTheDocument();
    // Ни одного похода за списком очереди ради счётчика (03 §3.3).
    expect(urls().filter((u) => u.includes("/inbox")).length).toBe(
      urls().slice(0, before).filter((u) => u.includes("/inbox")).length,
    );
  });

  it("диалог, вернувшийся после отказа, появляется молча", async () => {
    /**
     * ⚠ ЗВУК ЗНАЧИТ «ПРИШЁЛ НОВЫЙ КЛИЕНТ», И ЗНАЧИТЬ ЧТО-ТО ЕЩЁ ОН НЕ ДОЛЖЕН.
     *
     * С 13 августа отказ живёт три минуты (требование заказчика), после чего диалог
     * возвращается в очередь тому, кто нажал «Отклонить». Приезжает он тем же кадром
     * `inbox:new` — строка обязана появиться. Но клиент не новый: этот диалог оператор
     * уже видел и сам от него отказался. Позвони мы здесь — и у каждого, кто пользуется
     * кнопкой «Отклонить», каждые три минуты звучала бы ложная тревога.
     */
    renderAs("manager");
    await screen.findByRole("tab", { name: "Входящие, в очереди: 3" });

    act(() => {
      applyWsEvent({
        type: "inbox:new",
        ts: "2026-08-06T10:00:00Z",
        data: {
          conversation_id: "q-9",
          conversation: row("q-9", "Вернувшийся", true),
          can_claim: true,
          returned: true,
        },
      });
    });

    // Строка появилась — возврат виден без перезагрузки страницы.
    expect(await screen.findByRole("tab", { name: "Входящие, в очереди: 4" })).toBeInTheDocument();
    // А звука не было.
    expect(playInboxChime).not.toHaveBeenCalled();
  });

  it("повторный inbox:claimed счётчик в минус не уводит", async () => {
    renderAs("manager");
    await screen.findByRole("tab", { name: "Входящие, в очереди: 3" });

    const claimed = {
      type: "inbox:claimed" as const,
      ts: "2026-08-06T10:00:05Z",
      data: { conversation_id: "q-1", claimed_by: { id: "u-petr", full_name: "Пётр Ковалёв" } },
    };
    act(() => {
      applyWsEvent(claimed);
      applyWsEvent(claimed); // ретрансляция после reconnect
    });

    await waitFor(() => expect(useInboxStore.getState().count).toBe(2));
  });

  it("возвращённый в очередь диалог появляется снова — и звучит", async () => {
    const user = userEvent.setup();
    renderAs("manager");
    await user.click(await screen.findByRole("tab", { name: /Входящие/ }));
    await screen.findByText("Мария Иванова");
    vi.mocked(playInboxChime).mockClear();

    act(() => {
      applyWsEvent({
        type: "inbox:released",
        ts: "2026-08-06T10:05:00Z",
        data: {
          conversation_id: "q-9",
          conversation: row("q-9", "Никита Волков", true),
          released_by: { id: "u-petr", full_name: "Пётр Ковалёв" },
          can_claim: true,
        },
      });
    });

    // Диалог, который однажды уже никто не довёл, обязан вернуться заметно.
    expect(await screen.findByText("Никита Волков")).toBeInTheDocument();
    expect(await screen.findByRole("tab", { name: "Входящие, в очереди: 4" })).toBeInTheDocument();
    expect(playInboxChime).toHaveBeenCalledTimes(1);
  });

  it("возвращённый диалог встаёт НАВЕРХ очереди, а не в хвост", async () => {
    const user = userEvent.setup();
    renderAs("manager");
    await user.click(await screen.findByRole("tab", { name: /Входящие/ }));
    await screen.findByText("Мария Иванова");

    // При release сервер НЕ переставляет offered_at: клиент ждёт с утра, и его
    // место — первое. Дописанный в хвост, он спрятался бы под теми, кто ждёт
    // минуту, — ровно тот брошенный клиент, ради которого очередь и делалась.
    const returned = { ...row("q-0", "Вера Седых", true), offered_at: "2026-08-06T07:00:00Z" };
    act(() => {
      applyWsEvent({
        type: "inbox:released",
        ts: "2026-08-06T10:05:00Z",
        data: {
          conversation_id: "q-0",
          conversation: returned,
          released_by: { id: "u-petr", full_name: "Пётр Ковалёв" },
          can_claim: true,
        },
      });
    });

    await screen.findByText("Вера Седых");
    expect(inboxRows().map((r) => r.id)).toEqual(["q-0", "q-1", "q-2", "q-3"]);
  });

  it("кадр с can_claim=false не звенит и счётчик не двигает", async () => {
    renderAs("manager");
    await screen.findByRole("tab", { name: "Входящие, в очереди: 3" });
    vi.mocked(playInboxChime).mockClear();

    // Так хаб персонализирует кадр руководителю и наблюдателю: очередь им
    // видна, но она не их работа — звук и счётчик к ним не относятся.
    act(() => {
      applyWsEvent({
        type: "inbox:new",
        ts: "2026-08-06T10:00:00Z",
        data: { conversation_id: "q-5", conversation: row("q-5", "Лена Гор", true), can_claim: false },
      });
    });

    await waitFor(() => expect(useInboxStore.getState().count).toBe(3));
    expect(playInboxChime).not.toHaveBeenCalled();
  });

  it("принятая коллегой строка исчезает из очереди сама", async () => {
    const user = userEvent.setup();
    renderAs("manager");
    await user.click(await screen.findByRole("tab", { name: /Входящие/ }));
    expect(await screen.findByText("Сергей Орлов")).toBeInTheDocument();

    act(() => {
      applyWsEvent({
        type: "inbox:claimed",
        ts: "2026-08-06T10:00:05Z",
        data: { conversation_id: "q-2", claimed_by: { id: "u-petr", full_name: "Пётр Ковалёв" } },
      });
    });

    await waitFor(() => expect(screen.queryByText("Сергей Орлов")).toBeNull());
    expect(screen.getByText("Мария Иванова")).toBeInTheDocument();
  });
});
