import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { Heatmap } from "@/features/stats/components/Heatmap";
import { buildHeatScale, heatTooltip, toMatrix } from "@/features/stats/lib/heatmap";
import type { HeatmapCell, HeatmapResponse } from "@/shared/api/types";
import { renderWithProviders } from "./render";

/** Полная матрица 7×24: бэкенд отдаёт ровно 168 ячеек, нули включены (06 §4.3). */
function fullCells(value: (dow: number, hour: number) => number): HeatmapCell[] {
  const cells: HeatmapCell[] = [];
  for (let dow = 1; dow <= 7; dow += 1) {
    for (let hour = 0; hour < 24; hour += 1) cells.push({ dow, hour, value: value(dow, hour) });
  }
  return cells;
}

const response: HeatmapResponse = {
  tz: "Europe/Moscow",
  metric: "messages_in",
  // Рабочие часы нагружены, ночь пустая; вт 14:00 — пик 37.
  cells: fullCells((dow, hour) => {
    if (dow === 2 && hour === 14) return 37;
    if (hour >= 10 && hour < 20) return hour - 9;
    return 0;
  }),
};

describe("Тепловая карта 7×24 (11 §6.2)", () => {
  it("рисует ровно 168 ячеек и подписи дней", () => {
    const { container } = renderWithProviders(
      <Heatmap data={response} isPending={false} isError={false} onRetry={() => {}} />,
    );

    const grid = container.querySelector(".heatmap");
    expect(grid?.querySelectorAll(".heatmap__cell")).toHaveLength(168);
    expect(screen.getByText("пн")).toBeInTheDocument();
    expect(screen.getByText("вс")).toBeInTheDocument();
  });

  it("тултип ячейки — «Вт 14:00 — 37 входящих»", () => {
    renderWithProviders(<Heatmap data={response} isPending={false} isError={false} onRetry={() => {}} />);

    const peak = screen.getByLabelText("Вт 14:00 — 37 входящих");
    expect(peak).toHaveAttribute("title", "Вт 14:00 — 37 входящих");
    // Пик — максимальный уровень шкалы, ночная ячейка — нулевой (нейтральный фон).
    expect(peak).toHaveAttribute("data-level", "5");
    expect(screen.getByLabelText("Пн 03:00 — 0 входящих")).toHaveAttribute("data-level", "0");
  });

  it("интенсивность считается по квантилям, а не по максимуму (06 §3.4)", () => {
    // Один выброс не должен «выжечь» карту: обычные значения остаются различимыми.
    const scale = buildHeatScale([0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 1000]);

    expect(scale.level(0)).toBe(0);
    expect(scale.level(1)).toBe(1);
    expect(scale.level(1000)).toBe(5);
    expect(scale.level(5)).toBeGreaterThan(scale.level(2));
    expect(scale.level(5)).toBeLessThan(5);
  });

  it("неполный ответ не ломает сетку — недостающие ячейки нули", () => {
    const grid = toMatrix([{ dow: 1, hour: 0, value: 4 }]);
    expect(grid).toHaveLength(7);
    expect(grid[0]).toHaveLength(24);
    expect(grid[0][0]).toBe(4);
    expect(grid[6][23]).toBe(0);
  });

  it("склонение подписи входящих", () => {
    expect(heatTooltip(1, 9, 1)).toBe("Пн 09:00 — 1 входящее");
    expect(heatTooltip(7, 23, 5)).toBe("Вс 23:00 — 5 входящих");
  });

  it("при активном фильтре по менеджеру показывает бейдж «все менеджеры»", () => {
    renderWithProviders(
      <Heatmap data={response} isPending={false} isError={false} onRetry={() => {}} managerFilterActive />,
    );
    expect(screen.getByText("все менеджеры")).toBeInTheDocument();
  });
});
