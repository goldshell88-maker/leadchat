/**
 * СТАТИСТИКА, ПРАВКИ 4 СЕНТЯБРЯ: сторожа на каждое поведение, которое
 * перенесено из макета вкладки.
 *
 * Что здесь стережётся и почему именно так:
 *
 *   1. ПУСТОЙ ЧАС ТЕПЛОВОЙ КАРТЫ ВИДЕН В ОБЕИХ ТЕМАХ. Проверка не верит
 *      названию токена, а считает его значение по lc-vars.css отдельно для
 *      тёмной и светлой схемы: `--lc-bg-2` совпадает с панелью в тёмной,
 *      `--lc-bg-0` — в светлой, и обе ошибки выглядят в коде одинаково
 *      безобидно. Макет предлагал вторую.
 *   2. РАБОЧЕЕ ОКНО ОТМЕЧЕНО НА ШКАЛЕ ЧАСОВ и приходит из настроек, а не
 *      напечатано числами.
 *   3. ГРУПП ДВЕ, И СНИМОК «СЕЙЧАС» ОТЛИЧАЕТСЯ ФОРМОЙ, а не подписями под
 *      каждым значением.
 *   4. ДОЛЯ НАГРУЗКИ СЧИТАЕТСЯ ОТ САМОГО НАГРУЖЕННОГО, а не от итога:
 *      от итога все полоски вырождаются в одинаковые огрызки.
 *   5. ИТОГ И ШАПКА ТАБЛИЦЫ НЕ УЕЗЖАЮТ ВМЕСТЕ СО СТРОКАМИ.
 *
 * Каждый сторож проверен диверсией — сломан ровно тот код, который он стережёт.
 */
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { Heatmap } from "@/features/stats/components/Heatmap";
import { ManagersTable } from "@/features/stats/components/ManagersTable";
import { StatsPage } from "@/features/stats/StatsPage";
import { workBand } from "@/features/stats/lib/heatmap";
import type { HeatmapCell, HeatmapResponse, ManagersResponse } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/* ─────────────────────────────── данные ────────────────────────────────── */

const CELLS: HeatmapCell[] = [];
for (let dow = 1; dow <= 7; dow += 1) {
  for (let hour = 0; hour < 24; hour += 1) {
    CELLS.push({ dow, hour, value: hour >= 10 && hour < 20 ? hour : 0 });
  }
}
const HEAT: HeatmapResponse = { tz: "Europe/Moscow", metric: "messages_in", cells: CELLS };

const МЕНЕДЖЕРЫ: ManagersResponse = {
  period: { date_from: "2026-08-05", date_to: "2026-09-04", tz: "Europe/Moscow" },
  refreshed_at: "2026-09-04T11:05:12Z",
  rows: [
    {
      manager_id: "m-1",
      full_name: "Анна Смирнова",
      is_active: true,
      taken: 34,
      answered: 31,
      closed: 28,
      frt_avg_sec: 210,
      frt_median_sec: 74,
      frt_median_biz_sec: 71,
      messages_sent: 412,
    },
    {
      manager_id: "m-2",
      full_name: "Пётр Сидоров",
      is_active: true,
      taken: 7,
      answered: 6,
      closed: 5,
      frt_avg_sec: 120,
      frt_median_sec: 100,
      frt_median_biz_sec: 90,
      messages_sent: 44,
    },
    {
      manager_id: "m-3",
      full_name: "Олег Иванов",
      is_active: false,
      taken: 4,
      answered: 3,
      closed: 2,
      frt_avg_sec: null,
      frt_median_sec: null,
      frt_median_biz_sec: null,
      messages_sent: 12,
    },
  ],
  // Итог заведомо БОЛЬШЕ суммы показанных строк: он приходит по всей выборке,
  // и полоска, посчитанная от него, разъехалась бы с тем, что на экране.
  totals: {
    taken: 120,
    answered: 260,
    closed: 290,
    frt_median_sec: 95,
    frt_median_biz_sec: 88,
    messages_sent: 2040,
  },
};

