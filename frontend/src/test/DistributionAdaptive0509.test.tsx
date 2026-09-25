// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в DistributionShape0409.test.tsx и surfaceLadder.test.ts: сторож читает
// стиль файлом, потому что в vitest стоит `css: false` и в jsdom стилей нет.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { DistributionTab } from "@/features/settings/distribution/DistributionTab";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * АДАПТИВНОСТЬ И ИЕРАРХИЯ ЭКРАНА «РАСПРЕДЕЛЕНИЕ» (05.09).
 *
 * Замеры сделаны в браузере на настоящем экране (dev-preview, headless Chrome
 * с эмуляцией ширины), а не на глаз. Стерегутся четыре вещи, каждая из которых
 * ломается молча и ни одной рендер-проверкой не ловится:
 *
 *   1. ПОДПИСЬ ГЛАВНОЙ КНОПКИ НЕ РЕЖЕТСЯ. При окне 375 колонке достаётся 269,
 *      ряд действий был нерастекающимся flex'ом, и кнопка ужималась с 211 до
 *      104. Подпись Mantine рисует `nowrap` и режет БЕЗ многоточия — на экране
 *      оставалось «Сохранить». Кнопок сохранения на экране две, и они нарочно
 *      названы по-разному; узкий экран возвращал ровно ту беду, ради которой
 *      подписи разводили.
 *   2. ЦЕЛЬ НАЖАТИЯ НЕ МЕЛЬЧЕ 24. Метка «Без ограничения» была 155×16.
 *   3. МЕДИАЗАПРОС ПРАВИТ ТОТ ЖЕ МЕХАНИЗМ, ЧТО И БАЗОВОЕ ПРАВИЛО. Это ловушка
 *      этого проекта: в списке каналов узкий экран задавал `flex-direction`
 *      строке, которая к тому времени стала сеткой, — правило не делало
 *      ничего, и никто этого не видел.
 *   4. ДЛИНА ОКНА НАЗВАНА ЧИСЛОМ. Полоса показывает сдвиг, но длину с неё
 *      считают по клеткам, а на телефоне клетка 11 px.
 *
 * ⚠ КАЖДЫЙ СТОРОЖ ПРОВЕРЕН ДИВЕРСИЕЙ — ломали ровно то, что он стережёт, и
 * смотрели, что краснеет именно он (13 диверсий, 05.09):
 *   1. убрать `flex-wrap: wrap` у `.dist__actions`;
 *   2. увести селектор запрета сжатия мимо разметки (`> button` → `> a`);
 *   3. вернуть метке тумблера `min-height: 16px`;
 *   4. заменить в базовом `.dist__cols` сетку на `display: flex`, оставив
 *      `grid-template-columns` мёртвым — та самая ловушка проекта;
 *   5. убрать из медиазапроса `border-top: none`;
 *   6. убрать шов сверху из базового `.dist__block`;
 *   7. вернуть `--lc-a-800` закрашенной клетке;
 *   8. выбросить блок `prefers-reduced-motion`;
 *   9. оставить в нём только `.dist__cap`, забыв клетки;
 *  10. поставить группе потолка `--lc-surface` (мина выключенного поля);
 *  11. набрать показатель кеглем тела;
 *  12. считать длину окна из сохранённого, а не из полей;
 *  13. убрать `Math.max`, одну форму слова на все числа, ноль вместо тире.
 */

const CSS_PATH = "src/features/settings/distribution/distribution.css";

/** Стиль без комментариев: в них разобрано, ПОЧЕМУ было иначе, и наивный
    поиск краснел бы на самом объяснении. */
