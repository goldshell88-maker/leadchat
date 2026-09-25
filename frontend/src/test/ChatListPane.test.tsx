import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import type { ConversationFilters } from "@/shared/api/queryKeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

const convRow: ConversationDto = {
  id: "conv-1",
  status: "in_progress",
  channel: "avito",
  account: { id: "acc-1", title: "LP-Москва" },
  client: { id: "client-1", name: "Иван Петров", phone: null, avito_rating: 4.9 },
  assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
  item: { title: "Ремонт iPhone 13", url: null, price: null },
  last_message: {
    body: "А сколько будет стоить замена экрана?",
    direction: "in",
    created_at: "2026-08-04T09:40:12Z",
  },
  unread_count: 2,
  bot_active: false,
  tags: [],
  transferred_to_me: false,
  last_message_at: "2026-08-04T09:40:12Z",
};

function page(items: ConversationDto[]): ConversationsPage {
  return { items, page: { limit: 50, offset: 0, total: items.length } };
}

function renderPane() {
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

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

describe("ChatListPane", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const requestedUrls = () => fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));

  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" } });
    useUnreadStore.getState().clear();

    // Виртуализатор меряет скролл-элемент через offsetWidth/offsetHeight — в jsdom они нулевые.
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get: () => 600,
    });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
      configurable: true,
      get: () => 320,
    });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations")) {
        if (url.searchParams.get("q")) return jsonResponse(200, page([]));
        if (url.searchParams.get("tab") === "new") return jsonResponse(200, page([]));
        return jsonResponse(200, page([convRow]));
      }
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

  it("loads the default tab and renders conversation rows", async () => {
    renderPane();

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
    // В подстрочнике остался ТОЛЬКО канал: объявление слово в слово повторяло
    // блок «Заявка» в правой карточке (разбор владельца, 12 августа).
    expect(screen.getByText("LP-Москва")).toBeInTheDocument();
    expect(screen.queryByText(/Ремонт iPhone 13/)).toBeNull();
    expect(screen.getByText("2")).toBeInTheDocument(); // бейдж непрочитанных

    const urls = requestedUrls();
    expect(urls.some((u) => u.includes("/conversations?") && u.includes("tab=any"))).toBe(true);
  });

  it("переключение вкладки уходит в ?tab= и меняет пустое состояние", async () => {
    const user = userEvent.setup();
    renderPane();
    await screen.findByText("Иван Петров");

    await user.click(screen.getByRole("tab", { name: /^Мои/ }));

    await waitFor(() => {
      expect(requestedUrls().some((u) => u.includes("tab=mine"))).toBe(true);
    });
    expect(useChatUiStore.getState().filters.tab).toBe("mine");
  });

  it("вкладок состояния нет — их место заняли три пресета", async () => {
    renderPane();
    await screen.findByText("Иван Петров");

    expect(screen.queryByRole("tab", { name: "Новые" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Закрытые" })).not.toBeInTheDocument();
  });

  it("«Мои» вместе со статусом «Закрытые» не дают пустоту", async () => {
    // Вкладка «Мои» сама по себе означает «мои и ещё не закрытые». Если бы
    // статус просто добавлялся к этому условию, «Мои + Закрытые» превратилось
    // бы в «closed AND NOT closed» — пустой список на совершенно осмысленный
    // запрос «мои закрытые за сегодня». Явный выбор человека обязан
    // переопределять умолчание вкладки, и проверяется это здесь.
    //
    // Статус ставится через стор, а не селектом: селекта «Статус» в колонке
    // больше нет (он дублировал вкладки), а сам срез остался — его ставят
    // хоткеи Alt+3/Alt+4 и кнопка «Показать закрытые» в пустом состоянии.
    renderPane();
    await screen.findByText("Иван Петров");

    useChatUiStore.setState({ filters: { tab: "mine", status: "closed" } });

    await waitFor(() => {
      const url = requestedUrls().find((u) => u.includes("status=closed"));
      expect(url).toBeTruthy();
      expect(url).toContain("tab=mine");
    });
  });

  it("search input is debounced (300ms) into ?q= and dims the tabs", async () => {
    const user = userEvent.setup();
    renderPane();
    await screen.findByText("Иван Петров");

    await user.type(screen.getByLabelText("Поиск по диалогам"), "экран");

    // До дебаунса запроса с q= нет
    expect(requestedUrls().some((u) => u.includes("q=экран"))).toBe(false);

    await waitFor(
      () => {
        expect(requestedUrls().some((u) => u.includes("q=экран"))).toBe(true);
      },
      { timeout: 2000 },
    );

    // Один запрос на весь набранный текст, не по одному на букву
    expect(requestedUrls().filter((u) => u.includes("q=")).length).toBe(1);

    expect(screen.getByRole("tablist")).toHaveAttribute("data-dimmed");
    expect(await screen.findByText("Ничего не нашлось")).toBeInTheDocument();
  });

  // Закреплённые идут первыми, и без метки это читается как сбой сортировки:
  // строка стоит выше свежих, а почему — непонятно. Заказчик поймал ровно это.
  it("закреплённая строка помечена булавкой, обычная — нет", async () => {
    fetchMock.mockImplementation((input: string) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations")) {
        return jsonResponse(
          200,
          page([
            { ...convRow, id: "conv-pinned", client: { ...convRow.client, name: "Закреплённый" }, pinned: true },
            { ...convRow, id: "conv-plain", pinned: false },
          ]),
        );
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    renderPane();

    await screen.findByText("Закреплённый");
    expect(screen.getAllByLabelText("Закреплено")).toHaveLength(1);
  });
});

/**
 * #26 — неотправленный ответ виден в списке слева.
 *
 * ЧТО БЫЛО. Отправка упала — строка выглядела успешно отвеченной: «Вы: …» и
 * ничего красного. Красный крест существовал только в открытом диалоге, а
 * решение «идти дальше или вернуться» человек принимает по списку.
 */
describe("Неотправленный ответ в строке списка", () => {
  const failed: ConversationDto = {
    ...convRow,
    last_message: { body: "Перезвоню в течение часа", direction: "out", created_at: "2026-08-04T09:41:00Z" },
    unread_count: 0,
    undelivered: true,
  };

  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" } });
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  function serve(rows: ConversationDto[]) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/conversations")) return jsonResponse(200, page(rows));
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  }

  it("говорит словами, а не только цветом", async () => {
    serve([failed]);
    renderPane();

    // Словами: цвет один не годится — четверть мужчин-операторов различает
    // красный и зелёный хуже, чем принято считать, а строка одна из сорока.
    expect(await screen.findByText("не отправлено")).toBeInTheDocument();
  });

  it("поднимает полосу срочности выше остальных поводов", async () => {
    // У диалога разом негатив и провал доставки. Полоса одна, и она обязана
    // достаться провалу: негатив говорит «клиенту нужно внимание», провал —
    // «система не сделала того, что человек считает сделанным».
    serve([{ ...failed, tags: ["негатив"] }]);
    const { container } = renderPane();

    await screen.findByText("Иван Петров");
    expect(container.querySelector('.conv-card[data-urgency="undelivered"]')).toBeTruthy();
  });

  it("не трогает строки, где всё доставлено", async () => {
    serve([convRow]);
    const { container } = renderPane();

    await screen.findByText("Иван Петров");
    expect(screen.queryByText("не отправлено")).toBeNull();
    expect(container.querySelector('.conv-card[data-urgency="undelivered"]')).toBeNull();
  });
});

/**
 * Шапка списка не имеет права съёживаться под руками (бриф, пункт 5).
 *
 * ЧТО БЫЛО. Число контролов фильтра считалось от ЗАГРУЖЕННЫХ СТРОК: у
 * менеджера справочник аккаунтов закрыт правом, поэтому список каналов
 * собирался из самих диалогов. При смене фильтра запрос уходит с новым
 * ключом, строки на миг пустеют — каналов ноль, контрол один, и кнопка
 * «Фильтры» вместе с поповером исчезала прямо под пальцем.
 *
 * Хуже всего последствие: фильтр остался включённым, а снять его нечем —
 * и селект, и счётчик жили внутри поповера. Со стороны это «диалоги
 * пропали», и выход один: перезагрузить страницу, о чём человек не догадается.
 */
describe("Шапка фильтров переживает обновление списка", () => {
  const twoChannels = [
    { ...convRow, id: "c-1", account: { id: "acc-1", title: "Парт - 7" } },
    { ...convRow, id: "c-2", account: { id: "acc-2", title: "Парт - 9" } },
  ];

  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" } });
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  it("кнопка «Фильтры» остаётся, когда список на миг опустел", async () => {
    // Первый ответ — два канала, второй — пустой: ровно то, что видит
    // компонент между сменой фильтра и приходом новых строк.
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (!url.pathname.endsWith("/conversations")) {
          return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
        }
        call += 1;
        return jsonResponse(200, page(call === 1 ? twoChannels : []));
      }),
    );

    renderPane();
    await screen.findByRole("button", { name: /Фильтры/ });
    // Обе строки фикстуры от одного клиента — ищем все совпадения, иначе
    // поиск по одному падает на «найдено несколько».
    await screen.findAllByText("Иван Петров");

    // Смена фильтра роняет строки в ноль.
    useChatUiStore.setState({ filters: { tab: "all", accountId: "acc-1" } });

    // СНАЧАЛА дожидаемся, что список ДЕЙСТВИТЕЛЬНО опустел, — иначе проверка
    // ниже сработает на старой отрисовке и пройдёт при любой реализации.
    await waitFor(() => {
      expect(screen.queryAllByText("Иван Петров")).toHaveLength(0);
    });

    // И только теперь: кнопка обязана устоять на пустом списке.
    expect(screen.getByRole("button", { name: /Фильтры/ })).toBeInTheDocument();
  });

});

