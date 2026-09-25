import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TemplatesManager } from "@/features/templates/TemplatesManager";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TemplateDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Быстрые ответы: что строка списка говорит о заготовке и что редактор говорит
 * об области (перенос макета `design-system/templates.html`, 04.09).
 *
 * Три вещи, каждая из которых стоила человеку конкретной работы:
 *
 *   1. ЧАСТОТА БЫЛА НЕВИДИМА. `used_count` копится на сервере и уже поднимает
 *      ходовые заготовки в подсказке по «/», а в настройках — там, где
 *      библиотеку чистят, — числа не было. Между двумя похожими
 *      «Здравствуйте» выбирали наугад.
 *   2. ПОДСТАНОВКА ВЫГЛЯДЕЛА ОБЫЧНЫМ ТЕКСТОМ. Заготовка с незакрытым `{имя}`
 *      уходит клиенту в Авито как есть, отозвать сообщение там нечем.
 *   3. ПРАВИЛО ОБЛАСТИ ЛЕЖАЛО СЕРОЙ СТРОКОЙ ВНИЗУ ОКНА. «Перевести личный в
 *      общий нельзя» стояло ниже всего, что читают, — и без намёка на то, что
 *      именно нельзя.
 */

const ADMIN_PERMISSIONS: Permission[] = ["templates:shared", "templates:own"];

const SHARED: TemplateDto[] = [
  {
    id: "t-1",
    owner_id: null,
    title: "Территориально не подходит",
    body: "Здравствуйте, {имя}! По адресу из {объявление} мы не выезжаем",
    folder: "Отказы",
    used_count: 143,
  },
  {
    /*
     * ⚠ ФИГУРНЫЕ СКОБКИ ЕСТЬ, А ПОДСТАНОВКИ НЕТ. Без этой строки проверка
     * «подсвечиваем только настоящие переменные» зеленела бы и у подсветки,
     * которая красит любые скобки подряд.
     */
    id: "t-2",
    owner_id: null,
    title: "Гарантия 12 месяцев",
    body: "Гарантия 12 месяцев, номер талона {Г-12} назовёт мастер",
    folder: "Цена и гарантия",
    used_count: 0,
  },
  {
    /** Сервер постарше: поля `used_count` в ответе нет вовсе. */
    id: "t-3",
    owner_id: null,
    title: "Ждём запчасть",
    body: "Запчасть заказана, привезут за 3–5 дней",
    folder: "Сроки",
  },
];

const PERSONAL: TemplateDto[] = [
  {
    id: "t-9",
    owner_id: fakeUser.id,
    title: "Мой ответ",
    body: "Скоро буду",
    folder: null,
    used_count: 7,
  },
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

/** Строка списка по названию заготовки. */
function строка(название: string): HTMLElement {
  return screen.getByText(название).closest("tr") as HTMLElement;
}

/** Ячейка «Вставок» этой строки — та же подпись, что в шапке и в карточке. */
function вставок(название: string): HTMLElement | null {
  return строка(название).querySelector<HTMLElement>("[data-label='Вставок']");
}

describe("Строка списка называет частоту", () => {
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

  it("число вставок видно в строке, а не только подсказке по «/»", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    expect(screen.getByRole("columnheader", { name: "Вставок" })).toBeInTheDocument();
    expect(вставок("Территориально не подходит")).toHaveTextContent("143");
  });

  it("ноль пишется цифрой: это и есть кандидат на чистку", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    // Прочерк здесь означал бы «неизвестно», а известно ровно обратное:
    // заготовку не вставили ни разу.
    expect(вставок("Гарантия 12 месяцев")).toHaveTextContent("0");
  });

  it("поле, которого сервер не прислал, за ноль не выдаём", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    /*
     * Сборка фронта переживает сервер постарше (об этом сказано в самом
     * `TemplateDto`), и «0» в этот промежуток был бы утверждением, которого мы
     * не знаем: не «не вставляли», а «не посчитали».
     */
    const ячейка = вставок("Ждём запчасть");
    expect(ячейка).toHaveTextContent("—");
    expect(ячейка).not.toHaveTextContent("0");
  });
});

describe("Подстановка видна в тексте заготовки", () => {
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

  it("{имя} и {объявление} выделены, а текст вокруг них цел", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    const ряд = строка("Территориально не подходит");
    const подсвеченные = Array.from(ряд.querySelectorAll(".tpl-table__ph")).map((n) => n.textContent);
    expect(подсвеченные).toEqual(["{имя}", "{объявление}"]);

    // Разрезали на куски — значит обязаны склеиться обратно слово в слово:
    // подсветка не имеет права трогать то, что уйдёт клиенту.
    const ячейка = ряд.querySelector<HTMLElement>("[data-label='Текст']");
    expect(ячейка?.textContent).toBe(SHARED[0]!.body);
    expect(ячейка).toHaveAttribute("title", SHARED[0]!.body);
  });

  it("обычные фигурные скобки не красим: переменная не всякое, что в скобках", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    /*
     * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ПАДАЕТ ПО СВОЕЙ ПРИЧИНЕ. Строка на экране есть
     * и текст в ней виден целиком — проверяем именно ОТСУТСТВИЕ подсветки, а
     * не пустой список строк, который зеленел бы и на несуществующей таблице.
     */
    const ряд = строка("Гарантия 12 месяцев");
    expect(ряд).toHaveTextContent("{Г-12}");
    expect(ряд.querySelectorAll(".tpl-table__ph")).toHaveLength(0);
  });
});

describe("Кто видит: правило стоит там, где по нему могли бы кликнуть", () => {
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

  /** Обе половины сегмента в порядке разметки: общий, затем личный. */
  async function половины() {
    const область = await screen.findByRole("group", { name: "Кто видит" });
    const [общий, личный] = Array.from(область.querySelectorAll<HTMLElement>(".tpl-scope__opt"));
    return { область, общий: общий!, личный: личный! };
  }

  it("у общего ответа вторая половина закрыта — и это сказано словами, не цветом", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Территориально не подходит"));
    await user.click(await screen.findByRole("menuitem", { name: "Редактировать" }));

    const { область, общий, личный } = await половины();
    expect(общий).toHaveTextContent("Общий");
    expect(общий).toHaveAttribute("data-active", "true");
    expect(личный).not.toHaveAttribute("data-active");
    // Читалке цвет недоступен: состояние обеих половин названо текстом.
    expect(общий).toHaveTextContent("выбрано");
    expect(личный).toHaveTextContent("недоступно");
    expect(область).toHaveTextContent("Общий — его увидит вся команда");
  });

  it("у личного ответа сказано, что перевести его в общий нельзя", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.click(screen.getByRole("tab", { name: "Мои" }));
    await user.click(await screen.findByLabelText("Действия: Мой ответ"));
    await user.click(await screen.findByRole("menuitem", { name: "Редактировать" }));

    const { область, общий, личный } = await половины();
    expect(личный).toHaveAttribute("data-active", "true");
    expect(общий).not.toHaveAttribute("data-active");
    expect(область).toHaveTextContent("перевести личный в общий нельзя");
  });

  it("при создании подсвечена область той вкладки, где стоит человек", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.click(screen.getByRole("tab", { name: "Мои" }));
    await screen.findByText("Мой ответ");
    await user.click(screen.getByRole("button", { name: "Создать быстрый ответ" }));

    const { область, личный } = await половины();
    expect(личный).toHaveAttribute("data-active", "true");
    // Про «не меняется» здесь говорить рано — ответа ещё нет; говорим о том,
    // откуда область берётся.
    expect(область).toHaveTextContent("Область задаёт вкладка");
  });
});
