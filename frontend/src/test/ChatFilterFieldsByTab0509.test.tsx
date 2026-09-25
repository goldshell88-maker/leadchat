import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import type { ConversationFilters } from "@/shared/api/queryKeys";
import type { ConversationDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * НАБОР ПОЛЕЙ РАЗНЫЙ У РАЗНЫХ ВКЛАДОК (просьба владельца 05.09: «расширь
 * фильтры для вкладки „Все“, а не только по источникам»).
 *
 * Правило, которое здесь стережётся: вкладка отвечает на вопрос «чей диалог»,
 * и поле, на которое вкладка УЖЕ ответила, на ней вредно. «Ответственный» на
 * «Моих» имеет ровно одно осмысленное значение — я сам; человек его трогает,
 * список не меняется, и он перестаёт верить всей панели, а не одному полю.
 * Ровно за это 12 августа выкинули селекты «Статус: все» и «Менеджер: все».
 *
 * ⚠ ОТРИЦАТЕЛЬНЫЕ ПРОВЕРКИ ЗДЕСЬ ПАДАЮТ ПО СВОЕЙ ПРИЧИНЕ. На «Моих» панель
 * открыта и НЕ пуста — в ней стоят канал и метка, и это проверяется тут же.
 * Значит «состояния и ответственного нет» означает именно их отсутствие, а не
 * незагруженный экран или неоткрытую панель.
 */

const базовая: ConversationDto = {
  id: "conv-1",
  status: "in_progress",
  channel: "avito",
  account: { id: "acc-1", title: "Стас КП" },
  client: { id: "cl-1", name: "Иван Петров", phone: null, avito_rating: null },
  assignee: null,
  item: null,
  last_message: { body: "Здравствуйте", direction: "in", created_at: "2026-09-05T09:00:00Z" },
  unread_count: 0,
  bot_active: false,
  tags: ["негатив"],
  transferred_to_me: false,
  last_message_at: "2026-09-05T09:00:00Z",
};

const строки: ConversationDto[] = [
  базовая,
  { ...базовая, id: "conv-2", account: { id: "acc-2", title: "Парт - 9" } },
];

const ВСЕ_ПРАВА: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "accounts:read",
  "stats:all",
];

function renderPane(filters: ConversationFilters, permissions: Permission[] = ВСЕ_ПРАВА) {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions,
    accessToken: "test-token",
    bootstrapped: true,
  });
  useChatUiStore.setState({ activeConversationId: null, filters, inboxOpen: false });

  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/avito-accounts")) {
      return jsonResponse(200, {
        items: [
          { id: "acc-1", title: "Стас КП" },
          { id: "acc-2", title: "Парт - 9" },
        ],
      });
    }
    if (url.pathname.endsWith("/users/assignable")) {
      return jsonResponse(200, {
        items: [{ id: "u-7", full_name: "Пётр Ковалёв", role: "manager", is_online: true }],
      });
    }
    if (url.pathname.endsWith("/conversations")) {
      return jsonResponse(200, { items: строки, page: { limit: 50, offset: 0, total: 2 } });
    }
    return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
  });
  vi.stubGlobal("fetch", fetchMock);

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/chats"]}>
          <ChatListPane />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

async function открытьПанель() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /Фильтры/ }));
  return user;
}

