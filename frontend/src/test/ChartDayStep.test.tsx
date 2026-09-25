/**
 * Шаг по суткам в почасовом режиме (просьба владельца 22.08: «сделай так, чтобы
 * можно было выбирать день»).
 *
 * Неделя по часам — 168 столбиков: общая форма видна, а «что было позавчера в
 * обед» из неё уже не вычитать. Стрелки ставят период в ОДНИ сутки и шагают по
 * ним, не трогая ни метрику, ни группировку.
 *
 * Стрелки показываются ТОЛЬКО в почасовом режиме: по дням шаг в сутки
 * бессмыслен, там для этого есть пресеты периода.
 */
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MetricChart } from "@/features/stats/components/MetricChart";
import type { TimeseriesResponse } from "@/shared/api/types";
import { renderWithProviders } from "./render";

const ряд: TimeseriesResponse = {
  metric: "conversations_new",
  group: "hour",
  refreshed_at: "2026-08-22T11:05:12Z",
  points: [
    { ts: "2026-08-22T00:00:00Z", value: 3 },
    { ts: "2026-08-22T01:00:00Z", value: 5 },
  ],
};

function нарисовать(
  group: "day" | "hour",
  onDayShift = vi.fn(),
  onWholeWeek = vi.fn(),
  dayForwardDisabled = false,
) {
  renderWithProviders(
    <MetricChart
      data={{ ...ряд, group }}
      isPending={false}
      isError={false}
      onRetry={() => {}}
      metric="conversations_new"
      onMetricChange={() => {}}
      group={group}
      onGroupChange={() => {}}
      hourAllowed
      dayLabel="22 авг."
      onDayShift={onDayShift}
      onWholeWeek={onWholeWeek}
      dayForwardDisabled={dayForwardDisabled}
    />,
  );
  return { onDayShift, onWholeWeek };
}

describe("выбор дня на почасовом графике", () => {
  it("в почасовом режиме стрелки есть и подписан текущий день", () => {
    нарисовать("hour");
    expect(screen.getByRole("button", { name: "Предыдущий день" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Следующий день" })).toBeInTheDocument();
    expect(screen.getByText("22 авг.")).toBeInTheDocument();
  });

  it("по дням стрелок нет — там шаг в сутки бессмыслен", () => {
    нарисовать("day");
    expect(screen.queryByRole("button", { name: "Предыдущий день" })).toBeNull();
  });

  it("стрелка назад просит сдвиг на минус сутки", async () => {
    const user = userEvent.setup();
    const { onDayShift } = нарисовать("hour");
    await user.click(screen.getByRole("button", { name: "Предыдущий день" }));
    expect(onDayShift).toHaveBeenCalledWith(-1);
  });

  it("стрелка вперёд просит сдвиг на плюс сутки", async () => {
    const user = userEvent.setup();
    const { onDayShift } = нарисовать("hour");
    await user.click(screen.getByRole("button", { name: "Следующий день" }));
    expect(onDayShift).toHaveBeenCalledWith(1);
  });

  it("из одного дня можно вернуться ко всей неделе", async () => {
    const user = userEvent.setup();
    const { onWholeWeek } = нарисовать("hour");
    await user.click(screen.getByRole("button", { name: "неделя" }));
    expect(onWholeWeek).toHaveBeenCalled();
  });

  /*
   * Показан сегодняшний день — вперёд идти некуда. Кнопка ГАСНЕТ, а не молчит:
   * нажатие без последствий человек читает как поломку и жмёт ещё раз.
   */
  it("на сегодняшнем дне стрелка вперёд погашена и не зовёт сдвиг", async () => {
    const user = userEvent.setup();
    const { onDayShift } = нарисовать("hour", vi.fn(), vi.fn(), true);
    const вперёд = screen.getByRole("button", { name: "Следующий день" });
    expect(вперёд).toBeDisabled();
    await user.click(вперёд);
    expect(onDayShift).not.toHaveBeenCalled();
  });

  it("назад с сегодняшнего дня по-прежнему можно", async () => {
    const user = userEvent.setup();
    const { onDayShift } = нарисовать("hour", vi.fn(), vi.fn(), true);
    await user.click(screen.getByRole("button", { name: "Предыдущий день" }));
    expect(onDayShift).toHaveBeenCalledWith(-1);
  });
});