const SUMMARY = {
  period: { date_from: "2026-08-05", date_to: "2026-09-04", tz: "Europe/Moscow" },
  prev_period: { date_from: "2026-07-05", date_to: "2026-08-04" },
  refreshed_at: "2026-09-04T11:05:12Z",
  period_live: true,
  // Не 10–20 намеренно: подпись, печатающая зашитое окно, на этих часах
  // разошлась бы с данными (FUNC-42).
  work_hours: { start_hour: 9, end_hour: 21 },
  cards: {
    conversations_new: { value: 312, prev: 280, delta_pct: 11.4 },
    conversations_closed: { value: 290, prev: 301, delta_pct: -3.7 },
    in_progress_now: { value: 47, prev: null, delta_pct: null },
    waiting_now: { value: 6, prev: null, delta_pct: null },
    queue_now: { value: 23, prev: null, delta_pct: null },
    waiting_client_now: { value: 11, prev: null, delta_pct: null },
    frt_operator: {
      median_sec: 95,
      avg_sec: 340,
      median_biz_sec: 88,
      avg_biz_sec: 210,
      answered: 264,
      unanswered: 48,
      prev_median_sec: 120,
      delta_pct: -20.8,
    },
    frt_bot: { median_sec: 3, avg_sec: 4, answered: 295 },
    bot_closed: { pct: 18.6, closed_by_bot: 54, closed_total: 290, prev_pct: 15.2, delta_pct: 3.4 },
    phones_collected: { value: 78, by_source: { bot: 41, regex: 30, manual: 7 }, prev: 65, delta_pct: 20 },
    repeat_contacts: { reopened: 25, repeat_clients: 19, prev_reopened: 21 },
  },
};

/* ──────────────────────── чтение CSS и токенов темы ────────────────────── */

const stats = readFileSync("src/features/stats/stats.css", "utf-8") as string;
const vars = readFileSync("src/app/lc-vars.css", "utf-8") as string;

