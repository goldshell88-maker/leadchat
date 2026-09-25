import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TemplatesManager } from "@/features/templates/TemplatesManager";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TemplateDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Быстрые ответы: фильтры, вставка переменных и подтверждение удаления
 * (TPL-01, TPL-03, TPL-04).
 *
 * Общая беда всех трёх — экран говорит не то, что делает: пустота от фильтра
 * выглядела как пустой раздел, кнопка «вставить» дописывала в конец, а личный
 * быстрый ответ грозил пропасть «у всех».
 */

const ADMIN_PERMISSIONS: Permission[] = ["templates:shared", "templates:own"];

/** У общих и личных быстрых ответов папки СВОИ — на этом и ломался фильтр. */
const SHARED: TemplateDto[] = [
  {
    id: "t-1",
    owner_id: null,
    title: "Приветствие",
    body: "Здравствуйте! Уточните, пожалуйста, модель техники",
    folder: "Первый контакт",
  },
  {
    id: "t-2",
    owner_id: null,
    title: "Гарантия",
    body: "На работу мастера даём гарантию 12 месяцев",
    folder: "Отказы",
  },
];

const PERSONAL: TemplateDto[] = [
  { id: "t-9", owner_id: fakeUser.id, title: "Мой ответ", body: "Скоро буду", folder: "Мои заготовки" },
];

function setupFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const method = init?.method ?? "GET";
      if (url.pathname.endsWith("/templates") && method === "GET") {
        const items = url.searchParams.get("scope") === "personal" ? PERSONAL : SHARED;
        return jsonResponse(200, { items, page: { limit: 200, offset: 0, total: items.length } });
      }
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    }),
  );
}

