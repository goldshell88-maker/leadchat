// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts и cssDeadClasses.test.ts: сторож читает стиль файлом.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { DistributionTab } from "@/features/settings/distribution/DistributionTab";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Правки экрана «Распределение» по макету 04.09.
 *
 * Стережём ровно четыре поведения, каждое из которых ломается молча:
 *
 *   1. ОКНО ПОКАЗАНО ФОРМОЙ. Полоса из 24 клеток красится по НАБРАННОМУ в
 *      полях. Сломается связь полей с полосой — и она станет украшением,
 *      которое уверенно показывает чужое окно.
 *   2. ПОЯС НАЗВАН У ЗАГОЛОВКА. Сервер считает по Москве, интерфейс живёт по
 *      поясу браузера; смены у заказчика во Владивостоке и в Москве
 *      одновременно, и разница в семь часов не видна ни в одной цифре.
 *   3. СТРОКА «НЕ СОХРАНЕНО» НАЗЫВАЕТ ПРЕДМЕТ ЧИСЛАМИ. Факт без предмета не
 *      отличим от поломки: поле показывает 6, сервер держит 5, и оба выглядят
 *      одинаково.
 *   4. ПРАВИЛО ВЫБОРА НАЗВАНО. Экран включал раздачу и молчал о том, кому она
 *      отдаёт диалог.
 *
 * Плюс пятое, чисто внешнее: моноширинные цифры. Сторож держит и правило в
 * CSS, и то, что оно целится в настоящий класс поля, — селектор мимо разметки
 * выглядит точно так же, как работающий.
 */

const CSS_PATH = "src/features/settings/distribution/distribution.css";

/** Клетки полосы: 24 штуки, `data-on` — закрашенный час. */
function band(container: HTMLElement) {
  const cells = [...container.querySelectorAll(".dist__cell")];
  return {
    all: cells,
    lit: cells.filter((c) => c.hasAttribute("data-on")).map((c) => cells.indexOf(c)),
  };
}