/**
 * Заявка заказчика №1 (docs/33 §1): «вкладка “Все” показывает 0 и “Подключите
 * аккаунт Авито”, хотя подключено два аккаунта и поиск диалоги находит».
 *
 * ЧТО БЫЛО. Текст пустого состояния выбирался по ОДНОМУ праву `users:manage`:
 * админу показывался всегда, при любой причине пустоты. Он шёл в настройки,
 * видел там живые каналы и оставался ни с чем.
 *
 * Причин три, действие на каждую своё — здесь проверяется, что экран их
 * различает. Серверное поведение вкладки («все, кроме закрытых») не трогаем:
 * это отдельное решение заказчика, фронт лишь перестаёт о нём молчать.
 */
describe("Пустой список называет причину пустоты", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  const TWO_ACCOUNTS = [
    { id: "acc-1", title: "Парт - 7" },
    { id: "acc-2", title: "Парт - 9" },
  ];

  beforeEach(() => {
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  /** Админ: справочник каналов ему открыт, значит число каналов на экране известно. */
  function renderFor({
    accounts,
    filters = { tab: "all" },
    rows = [],
    assigneeLabel = null,
  }: {
    accounts: Array<{ id: string; title: string }>;
    filters?: ConversationFilters;
    rows?: ConversationDto[];
    assigneeLabel?: string | null;
  }) {
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["conversations:read", "accounts:read", "users:manage"],
      accessToken: "test-token",
      bootstrapped: true,
    });
    useChatUiStore.setState({ activeConversationId: null, filters, assigneeLabel });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/avito-accounts")) {
        return jsonResponse(200, { items: accounts, page: { limit: 50, offset: 0, total: accounts.length } });
      }
      if (url.pathname.endsWith("/conversations")) return jsonResponse(200, page(rows));
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    return renderPane();
  }

  it("каналов нет — единственный случай, где верно «подключите аккаунт»", async () => {
    renderFor({ accounts: [] });

    expect(await screen.findByText(/Не подключён ни один аккаунт Авито/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "К настройкам" })).toBeInTheDocument();
    expect(screen.queryByText("Открытых диалогов нет")).toBeNull();
  });

  it("на «Все» закрытые уже видны — кнопки нет, и текст не обещает лишнего", async () => {
    /*
     * ⚠ 18.08: вкладка «Все» включает закрытые и архив ВСЕГДА (решение
     * владельца, api.ts шлёт tab=any). Кнопка «Показать закрытые» на ней
     * ничего не меняла — экран предлагал действие без последствий.
     */
    renderFor({ accounts: TWO_ACCOUNTS });

    expect(await screen.findByText("Диалогов нет")).toBeInTheDocument();
    expect(screen.queryByText(/Не подключён ни один аккаунт/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Показать закрытые" })).toBeNull();
    expect(screen.getByText(/включая закрытые и архив/)).toBeInTheDocument();

    /*
     * ⚠ ЖДЁМ ПЕРЕКЛЮЧАТЕЛЬ, А НЕ ФИЛЬТР СТАТУСА — правка 14 августа.
     * Раньше здесь стояло `filters.status === "closed"`, и это закрепляло
     * расхождение: текст над кнопкой обещает переключатель «Показывать
     * закрытые» из «Фильтров», а кнопка ставила совсем другое поле. Нажали —
     * закрытые появились; открыли «Фильтры» — переключатель выключен, бейдж
     * сужений пуст, снять сужение нечем.
     *
     * Заодно меняется и выборка, в сторону обещанного текстом: `withClosed`
     * значит «всё, включая закрытые» (tab=any), а `status=closed` показывал
     * ТОЛЬКО закрытые.
     */
  });

  it("сузили фильтром — «ничего не подходит», и сужения снимаются одной кнопкой", async () => {
    const user = userEvent.setup();
    // Оба сужения панели разом: кнопка обязана снять каждое, а не одно.
    renderFor({
      accounts: TWO_ACCOUNTS,
      filters: { tab: "all", accountId: "acc-1", tag: "негатив" },
    });

    expect(await screen.findByText("Ничего не подходит под фильтры")).toBeInTheDocument();
    expect(screen.queryByText("Открытых диалогов нет")).toBeNull();
    // Бейдж сужений на кнопке «Фильтры» — до сброса он горит.
    expect(screen.getByLabelText("Сужений: 2")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Сбросить фильтры" }));

    const { accountId, tag } = useChatUiStore.getState().filters;
    expect({ accountId, tag }).toEqual({ accountId: undefined, tag: undefined });
    // Кнопка обязана гасить ровно то, что считает бейдж: разъедься они —
    // список вернулся бы, а метка сужения осталась гореть.
    expect(screen.queryByLabelText("Сужений: 2")).toBeNull();
  });

  /**
   * СУЖЕНИЕ ПО СОСТОЯНИЮ ОБЯЗАНО НАЗЫВАТЬ СЕБЯ.
   *
   * Селект «Статус» из панели ушёл (он дублировал вкладки), а сам срез остался:
   * его ставят хоткеи Alt+3/Alt+4 и кнопка «Показать закрытые» отсюда же.
   * Значит появилось положение, в котором список сужен, а контрола, который
   * это показывает, на экране нет. Промолчи мы — человек прочитал бы
   * «Открытых диалогов нет», глядя как раз на выборку закрытых.
   */
  it("пусто по срезу состояния — срез назван словами и снимается кнопкой", async () => {
    const user = userEvent.setup();
    renderFor({ accounts: TWO_ACCOUNTS, filters: { tab: "all", status: "closed" } });

    expect(await screen.findByText("Нет диалогов: закрытые")).toBeInTheDocument();
    expect(screen.queryByText("Открытых диалогов нет")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Снять сужение" }));
    expect(useChatUiStore.getState().filters.status).toBeUndefined();
  });

  /**
   * ТО ЖЕ САМОЕ, НО ПРО СОТРУДНИКА — И ЭТО НЕ ПОВТОР ПРЕДЫДУЩЕГО СЛУЧАЯ.
   *
   * Селект «Менеджер» ушёл из панели ТЕМ ЖЕ коммитом 12 августа, что и
   * «Статус», и по тому же доводу. Но в перечень причин пустоты его тогда не
   * внесли, и на бою (замер 14 августа) это стоило руководителю следующего:
   * он приходит из отчёта, где у сотрудника 34 диалога, и читает «Открытых
   * диалогов нет». Открытые диалоги есть — их просто ведёт кто-то другой.
   */
  it("пусто по сотруднику — назван он, а не «открытых диалогов нет»", async () => {
    const user = userEvent.setup();
    renderFor({
      accounts: TWO_ACCOUNTS,
      filters: { tab: "all", assigneeId: "u-7" },
      assigneeLabel: "Пётр Ковалёв",
    });

    expect(await screen.findByText("Нет диалогов: Пётр Ковалёв")).toBeInTheDocument();
    expect(screen.queryByText("Открытых диалогов нет")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Показать всех" }));
    expect(useChatUiStore.getState().filters.assigneeId).toBeUndefined();
  });

  /**
   * СРЕЗ ПО СОСТОЯНИЮ ВИДЕН И ПРИ НЕПУСТОМ СПИСКЕ (правка 14 августа).
   *
   * Справка обещает: Alt+3 / Alt+4 — «„Все“ с фильтром состояния». Обработчик
   * его и ставит. А показать было нечем: селект «Статус» из панели ушёл
   * 12 августа, в бейдж сужений состояние не входит по записанному решению, и
   * при НЕПУСТОМ результате человек нажимал сочетание, видел другой список и
   * не имел на экране ни признака сужения, ни кнопки его снять.
   */
  it("срез по состоянию назван чипом и снимается им же", async () => {
    const user = userEvent.setup();
    renderFor({
      accounts: TWO_ACCOUNTS,
      rows: [convRow],
      filters: { tab: "all", status: "closed" },
    });

    expect(await screen.findByText(/Состояние: закрытые/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Снять сужение по состоянию" }));
    expect(useChatUiStore.getState().filters.status).toBeUndefined();
  });

  /**
   * ⚠ САМЫЙ ВРЕДНЫЙ ИЗ ТРЁХ: ДВЕ РАЗНЫЕ НЕПРАВДЫ ПОДРЯД.
   *
   * Когда вместе с сотрудником переносился канал, экран писал «Ничего не
   * подходит под фильтры»; кнопка «Сбросить фильтры» снимала канал, список
   * оставался пустым, а надпись МЕНЯЛАСЬ на «Открытых диалогов нет». Человек
   * послушно выполнял оба совета и оба раза оставался ни с чем.
   *
   * Поэтому при двух сужениях называется то, которого на экране НЕ ВИДНО:
   * канал лежит в панели, и снять его человек может сам.
   */
  it("сужений два — назван сотрудник, потому что канал видно и так", async () => {
    renderFor({
      accounts: TWO_ACCOUNTS,
      filters: { tab: "all", assigneeId: "u-7", accountId: "acc-1" },
      assigneeLabel: "Пётр Ковалёв",
    });

    expect(await screen.findByText("Нет диалогов: Пётр Ковалёв")).toBeInTheDocument();
    expect(screen.getByText("Список сужен по сотруднику — и ещё фильтрами из панели")).toBeInTheDocument();
    expect(screen.queryByText("Ничего не подходит под фильтры")).toBeNull();
  });

  /**
   * И ВТОРАЯ ПОЛОВИНА ТОЙ ЖЕ БЕДЫ: сужение видно, даже когда список НЕ пуст.
   * Пустое состояние объясняет срез само — но при двух найденных диалогах
   * экран молчал вовсе, и руководитель, пришедший из отчёта с числом 34,
   * просто считал, что врёт отчёт.
   */
  it("список не пуст — чип над ним всё равно называет сотрудника и снимает сужение", async () => {
    const user = userEvent.setup();
    renderFor({
      accounts: TWO_ACCOUNTS,
      rows: [convRow],
      filters: { tab: "all", assigneeId: "u-7" },
      assigneeLabel: "Пётр Ковалёв",
    });

    expect(await screen.findByText("Оператор: Пётр Ковалёв")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Показать диалоги всех сотрудников" }));
    expect(useChatUiStore.getState().filters.assigneeId).toBeUndefined();
    // Подпись уходит вместе с сужением: чип с чужим именем над полным списком
    // был бы ровно тем же враньём, только наоборот.
    expect(useChatUiStore.getState().assigneeLabel).toBeNull();
  });
});

/**
 * ПЕРЕКРОЙКА ЛЕВОЙ КОЛОНКИ 12 АВГУСТА — разбор живого экрана владельцем.
 *
 * Принцип один на всю работу: ОДНО ДЕЙСТВИЕ ЖИВЁТ В ОДНОМ МЕСТЕ, ОДНО ЗНАЧЕНИЕ
 * ПОКАЗЫВАЕТСЯ ОДИН РАЗ. В колонке нашлось три нарушения:
 *
 *  · счётчик очереди светился ТРИЖДЫ — бейджем на иконке раздела в рельсе,
 *    бейджем на табе «Входящие» и цифрой у заголовка «Чаты» (плюс пунктирный
 *    чип «кроме закрытых», заведённый накануне, чтобы цифру объяснить);
 *  · «Фильтры» повторяли табы: внутри «Статус: все» и «Менеджер: все», то есть
 *    те же два вопроса, на которые отвечают «Входящие / Мои / Все»;
 *  · панель фильтров была поповером и накрывала список — заявка №6, которую
 *    уже пытались чинить шириной поповера.
 */
describe("Левая колонка после перекройки", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  const twoChannels = [
    { ...convRow, id: "c-1", account: { id: "acc-1", title: "Парт - 7" }, tags: ["негатив"] },
    { ...convRow, id: "c-2", account: { id: "acc-2", title: "Парт - 9" } },
  ];

  function renderWith(rows: ConversationDto[], filters: ConversationFilters = { tab: "all" }) {
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["conversations:read", "messages:send", "conversations:manage", "stats:all"],
      accessToken: "test-token",
      bootstrapped: true,
    });
    useChatUiStore.setState({ activeConversationId: null, filters, inboxOpen: false });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations")) {
        return jsonResponse(200, { items: rows, page: { limit: 50, offset: 0, total: 120 } });
      }
      if (url.pathname.endsWith("/inbox")) {
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });
    vi.stubGlobal("fetch", fetchMock);
    return renderPane();
  }

  beforeEach(() => {
    useUnreadStore.getState().clear();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
  });

  it("у заголовка «Чаты» нет ни числа, ни чипа-оговорки", async () => {
    const { container } = renderWith(twoChannels);
    await screen.findAllByText("Иван Петров");

    // Число 120 приходит в `page.total` — раньше оно печаталось прямо тут,
    // повторяя бейдж таба «Входящие» и бейдж раздела в панели.
    const title = container.querySelector(".chat-list-pane__title");
    expect(title?.textContent).toBe("Чаты");
    expect(screen.queryByLabelText(/Всего диалогов/)).toBeNull();
    expect(screen.queryByText("кроме закрытых")).toBeNull();
  });

  /**
   * CHATS-08 — проверено по коду и подтверждено: `unreadStore` засеивается
   * только выданными страницами (`fetchConversations` → `seedFromRows`,
   * страница 50), а `selectMineUnreadDialogs` считает по этому же складу.
   * Значит бейдж «Мои» показывал долю правды и рос по мере прокрутки. Убран:
   * неверный счётчик хуже отсутствующего — по нему решают, идти ли в раздел.
   */
  it("бейдж непрочитанных остался только у «Входящих»", async () => {
    renderWith(twoChannels);
    await screen.findAllByText("Иван Петров");

    // Склад непрочитанных полон — ровно то положение, в котором бейдж горел.
    expect(useUnreadStore.getState().byConversation["c-1"].count).toBe(2);

    const mine = screen.getByRole("tab", { name: /^Мои/ });
    expect(mine.querySelector(".chat-tabs__count")).toBeNull();
    expect(mine.textContent).toBe("Мои");

    const all = screen.getByRole("tab", { name: /^Все/ });
    expect(all.querySelector(".chat-tabs__count")).toBeNull();
  });

  /**
   * ⚠ ЧТО ЭТОТ СТОРОЖ СТЕРЕЖЁТ ПОСЛЕ 05.09 — ЧИТАТЬ ПЕРЕД ПРАВКОЙ.
   *
   * Сужения по состоянию и по ответственному в панель ВЕРНУЛИСЬ (просьба
   * владельца «расширь фильтры для вкладки „Все“»), и этот тест по-прежнему
   * зелёный не по случайности. Убирали 12 августа не сами сужения, а два
   * селекта СО ЗНАЧЕНИЕМ «ВСЕ»: «Статус: все» и «Менеджер: все» спрашивали
   * ровно то, на что отвечает ряд вкладок над ними. Их имена и стережёт
   * проверка ниже — вернуться они не должны.
   *
   * У новых полей ни этих имён, ни пункта «все»: «Фильтр по состоянию» и
   * «Фильтр по ответственному», а «снять» делает крестик чипа и `clearable`.
   * Свой сторож у них тоже свой — `ChatFilterFieldsByTab0509.test.tsx`.
   */
  it("в «Фильтрах» нет статуса и менеджера — они и есть табы", async () => {
    const user = userEvent.setup();
    renderWith(twoChannels);
    await screen.findAllByText("Иван Петров");

    await user.click(screen.getByRole("button", { name: /Фильтры/ }));

    // Канал и метка остались: сужения, которых табами не выразить.
    // `All` — Mantine рисует у Select видимый input и скрытый, оба с меткой.
    expect((await screen.findAllByLabelText("Фильтр по каналу")).length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Фильтр по метке").length).toBeGreaterThan(0);
    // А эти два спрашивали ровно то же, что и три таба сверху.
    expect(screen.queryByLabelText("Фильтр по статусу")).toBeNull();
    expect(screen.queryByLabelText("Фильтр по менеджеру")).toBeNull();
  });

  /**
   * Заявка владельца №6. Ключевое слово — ВНУТРИ: панель обязана быть частью
   * колонки, а не слоем над ней. Проверяем не «красиво», а структурно: узел
   * панели лежит в шапке колонки, а лента списка идёт СЛЕДОМ за шапкой, то
   * есть панель её сдвигает, а не накрывает.
   */
  it("панель фильтров рисуется внутри колонки, а не поверх списка", async () => {
    const user = userEvent.setup();
    const { container } = renderWith(twoChannels);
    await screen.findAllByText("Иван Петров");

    await user.click(screen.getByRole("button", { name: /Фильтры/ }));

    const pane = container.querySelector(".chat-list-pane");
    const header = container.querySelector(".chat-list-pane__header");
    const panel = (await screen.findAllByLabelText("Фильтр по каналу"))[0];
    const scroll = container.querySelector(".chat-list-pane__scroll");

    expect(header?.contains(panel)).toBe(true);
    // Поповер Mantine рисуется вне колонки (в портале в конце документа) —
    // здесь этого быть не должно ни при каких условиях.
    expect(pane?.contains(panel)).toBe(true);
    expect(panel.closest(".mantine-Popover-dropdown")).toBeNull();
    // Список — сосед шапки, а не подложка под ней.
    expect(scroll?.contains(panel)).toBe(false);
    expect(header?.nextElementSibling).toBe(scroll);
  });

  it("нажатие на таб снимает состояние и менеджера — таб это пресет", async () => {
    const user = userEvent.setup();
    renderWith(twoChannels, { tab: "all", status: "closed", assigneeId: "u-7" });
    await screen.findAllByText("Иван Петров");

    await user.click(screen.getByRole("tab", { name: /^Мои/ }));

    const f = useChatUiStore.getState().filters;
    expect({ tab: f.tab, status: f.status, assigneeId: f.assigneeId }).toEqual({
      tab: "mine",
      status: undefined,
      assigneeId: undefined,
    });
  });

  it("выбранный канал не даёт селекту исчезнуть на опустевшем списке", async () => {
    // CHATS-16 в прежнем виде: «максимум увиденного» держали в ref, который
    // мутировали в теле рендера. Теперь условие прямое — контрол виден, пока
    // его значение выбрано, — и проверяется оно на самом опасном моменте:
    // строк ноль, каналов из строк ноль, фильтр включён.
    const user = userEvent.setup();
    renderWith([], { tab: "all", accountId: "acc-1" });

    await user.click(await screen.findByRole("button", { name: /Фильтры/ }));
    expect((await screen.findAllByLabelText("Фильтр по каналу")).length).toBeGreaterThan(0);
  });
});
