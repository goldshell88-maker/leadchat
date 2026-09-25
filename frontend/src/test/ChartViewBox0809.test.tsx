// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]);
// так же поступают соседние сторожа, читающие файлы стилей и исходники.
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";
import { MetricChart } from "@/features/stats/components/MetricChart";
import type { TimeseriesResponse } from "@/shared/api/types";
import { RAIL_WIDTH_EXPANDED } from "@/shared/stores/railStore";
import { renderWithProviders } from "./render";

/**
 * ГРАФИК УМЕНЬШАЛСЯ ВДВОЕ ОТ ОДНОГО ПИКСЕЛЯ ШИРИНЫ ОКНА (08.09).
 *
 * ⚠ ЧЕСТНО О ГРАНИЦАХ ЭТОГО СТОРОЖА. jsdom не считает раскладку и не рисует
 * SVG: сравнить кегль на экране здесь нечем в принципе. Ниже стережётся
 * ПРИЧИНА — из чего берётся система координат графика и чем задан порог
 * раскладки, — а результат мерян в браузере на стенде (.dump/prov-grafik-*,
 * .dump/prov-stats-*). Числа замера ниже.
 *
 * ЧТО БЫЛО. `viewBox="0 0 720 240"` при `width: 100%; height: auto` — картинка
 * тянулась целиком, вместе с кеглем подписей. Кегль подписи оси на матрице
 * (рельса развёрнута — 218, умолчание):
 *   1024 — 13,1   1180 — 15,9   1181 — 8,7   1280 — 9,7   1366 — 10,6
 *   1440 — 11,4   1600 — 13,1   1920 — 17,6  2560 — 24,8
 * То есть рост окна на ОДИН пиксель (1180 → 1181, порог двух колонок) ужимал
 * график с 880×293 до 482×161 и подпись почти вдвое; на ноутбуках 1181–1440
 * даты на оси были нечитаемы, а на мониторе 2560 подпись оси была крупнее
 * заголовка таблицы под ней. Число подписей при этом всегда было 7: их
 * плотность считалась для поля в 720 единиц, которого на экране не было.
 *
 * ЧТО СТАЛО. `viewBox` считается по фактической ширине блока, масштаб 1:1:
 *   1024 — 13,0   1180 — 13,0   1181 — 13,0   1280 — 13,0   1366 — 13,0
 *   1440 — 13,0   1600 — 13,0   1920 — 14,0   2560 — 14,0
 * (14 на широких — это сам токен `--lc-fz-note`, он растёт от 1800.)
 * Подписей теперь 5…9 по месту, ни одной внахлёст и ни одной за краем; высота
 * SVG ровно 240 на всей матрице. При СВЁРНУТОЙ рельсе (72) проверено на
 * 1024/1181/2560: 13,0 / 13,0 / 14,0 — тот же ответ, что и при развёрнутой.
 */

