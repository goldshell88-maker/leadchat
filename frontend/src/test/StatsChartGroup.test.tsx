import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MetricChart } from "@/features/stats/components/MetricChart";
import type { TimeseriesResponse } from "@/shared/api/types";
import { daysInPeriod, hourGroupAllowed, presetPeriod, shiftDate } from "@/shared/lib/period";
import { renderWithProviders } from "./render";

const series: TimeseriesResponse = {
  metric: "conversations_new",
  group: "day",
  refreshed_at: "2026-08-04T11:05:12Z",
  points: [
    { ts: "2026-08-01", value: 41 },
    { ts: "2026-08-02", value: 0 },
    { ts: "2026-08-03", value: 55 },
    { ts: "2026-08-04", value: 38 },
  ],
};

function renderChart(hourAllowed: boolean, onGroupChange = vi.fn()) {
  renderWithProviders(
    <MetricChart
      data={series}
      isPending={false}
      isError={false}
      onRetry={() => {}}
      metric="conversations_new"
      onMetricChange={() => {}}
      group="day"
      onGroupChange={onGroupChange}
      hourAllowed={hourAllowed}
    />,
  );
  return onGroupChange;
}

/** «По часам» разрешено только при периоде ≤ 7 дней (06 §4.2) — блокируем в UI. */
describe("Группировка графика по часам", () => {
  it("период ≤ 7 дней: считается разрешённым", () => {
    expect(hourGroupAllowed(presetPeriod("today"))).toBe(true);
    expect(hourGroupAllowed(presetPeriod("last7"))).toBe(true);
    expect(daysInPeriod(presetPeriod("last7"))).toBe(7);
  });

  it("период 8 дней и больше: запрещено", () => {
    const to = "2026-08-08";
    expect(hourGroupAllowed({ dateFrom: shiftDate(to, -7), dateTo: to })).toBe(false); // 8 дней
    expect(hourGroupAllowed(presetPeriod("last30"))).toBe(false);
  });

  /*
   * ⚠ ПЕРЕВЁРНУТО 22.08 ПО ЖАЛОБЕ ВЛАДЕЛЬЦА: «не доступна статистика по часам,
   * хотя уже должна работать».
   *
   * Прежде кнопка гасла на длинном периоде, и это выглядело разумно — правило
   * «часы только до семи дней» верное (год по часам это 8760 точек). Но период
   * по умолчанию тридцать дней, значит кнопка была погашена ПРИ КАЖДОМ открытии
   * экрана, а причина жила в подсказке НА ПОГАШЕННОЙ кнопке — то есть там, куда
   * не наводят. Функция была, дверь была заперта, табличка висела с той стороны.
   *
   * Теперь кнопка всегда живая: нажатие просит страницу сжать период до недели
   * и включить часы (StatsPage), а рядом всплывает тост — молча менять период
   * нельзя. Ограничение никуда не делось, изменился ответ на нажатие: вместо
   * тишины человек получает то, за чем нажимал.
   */
  it("на длинном периоде кнопка живая и просит переключить на часы", async () => {
    const user = userEvent.setup();
    const onGroupChange = renderChart(false);

    const hourBtn = screen.getByRole("button", { name: /по часам/ });
    expect(hourBtn).toBeEnabled();

    await user.click(hourBtn);
    expect(onGroupChange).toHaveBeenCalledWith("hour");
  });

  it("на длинном периоде видна причина без наведения", () => {
    renderChart(false);
    expect(screen.getByRole("button", { name: /по часам/ }).textContent).toContain("неделя");
  });

  it("на коротком периоде кнопка активна и переключает на hour", async () => {
    const user = userEvent.setup();
    const onGroupChange = renderChart(true);

    const hourBtn = screen.getByRole("button", { name: /по часам/ });
    expect(hourBtn).toBeEnabled();

    await user.click(hourBtn);
    expect(onGroupChange).toHaveBeenCalledWith("hour");
  });

  it("столбцы рисуются своим SVG — по столбику на точку ряда", () => {
    const { container } = renderWithProviders(
      <MetricChart
        data={series}
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

    expect(container.querySelectorAll(".chart-bar")).toHaveLength(series.points.length);
    expect(container.querySelector(".chart-line")).toBeNull();
  });

  it("медиана времени ответа рисуется линией и РВЁТСЯ на null (нет данных ≠ ноль)", () => {
    const frt: TimeseriesResponse = {
      metric: "frt_operator_median",
      group: "day",
      refreshed_at: null,
      points: [
        { ts: "2026-08-01", value: 95 },
        { ts: "2026-08-02", value: null },
        { ts: "2026-08-03", value: 120 },
        { ts: "2026-08-04", value: 88 },
      ],
    };

    const { container } = renderWithProviders(
      <MetricChart
        data={frt}
        isPending={false}
        isError={false}
        onRetry={() => {}}
        metric="frt_operator_median"
        onMetricChange={() => {}}
        group="day"
        onGroupChange={() => {}}
        hourAllowed
      />,
    );

    // Разрыв → два отдельных полилайна, точки только у не-null значений.
    expect(container.querySelectorAll(".chart-line")).toHaveLength(2);
    expect(container.querySelectorAll(".chart-dot")).toHaveLength(3);
    expect(container.querySelectorAll(".chart-bar")).toHaveLength(0);
  });
});
