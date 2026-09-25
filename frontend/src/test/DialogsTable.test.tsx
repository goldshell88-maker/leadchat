import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { TablePage } from "@/features/table/TablePage";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно. Тот же приём, что в
// breakpoints.test.ts и mantineStyles.test.ts.
import { readFileSync } from "node:fs";
import type { TableRow } from "@/features/table/api";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Таблица диалогов (/dialogs, план 7.3).
 *
 * Проверяется не «рисуются ли строки», а то, на чём отчёт врёт руководителю
 * или отказывает ему в работе: «ещё не ответили» не должно выглядеть как
 * мгновенный ответ; срез обязан жить в адресе, иначе его не сохранить и не
 * передать; строка обязана быть ссылкой, иначе разбор нельзя вести в
 * нескольких вкладках; в фильтре операторов обязаны быть уволенные — их и
 * ищут чаще всего.
 */

const ANNA = "3f1c1a2e-0000-4000-8000-000000000001";

const ROWS: TableRow[] = [
  {
    id: "c-1",
    status: "in_progress",
    client_name: "Павел Ушаков",
    client_phone: "+7 908 123-45-08",
    assignee_id: ANNA,
    assignee_name: "Анна Смирнова",
    // Отдел приезжает ОТДЕЛЬНЫМ полем, а не вклеенным в имя: имя уходит ещё
    // и в выгрузку CSV, где скобки сломали бы фильтр Excel по оператору.
    assignee_department: "ОКК",
    item_title: "Кофемашина DeLonghi",
    account_id: "acc-1",
    account_title: "Парт-7",
    tags: ["срочно"],
    bot_active: false,
    unread_count: 0,
    last_message_at: "2026-08-06T14:24:00Z",
    messages_count: 3,
    first_response_sec: 41 * 60, // дольше пятнадцати минут — «поздно»
    duration_sec: 52 * 60,
  },
  {
    id: "c-2",
    status: "new",
    client_name: "Алексей Смирнов",
    client_phone: null,
    assignee_id: null,
    assignee_name: null,
    item_title: null,
    account_id: "acc-1",
    account_title: "Парт-7",
    tags: ["ночной-лид"],
    bot_active: true,
    unread_count: 1,
    last_message_at: "2026-08-06T14:20:00Z",
    messages_count: 1,
    first_response_sec: null, // ещё не ответили
    duration_sec: 0, // переписка из одного сообщения — ноль тут честен
  },
  {
    id: "c-3",
    status: "closed",
    client_name: "Мария Кузнецова",
    client_phone: null,
    assignee_id: ANNA,
    assignee_name: "Анна Смирнова",
    // Отдел приезжает ОТДЕЛЬНЫМ полем, а не вклеенным в имя: имя уходит ещё
    // и в выгрузку CSV, где скобки сломали бы фильтр Excel по оператору.
    assignee_department: "ОКК",
    item_title: "Стиральная машина Indesit",
    account_id: "acc-2",
    account_title: "Парт-900",
    tags: [],
    bot_active: false,
    unread_count: 0,
    last_message_at: "2026-08-06T13:13:00Z",
    messages_count: 3,
    first_response_sec: 3 * 60,
    duration_sec: 18 * 60,
  },
];

/** Показывает query-строку роутера: срез обязан быть виден в адресе. */
/**
 * ОТКРЫТЬ ПАНЕЛЬ «ЕЩЁ» (разбор интерфейса 13.08).
 *
 * Статус, оператор, бот и метка уехали под кнопку: ряд фильтров переносился в две
 * строки и отнимал у таблицы 72px высоты. Тесты, которые эти поля трогают, теперь
 * обязаны сперва раскрыть панель — ровно как человек.
 *
 * ⚠ ЭТО НЕ «ПОДГОНКА ТЕСТА ПОД ПРАВКУ». Пять упавших проверок и были доказательством,
 * что поля действительно спрятаны; если завтра панель сломается, они упадут снова —
 * уже на самой кнопке, а не на поле.
 */
async function открытьЕщё() {
  await userEvent.click(screen.getByRole("button", { name: /^Ещё/ }));
}

function UrlProbe() {
  const { search } = useLocation();
  return <output data-testid="url">{search}</output>;
}