describe("Распределение: окно формой, пояс, предмет правки", () => {
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
        if (init?.method === "PATCH") {
          const body = JSON.parse(String(init.body));
          if (body.enabled !== undefined) state.enabled = body.enabled;
          if (body.max_active_unlimited) state.max_active = null;
          else if (body.max_active !== undefined) state.max_active = body.max_active;
          return jsonResponse(200, state);
        }
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

  it("полоса закрашена ровно по сохранённому окну: 8–22 это часы с 8-го по 21-й", async () => {
    const { container } = render();
    await screen.findByLabelText("Начало рабочего дня, час по Москве");

    const { all, lit } = band(container);
    // Суток 24, и клетка — это ЧАС, а не деление шкалы: иначе форма перестаёт
    // быть измеримой глазом.
    expect(all).toHaveLength(24);
    // 8:00–22:00 — четырнадцать часов. Клетка 22 уже пустая: окно кончается
    // в 22:00, а не в 23:00.
    expect(lit).toEqual([8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]);
  });

  it("полоса идёт за полями: правка «До» перекрашивает её сразу, до сохранения", async () => {
    const { container } = render();
    const to = await screen.findByLabelText("Конец рабочего дня, час по Москве");

    await userEvent.clear(to);
    await userEvent.type(to, "12");

    // Полоса — подпись к НАБРАННОМУ. Возьми она сохранённое, человек правил бы
    // окно, глядя на форму прежнего.
    expect(band(container).lit).toEqual([8, 9, 10, 11]);
  });

  it("вывернутое окно даёт полосу без единой закрашенной клетки", async () => {
    const { container } = render();
    const to = await screen.findByLabelText("Конец рабочего дня, час по Москве");

    await userEvent.clear(to);
    await userEvent.type(to, "5");

    /*
     * ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ПАДАЕТ ПО СВОЕЙ ПРИЧИНЕ: до правки на экране было
     * четырнадцать закрашенных клеток (проверено соседним случаем), и пустота
     * здесь — следствие вывернутой пары 8 → 5, а не пустого экрана.
     */
    expect(band(container).all).toHaveLength(24);
    expect(band(container).lit).toEqual([]);
    // Запрет под полями остаётся на месте: форма его не заменяет, а объясняет.
    expect(screen.getByText(/Конец должен быть позже начала/)).toBeInTheDocument();
  });

  it("круглосуточно закрашивает все сутки", async () => {
    const { container } = render();
    await screen.findByLabelText("Начало рабочего дня, час по Москве");

    await userEvent.click(screen.getByRole("switch", { name: /Круглосуточно/ }));

    // 0–24, а не 0–23: последний час суток раньше выпадал из расчёта всегда.
    expect(band(container).lit).toHaveLength(24);
  });

  it("полоса скрыта от читалки с экрана — числа уже названы полями", async () => {
    const { container } = render();
    await screen.findByLabelText("Начало рабочего дня, час по Москве");

    const shape = container.querySelector(".dist__shape");
    expect(shape).not.toBeNull();
    // Двадцать четыре безымянные клетки подряд — это шум вместо настройки.
    expect(shape).toHaveAttribute("aria-hidden", "true");
  });

  it("часовой пояс назван плашкой у заголовка и со смещением", async () => {
    const { container } = render();
    await screen.findByLabelText("Начало рабочего дня, час по Москве");

    const tz = container.querySelector(".dist__tz");
    expect(tz, "плашки пояса у заголовка нет").not.toBeNull();
    // Одного слова «московское» мало: смещение — это то, что владивостокский
    // руководитель пересчитывает на свою смену.
    expect(tz).toHaveTextContent(/по московскому времени, UTC\+3/);
  });

  it("строка «не сохранено» называет предмет числами: предел 6 вместо 5", async () => {
    render();
    await screen.findByRole("switch", { name: /Раздавать диалоги автоматически/ });
    await userEvent.click(screen.getByRole("switch", { name: /Раздавать диалоги автоматически/ }));

    const cap = screen.getByLabelText("Сколько диалогов держать на одном менеджере");
    await userEvent.clear(cap);
    await userEvent.type(cap, "6");

    // Оба факта названы: и что раздача включается, и что потолок стал другим.
    // Без чисел строка неотличима от поломки — поле показывает 6, сервер 5.
    expect(screen.getByText(/раздача включается, предел 6 вместо 5/)).toBeInTheDocument();
  });

  it("строка «не сохранено» у часов называет оба окна", async () => {
    render();
    const to = await screen.findByLabelText("Конец рабочего дня, час по Москве");

    await userEvent.clear(to);
    await userEvent.type(to, "21");

    expect(screen.getByText(/окно 8:00–21:00 вместо 8:00–22:00/)).toBeInTheDocument();
  });

  it("экран называет правило, по которому выбирается получатель", async () => {
    render();
    await screen.findByRole("switch", { name: /Раздавать диалоги автоматически/ });

    // Правило зашито в движке (app/services/distribution.py): сначала меньшая
    // нагрузка, при равенстве — тот, кому дольше не доставалось.
    expect(screen.getByText(/меньше всего активных диалогов/)).toBeInTheDocument();
    expect(screen.getByText(/дольше других не получал новых/)).toBeInTheDocument();
  });

  it("цифры экрана моноширинные, и правило целится в настоящий класс поля", async () => {
    render();
    const from = await screen.findByLabelText("Начало рабочего дня, час по Москве");

    const css = (readFileSync(CSS_PATH, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, " ");
    const rule = /([^{}]*)\{\s*font-variant-numeric:\s*var\(--lc-num\);\s*\}/.exec(css);
    expect(rule, `${CSS_PATH}: моноширинные цифры не заданы токеном`).not.toBeNull();

    /*
     * ⚠ СЕЛЕКТОР МИМО РАЗМЕТКИ ВЫГЛЯДИТ КАК РАБОТАЮЩИЙ. Правило в файле есть,
     * тест зелёный, а цифры на экране прыгают. Поэтому сторож не читает CSS
     * отдельно от экрана: он требует, чтобы поле часов попадало хотя бы под
     * один из перечисленных селекторов.
     */
    const selectors = rule![1].split(",").map((s) => s.trim()).filter(Boolean);
    expect(
      selectors.some((s) => from.matches(s)),
      `поле часов не попадает ни под один селектор: ${selectors.join(" | ")}`,
    ).toBe(true);
  });
});