describe("Быстрые ответы — фильтры и пустое состояние", () => {
  beforeEach(() => {
    queryClient.clear();
    setupFetch();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("пустота от поиска названа причиной, а не выдана за пустой раздел", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.type(screen.getByLabelText("Поиск по быстрым ответам"), "посудомойка");

    expect(await screen.findByText("Ничего не нашлось")).toBeInTheDocument();
    expect(screen.getByText("По запросу «посудомойка» ничего нет")).toBeInTheDocument();
    // Именно этого экрана здесь быть не должно: ответы есть, просто их скрыл
    // фильтр, а «Создайте первые» отправляет заводить второй такой же.
    expect(screen.queryByText("Общих быстрых ответов нет")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Сбросить фильтры" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.getByLabelText("Поиск по быстрым ответам")).toHaveValue("");
  });

  it("подпись поля поиска — с прописной и говорит, где ищет", async () => {
    /*
     * Было «поиск» — единственная строчная подпись поля на весь продукт
     * (рядом «Поиск: имя, телефон, текст», «Имя или email»,
     * «Статус: любой»). Разнобой в одном месте из двадцати читается не как
     * стиль, а как недоделка.
     *
     * Заодно подпись перестала умалчивать: поле ищет и по названию, и по
     * тексту ответа. Проверяем не только букву, но и что обещание правдиво, —
     * иначе подпись стала бы красивым враньём.
     */
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    const search = screen.getByLabelText("Поиск по быстрым ответам");
    expect(search).toHaveAttribute("placeholder", "Поиск по названию и тексту");

    // «мастера» встречается только в ТЕЛЕ «Гарантии», а не в названии.
    await user.type(search, "мастера");
    const rows = await screen.findAllByRole("row");
    expect(rows.some((r) => within(r).queryByText("Гарантия"))).toBe(true);
    expect(rows.some((r) => within(r).queryByText("Приветствие"))).toBe(false);
  });

  it("пустой раздел остаётся пустым разделом: причину фильтра не выдумываем", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { items: [], page: { limit: 200, offset: 0, total: 0 } })),
    );
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });

    expect(await screen.findByText("Общих быстрых ответов нет")).toBeInTheDocument();
    expect(screen.queryByText("Ничего не нашлось")).toBeNull();
  });

  it("папка «Общих» не уезжает во вкладку «Мои» невидимым фильтром", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    // Папка «Отказы» есть только у общих быстрых ответов.
    await user.click(screen.getByRole("button", { name: "Отказы" }));
    expect(await screen.findByText("Гарантия")).toBeInTheDocument();
    expect(screen.queryByText("Приветствие")).toBeNull();

    await user.click(screen.getByRole("tab", { name: "Мои" }));

    // Раньше фильтр оставался включённым, кнопки «Отказы» в полосе уже не было,
    // и человек видел пустой экран без единого признака причины.
    expect(await screen.findByText("Мой ответ")).toBeInTheDocument();
    expect(screen.queryByText("Ничего не нашлось")).toBeNull();
    expect(screen.getByRole("button", { name: "Все" })).toHaveAttribute("data-active");
  });

  /*
   * СЛОВАРЬ (10 §7.2). «Шаблон» разрешён только как имя раздела; сущность
   * называется быстрым ответом. Пока на кнопке было «+ Шаблон», а в панели
   * ввода та же вещь звалась быстрым ответом, диспетчер не мог понять, одно
   * это или разное.
   */
  it("на экране нет «шаблонов» — только быстрые ответы", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    expect(screen.getByRole("button", { name: "Создать быстрый ответ" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Шаблон" })).toBeNull();
    expect(screen.getByLabelText("Поиск по быстрым ответам")).toBeInTheDocument();
  });

  /*
   * Обрезка многоточием без подсказки (docs/39 §5). Колонка названия — 200px,
   * текста — остаток; прочитать целиком было негде, а два ответа с одинаковым
   * началом («Здравствуйте! Мастер приедет…») выглядели одной строкой.
   */
  it("обрезанные ячейки договаривают подсказкой", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    expect(screen.getByText("Приветствие")).toHaveAttribute("title", "Приветствие");
    expect(screen.getByText(SHARED[0]!.body)).toHaveAttribute("title", SHARED[0]!.body);
  });

  /*
   * Вкладки объявлены ролями `tablist`/`tab` — значит и вести себя обязаны как
   * вкладки: внутри полосы ходят стрелками, Tab уводит в содержимое. Раньше
   * ролей было ровно две строки разметки, а поведения не было никакого.
   */
  it("между вкладками можно перейти стрелкой, а не только мышью", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    const shared = screen.getByRole("tab", { name: "Общие" });
    const personal = screen.getByRole("tab", { name: "Мои" });
    // Перекатывающийся tabIndex: Tab'ом достижима ровно одна вкладка.
    expect(shared).toHaveAttribute("tabindex", "0");
    expect(personal).toHaveAttribute("tabindex", "-1");

    shared.focus();
    await user.keyboard("{ArrowRight}");

    expect(await screen.findByText("Мой ответ")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Мои" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Мои" })).toHaveFocus();

    // Закольцовка: со второй вкладки стрелка вправо возвращает на первую.
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Общие" })).toHaveAttribute("aria-selected", "true");
  });

  it("панель вкладки подписана самой вкладкой", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", "tpl-tab-shared");
    expect(screen.getByRole("tab", { name: "Общие" })).toHaveAttribute(
      "aria-controls",
      panel.id,
    );
  });

  /*
   * TPL-05. Запрос берёт первые 200, а поиск локальный — по ним же. С 201-й
   * записи часть библиотеки для экрана не существует, и на запрос по ней он
   * уверенно отвечает «Ничего не нашлось». Пока серверный поиск не подключён,
   * экран обязан хотя бы назвать неполноту вслух.
   */
  it("неполный список назван неполным, а не выдан за всю библиотеку", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, { items: SHARED, page: { limit: 200, offset: 0, total: 260 } }),
      ),
    );
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    expect(screen.getByText(/Показаны первые 2 из 260/)).toBeInTheDocument();
  });

  it("полный список ничего лишнего не пишет", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    expect(screen.queryByText(/Показаны первые/)).toBeNull();
  });

  it("поиск переход между вкладками переживает: он виден в своей строке", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.type(screen.getByLabelText("Поиск по быстрым ответам"), "ответ");
    await user.click(screen.getByRole("tab", { name: "Мои" }));

    expect(screen.getByLabelText("Поиск по быстрым ответам")).toHaveValue("ответ");
    expect(await screen.findByText("Мой ответ")).toBeInTheDocument();
  });
});

