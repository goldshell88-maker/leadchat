/*
 * Сторожа четырёх находок разбора 23.08 по оси и шкалам статистики.
 *
 * Все четыре — про ЧИСЛА, а не про вёрстку, поэтому проверяются настоящими
 * вызовами, а не чтением стилей: арифметику можно сломать в тесте.
 */

import { describe, expect, it } from "vitest";
import { buildHeatScale } from "@/features/stats/lib/heatmap";
import { formatDuration, formatDurationAxis } from "@/features/stats/lib/format";
import { labelWidth } from "@/features/stats/lib/axisLabels";
import { moscowDayOfWeek } from "@/shared/lib/moscowTime";

/*
 * №6. У подписи оси Y ровно 40 единиц viewBox: она прижата к `x = PAD.left - 8`
 * и растёт ВЛЕВО, а левее нуля SVG режет по viewBox. Полная форма «1 ч 20 м» —
 * восемь знаков, это ~52 единицы: подпись обрезалась посреди числа, то есть ось
 * показывала не то, что значила.
 */
describe("Подпись оси помещается в отведённые 40 единиц", () => {
  const БЮДЖЕТ = 40;
  const СЛУЧАИ = [45, 90, 599, 3600, 4800, 3599, 86_399];

  for (const sec of СЛУЧАИ) {
    it(`${sec} с → «${formatDurationAxis(sec)}» влезает`, () => {
      expect(labelWidth(formatDurationAxis(sec))).toBeLessThanOrEqual(БЮДЖЕТ);
    });
  }

  it("полная форма для тех же значений в бюджет НЕ влезала — иначе правка была не нужна", () => {
    const широкие = СЛУЧАИ.filter((s) => labelWidth(formatDuration(s)) > БЮДЖЕТ);
    expect(широкие.length, "ни одна полная подпись не выходила за край — проверьте бюджет").
      toBeGreaterThan(0);
  });

  it("смысл не потерян: те же цифры и единицы, только без пробелов", () => {
    expect(formatDurationAxis(4800)).toBe(formatDuration(4800).replace(/ /g, ""));
    expect(formatDurationAxis(4800)).toContain("1");
    expect(formatDurationAxis(4800)).toContain("ч");
  });
});

/*
 * №7. Пороги теплокарты — квантили выборки. Когда выборка почти вся из единиц,
 * все четыре квантиля равны единице, уровень считается числом превышенных
 * порогов — и час с ДВУМЯ обращениями красится как час с сорока.
 */
describe("Тепловая карта не выжигается на тихом периоде", () => {
  const ТИХАЯ = [...Array<number>(20).fill(1), 2, 3, 40];

  it("час с двумя обращениями не красится как час с сорока", () => {
    const шкала = buildHeatScale(ТИХАЯ);
    expect(шкала.level(2)).toBeLessThan(шкала.level(40));
  });

  it("двойка остаётся в нижней половине шкалы, а максимум — на верхней ступени", () => {
    const шкала = buildHeatScale(ТИХАЯ);
    expect(шкала.level(2)).toBeLessThanOrEqual(2);
    expect(шкала.level(40)).toBe(5);
  });

  it("ноль — всегда нулевой уровень, чем бы ни была выборка", () => {
    expect(buildHeatScale(ТИХАЯ).level(0)).toBe(0);
    expect(buildHeatScale([]).level(0)).toBe(0);
  });

  it("на здоровой выборке шкала осталась квантильной: пик не выжигает карту", () => {
    const шкала = buildHeatScale([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 500]);
    expect(шкала.level(5)).toBeGreaterThan(1);
    expect(шкала.level(500)).toBe(5);
  });
});

/*
 * №21. Столбики недели сервер строит от МОСКОВСКОЙ даты (06 §0.1), а подписи
 * считались от часов браузера. Восточнее Москвы после местной полуночи вся
 * неделя подписана со сдвигом на сутки.
 */
describe("День недели у недельного графика — московский", () => {
  it("полночь по Москве 1 января 2026 — четверг, независимо от часов машины", () => {
    // 2026-01-01T00:30 по Москве = 2025-12-31T21:30 UTC.
    expect(moscowDayOfWeek(new Date("2025-12-31T21:30:00Z"))).toBe(4);
  });

  it("за минуту ДО московской полуночи день ещё прежний", () => {
    expect(moscowDayOfWeek(new Date("2025-12-31T20:59:00Z"))).toBe(3);
  });

  it("считает по Москве, а не по UTC: в 23:30 UTC московский день уже следующий", () => {
    const at = new Date("2026-01-01T23:30:00Z"); // 02:30 2 января по Москве
    expect(moscowDayOfWeek(at)).toBe(5);
    expect(moscowDayOfWeek(at)).not.toBe(at.getUTCDay());
  });
});