/** Тело правила по селектору — комментарии срезаны, они полны слов «--lc-bg-2». */
function правило(css: string, селектор: string): string {
  const чистый = css.replace(/\/\*[\s\S]*?\*\//g, " ");
  const at = чистый.indexOf(`\n${селектор} {`);
  expect(at, `правило ${селектор} не найдено`).toBeGreaterThan(-1);
  return чистый.slice(at, чистый.indexOf("}", at));
}

/** Токен из `background: var(--lc-…)` правила. */
function фонТокеном(селектор: string): string {
  const m = /background(?:-color)?:\s*var\((--lc-[a-z0-9-]+)\)/.exec(правило(stats, селектор));
  expect(m, `${селектор}: фон задан не токеном`).not.toBeNull();
  return m![1];
}

/**
 * Объявления токенов из блоков, чей селектор подошёл. Считаем только прямые
 * объявления блока (глубина 1): вложенный `@media` со шкалой кеглей нам не
 * нужен, а его скобки иначе сбили бы разбор.
 */
function объявления(подходит: (селектор: string) => boolean): Map<string, string> {
  const out = new Map<string, string>();
  let depth = 0;
  let буфер = "";
  let активен = false;
  for (const сырая of (vars.replace(/\/\*[\s\S]*?\*\//g, " ") as string).split("\n")) {
    const строка = сырая.trim();
    if (!строка) continue;
    if (depth === 0) {
      буфер += ` ${строка}`;
      if (строка.includes("{")) {
        активен = подходит(буфер.slice(0, буфер.indexOf("{")).trim());
        буфер = "";
        depth = 1;
      }
      continue;
    }
    if (depth === 1 && активен) {
      const m = /^(--lc-[a-z0-9-]+):\s*([^;]+);/.exec(строка);
      if (m) out.set(m[1], m[2].trim());
    }
    depth += (строка.match(/\{/g) ?? []).length - (строка.match(/\}/g) ?? []).length;
    if (depth < 0) depth = 0;
  }
  return out;
}

const ТЁМНАЯ = (с: string) => !с.includes("light") && !с.includes("data-lc-preset") && с.startsWith(":root");
const СВЕТЛАЯ = (с: string) => с === ':root[data-mantine-color-scheme="light"]' || ТЁМНАЯ(с);

/** Значение токена: `--lc-bg-2 → var(--lc-bg-raise) → #1a221e`. */
function значение(карта: Map<string, string>, токен: string, глубина = 0): string {
  const v = карта.get(токен);
  if (v === undefined || глубина > 8) return токен;
  const m = /^var\(\s*(--lc-[a-z0-9-]+)\s*\)$/.exec(v);
  return m ? значение(карта, m[1], глубина + 1) : v;
}

/* ─────────────────────────────── сторожа ───────────────────────────────── */

describe("Тепловая карта: пустой час виден, рабочее окно отмечено", () => {
  /*
   * ⚠ ЭТОТ СТОРОЖ СЧИТАЕТ ЦВЕТА, А НЕ СЛИЧАЕТ ИМЕНА ТОКЕНОВ.
   *
   * Дефект: ячейка с нулём красилась `--lc-bg-2`, а панель под ней —
   * `--lc-surface`, и в тёмной теме это один цвет (#1a221e). Ночные ячейки
   * сливались с панелью, и сетка 7×24 теряла форму там, где данных нет.
   *
   * Ровно та же ловушка стоит с другой стороны: `--lc-bg-0` (его и предлагал
   * макет) в светлой теме равен `--lc-surface` — оба белые. Проверка,
   * запрещающая одно имя, пропустила бы второе; поэтому сравниваются ЗНАЧЕНИЯ
   * и обе схемы сразу.
   */
  it("нулевая ячейка отличается от панели и в тёмной теме, и в светлой", () => {
    const ячейка = фонТокеном(".heatmap__cell");
    const панель = фонТокеном(".stats-panel");
    expect(панель).toBe("--lc-surface");

    for (const [имя, подходит] of [
      ["тёмная", ТЁМНАЯ],
      ["светлая", СВЕТЛАЯ],
    ] as const) {
      const карта = объявления(подходит);
      const фонЯчейки = значение(карта, ячейка);
      const фонПанели = значение(карта, панель);
      expect(фонЯчейки, `${имя} тема: токены не разобрались`).toMatch(/^#|^rgb/);
      expect(
        фонЯчейки,
        `${имя} тема: пустая ячейка (${ячейка} → ${фонЯчейки}) неотличима от панели (${панель} → ${фонПанели})`,
      ).not.toBe(фонПанели);
    }
  });

  it("рабочее окно 10:00–20:00 подчёркнуто сплошной чертой в десять колонок", () => {
    const { container } = renderWithProviders(
      <Heatmap
        data={HEAT}
        isPending={false}
        isError={false}
        onRetry={() => {}}
        workHours={{ start_hour: 10, end_hour: 20 }}
      />,
    );

    // Колонка 1 — подписи дней, час H живёт в колонке H + 2.
    const черта = container.querySelector(".heatmap__band i") as HTMLElement | null;
    expect(черта, "черты рабочего окна нет").not.toBeNull();
    expect(черта!.style.gridColumn).toBe("12 / span 10");

    const часы = container.querySelectorAll(".heatmap__hour");
    expect(часы).toHaveLength(24);
    expect(часы[9].hasAttribute("data-work"), "09:00 отмечен рабочим").toBe(false);
    expect(часы[10].hasAttribute("data-work"), "10:00 не отмечен рабочим").toBe(true);
    expect(часы[19].hasAttribute("data-work"), "19:00 не отмечен рабочим").toBe(true);
    expect(часы[20].hasAttribute("data-work"), "20:00 отмечен рабочим").toBe(false);

    // Черта — не ячейка данных: их по-прежнему ровно 168.
    expect(container.querySelectorAll(".heatmap .heatmap__cell")).toHaveLength(168);
    expect(screen.getByText("подчёркнуты рабочие часы 10:00–20:00")).toBeInTheDocument();
  });

  it("часы приходят из настроек: без них черты нет вовсе", () => {
    // Старый сервер `work_hours` не присылает. Нарисованная наугад полоса хуже
    // отсутствующей: по ней ставят дежурного.
    const { container } = renderWithProviders(
      <Heatmap data={HEAT} isPending={false} isError={false} onRetry={() => {}} />,
    );
    expect(container.querySelector(".heatmap__band")).toBeNull();
    expect(container.querySelector("[data-work]")).toBeNull();
    expect(screen.queryByText(/подчёркнуты рабочие часы/)).toBeNull();
  });

  it("вывернутое или невозможное окно черты не даёт", () => {
    expect(workBand({ start_hour: 20, end_hour: 10 })).toBeNull();
    expect(workBand({ start_hour: 0, end_hour: 25 })).toBeNull();
    expect(workBand({ start_hour: 9.5, end_hour: 21 })).toBeNull();
    expect(workBand({ start_hour: 0, end_hour: 24 })).toEqual({ column: 2, span: 24 });
  });
});

describe("Полоска нагрузки в таблице по людям", () => {
  const таблица = (data: ManagersResponse) =>
    renderWithProviders(
      <ManagersTable
        data={data}
        isPending={false}
        isError={false}
        onRetry={() => {}}
        sort="messages_sent"
        order="desc"
        onSortChange={() => {}}
        selectedManagerIds={[]}
        onSelectManager={() => {}}
        onOpenChats={() => {}}
      />,
    );

  const ширины = (container: HTMLElement) =>
    [...container.querySelectorAll(".managers__load-bar i")].map((i) => (i as HTMLElement).style.width);

  /*
   * ⚠ ДОЛЯ — ОТ САМОГО НАГРУЖЕННОГО, А НЕ ОТ ИТОГА. Итог здесь 2040 на трёх
   * показанных строках: считай мы от него — лидер получил бы 20 % ширины, а
   * все три полоски слились бы в одинаковые огрызки. Числа подобраны так, что
   * подмена делителя видна сразу: 412 из 412 — это 100 %, а 412 из 2040 — 20 %.
   */
  it("ширина считается от максимума по показанным строкам, а не от ИТОГО", () => {
    const { container } = таблица(МЕНЕДЖЕРЫ);
    expect(ширины(container)).toEqual(["100%", "11%", "3%"]);
  });

  it("смена без единого сообщения полосок не рисует", () => {
    // Пустые рамки у всех тринадцати читались бы как «данные не доехали».
    const нули: ManagersResponse = {
      ...МЕНЕДЖЕРЫ,
      rows: МЕНЕДЖЕРЫ.rows.map((r) => ({ ...r, messages_sent: 0 })),
    };
    const { container } = таблица(нули);
    expect(container.querySelectorAll(".managers__load-bar")).toHaveLength(0);
    // Само число при этом на месте — полоска только вторая подача.
    expect(container.querySelectorAll('td[data-label="Сообщений"]')).toHaveLength(4);
  });

  it("ИТОГО и шапка прилипают к краям прокручиваемого тела", () => {
    // С 35 сотрудниками итог иначе виден только тому, кто долистал до конца, а
    // шапка уезжает вверх — и колонка «1 м 12 с» остаётся без названия.
    const обёртка = правило(stats, ".managers-scroll");
    expect(обёртка, "тело таблицы не прокручивается").toMatch(/overflow-y:\s*auto/);
    expect(обёртка, "потолка высоты нет — прокручивать нечего").toMatch(/max-height:\s*\d+px/);

    const итог = правило(stats, ".managers__totals td");
    expect(итог, "ИТОГО уедет вместе со строками").toMatch(/position:\s*sticky/);
    expect(итог, "ИТОГО прилипает не к нижнему краю").toMatch(/bottom:\s*0/);
    // Фон обязан совпасть с обёрткой, иначе строки просвечивают сквозь итог.
    expect(итог).toMatch(/background:\s*var\(--lc-surface\)/);

    const шапка = правило(stats, ".managers th");
    expect(шапка, "шапка уедет вместе со строками").toMatch(/position:\s*sticky/);
    expect(шапка).toMatch(/background:\s*var\(--lc-surface\)/);
  });
});

describe("Карточки: две группы, снимок «сейчас» отличается формой", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

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
      if (url.pathname.endsWith("/stats/summary")) return jsonResponse(200, SUMMARY);
      if (url.pathname.endsWith("/stats/managers")) return jsonResponse(200, МЕНЕДЖЕРЫ);
      if (url.pathname.endsWith("/stats/heatmap")) return jsonResponse(200, HEAT);
      if (url.pathname.endsWith("/stats/timeseries")) {
        return jsonResponse(200, {
          metric: "conversations_new",
          group: "day",
          refreshed_at: SUMMARY.refreshed_at,
          points: [{ ts: "2026-09-03", value: 41 }],
        });
      }
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("за период — семь равных карточек, «сейчас» — плоская полоса из четырёх", async () => {
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    const заПериод = container.querySelector(".stats-cards");
    expect(заПериод?.querySelectorAll(":scope > .stat-card")).toHaveLength(7);

    /*
     * Полоса — не третий ряд карточек: у её ячеек нет ни поднятой поверхности,
     * ни собственной рамки (`.stat-card--flat` в stats.css). Проверяем прямыми
     * детьми: подсказка Mantine оборачивает карточку в свой узел — и тогда
     * `:first-child`, которым снимается левая граница, промахнётся.
     */
    const сейчас = container.querySelector(".stats-now");
    expect(сейчас, "полосы «Прямо сейчас» нет").not.toBeNull();
    expect(сейчас!.querySelectorAll(":scope > .stat-card--flat")).toHaveLength(4);
    expect(сейчас!.querySelectorAll(".stat-card:not(.stat-card--flat)")).toHaveLength(0);

    // Третьей группы («Качество работы за период») больше нет: она делила
    // пополам величины одной природы.
    expect(container.querySelectorAll(".stats-group")).toHaveLength(2);
    expect(screen.queryByText("Качество работы за период")).toBeNull();
  });

  it("подписей «сейчас» под значениями не осталось ни одной", async () => {
    // Их было четыре штуки на одну мысль, а мысль уже сказана заголовком
    // группы «Прямо сейчас · не зависит от выбранного периода».
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    expect(screen.getByText("не зависит от выбранного периода")).toBeInTheDocument();
    const подписи = [...container.querySelectorAll(".stat-card__caption")].map((s) => s.textContent);
    expect(подписи).not.toContain("сейчас");
  });

  it("«Отвечено» стоит карточкой, а не строкой в чужой подсказке", async () => {
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await screen.findByText("312");

    const карточка = container.querySelector('.stat-card[data-metric="answered"]');
    expect(карточка, "карточки «Отвечено» нет").not.toBeNull();
    expect(карточка!.querySelector(".stat-card__value")).toHaveTextContent("264");
    expect(карточка!.querySelector(".stat-card__caption")).toHaveTextContent("без ответа 48");
  });

  it("срез стоит в одной строке с заголовком, а не вторым рядом под ним", async () => {
    // Второй ряд контролов отнимал 76 пикселей вертикали у экрана, за которым
    // сидят смену; слот `actions` общей шапки для того и существует.
    const { container } = renderWithProviders(<StatsPage />, { route: "/stats" });
    await waitFor(() => expect(container.querySelector(".stats-filters")).not.toBeNull());

    expect(container.querySelector(".page-header__actions .stats-filters")).not.toBeNull();
  });
});

describe("Сетка и цифры", () => {
  it("ряд карточек фиксирован семью колонками, а не auto-fill", () => {
    // `auto-fill` считает места по ширине экрана: на мониторе 2560 он давал
    // восемь мест под семь карточек, и ряд выглядел рваным.
    const тело = правило(stats, ".stats-cards");
    expect(тело).toMatch(/grid-template-columns:\s*repeat\(7,\s*minmax\(0,\s*1fr\)\)/);
    expect(тело, "сетка снова считает места по ширине окна").not.toMatch(/auto-fill|auto-fit/);
  });

  it("числа набраны моноширинными цифрами и в карточках, и в колонках", () => {
    // В пропорциональном наборе «1» уже «8»: соседние числа с равным числом
    // разрядов сравниваются по ширине пятна, а не по разрядам.
    for (const селектор of [".stat-card__value", ".managers__totals td"]) {
      expect(правило(stats, селектор), `${селектор}: цифры не моноширинные`).toMatch(
        /font-variant-numeric:\s*var\(--lc-num\)/,
      );
    }
    expect(правило(stats, ".managers th[data-numeric],\n.managers td[data-numeric]")).toMatch(
      /font-variant-numeric:\s*var\(--lc-num\)/,
    );
  });
});