describe("Быстрые ответы — вставка переменной и удаление", () => {
  beforeEach(() => {
    queryClient.clear();
    setupFetch();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("переменная встаёт в каретку, а не в конец текста", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Создать быстрый ответ" }));
    const body = (await screen.findByLabelText("Текст")) as HTMLTextAreaElement;
    await user.type(body, "Здравствуйте, ! Мастер будет к 15:00");

    // Каретка после «Здравствуйте, » — ровно туда человек её и ставит.
    body.setSelectionRange(14, 14);
    await user.click(screen.getByRole("button", { name: "Вставить переменную {имя}" }));

    expect(body).toHaveValue("Здравствуйте, {имя}! Мастер будет к 15:00");
    // И продолжать печатать можно с того же места, а не с конца абзаца.
    expect(body.selectionStart).toBe(14 + "{имя}".length);
  });

  /*
   * Счётчик знаков пересчитывается на КАЖДОЙ букве — то есть неверное
   * окончание мозолит глаз чаще любой другой строки продукта. Было «1
   * символов» на всех числах без исключения.
   */
  it("счётчик знаков склоняется, а не печатает «1 символов»", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Создать быстрый ответ" }));
    const body = await screen.findByLabelText("Текст");

    await user.type(body, "а");
    expect(screen.getByText(/^1 символ ·/)).toBeInTheDocument();
    await user.type(body, "бв");
    expect(screen.getByText(/^3 символа ·/)).toBeInTheDocument();
    await user.type(body, "гдеж");
    expect(screen.getByText(/^7 символов ·/)).toBeInTheDocument();
  });

  it("в нетронутое поле переменная дописывается в конец", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    // Открыли готовый ответ и сразу нажали переменную, в текст не заходя:
    // каретки нет вовсе, и «в конец» здесь единственное осмысленное место.
    await user.click(screen.getByLabelText("Действия: Приветствие"));
    await user.click(await screen.findByRole("menuitem", { name: "Редактировать" }));
    const body = (await screen.findByLabelText("Текст")) as HTMLTextAreaElement;

    await user.click(screen.getByRole("button", { name: "Вставить переменную {имя}" }));

    expect(body).toHaveValue("Здравствуйте! Уточните, пожалуйста, модель техники{имя}");
  });

  it("удаление личного быстрого ответа не грозит чужой бедой, общего — грозит", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    // Общий: предупреждение настоящее — ответ пропадёт у всей смены.
    await user.click(screen.getByLabelText("Действия: Приветствие"));
    await user.click(await screen.findByRole("menuitem", { name: "Удалить" }));
    const sharedDialog = await screen.findByRole("dialog");
    expect(
      within(sharedDialog).getByText(/пропадёт из списка быстрых ответов у всей команды/),
    ).toBeInTheDocument();
    await user.click(within(sharedDialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // Личный: у остальных его нет и не было — пугать нечем.
    await user.click(screen.getByRole("tab", { name: "Мои" }));
    await user.click(await screen.findByLabelText("Действия: Мой ответ"));
    await user.click(await screen.findByRole("menuitem", { name: "Удалить" }));
    const personalDialog = await screen.findByRole("dialog");
    expect(within(personalDialog).getByText(/Он личный — у остальных его нет/)).toBeInTheDocument();
    expect(within(personalDialog).queryByText(/у всей команды/)).toBeNull();
  });
});
