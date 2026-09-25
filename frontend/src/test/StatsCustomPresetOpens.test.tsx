/**
 * «ПРОИЗВОЛЬНЫЙ» ОТКРЫВАЕТ КАЛЕНДАРЬ.
 *
 * ЧТО БЫЛО. Пункт «Произвольный» в списке периодов был пунктом без
 * последствий: `applyPreset` глушил его строкой `if (next === "custom")
 * return;` с объяснением «его ставит onCustomPeriod парой from/to». Но
 * `onCustomPeriod` зовёт КАЛЕНДАРЬ, а календарь рисуется только когда в адресе
 * уже лежит `period=custom` с парой дат. Круг замкнут: чтобы открыть календарь,
 * нужен период, который задаётся только календарём.
 *
 * На экране это выглядело так: список закрывается, подпись откатывается к
 * прежнему пресету, ничего не происходит. То есть выбрать свой диапазон — и
 * выгрузить CSV за него — из интерфейса было НЕЛЬЗЯ вовсе.
 *
 * ЧТО СТАЛО. Выбор «Произвольного» кладёт в адрес текущий применённый период,
 * календарь открывается с ним и правится кликами. Соседние экраны так и
 * делают: журнал уведомлений и журнал аудита засевают диапазон сами.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { StatsPage } from "@/features/stats/StatsPage";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const ПУСТО = { refreshed_at: null, rows: [], cells: [], points: [], items: [] };

describe("Статистика: пункт «Произвольный»", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "stats:all"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, ПУСТО)),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("выбор «Произвольного» показывает календарь, а не проглатывает нажатие", async () => {
    const user = userEvent.setup();
    renderWithProviders(<StatsPage />, { route: "/stats" });

    // До выбора календаря нет — стоит подпись применённого периода.
    expect(screen.queryByLabelText("Произвольный период")).toBeNull();

    await user.click(screen.getByRole("textbox", { name: "Период" }));
    await user.click(await screen.findByRole("option", { name: "Произвольный" }));

    expect(await screen.findByLabelText("Произвольный период")).toBeInTheDocument();
  });

  it("календарь открыт на том периоде, что человек и так видел", async () => {
    const user = userEvent.setup();
    // Заходим с «7 дней»: заготовкой обязан стать именно он, а не выдуманный
    // диапазон — иначе выборка молча подменится под руками.
    renderWithProviders(<StatsPage />, { route: "/stats?period=last7" });

    await user.click(screen.getByRole("textbox", { name: "Период" }));
    await user.click(await screen.findByRole("option", { name: "Произвольный" }));

    const поле = await screen.findByLabelText("Произвольный период");
    // Семь суток включительно — готовый диапазон «N – M», а не пустое
    // «Выберите даты»: календарь открыт на том, что человек и так видел.
    expect(поле.textContent).toMatch(/\d.+\s–\s.+\d/);
    expect(поле.textContent).not.toContain("Выберите даты");
  });

  it("уход с «Произвольного» уносит и даты", async () => {
    const user = userEvent.setup();
    renderWithProviders(<StatsPage />, { route: "/stats?period=custom&from=2026-08-01&to=2026-08-05" });

    expect(screen.getByLabelText("Произвольный период")).toBeInTheDocument();
    await user.click(screen.getByRole("textbox", { name: "Период" }));
    await user.click(await screen.findByRole("option", { name: "30 дней" }));

    expect(screen.queryByLabelText("Произвольный период")).toBeNull();
  });
});