const высота = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const ширина = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("Набор полей фильтра по вкладкам", () => {
  beforeEach(() => {
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (высота) Object.defineProperty(HTMLElement.prototype, "offsetHeight", высота);
    if (ширина) Object.defineProperty(HTMLElement.prototype, "offsetWidth", ширина);
  });

  it("на «Все» есть состояние, ответственный и «ждут ответа»", async () => {
    renderPane({ tab: "all" });
    await открытьПанель();

    // Mantine рисует у Select видимый input и скрытый — оба с меткой.
    expect((await screen.findAllByLabelText("Фильтр по состоянию")).length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Фильтр по ответственному").length).toBeGreaterThan(0);
    expect(screen.getByRole("switch", { name: /Ждут ответа/ })).toBeInTheDocument();
  });

  it("на «Мои» этих полей нет — вкладка на них уже ответила", async () => {
    renderPane({ tab: "mine" });
    await открытьПанель();

    // Сперва убеждаемся, что панель ОТКРЫТА и не пуста: без этого отсутствие
    // полей ниже ничего не доказывало бы.
    expect((await screen.findAllByLabelText("Фильтр по каналу")).length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Фильтр по метке").length).toBeGreaterThan(0);

    expect(screen.queryByLabelText("Фильтр по состоянию")).toBeNull();
    expect(screen.queryByLabelText("Фильтр по ответственному")).toBeNull();
    expect(screen.queryByRole("switch", { name: /Ждут ответа/ })).toBeNull();
  });

  it("без права `stats:all` ответственного нет, а состояние есть", async () => {
    /*
     * Право то же, за которым фильтр «Менеджер ▾» стоял до 12 августа. И оно
     * же спасает от мёртвого контрола: справочник `/users/assignable` закрыт
     * правом `conversations:manage`; покажи мы поле оператору — он получил бы
     * пустой выпадающий список без единого слова о причине.
     */
    renderPane({ tab: "all" }, ["conversations:read", "messages:send", "conversations:manage"]);
    await открытьПанель();

    expect((await screen.findAllByLabelText("Фильтр по состоянию")).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Фильтр по ответственному")).toBeNull();
  });

  it("выбор состояния попадает в фильтры и называет себя чипом", async () => {
    const user = await открытьПанельПосле({ tab: "all" });

    await user.click(screen.getAllByLabelText("Фильтр по состоянию")[0]);
    await user.click(await screen.findByRole("option", { name: "Закрытые" }));

    expect(useChatUiStore.getState().filters.status).toBe("closed");
    expect(await screen.findByText("Состояние: закрытые")).toBeInTheDocument();
  });

  /**
   * ОДНО ИЗМЕРЕНИЕ — ОДИН КОНТРОЛ. «Ничей» стоит пунктом внутри
   * «Ответственного», а не отдельным выключателем: двумя контролами человек
   * выбрал бы «Пётр Ковалёв» И «без ответственного» разом — срез, пустой
   * всегда. На проводе они по-прежнему разные параметры, иначе опечатка в
   * uuid вместо честного 422 давала бы «показал всё».
   */
  it("«Без ответственного» — пункт того же поля, и он исключает выбор человека", async () => {
    const user = await открытьПанельПосле({ tab: "all", assigneeId: "u-7" });

    await user.click(screen.getAllByLabelText("Фильтр по ответственному")[0]);
    await user.click(await screen.findByRole("option", { name: "Без ответственного" }));

    const f = useChatUiStore.getState().filters;
    expect(f.unassigned).toBe(true);
    expect(f.assigneeId).toBeUndefined();
    // Ищем именно ЧИП: то же слово стоит и в самом поле как выбранное
    // значение, и в списке вариантов — а стережём мы видимость сужения.
    expect(
      await screen.findByText("Без ответственного", { selector: ".chat-list-pane__chip-label" }),
    ).toBeInTheDocument();
  });

  it("переход на «Мои» снимает сужения, которых там не бывает", async () => {
    /*
     * «Мои + ничей» — заведомо пустой список: диалог не может быть моим и
     * ничьим сразу. Человек искал бы причину в чём угодно, кроме фильтра,
     * которого на этой вкладке даже не показывают.
     */
    const user = userEvent.setup();
    renderPane({ tab: "all", unassigned: true, status: "closed", accountId: "acc-1" });
    await screen.findAllByText("Иван Петров");

    await user.click(screen.getByRole("tab", { name: /^Мои/ }));

    const f = useChatUiStore.getState().filters;
    expect({ unassigned: f.unassigned, status: f.status }).toEqual({
      unassigned: undefined,
      status: undefined,
    });
    // А канал переживает переключение: он про диалог, а не про принадлежность.
    expect(f.accountId).toBe("acc-1");
  });
});

async function открытьПанельПосле(filters: ConversationFilters) {
  renderPane(filters);
  return открытьПанель();
}