const СТАТИСТИКА = readFileSync("src/features/stats/stats.css", "utf-8") as string;
const СТИЛИ = СТАТИСТИКА.replace(/\/\*[\s\S]*?\*\//g, " ");
const ГРАФИК = (readFileSync("src/features/stats/components/MetricChart.tsx", "utf-8") as string)
  .replace(/\/\*[\s\S]*?\*\//g, " ");
const ОСЬ = (readFileSync("src/features/stats/lib/axisLabels.ts", "utf-8") as string)
  .replace(/\/\*[\s\S]*?\*\//g, " ");
const ТОКЕНЫ = readFileSync("src/app/lc-vars.css", "utf-8") as string;

/** Тело блока со счётом скобок: `@supports`, `@media`, `@container`. */
function блок(где: string, голова: string): string {
  const at = где.indexOf(`${голова} {`);
  expect(at, `блок ${голова} не найден`).toBeGreaterThan(-1);
  return отПозиции(где, at, голова.length);
}

function отПозиции(где: string, at: number, длинаГоловы: number): string {
  let depth = 0;
  for (let i = at + длинаГоловы; i < где.length; i += 1) {
    if (где[i] === "{") depth += 1;
    else if (где[i] === "}") {
      depth -= 1;
      if (depth === 0) return где.slice(at, i + 1);
    }
  }
  throw new Error(`блок с позиции ${at} не закрыт`);
}

/** Все блоки с такой головой — их в файле может быть несколько. */
function всеБлоки(где: string, голова: string): string[] {
  const out: string[] = [];
  for (let at = где.indexOf(`${голова} {`); at > -1; at = где.indexOf(`${голова} {`, at + 1)) {
    out.push(отПозиции(где, at, голова.length));
  }
  return out;
}

function правило(селектор: string): string {
  const at = СТИЛИ.indexOf(`${селектор} {`);
  expect(at, `правило ${селектор} не найдено`).toBeGreaterThan(-1);
  return СТИЛИ.slice(at, СТИЛИ.indexOf("}", at));
}

function число(re: RegExp, где: string, что: string): number {
  const m = re.exec(где);
  expect(m, `не нашлось: ${что}`).toBeTruthy();
  return Number(m![1]);
}

/** Поле страницы в пикселях: `--lc-page-pad-x` ссылается на шкалу отступов. */
function полеСтраницы(): number {
  const шаг = /--lc-page-pad-x:\s*var\(--lc-space-(\d+)\)/.exec(ТОКЕНЫ);
  expect(шаг, "поле страницы больше не берётся со шкалы отступов").toBeTruthy();
  return число(new RegExp(`--lc-space-${шаг![1]}:\\s*(\\d+)px`), ТОКЕНЫ, "шаг шкалы");
}

describe("График — система координат равна фактической ширине блока", () => {
  it("ширина viewBox берётся из замера, а не из постоянной", () => {
    expect(ГРАФИК, "график перестал мерить свой блок").toMatch(/useInlineSize\(\)/);
    expect(ГРАФИК, "viewBox снова задан постоянным числом").toMatch(
      /viewBox=\{`0 0 \$\{W\} \$\{H\}`\}/,
    );
    // Замер применяется, а не лежит без дела: W — это померенное, и только при
    // его отсутствии берётся запасное.
    expect(ГРАФИК).toMatch(/const W = plotWidth !== null && plotWidth > 0 \? plotWidth : W_FALLBACK/);
    // Плотность подписей считается от ТОГО ЖЕ поля. Без этого подписи налезали
    // бы друг на друга: их шаг подбирался бы под 720 единиц, а рисовались бы
    // они в 482.
    expect(ГРАФИК, "подписи оси снова расставляются по запасному полю").toMatch(
      /labelIndexes\(geometry\.n, 8, sampleLabel, полеПостроения\)/,
    );
  });

  it("запасная ширина и отступы поля построения не разъехались с осью", () => {
    const запас = число(/const W_FALLBACK = (\d+);/, ГРАФИК, "запасная ширина графика");
    const left = число(/PAD = \{ top: \d+, right: \d+, bottom: \d+, left: (\d+) \}/, ГРАФИК, "левый отступ");
    const right = число(/PAD = \{ top: \d+, right: (\d+),/, ГРАФИК, "правый отступ");
    const поле = /export const PLOT_W = (\d+) - (\d+) - (\d+);/.exec(ОСЬ);
    expect(поле, "поле построения оси задано не числами").toBeTruthy();
    // Умолчание оси обязано совпадать с умолчанием графика: разойдись они, и
    // подписи считались бы по одному полю, а рисовались в другом — ровно тот
    // класс, из-за которого они и налезали.
    expect([Number(поле![1]), Number(поле![2]), Number(поле![3])]).toEqual([запас, left, right]);
  });

  it("высота графика — число, и одно и то же в трёх местах", () => {
    const svg = правило(".stats-chart__svg");
    expect(svg, "высота снова считается из соотношения сторон — и снова тянет кегль").not.toMatch(
      /height:\s*auto/,
    );
    const вСтилях = число(/height:\s*(\d+)px/, svg, "высота SVG в стилях");
    const вГрафике = число(/const H = (\d+);/, ГРАФИК, "H в MetricChart");
    const вЗаготовке = число(/height:\s*(\d+)px/, правило(".stats-skeleton--chart"), "высота заготовки");
    // Разойдись эти три числа — и приезд данных снова двигал бы вниз всё, что
    // под графиком (при `auto` заготовка обещала 240, а на 1181 приезжал 161).
    expect(вСтилях).toBe(вГрафике);
    expect(вЗаготовке).toBe(вГрафике);
  });
});

describe("Статистика — пороги раскладки считают ширину страницы, а не окна", () => {
  it("страница объявлена контейнером, и обе сетки спрашивают её ширину", () => {
    const страница = правило(".stats-page");
    expect(страница).toMatch(/container-type:\s*inline-size/);
    expect(страница).toMatch(/container-name:\s*stats-page/);

    expect(СТИЛИ).toMatch(/@container stats-page \(max-width: 914px\) \{\s*\.stats-page__row/);
    expect(СТИЛИ).toMatch(/@container stats-page \(max-width: 914px\) \{\s*\.stats-cards/);
    expect(СТИЛИ).toMatch(/@container stats-page \(max-width: 634px\) \{\s*\.stats-cards/);
  });

  it("страховка по окну не спорит с контейнерным запросом", () => {
    // ⚠ У ПОРОГА «НЕ ШИРЕ ЧЕМ» СТРАХОВКА ОБЯЗАНА БЫТЬ ОТДЕЛЕНА `@supports`.
    // Иначе в браузере, где работают оба, побеждает та ветка, что ниже по
    // файлу: при свёрнутой рельсе окно 1180 даёт содержимому 1060, контейнерный
    // запрос молчит, а медиазапрос ставит одну колонку — то есть отменяет саму
    // правку.
    // Веток `@supports` в файле две — у сетки страницы и у карточек, каждая
    // рядом со своим правилом. Собираем обе: страховка это они вместе.
    const страховки = всеБлоки(СТИЛИ, "@supports not (container-type: inline-size)");
    expect(страховки.length, "страховка перестала быть отдельной веткой").toBeGreaterThan(0);
    const страховка = страховки.join("\n");
    for (const порог of [1180, 900]) {
      expect(
        new RegExp(`@media \\(max-width: ${порог}px\\)`).test(страховка),
        `страховка ${порог} ушла из-под @supports`,
      ).toBe(true);
    }
    /*
     * И обратная половина — та, ради которой проверка и переписана: снаружи
     * страховки НИ ОДИН медиазапрос не должен трогать эти две сетки. Проверять
     * «есть ли 1180 под @supports» мало: диверсия, вынувшая наружу одну ветку
     * из двух, проходила такую проверку насквозь.
     */
    let снаружи = СТИЛИ;
    for (const ветка of страховки) снаружи = снаружи.replace(ветка, " ");
    for (const m of снаружи.matchAll(/@media[^{]*\{/g)) {
      const тело = блок(снаружи, снаружи.slice(m.index, m.index! + m[0].length - 1).trim());
      expect(тело, `медиазапрос снаружи страховки правит сетки: ${m[0]}`).not.toMatch(
        /\.stats-page__row|\.stats-cards/,
      );
    }
  });

  it("порог содержимого — это прежний порог окна минус рельса и поля", () => {
    // Так видно, что момент переключения у большинства не сдвинулся: 914 —
    // ровно то, что доставалось содержимому при окне 1180 и развёрнутой
    // рельсе. Изменилось другое: теперь то же число решает и при свёрнутой.
    const поле = полеСтраницы();
    expect(914 + 2 * поле + RAIL_WIDTH_EXPANDED).toBe(1180);
    expect(634 + 2 * поле + RAIL_WIDTH_EXPANDED).toBe(900);
  });
});

/* ─────────── ветка отрисовки: viewBox повторяет померенную ширину ───────── */

const РЯД: TimeseriesResponse = {
  metric: "conversations_new",
  group: "day",
  refreshed_at: "2026-09-07T11:05:12Z",
  points: Array.from({ length: 30 }, (_, i) => ({
    ts: `2026-08-${String(9 + i).padStart(2, "0")}`.replace("2026-08-3", "2026-09-0"),
    value: 40 + ((i * 37) % 120),
  })),
};

const роднойRect = Element.prototype.getBoundingClientRect;

function ширинаБлока(width: number) {
  Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0, width, height: 0 } as DOMRect;
  };
}

function нарисовать() {
  const { container } = renderWithProviders(
    <MetricChart
      data={РЯД}
      isPending={false}
      isError={false}
      onRetry={() => {}}
      metric="conversations_new"
      onMetricChange={() => {}}
      group="day"
      onGroupChange={() => {}}
      hourAllowed
    />,
  );
  const svg = container.querySelector(".stats-chart__svg")!;
  const подписи = [...container.querySelectorAll("text.chart-axis-label")].filter(
    (t) => Number(t.getAttribute("y")) > 200,
  );
  return { viewBox: svg.getAttribute("viewBox"), подписей: подписи.length };
}

describe("График — отрисовка следует за замером", () => {
  afterEach(() => {
    Element.prototype.getBoundingClientRect = роднойRect;
  });

  it("померили 482 — рисуем в 482, а не растягиваем 720", () => {
    // 482 — ширина блока при окне 1181 и развёрнутой рельсе, тот самый случай
    // из жалобы. Раньше здесь стояло бы «0 0 720 240» и масштаб 0,670.
    ширинаБлока(482);
    expect(нарисовать().viewBox).toBe("0 0 482 240");
  });

  it("померили 1275 — рисуем в 1275", () => {
    ширинаБлока(1275);
    expect(нарисовать().viewBox).toBe("0 0 1275 240");
  });

  it("мерить нечем — берётся запасная ширина, как было", () => {
    // jsdom и любая среда без раскладки. Прежнее поведение сохраняется целиком.
    expect(нарисовать().viewBox).toBe("0 0 720 240");
  });

  it("плотность подписей следует за шириной, а не за постоянной", () => {
    // Замер на стенде: 5 подписей в 482 и 9 в 1275 — против семи всегда, как
    // было раньше. Одинаковое число подписей на любой ширине и означало, что
    // их шаг считается по полю, которого на экране нет.
    ширинаБлока(482);
    const узко = нарисовать().подписей;
    Element.prototype.getBoundingClientRect = роднойRect;
    ширинаБлока(1275);
    const широко = нарисовать().подписей;
    expect(узко).toBe(5);
    expect(широко).toBe(9);
    expect(узко).toBeLessThan(широко);
  });
});