describe("Таблица диалогов", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  /** Запросы именно к таблице — служебные справочники в счёт не идут. */
  const tableCalls = () =>
    fetchMock.mock.calls
      .map((c) => new URL(String(c[0]), "http://localhost"))
      .filter((u) => u.pathname.endsWith("/conversations/table"));

  const lastTableCall = () => tableCalls().at(-1)!;

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "stats:all", "accounts:read"],
      accessToken: "t",
      bootstrapped: true,
    });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations/table")) {
        return jsonResponse(200, { items: ROWS, page: { limit: 50, offset: 0, total: 3 } });
      }
      if (url.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      if (url.pathname.endsWith("/avito-accounts")) {
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const render = (route = "/dialogs") =>
    renderWithProviders(
      <>
        <TablePage />
        <UrlProbe />
      </>,
      { route },
    );

  const url = () => screen.getByTestId("url").textContent ?? "";

  /** Как `headerCell`, но без падения: колонки теперь бывают скрыты фильтром. */
  const headerCellOrNull = (title: string) =>
    [...document.querySelectorAll(".dt__scroll th")].find((th) =>
      th.textContent?.trim().startsWith(title),
    );

  const headerCell = (title: string) =>
    [...document.querySelectorAll(".dt__scroll th")].find((th) =>
      th.textContent?.trim().startsWith(title),
    )!;

  it("«ещё не ответили» — прочерк, а не ноль секунд", async () => {
    const { container } = render();
    expect(await screen.findByText("Павел Ушаков")).toBeInTheDocument();

    const frt = [...container.querySelectorAll(".dt__frt")];
    expect(frt.map((n) => n.textContent)).toEqual(["41 мин", "—", "3 мин"]);

    // Ноль читался бы как «ответили мгновенно» — то есть отчёт показывал бы
    // идеальную скорость ровно там, где клиента не обслужили вовсе.
    expect(frt[1]).toHaveAttribute("data-never");
    expect(frt[1]).not.toHaveAttribute("data-late");
    // Красным — только просроченное. Прочерк приглушён намеренно: красный в
    // каждой третьей строке приучил бы не смотреть на цвет вовсе.
    expect(frt[0]).toHaveAttribute("data-late");
    expect(frt[2]).not.toHaveAttribute("data-late");
  });

  /* --- часовой пояс: одно место вместо трёх --------------------------------
   *
   * ПЕРЕКРОЙКА (разбор живого экрана владельцем). Про пояс здесь говорили
   * трижды: строкой в шапке «Время в таблице московское: сейчас в Москве…»,
   * подписью «(МСК)» в заголовке колонки и подсказкой «21:24 у вас · 14:24 по
   * Москве» по наведению на ячейку. Три текста об одном — шум: читать их
   * перестают все, а часы в уме человек всё равно вычитает, потому что
   * смотрит-то он на число в ячейке.
   *
   * Теперь место одно — переключатель над таблицей: он и называет пояс, и
   * меняет его.
   *
   * ЧАСЫ МАШИНЫ, ГДЕ ИДЁТ ПРОГОН, НЕИЗВЕСТНЫ и решают исход: у сотрудника с
   * московскими часами выбора нет и переключателя тоже. Поэтому пояс всюду
   * задаётся явно — иначе тест зеленел бы на ноутбуке разработчика во
   * Владивостоке и падал на сборке в UTC, и падал бы не по делу.
   */

  const VLADIVOSTOK = "Asia/Vladivostok"; // UTC+10
  const SIMFEROPOL = "Europe/Simferopol"; // имя не московское, а часы московские

  function pretendZone(timeZone: string) {
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      timeZone,
    } as Intl.ResolvedDateTimeFormatOptions);
  }

  const lastCells = (container: HTMLElement) =>
    [...container.querySelectorAll(".dt__scroll tbody tr")].map(
      (tr) => tr.lastElementChild?.textContent,
    );

  const zonePicker = () =>
    screen.queryByRole("textbox", { name: "Часовой пояс времени в таблице" });

  it("по умолчанию время московское — отчёт остаётся общим документом", async () => {
    // Покажи отчёт время браузера, руководитель в Москве и руководитель во
    // Владивостоке, обсуждая одну строку, называли бы разные часы, и спор о
    // том, когда ответили клиенту, нечем было бы разрешить. Ровно та же
    // причина, по которой московское время печатается в выгрузке.
    pretendZone(VLADIVOSTOK);
    const { container } = render();
    await screen.findByText("Павел Ушаков");

    // 14:24 UTC → 17:24 по Москве, а не 00:24 по часам сотрудника.
    expect(lastCells(container)[0]).toContain("17:24");
  });

  it("пояс объясняется ОДИН раз — переключателем, а не тремя подписями", async () => {
    pretendZone(VLADIVOSTOK);
    const { container } = render();
    await screen.findByText("Павел Ушаков");

    expect(zonePicker()).toBeInTheDocument();

    // Ни одного из трёх прежних объяснений остаться не должно.
    expect(screen.queryByText(/Время в таблице московское/)).toBeNull();
    const headers = [...container.querySelectorAll(".dt__scroll th")].map((th) => th.textContent);
    expect(headers.some((h) => h?.includes("МСК"))).toBe(false);
    const cell = container.querySelector(".dt__scroll tbody tr")?.lastElementChild;
    expect(cell?.querySelector("span")?.getAttribute("title")).toBeNull();
  });

  it("переключение на свои часы пересчитывает колонку и живёт в ссылке", async () => {
    pretendZone(VLADIVOSTOK);
    const { container } = render();
    await screen.findByText("Павел Ушаков");
    const before = tableCalls().length;

    await userEvent.click(zonePicker()!);
    await userEvent.click(await screen.findByRole("option", { name: "Время: у вас" }));

    // 06.08 14:24 UTC → 07.08 00:24 на UTC+10: и час другой, и СУТКИ другие —
    // ради этого сотрудник и вычитал семь часов в уме каждую сверку.
    await waitFor(() => expect(lastCells(container)[0]).toContain("07.08 00:24"));
    // Срез живёт в адресе целиком, и способ читать время — его часть: ссылку
    // пересылают, и открыться она обязана тем же самым.
    expect(url()).toContain("tz=local");
    // Но таблицу за этим не перезапрашивают: сервер отдаёт моменты в UTC, а в
    // какие часы их перевести — дело экрана.
    expect(tableCalls().length).toBe(before);
  });

  it("на своих часах кнопка выгрузки предупреждает, что в файле — московское", async () => {
    // Время в CSV печатает сервер, и оно всегда московское. Пока экран тоже
    // московский, говорить не о чем; после переключения экран и файл
    // расходятся — промолчать значит дать человеку решить, что выгрузка врёт.
    pretendZone(VLADIVOSTOK);
    render("/dialogs?tz=local");
    await screen.findByText("Павел Ушаков");

    expect(screen.getByRole("button", { name: "Выгрузить CSV (МСК)" })).toBeInTheDocument();
  });

  it("«Сбросить» снимает фильтры, но не переводит часы обратно", async () => {
    // «Сбросить» отвечает на вопрос «покажи всё заново», а не «читай время
    // чужими часами». Разбор — это десятки сбросов подряд.
    pretendZone(VLADIVOSTOK);
    render("/dialogs?status=closed&tz=local");
    await screen.findByText("Павел Ушаков");

    await userEvent.click(screen.getByRole("button", { name: "Сбросить фильтры" }));

    await waitFor(() => expect(url()).not.toContain("status="));
    expect(url()).toContain("tz=local");
  });

  it("у сотрудника с московскими часами переключателя нет вовсе", async () => {
    // Обе половины показали бы одно и то же число. Выбор без разницы заставляет
    // человека проверять, не показалось ли ему.
    pretendZone(SIMFEROPOL); // имя зоны своё, часы московские
    const { container } = render();
    await screen.findByText("Павел Ушаков");

    expect(zonePicker()).toBeNull();
    expect(lastCells(container)[0]).toContain("17:24");
  });

  // --- горизонтальная прокрутка ---------------------------------------------
  //
  // Заказчик винил колонку «Объявление». Замер показал обратное: пустая колонка
  // занимала 118 px против 226 px заполненной, то есть пустота таблицу СУЖАЛА,
  // а прокрутку давали неразрывные заголовки — около 1319 px минимальной ширины
  // на девять столбцов. С 11 августа объявления заполняются, и прокрутка только
  // усилилась бы.

  it("узкий экран превращает строки в карточки, а не в прокрутку вбок", async () => {
    const { container } = render();
    await screen.findByText("Павел Ушаков");

    const table = container.querySelector("table")!;
    // Механизм общий (app/lc-table-cards.css) и подключён к трём другим
    // таблицам; эта осталась без него — при том, что столбцов у неё больше всех.
    expect(table.className).toContain("lc-table--cards");

    // Подпись в карточке берётся из `data-label`: CSS не умеет читать заголовок
    // соседнего элемента. Ячейка без подписи оказалась бы в карточке немой —
    // число без объяснения, что это за число.
    const headers = [...table.querySelectorAll("thead th")].map((th) => th.textContent?.trim());
    const labels = [...table.querySelectorAll("tbody tr")[0].querySelectorAll("td")].map((td) =>
      td.getAttribute("data-label"),
    );
    expect(labels).toEqual(headers);
  });

  it("заголовки переносятся — иначе таблица шире экрана на любом ноутбуке", () => {
    /*
     * Сторож по исходнику. В конфигурации тестов стоит `css: false`, поэтому
     * стили не применяются вовсе и ни один рендер-тест возврата
     * `white-space: nowrap` не заметит. По той же причине файл читается с
     * диска, а не импортируется с `?raw`: на `?raw` Vite отдал бы пустую
     * строку, и проверка молча позеленела бы, ничего не проверив.
     */
    const tableCss = readFileSync("src/features/table/table.css", "utf-8") as string;
    const thRule = /\.dt__scroll \.mantine-Table-th \{([^}]*)\}/.exec(tableCss);
    expect(thRule, "правило заголовков таблицы разбора пропало").toBeTruthy();
    expect(thRule![1]).not.toMatch(/white-space\s*:\s*nowrap/);
  });

  // --- сортировка -----------------------------------------------------------

  it("«Первый ответ» сортируется — ради этого вопроса экран и заведён", async () => {
    render();
    await screen.findByText("Павел Ушаков");

    await userEvent.click(screen.getByRole("button", { name: /Первый ответ/ }));

    await waitFor(() => {
      expect(lastTableCall().searchParams.get("sort")).toBe("first_response_sec");
      // Сначала худшие: за этим на экран и приходят.
      expect(lastTableCall().searchParams.get("direction")).toBe("desc");
    });
    expect(headerCell("Первый ответ")).toHaveAttribute("aria-sort", "descending");
  });

  it("сортировка — настоящая кнопка, а не onClick на ячейке", async () => {
    render();
    await screen.findByText("Павел Ушаков");

    // С onClick на `<th>` до сортировки нельзя было добраться табом, Enter и
    // пробел не работали, а глобальное правило фокусной рамки на нефокусируемый
    // узел не попадает вовсе.
    expect(headerCell("Статус").querySelector("button")).toBeTruthy();
    expect(headerCell("Первый ответ").querySelector("button")).toBeTruthy();

    // Несортируемые остаются обычным текстом — честнее, чем кнопка, которая
    // ничего не делает.
    expect(headerCell("Длительность").querySelector("button")).toBeNull();
    expect(headerCell("Сообщений").querySelector("button")).toBeNull();
    expect(headerCell("Клиент").querySelector("button")).toBeNull();
  });

  it("клик по несортируемой колонке не шлёт запрос", async () => {
    render();
    await screen.findByText("Павел Ушаков");
    const before = tableCalls().length;

    await userEvent.click(headerCell("Длительность"));

    expect(tableCalls()).toHaveLength(before);
  });

  it("повторный клик по колонке переворачивает порядок", async () => {
    render();
    await screen.findByText("Павел Ушаков");

    await userEvent.click(screen.getByRole("button", { name: /Статус/ }));
    await waitFor(() => expect(lastTableCall().searchParams.get("sort")).toBe("status"));

    await userEvent.click(screen.getByRole("button", { name: /Статус/ }));
    await waitFor(() => expect(lastTableCall().searchParams.get("direction")).toBe("asc"));
    // Направление сообщается и скринридеру: иначе для него все заголовки
    // остаются одинаковыми, как бы ни крутилась стрелка.
    expect(headerCell("Статус")).toHaveAttribute("aria-sort", "ascending");
  });

  // --- срез живёт в адресе --------------------------------------------------

  it("фильтр уезжает в адрес — ссылку можно передать и открыть заново", async () => {
    render();
    await screen.findByText("Павел Ушаков");

    await открытьЕщё();

    await userEvent.click(screen.getByRole("textbox", { name: "Фильтр по статусу" }));
    // «Закрытые», а не «Закрыт»: пункты фильтра пришли из общего словаря во
    // МНОЖЕСТВЕННОМ числе (docs/38 §1). Фильтр отбирает множество строк, и до
    // этой правки в списке чатов стояло «Закрытые», а здесь — «Закрыт»: одно
    // и то же действие называлось двумя словами на соседних экранах.
    await userEvent.click(await screen.findByRole("option", { name: "Закрытые" }));

    await waitFor(() => expect(url()).toContain("status=closed"));
  });

  it("адрес восстанавливает срез целиком: фильтры, сортировка, страница", async () => {
    // F5, «Назад» и чужая ссылка — один и тот же сценарий: состояние приходит
    // снаружи. Пока оно жило в useState, всё это сбрасывало разбор к «30 дней,
    // всё подряд».
    render(
      `/dialogs?status=closed&assignee=${ANNA}&tag=срочно&bot=no&sort=first_response_sec&dir=asc&offset=50`,
    );
    await screen.findByText("Павел Ушаков");

    const p = tableCalls()[0].searchParams;
    expect(p.get("status")).toBe("closed");
    expect(p.get("assignee_id")).toBe(ANNA);
    expect(p.get("tag")).toBe("срочно");
    expect(p.get("bot_active")).toBe("false");
    expect(p.get("sort")).toBe("first_response_sec");
    expect(p.get("direction")).toBe("asc");
    expect(p.get("offset")).toBe("50");
  });

  it("неизвестная сортировка в адресе не роняет экран", async () => {
    // Адрес приходит из закладки и из чужого сообщения. Колонка, которой сервер
    // не знает, дала бы 400 на весь экран — вместо таблицы красный блок.
    render("/dialogs?sort=выдумка");
    await screen.findByText("Павел Ушаков");
    expect(tableCalls()[0].searchParams.get("sort")).toBe("last_message_at");
  });

  it("умолчания в адрес не пишутся, а «Сбросить» его очищает", async () => {
    render("/dialogs?status=closed&sort=first_response_sec");
    await screen.findByText("Павел Ушаков");

    await userEvent.click(screen.getByRole("button", { name: "Сбросить фильтры" }));

    // Ссылка на «последние 30 дней, всё подряд» должна выглядеть как /dialogs,
    // а не как строка из восьми параметров, повторяющих умолчания.
    await waitFor(() => expect(url()).toBe(""));
  });

  it("уход со «своего периода» уносит и его даты", async () => {
    render("/dialogs?period=custom&from=2026-08-01&to=2026-08-05");
    await screen.findByText("Павел Ушаков");

    await userEvent.click(screen.getByRole("textbox", { name: "Период" }));
    await userEvent.click(await screen.findByRole("option", { name: "30 дней" }));

    // Иначе даты остались бы в ссылке молчаливым мусором и всплыли бы при
    // возврате к «своему периоду»: человек выбрал бы его заново и получил бы
    // позапрошлый диапазон, которого не задавал.
    await waitFor(() => expect(url()).not.toContain("from="));
    expect(url()).not.toContain("to=");
  });

  it("метка уезжает в адрес не по букве, а после паузы", async () => {
    render();
    await screen.findByText("Павел Ушаков");
    const before = tableCalls().length;

    // Каждая буква стоила запроса, а каждый запрос — COUNT по всей выборке
    // плюс подзапросы по messages. Слово «негатив» = семь тяжёлых запросов,
    // из которых шесть заведомо никому не нужны.
    await открытьЕщё();
    await userEvent.type(screen.getByRole("textbox", { name: "Фильтр по метке" }), "негатив");

    await waitFor(() => expect(lastTableCall().searchParams.get("tag")).toBe("негатив"));
    // Главное здесь — ЧИСЛО: на семь нажатий один запрос, а не семь.
    expect(tableCalls().length).toBe(before + 1);
  });

  // --- строка как ссылка ----------------------------------------------------

  it("строку можно открыть в новой вкладке — это настоящая ссылка", async () => {
    render();
    await screen.findByText("Павел Ушаков");

    // Разбор устроен так: из выборки открывают десяток диалогов подряд, и
    // открывать их надо в соседних вкладках, не потеряв саму выборку. С
    // onClick на строке ни Ctrl+клик, ни средняя кнопка, ни контекстное меню
    // не работали — оставалось уйти и вернуться, растеряв по дороге фильтры.
    const link = screen.getByRole("link", { name: "Павел Ушаков" });
    expect(link).toHaveAttribute("href", "/chats/c-1");
  });

  // --- фильтр «Оператор» ----------------------------------------------------

  it("в фильтре операторов есть отключённые — их и ищут чаще всего", async () => {
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["conversations:read", "conversations:manage", "accounts:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const u = new URL(String(input), "http://localhost");
      if (u.pathname.endsWith("/users/assignable")) {
        return jsonResponse(200, {
          items: [
            { id: "u-1", full_name: "Пётр Ковалёв", is_active: true, department: "Чатер" },
            { id: "u-2", full_name: "Олег Уволенный", is_active: false, department: "ОКК" },
          ],
        });
      }
      if (u.pathname.endsWith("/conversations/table")) {
        return jsonResponse(200, { items: ROWS, page: { limit: 50, offset: 0, total: 3 } });
      }
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });

    render();
    await screen.findByText("Павел Ушаков");

    // Деактивация диалоги не переназначает: имя уволенного остаётся в колонке
    // «Оператор» у сотен строк, и «покажи всё, что от него осталось» — первый
    // же вопрос после увольнения.
    const asked = fetchMock.mock.calls
      .map((c) => new URL(String(c[0]), "http://localhost"))
      .find((u) => u.pathname.endsWith("/users/assignable"))!;
    expect(asked.searchParams.get("include_inactive")).toBe("true");

    await открытьЕщё();

    await userEvent.click(screen.getByRole("textbox", { name: "Фильтр по оператору" }));
    // Подпись из справочника — тоже с отделом (04.09), и «(отключён)» стоит
    // ПОСЛЕ него: отдел про человека, «отключён» — про учётную запись.
    expect(
      await screen.findByRole("option", { name: "Олег Уволенный (ОКК) (отключён)" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Пётр Ковалёв (Чатер)" })).toBeInTheDocument();
  });

  it("оператор из строк попадает в фильтр, даже если справочник его не знает", async () => {
    // Роль head ведёт диалоги, но в справочнике «кому можно передать» её нет —
    // передать ей нельзя. В колонке «Оператор» такое имя тем не менее стоит.
    render();
    await screen.findByText("Павел Ушаков");

    await открытьЕщё();

    await userEvent.click(screen.getByRole("textbox", { name: "Фильтр по оператору" }));
    // Подпись опции — с отделом: колонка «Оператор» подписывает человека так
    // же, и фильтр обязан называть его тем же, чем строка (04.09).
    expect(
      await screen.findByRole("option", { name: "Анна Смирнова (ОКК)" }),
    ).toBeInTheDocument();
  });

  // --- фильтры видны в строках ----------------------------------------------

  /*
   * ОТДЕЛ В КОЛОНКЕ «ОПЕРАТОР» (просьба владельца 04.09).
   *
   * ⚠ ЗАЧЕМ ЗДЕСЬ. В «Разборе» строк по сотне, у заказчика похожие имена, и по
   * одному имени не понять, чей это диалог и к какому отделу идти с вопросом.
   *
   * ⚠ ПОЛНАЯ ПОДПИСЬ — В `title`. Ячейка режется многоточием С КОНЦА
   * (`dt__ell`), и отдел стоит в хвосте намеренно: при узкой колонке уходит он,
   * а имя остаётся целым. Наведение всё равно показывает подпись целиком.
   */
  it("в колонке «Оператор» имя подписано отделом, у ничейного — прочерк", async () => {
    const { container } = render();
    await screen.findByText("Павел Ушаков");

    const ячейка = container.querySelector<HTMLElement>(".dt__ell[title='Анна Смирнова (ОКК)']");
    expect(ячейка).not.toBeNull();
    expect(ячейка!.textContent).toBe("Анна Смирнова (ОКК)");

    // Диалог без ответственного: прочерк, а не «()» и не пустая ячейка.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("метки и бот показаны в строках — иначе фильтр по ним нечем проверить", async () => {
    const { container } = render();
    await screen.findByText("Павел Ушаков");

    expect(container.querySelector(".dt__tag")?.textContent).toBe("срочно");
    // У второй строки бот включён — признак обязан быть виден.
    expect(container.querySelectorAll(".dt__bot")).toHaveLength(1);
  });

  // --- отказы ---------------------------------------------------------------

  it("отказ загрузки объясняется словами сервера, а не общим «не получилось»", { timeout: 15000 }, async () => {
    // Самые частые отказы этого экрана вызваны самой выборкой: слишком глубокая
    // страница, слишком широкий срез для сортировки по метрике. Сервер говорит,
    // что именно сделать; выбрасывать этот текст — значит превращать разрешимый
    // тупик в необъяснимый.
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const u = new URL(String(input), "http://localhost");
      if (u.pathname.endsWith("/conversations/table")) {
        return jsonResponse(400, {
          error: {
            code: "validation_error",
            message: "Сортировка по этой колонке считается по всей выборке — сузьте фильтры",
          },
        });
      }
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });

    render("/dialogs?sort=first_response_sec");

    // Ждём дольше обычного: общий queryClient переспрашивает дважды с паузами
    // (retry: 2), и до отказа проходит около трёх секунд.
    expect(
      await screen.findByText(
        "Сортировка по этой колонке считается по всей выборке — сузьте фильтры",
        {},
        { timeout: 8000 },
      ),
    ).toBeInTheDocument();
    // Незрячий иначе не узнаёт ничего: таблица просто исчезает.
    expect(screen.getByRole("alert")).toBeInTheDocument();
    // Повтор такой отказ не лечит, а заголовка сортировки на экране уже нет:
    // без второго выхода человек остаётся в тупике. Ищем ИМЕННО кнопку
    // отказного состояния: такая же есть в ряду фильтров выше, и `getByRole`
    // по всему экрану находил бы две.
    expect(
      within(screen.getByRole("alert")).getByRole("button", { name: "Сбросить фильтры" }),
    ).toBeInTheDocument();
  });

  it("выгрузка уходит с теми же фильтрами, что и экран", async () => {
    // Выгрузка, отличающаяся от увиденного, заставляет человека решить, что
    // врёт экран. Поэтому параметры собираются одной функцией — и тест ловит
    // именно это. С 24.09 файл собирает воркер: кнопка ставит задачу, а
    // готовый файл приходит ссылкой в тосте общего хука выгрузок.
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const u = new URL(String(input), "http://localhost");
      if (u.pathname.endsWith("/conversations/table/export") && init?.method === "POST") {
        return jsonResponse(202, { job_id: "job-1" });
      }
      if (u.pathname.endsWith("/conversations/table/export/job-1")) {
        return jsonResponse(200, {
          job_id: "job-1",
          status: "pending",
          format: "csv",
          rows: null,
          url: null,
          expires_at: null,
          error: null,
        });
      }
      if (u.pathname.endsWith("/conversations/table")) {
        return jsonResponse(200, { items: ROWS, page: { limit: 50, offset: 0, total: 3 } });
      }
      if (u.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });

    render("/dialogs?status=closed");
    await screen.findByText("Павел Ушаков");

    await userEvent.click(screen.getByRole("button", { name: /Выгрузить CSV/ }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((c) =>
          String(c[0]).includes("/conversations/table/export/job-1"),
        ),
      ).toBe(true),
    );
    const start = fetchMock.mock.calls.find(
      (c) =>
        new URL(String(c[0]), "http://localhost").pathname.endsWith("/conversations/table/export") &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    )!;
    const exportUrl = new URL(String(start[0]), "http://localhost");
    expect(exportUrl.searchParams.get("status")).toBe("closed");
    expect(exportUrl.searchParams.get("date_from")).toBeTruthy();
    // Страница у выгрузки своя — она берёт всю выборку целиком.
    expect(exportUrl.searchParams.get("offset")).toBeNull();
    // Пока задача идёт, вторую не запустить: слот на сервере один.
    expect(screen.getByRole("button", { name: /Выгрузить CSV/ })).toBeDisabled();
  });

  it("подсказки меток собраны из того, что на экране", async () => {
    render();
    await screen.findByText("Павел Ушаков");

    await открытьЕщё();

    const input = screen.getByRole("textbox", { name: "Фильтр по метке" });
    await userEvent.click(input);

    // Справочника меток в базе нет: их ставят и боты, и операторы. Список,
    // притворяющийся полным, был бы хуже подсказок — метку, которой не оказалось
    // на текущей странице, человек просто не нашёл бы.
    expect(await screen.findByRole("option", { name: "ночной-лид" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "срочно" })).toBeInTheDocument();
  });

  it("пустая выборка предлагает снять фильтры, а не просто молчит", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const u = new URL(String(input), "http://localhost");
      if (u.pathname.endsWith("/conversations/table")) {
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }
      if (u.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });

    render();
    const пусто = await screen.findByText("Ничего не нашлось");
    expect(пусто).toBeInTheDocument();
    // Кнопка ПУСТОГО СОСТОЯНИЯ, а не та же самая из ряда фильтров выше: с тех
    // пор как «Сбросить» на экране переименован в «Сбросить фильтры» — как на
    // соседних экранах, — одинаковых имён на странице два, и это не дефект:
    // одно и то же действие предлагается в двух местах намеренно, потому что
    // кнопку в ряду фильтров легко не заметить.
    const блок = пусто.closest(".lc-empty") as HTMLElement;
    expect(блок, "пустое состояние рисуется не через EmptyState").toBeTruthy();
    expect(within(блок).getByRole("button", { name: "Сбросить фильтры" })).toBeInTheDocument();
  });

  it("период — календарные сутки по Москве, а не «последние 168 часов»", async () => {
    render();
    await screen.findByText("Павел Ушаков");

    // По умолчанию — 30 дней: у отчёта не бывает «пусто, выберите период».
    const from = tableCalls()[0].searchParams.get("date_from")!;
    expect(from).toBeTruthy();
    // Граница — полночь по Москве, а не «сейчас минус тридцать суток»: иначе
    // одна и та же ссылка у московского и владивостокского руководителя дала бы
    // разные наборы строк, а ключ запроса менялся бы на каждый клик.
    expect(from).toMatch(/T00:00:00\+03:00$/);
    const days = (Date.now() - Date.parse(from)) / 86_400_000;
    expect(days).toBeGreaterThan(29);
    expect(days).toBeLessThan(31);
  });
  /*
   * КОЛОНКА, ЗАКРЕПЛЁННАЯ ФИЛЬТРОМ, НЕ ПОКАЗЫВАЕТСЯ (разбор интерфейса 13.08).
   *
   * Отфильтровали по статусу — и колонка «Статус» повторяет одно слово во всех
   * строках до конца выдачи. Это не данные, а эхо собственного фильтра.
   *
   * ⚠ ВТОРАЯ ПОЛОВИНА ПРОВЕРКИ ВАЖНЕЕ ПЕРВОЙ: без фильтра колонка обязана быть на
   * месте. Спрятать её насовсем значит отнять у разбора главный признак строки.
   *
   * ⚠ И ТРЕТЬЯ: решает именно ФИЛЬТР, а не совпадение значений на странице. Замер
   * живого экрана показал, что у «Оператора» на текущей странице одно значение на
   * все одиннадцать строк — но это случайность выборки. Спрячь по совпадению, и на
   * следующей странице колонка вернётся, таблица поменяет форму под руками.
   */
  it("фильтр по статусу убирает колонку «Статус», без фильтра она на месте", async () => {
    render("/dialogs?status=new");
    await screen.findByText("Павел Ушаков");
    expect(headerCellOrNull("Статус"), "колонка-эхо фильтра осталась").toBeUndefined();
    // Соседние колонки при этом никуда не делись.
    expect(headerCellOrNull("Клиент")).toBeTruthy();
    expect(headerCellOrNull("Канал")).toBeTruthy();
  });

  it("фильтр по каналу убирает колонку «Канал»", async () => {
    render("/dialogs?account=acc-1");
    await screen.findByText("Павел Ушаков");
    expect(headerCellOrNull("Канал")).toBeUndefined();
    expect(headerCellOrNull("Статус"), "убрали лишнюю колонку").toBeTruthy();
  });

  /*
   * ЧИП ОБЪЯСНЯЕТ ПРОПАВШУЮ КОЛОНКУ — И ЭТО НЕ УКРАШЕНИЕ (разбор интерфейса 13.08).
   *
   * Правило выше («колонка, закреплённая фильтром, не показывается») держалось на
   * молчаливом основании: САМ ФИЛЬТР ВСЕГДА НА ВИДУ, и человек видит причину пропажи
   * рядом с таблицей. Спрятав статус, оператора, бота и метку под кнопку «Ещё», это
   * основание выбили — таблица могла остаться без трёх колонок из девяти без единого
   * слова о том, почему.
   *
   * Расплата — чипы снаружи, со ЗНАЧЕНИЕМ СЛОВАМИ. Голого числа на кнопке мало: в
   * списке чатов его хватало, потому что там сужение ничего из строк не прячет.
   *
   * ⚠ ЧТО ИМЕННО ОХРАНЯЕТСЯ. Не «чипы существуют», а СВЯЗКА: спрятанная колонка ↔
   * объяснение снаружи. Разъедутся эти два места — и разбор получит таблицу, которая
   * молча меняет форму. Поймать иначе нельзя: обе половины по отдельности выглядят
   * исправными.
   */
  it("спрятанный фильтр объясняет себя чипом со значением", async () => {
    render("/dialogs?status=new");
    await screen.findByText("Павел Ушаков");

    // Колонка ушла — и ровно поэтому объяснение обязано быть на экране.
    expect(headerCellOrNull("Статус")).toBeUndefined();
    // Панель ЗАКРЫТА: чип виден без единого клика, как и пропажа колонки.
    expect(screen.queryByRole("textbox", { name: "Фильтр по статусу" })).toBeNull();
    expect(screen.getByText("Статус: Новые")).toBeInTheDocument();
  });

  it("⚠ ЧИСЛО НА КНОПКЕ РАВНО ЧИСЛУ ЧИПОВ", async () => {
    /*
     * Счётчик и чипы идут из одного массива, и разъехаться им негде — но именно это
     * «негде» и проверяется: правило записано в ChatListPane и перенесено сюда
     * дословно, а такие переносы ломаются молча.
     */
    render("/dialogs?status=new&bot=yes&tag=негатив");
    await screen.findByText("Павел Ушаков");

    expect(screen.getByLabelText("Сужений: 3")).toBeInTheDocument();
    for (const текст of ["Статус: Новые", "Бот ведёт", "Метка: негатив"]) {
      expect(screen.getByText(текст), `нет чипа «${текст}»`).toBeInTheDocument();
    }
  });

  it("⚠ КАНАЛ И ПОИСК В СУЖЕНИЯ НЕ СЧИТАЮТСЯ — ОНИ СНАРУЖИ", async () => {
    /*
     * Счётчик обязан считать РОВНО поля внутри панели. Канал остался в ряду на виду,
     * и чип к нему был бы вторым именем одного и того же — а число на кнопке
     * обещало бы сужение, которого в панели нет.
     */
    render("/dialogs?account=acc-1&q=не%20дозвонился");
    await screen.findByText("Павел Ушаков");

    expect(screen.queryByLabelText(/Сужений:/), "канал и поиск попали в счётчик").toBeNull();
    // При этом колонка «Канал» скрыта, и объясняет её собственное поле фильтра.
    expect(headerCellOrNull("Канал")).toBeUndefined();
    expect(screen.getByRole("textbox", { name: "Фильтр по каналу" })).toBeInTheDocument();
  });

  it("крестик на чипе снимает своё сужение и возвращает колонку", async () => {
    render("/dialogs?status=new");
    await screen.findByText("Павел Ушаков");
    expect(headerCellOrNull("Статус")).toBeUndefined();

    await userEvent.click(screen.getByRole("button", { name: "Снять сужение: Статус: Новые" }));

    expect(screen.queryByText("Статус: Новые")).toBeNull();
    expect(headerCellOrNull("Статус"), "колонка не вернулась").toBeTruthy();
  });

  /*
   * ПОЯС И ВЫГРУЗКА ЖИВУТ В ШАПКЕ, А НЕ СРЕДИ СУЖЕНИЙ (разбор интерфейса 13.08).
   *
   * Ни то, ни другое выборку не сужает: пояс меняет показ времени, выгрузка уносит
   * показанное в файл. В ряду фильтров они стояли по старшинству, а не по смыслу, и
   * стоили ему переноса: на 1440px ряду не хватало ровно 21 пикселя, и лишним был
   * именно пояс — седьмой контрол среди сужений.
   *
   * ⚠ ПОЧЕМУ ЭТО СТОРОЖИТСЯ, А НЕ ПРОСТО СДЕЛАНО. Вернуть пояс в ряд — правка на одну
   * строку, и выглядеть она будет безобидно: контрол тот же, экран тот же. Сломается
   * не вид, а СМЫСЛ ряда: «здесь стоит то, чем сужают». Ряд, где среди сужений стоит
   * не-сужение, снова начнёт переноситься, и снова будет непонятно, почему.
   *
   * Проверяется положение в разметке, а не пиксели: ширины в jsdom нулевые (css:false),
   * и утверждение про «одну строку» было бы зелёным при любой вёрстке. Числа померены
   * в браузере и записаны рядом с самой правкой.
   */
  it("переключатель пояса стоит вне ряда фильтров", async () => {
    render("/dialogs");
    await screen.findByText("Павел Ушаков");

    const пояс = screen.queryByRole("textbox", { name: "Часовой пояс времени в таблице" });
    if (пояс === null) return; // у московского сотрудника переключателя нет вовсе

    expect(
      пояс.closest(".dt__filters"),
      "пояс вернулся в ряд сужений — он не сужение",
    ).toBeNull();
    expect(пояс.closest(".page-header"), "пояс потерялся по дороге в шапку").toBeTruthy();
  });

  it("выгрузка стоит рядом со счётчиком, а не среди фильтров", async () => {
    /*
     * Соседство не случайно: «Найдено», «Время» и «Выгрузить» говорят о выборке
     * ЦЕЛИКОМ — сколько нашлось, в каких часах показано, чем унести.
     *
     * ⚠ И ПОЯС С ВЫГРУЗКОЙ РАЗЛУЧАТЬ НЕЛЬЗЯ: оговорка «(МСК)» появляется на самой
     * кнопке ровно тогда, когда экран переведён на местные часы, а файл остаётся
     * московским. Разведи их — и предупреждение оторвётся от своей причины.
     */
    render("/dialogs");
    await screen.findByText("Павел Ушаков");

    const выгрузка = screen.getByRole("button", { name: /Выгрузить CSV/ });
    expect(выгрузка.closest(".dt__filters"), "выгрузка вернулась в ряд сужений").toBeNull();
    const шапка = выгрузка.closest(".page-header");
    expect(шапка, "выгрузка потерялась по дороге в шапку").toBeTruthy();
    expect(шапка?.textContent, "счётчик разъехался с выгрузкой").toContain("Найдено");
  });

  it("без фильтров показаны все колонки", async () => {
    render();
    await screen.findByText("Павел Ушаков");
    for (const имя of ["Клиент", "Статус", "Оператор", "Канал"]) {
      expect(headerCellOrNull(имя), `колонка «${имя}» пропала без фильтра`).toBeTruthy();
    }
  });
});
