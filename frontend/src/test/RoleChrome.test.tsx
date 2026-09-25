import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { MyTodayMenuItem } from "@/features/stats/MyTodayWidget";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { defaultTabForRole, useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const convRow: ConversationDto = {
  id: "conv-1",
  status: "in_progress",
  channel: "avito",
  account: { id: "acc-1", title: "LP-Москва" },
  client: { id: "client-1", name: "Иван Петров", phone: null, avito_rating: null },
  assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
  item: null,
  last_message: null,
  unread_count: 0,
  bot_active: false,
  // Метка нужна фикстуре: панель «Фильтры» показывается, только когда ей есть
  // что показать, а после перекройки полей в ней два — канал и метка.
  tags: ["важное"],
  transferred_to_me: false,
  last_message_at: "2026-08-04T09:40:12Z",
};

const page: ConversationsPage = { items: [convRow], page: { limit: 50, offset: 0, total: 120 } };

const MY_TODAY = {
  date: "2026-08-05",
  active_now: 7,
  waiting_reply_now: 2,
  taken_today: 5,
  closed_today: 3,
  messages_sent_today: 64,
  frt_median_sec_today: 74,
  answered_today: 6,
};

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

function renderList(role: Role, permissions: Permission[]) {
  resetSessionStore({
    user: { ...fakeUser, role },
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
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

/** Ролевые различия левой колонки (11 §2.5, 03 §5.2). */
describe("Левая колонка по ролям", () => {
  beforeEach(() => {
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" } });
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/users/assignable")) {
          return jsonResponse(200, {
            items: [{ id: "u-2", full_name: "Пётр Ковалёв", role: "manager", is_online: true }],
          });
        }
        if (url.pathname.endsWith("/stats/my/today")) return jsonResponse(200, MY_TODAY);
        if (url.pathname.endsWith("/conversations")) return jsonResponse(200, page);
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

  /**
   * ФИЛЬТРА «ПО МЕНЕДЖЕРУ» В КОЛОНКЕ БОЛЬШЕ НЕТ НИ У КОГО — включая head.
   *
   * Разбор живого экрана 12 августа: «Фильтры» повторяли табы. «Менеджер: все»
   * спрашивал ровно то же, что и таб «Мои», только другим способом и с другим
   * следом на экране: после выбора менеджера таб оставался подсвеченным, хотя
   * показывал уже не своё. Срез по конкретному сотруднику никуда не делся —
   * он живёт в «Разборе диалогов» (/table, параметр `assignee`), где рядом
   * есть период, сортировка и выгрузка, то есть всё, ради чего руководитель
   * этот срез и строит.
   */
  it("фильтра «по менеджеру» нет и у руководителя — это таб «Мои»", async () => {
    const user = userEvent.setup();
    renderList("head", ["conversations:read", "conversations:manage", "notes:write", "stats:all", "stats:own"]);
    await screen.findByText("Иван Петров");

    // Проверка обязана оставаться содержательной: сначала убеждаемся, что
    // область фильтров вообще отрисована (у head каналов в строках один, зато
    // метка «важное» даёт панели повод существовать), и лишь потом — что
    // менеджерского селекта в ней нет.
    await user.click(await screen.findByRole("button", { name: /Фильтры/ }));
    expect((await screen.findAllByLabelText("Фильтр по метке")).length).toBeGreaterThan(0);

    expect(screen.queryAllByLabelText("Фильтр по менеджеру")).toHaveLength(0);
    expect(screen.queryAllByLabelText("Фильтр по статусу")).toHaveLength(0);
    // Зато таб на месте — и он же отвечает на вопрос «чьи диалоги».
    expect(screen.getByRole("tab", { name: /^Мои/ })).toBeInTheDocument();
  });

  it("менеджер: фильтров-дублей нет, виджет «моя статистика» есть", async () => {
    renderList("manager", ["conversations:read", "messages:send", "conversations:manage", "stats:own"]);

    await screen.findByText("Иван Петров");
    // Содержательность держим на табах: они отрисовались — значит колонка жива,
    // и отсутствие селектов ниже означает именно отсутствие, а не пустой экран.
    expect(screen.getByRole("tablist")).toBeInTheDocument();
    expect(screen.queryAllByLabelText("Фильтр по менеджеру")).toHaveLength(0);
    expect(screen.queryAllByLabelText("Фильтр по статусу")).toHaveLength(0);

    const widget = await screen.findByLabelText("Моя статистика за сегодня");
    expect(widget).toHaveTextContent("Сегодня: 5 взято · 3 закрыто");
    expect(widget).toHaveTextContent("2 ждут ответа");
  });

  /**
   * СЧЁТЧИКА ДИАЛОГОВ В КОЛОНКЕ БОЛЬШЕ НЕТ — ни в подвале, ни в шапке.
   *
   * В подвале он повторял шапку (UX-аудит, docs/17), а 12 августа ушёл и из
   * шапки: при открытой очереди это было то же число, что уже светилось
   * бейджем таба «Входящие» и бейджем иконки раздела в рельсе. Сервер по-
   * прежнему отдаёт `page.total: 120` — и именно поэтому проверка ищет его на
   * экране: она обязана падать, если число вернут.
   */
  it("наблюдатель: ни фильтров-дублей, ни виджета, ни счётчика диалогов", async () => {
    renderList("observer", ["conversations:read"]);

    await screen.findByText("Иван Петров");
    expect(screen.getByRole("tablist")).toBeInTheDocument();
    expect(screen.queryAllByLabelText("Фильтр по менеджеру")).toHaveLength(0);
    expect(screen.queryByLabelText("Моя статистика за сегодня")).toBeNull();
    expect(screen.queryByLabelText(/Всего диалогов/)).toBeNull();
    expect(screen.queryByText("120")).toBeNull();
  });

  it("админ: счётчика диалогов тоже нет", async () => {
    renderList("admin", ["conversations:read", "messages:send", "conversations:manage", "stats:all", "stats:own"]);

    await screen.findByText("Иван Петров");
    expect(screen.queryByLabelText(/Всего диалогов/)).toBeNull();
    expect(screen.queryByText("120")).toBeNull();
    expect(screen.queryByLabelText("Моя статистика за сегодня")).toBeNull();
  });

  it("виджет не запрашивается ролями без stats:own", async () => {
    renderList("observer", ["conversations:read"]);
    await screen.findByText("Иван Петров");

    const urls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) =>
      String(c[0]),
    );
    expect(urls.some((u) => u.includes("/stats/my/today"))).toBe(false);
  });
});

/** Мобильная раскладка: виджет переезжает в меню аватара (11 §6.4). */
describe("«Моя статистика» пунктом меню аватара", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "manager" },
      permissions: ["conversations:read", "messages:send", "stats:own"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/stats/my/today")) return jsonResponse(200, MY_TODAY);
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("открывает те же цифры, что и поповер в подвале колонки", async () => {
    const user = userEvent.setup();
    // Оборачивать в `Menu` больше нечем: окно сотрудника стало `Popover`, а
    // строка в нём — обычной кнопкой (макет от 12 августа). Рисуем её так же,
    // как она живёт на экране, — саму по себе.
    renderWithProviders(<MyTodayMenuItem />);

    await user.click(await screen.findByRole("button", { name: "Моя статистика за сегодня" }));

    const modal = await screen.findByRole("dialog");
    expect(modal).toHaveTextContent("В работе сейчас");
    expect(modal).toHaveTextContent("Ждут моего ответа");
    expect(modal).toHaveTextContent("1 м 14 с"); // FRT 74 c — как в поповере
  });
});

/** Вкладка по умолчанию и сброс состояния при смене роли (11 §2.5, 01 §3.4). */
describe("Сброс UI-состояния при смене роли", () => {
  it("вкладка по умолчанию: менеджер — «Мои», остальные — «Все»", () => {
    expect(defaultTabForRole("manager")).toBe("mine");
    expect(defaultTabForRole("head")).toBe("all");
    expect(defaultTabForRole("admin")).toBe("all");
    expect(defaultTabForRole("observer")).toBe("all");
    expect(defaultTabForRole(undefined)).toBe("all");
  });

  it("resetForRole чистит фильтры и оверлей, черновики того же пользователя сохраняет", () => {
    useChatUiStore.setState({
      filters: { tab: "closed", q: "экран", assigneeId: "u-9" },
      clientCardOpen: true,
      activeConversationId: "conv-1",
      drafts: { "conv-1": { text: "черновик", isNote: false } },
      draftsOwnerId: fakeUser.id,
    });

    useChatUiStore.getState().resetForRole({ id: fakeUser.id, role: "manager" });

    const s = useChatUiStore.getState();
    expect(s.filters).toEqual({ tab: "mine" });
    expect(s.clientCardOpen).toBe(false);
    // Активный диалог — из URL (03 §6), сброс его не трогает: иначе холодный
    // старт по ссылке /chats/:id терял бы «открытый» диалог для WS и счётчиков.
    expect(s.activeConversationId).toBe("conv-1");
    expect(s.drafts["conv-1"].text).toBe("черновик");
  });

  it("другой пользователь на том же компьютере не получает чужие черновики", () => {
    useChatUiStore.setState({
      drafts: { "conv-1": { text: "текст менеджера", isNote: false } },
      draftsOwnerId: fakeUser.id,
    });

    useChatUiStore.getState().resetForRole({ id: "other-user", role: "head" });

    const s = useChatUiStore.getState();
    expect(s.drafts).toEqual({});
    expect(s.filters).toEqual({ tab: "all" });
  });
});