function css(): string {
  return (readFileSync(CSS_PATH, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, " ");
}

/**
 * Всё, что объявлено селектору ВНЕ медиазапросов, — одной строкой.
 *
 * ⚠ ПОИСК ПО `\n.dist__block {` УЖЕ ОДИН РАЗ СОВРАЛ. Стоило свойству переехать
 * в правило с общим списком селекторов (`.dist__col,\n.dist__block {`), и
 * наивный поиск находил ВТОРУЮ строку списка, то есть чужое тело: сторож
 * покраснел на верной правке. Поэтому селектор ищется как целый элемент
 * списка, а тела всех подходящих правил складываются — свойство объявлено, где
 * бы его ни объявили.
 */
function rule(scope: string, selector: string): string {
  const bodies: string[] = [];
  for (const m of scope.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const list = m[1].split(",").map((s) => s.trim());
    if (list.includes(selector)) bodies.push(m[2]);
  }
  expect(bodies.length, `${CSS_PATH}: правило ${selector} не найдено`).toBeGreaterThan(0);
  return bodies.join("\n");
}

/** Стиль без медиазапросов: базовые правила и только они. */
function base(): string {
  let text = css();
  for (;;) {
    const at = text.indexOf("@media");
    if (at === -1) return text;
    let depth = 0;
    let i = text.indexOf("{", at);
    for (; i < text.length; i++) {
      if (text[i] === "{") depth++;
      else if (text[i] === "}" && --depth === 0) break;
    }
    text = text.slice(0, at) + text.slice(i + 1);
  }
}

/**
 * Тело @media-блока целиком — по счётчику скобок, а не по первой закрывающей.
 *
 * Одинаковый порог встречается в файле дважды (раскладка колонок и ширина
 * полосы — разные заботы, разные места), поэтому нужный блок отбирается по
 * тому, что в нём лежит. Без этого сторож читал бы первый попавшийся и
 * зеленел бы на чужом правиле.
 */
function media(query: string, contains: string): string {
  const text = css();
  for (let at = text.indexOf(query); at > -1; at = text.indexOf(query, at + 1)) {
    let depth = 0;
    let i = text.indexOf("{", at);
    const from = i;
    for (; i < text.length; i++) {
      if (text[i] === "{") depth++;
      else if (text[i] === "}" && --depth === 0) break;
    }
    const body = text.slice(from, i);
    if (body.includes(contains)) return body;
  }
  expect.fail(`${CSS_PATH}: нет медиазапроса ${query} с правилом ${contains}`);
}

/** Селекторы правила, в теле которого стоит `property: value`. */
function selectorsWith(scope: string, declaration: string): string[] {
  const re = new RegExp(`([^{}]*)\\{[^{}]*${declaration.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`, "g");
  const found: string[] = [];
  for (const m of scope.matchAll(re)) {
    for (const s of m[1].split(",")) if (s.trim()) found.push(s.trim());
  }
  return found;
}

describe("Распределение: адаптивность и иерархия", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let state = { enabled: false, max_active: 5 as number | null };
  let hours = { start_hour: 8, end_hour: 22 };

  beforeEach(() => {
    queryClient.clear();
    state = { enabled: false, max_active: 5 };
    hours = { start_hour: 8, end_hour: 22 };
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["settings:manage"],
      accessToken: "t",
      bootstrapped: true,
    });

    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/settings/distribution")) {
        if (init?.method === "PATCH") return jsonResponse(200, state);
        return jsonResponse(200, state);
      }
      if (url.pathname.endsWith("/settings/work-hours")) {
        if (init?.method === "PATCH") {
          hours = JSON.parse(String(init.body));
          return jsonResponse(200, hours);
        }
        return jsonResponse(200, hours);
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const render = () =>
    renderWithProviders(<DistributionTab />, { route: "/settings/distribution" });

  it("подпись главной кнопки не режется узким экраном", async () => {
    render();
    const save = await screen.findByRole("button", { name: "Сохранить распределение" });

    const actions = rule(base(), ".dist__actions");
    // Без переноса строка «что не сохранено» отбирает у кнопки ширину: замер
    // 05.09 на окне 375 — 104 px вместо 211, подпись обрезана до «Сохранить».
    expect(actions, "ряд действий не переносится, и кнопке нечем защититься").toMatch(
      /flex-wrap:\s*wrap/,
    );

    /*
     * ⚠ ПРАВИЛО МИМО РАЗМЕТКИ ВЫГЛЯДИТ КАК РАБОТАЮЩЕЕ — в CSS оно есть, тест
     * зелёный, а кнопка на телефоне всё так же обрезана. Поэтому сторож не
     * читает стиль отдельно от экрана: он требует, чтобы НАСТОЯЩАЯ кнопка
     * сохранения попадала под селектор запрета сжатия.
     */
    const selectors = selectorsWith(css(), "flex: none");
    const мимо = `кнопка не попадает ни под один селектор: ${selectors.join(" | ")}`;
    expect(
      selectors.some((s) => save.matches(s)),
      мимо,
    ).toBe(true);
  });

  it("цель нажатия у «Без ограничения» не мельче 24 пикселей", async () => {
    render();
    const toggle = await screen.findByRole("switch", { name: "Без ограничения" });
    // Мишень — метка целиком, а не полоса: за неё и берутся пальцем.
    const target = toggle.closest("label");
    expect(target, "у тумблера нет метки-мишени").not.toBeNull();

    const selectors = selectorsWith(css(), "min-height: 24px");
    expect(
      selectors.some((s) => target!.matches(s)),
      `мишень не попадает ни под один селектор: ${selectors.join(" | ")}`,
    ).toBe(true);
  });

  it("медиазапрос говорит на языке базового правила, а не соседнего", () => {
    const text = base();
    const wide = media("@media (min-width: 1560px)", ".dist__cols");

    /*
     * ЛОВУШКА ЭТОГО ПРОЕКТА. В списке каналов узкий экран задавал строке
     * `flex-direction: column`, а строка к тому времени стала СЕТКОЙ: правило
     * не делало ничего, и название канала на телефоне резалось. Здесь сторож
     * держит пару: колонки объявлены сеткой базово — значит и переставляет их
     * медиазапрос сеткой.
     */
    // Обе половины механизма: и раскладка, и её колонки. `display: flex`
    // рядом с уцелевшим `grid-template-columns` — это и есть мёртвое свойство,
    // ради которого сторож написан.
    expect(rule(text, ".dist__cols"), "колонки объявлены не сеткой").toMatch(
      /display:\s*grid/,
    );
    expect(rule(text, ".dist__cols"), "у сетки не объявлены колонки").toMatch(
      /grid-template-columns:/,
    );
    expect(wide, "широкий экран правит не тот механизм, каким объявлены колонки").toMatch(
      /\.dist__cols\s*\{[^}]*grid-template-columns:/,
    );

    // Вторая половина той же пары: шов между решениями. Базово он сверху,
    // рядом — слева, и оба конца обязаны быть переключены, иначе на широком
    // экране черта повиснет над серединой правой колонки.
    expect(rule(text, ".dist__block"), "шов сверху пропал из базового правила").toMatch(
      /border-top:\s*1px/,
    );
    expect(wide, "горизонтальный шов не убран на широком экране").toMatch(
      /border-top:\s*none/,
    );
    expect(wide, "вертикального шва между колонками нет").toMatch(/border-left:\s*1px/);
  });

  it("закрашенный час красится индикатором, а не ступенью палитры", () => {
    /*
     * `--lc-a-800` давал к фону пустой клетки 2.15 в тёмной теме и 1.65 в
     * пресете «индиго» при норме 3.0 для нетекстовой графики: полоса, ради
     * которой блок и рисуется, читалась ровным бруском.
     */
    const тело = rule(base(), ".dist__cell[data-on]");
    expect(тело, "закрашенный час взят ступенью палитры мимо семантики").not.toMatch(
      /--lc-a-\d/,
    );
    expect(тело).toMatch(/background:\s*var\(--lc-primary\)/);
  });

  it("движение отключается по просьбе системы", () => {
    const тише = media("@media (prefers-reduced-motion: reduce)", "transition");
    // Оба перехода экрана — объясняющие, но объяснение не стоит тошноты.
    for (const selector of [".dist__cap", ".dist__cell"]) {
      expect(тише, `${selector} продолжает двигаться при запрете движения`).toContain(selector);
    }
    expect(тише).toMatch(/transition:\s*none/);
  });

  it("длина окна названа числом и идёт за полями", async () => {
    const { container } = render();
    await screen.findByLabelText("Начало рабочего дня, час по Москве");

    const num = () => container.querySelector(".dist__len-num")?.textContent;
    const cap = () => container.querySelector(".dist__len-cap")?.textContent;

    // Сохранено 8:00–22:00 — четырнадцать часов.
    expect(num()).toBe("14");
    expect(cap()).toBe("часов в сутках");

    const to = screen.getByLabelText("Конец рабочего дня, час по Москве");
    await userEvent.clear(to);
    await userEvent.type(to, "12");

    // Число — подпись к НАБРАННОМУ, как и полоса: возьми оно сохранённое,
    // человек правил бы окно, глядя на длину прежнего.
    expect(num()).toBe("4");
    // И форма слова меняется вместе с числом: «4 часов» читается как недоделка.
    expect(cap()).toBe("часа в сутках");
  });

  it("число и подпись — разные ступени кегля, а не одна", () => {
    const text = base();
    expect(rule(text, ".dist__len-num"), "показатель набран не ступенью показателя").toMatch(
      /font-size:\s*var\(--lc-fz-metric\)/,
    );
    expect(rule(text, ".dist__len-cap"), "подпись набрана не мелкой ступенью").toMatch(
      /font-size:\s*var\(--lc-fz-caption\)/,
    );
  });

  it("вывернутое окно даёт ноль, а не отрицательные часы", async () => {
    const { container } = render();
    const from = await screen.findByLabelText("Начало рабочего дня, час по Москве");

    /*
     * ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ПАДАЕТ ПО СВОЕЙ ПРИЧИНЕ: до правки на экране
     * стояло «14» (проверено соседним случаем), так что ноль здесь — следствие
     * вывернутой пары 23 → 22, а не пустого экрана.
     */
    await userEvent.clear(from);
    await userEvent.type(from, "23");

    expect(container.querySelector(".dist__len-num")?.textContent).toBe("0");
    // Ноль часов — не «мало», а сломанное окно: красится как беда, а не как
    // рядовая цифра.
    expect(container.querySelector(".dist__len-num")).toHaveAttribute("data-empty");
  });

  it("пустое поле — это «—», а не ноль", async () => {
    const { container } = render();
    const from = await screen.findByLabelText("Начало рабочего дня, час по Москве");
    await userEvent.clear(from);

    // У ненабранного окна длины нет, и объявлять его сломанным нельзя: человек
    // просто стирает цифру, чтобы напечатать другую.
    expect(container.querySelector(".dist__len-num")?.textContent).toBe("—");
    expect(container.querySelector(".dist__len-num")).not.toHaveAttribute("data-empty");
  });

  it("группа потолка стоит на поверхности, на которой видно выключенное поле", () => {
    /*
     * ⚠ МИНА НАЗВАНА В lc-vars.css: `--lc-disabled-bg` и `--lc-surface` — один
     * и тот же `--lc-bg-raise`. Поле потолка выключено ровно в том состоянии,
     * в котором экран живёт сегодня (автораздача выключена), и на `--lc-surface`
     * оно дало бы к подложке 1.00 — то есть исчезло бы.
     */
    const тело = rule(base(), ".dist__cap");
    expect(тело, "у группы нет собственной поверхности — она осталась отбивкой").toMatch(
      /background:\s*var\(--lc-bg-1\)/,
    );
    expect(тело, "группа встала на ту же поверхность, что и выключенное поле").not.toMatch(
      /background:\s*var\(--lc-surface\)/,
    );
  });
});
