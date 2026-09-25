/**
 * Подписи оси X не налезают друг на друга (бриф, пункт 1).
 *
 * ЧТО БЫЛО. Последняя точка подписывалась БЕЗУСЛОВНО, поверх равномерной
 * сетки. При тридцати днях — а это пресет по умолчанию — она приходилась
 * вплотную к предыдущей, и на графике читалось «7 авг8 авг».
 *
 * Правая дата на графике самая нужная: она отвечает на вопрос «по какой день
 * данные». Поэтому она остаётся всегда, но теперь ВЫТЕСНЯЕТ соседку, а не
 * пририсовывается рядом.
 *
 * Проверяем не картинку, а расстояния: между соседними подписями обязан
 * помещаться сам текст.
 */

import { describe, expect, it } from "vitest";
import { labelIndexes, labelWidth, perPoint } from "@/features/stats/lib/axisLabels";

/**
 * Расстояние между соседними подписями в единицах viewBox — по той же
 * `perPoint`, по которой график ставит подписи. Своя копия формулы здесь
 * завышала зазор на коротких рядах, то есть мерила не экран.
 */
function gaps(indexes: number[], count: number): number[] {
  return indexes.slice(1).map((cur, k) => (cur - indexes[k]) * perPoint(count));
}

describe("Подписи оси X", () => {
  it.each([
    ["30 дней — пресет по умолчанию", 30, "00 ммм"],
    ["7 дней", 7, "00 ммм"],
    ["7 дней по часам", 168, "8 авг 14:00"],
    ["две точки", 2, "00 ммм"],
    ["одна точка", 1, "00 ммм"],
  ])("%s: соседи не пересекаются", (_name, count, sample) => {
    const indexes = [...labelIndexes(count, 8, sample)].sort((a, b) => a - b);
    const tooClose = gaps(indexes, count).filter((g) => g < labelWidth(sample));
    expect(tooClose).toEqual([]);
  });

  it("последняя точка подписана всегда — по ней читают, до какого дня данные", () => {
    for (const count of [1, 2, 7, 30, 168]) {
      expect([...labelIndexes(count, 8, "00 ммм")]).toContain(count - 1);
    }
  });

  it("при двух точках остаются обе, а не одна", () => {
    // Крайний случай вытеснения: нестрогое сравнение иначе съело бы первую.
    expect([...labelIndexes(2, 8, "00 ммм")].sort()).toEqual([0, 1]);
  });

  it("проверка ловит прежнее поведение", () => {
    // Как было: равномерная сетка ПЛЮС безусловная последняя.
    const asBefore = new Set<number>();
    const step = Math.max(1, Math.ceil(30 / 8));
    for (let i = 0; i < 30; i += step) asBefore.add(i);
    asBefore.add(29);

    const indexes = [...asBefore].sort((a, b) => a - b);
    const tooClose = gaps(indexes, 30).filter((g) => g < labelWidth("00 ммм"));
    expect(tooClose.length).toBeGreaterThan(0);
  });
});
