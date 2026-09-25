import { describe, expect, it } from "vitest";
import { resolveCustomPeriod } from "@/features/stats/lib/customPeriod";
import { MAX_PERIOD_DAYS, periodTooLong } from "@/shared/lib/period";

/**
 * STATS-05: календарь «Произвольный период» пускал любой диапазон.
 *
 * Ограничение сверху было объявлено (`periodTooLong`, `MAX_PERIOD_DAYS`) и не
 * вызывалось НИ ИЗ ОДНОГО места продукта, а у календаря стоял только `maxDate`
 * — нижней границы нет, и «2020 — 2026» выбирается в два клика. Дальше все
 * четыре запроса экрана получали 400 разом, и человек читал «Не получилось
 * загрузить статистику» без единого слова о причине.
 */
describe("resolveCustomPeriod (STATS-05)", () => {
  const day = (iso: string) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  };

  it("первый клик диапазона ничего не применяет", () => {
    expect(resolveCustomPeriod(day("2026-08-01"), null)).toEqual({ kind: "wait" });
    expect(resolveCustomPeriod(null, null)).toEqual({ kind: "wait" });
  });

  it("нормальный период применяется", () => {
    expect(resolveCustomPeriod(day("2026-07-01"), day("2026-08-05"))).toEqual({
      kind: "period",
      period: { dateFrom: "2026-07-01", dateTo: "2026-08-05" },
    });
  });

  it("период длиннее предела НЕ применяется и объясняет причину", () => {
    const result = resolveCustomPeriod(day("2020-01-01"), day("2026-08-05"));
    expect(result.kind).toBe("error");
    // Сообщение обязано назвать и предел, и действие: «сузьте» без числа
    // оставляет человека гадать, насколько именно.
    if (result.kind === "error") {
      expect(result.message).toContain(String(MAX_PERIOD_DAYS));
      expect(result.message).toContain("сузьте");
    }
  });

  it("граница ровно на пределе ещё проходит, следующий день — уже нет", () => {
    // 366 суток включительно — ровно то, что разрешает сервер (06 §4).
    const from = day("2025-01-01");
    const okTo = new Date(from);
    okTo.setDate(okTo.getDate() + MAX_PERIOD_DAYS - 1);
    const tooFar = new Date(from);
    tooFar.setDate(tooFar.getDate() + MAX_PERIOD_DAYS);

    expect(resolveCustomPeriod(from, okTo).kind).toBe("period");
    expect(resolveCustomPeriod(from, tooFar).kind).toBe("error");
    // Предел взят у общей проверки, а не переписан здесь третьей копией.
    expect(periodTooLong({ dateFrom: "2025-01-01", dateTo: "2026-01-02" })).toBe(true);
  });
});
