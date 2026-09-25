import { MAX_PERIOD_DAYS, periodTooLong, toIsoDate, type Period } from "@/shared/lib/period";

/**
 * Что делать с парой дат, выбранной в календаре «Произвольный период».
 *
 * Решение вынесено из обработчика отдельной функцией не ради красоты: у
 * `DatePickerInput` три исхода на одно событие (диапазон ещё не собран, он
 * слишком длинный, он годится), и внутри JSX они читались как одна строка с
 * ранним `return` — ровно там, где раньше не было проверки длины вовсе.
 * Отдельной функцией все три видны сразу и проверяются без календаря.
 */
export type CustomPeriodResult =
  /** Первый клик: `[from, null]` — ждём вторую дату, ничего не меняем. */
  | { kind: "wait" }
  /** Диапазон длиннее лимита — не применяем и объясняем (STATS-05). */
  | { kind: "error"; message: string }
  | { kind: "period"; period: Period };

export function resolveCustomPeriod(from: Date | null, to: Date | null): CustomPeriodResult {
  if (!from || !to) return { kind: "wait" };
  const period = { dateFrom: toIsoDate(from), dateTo: toIsoDate(to) };
  if (periodTooLong(period)) {
    // Сервер на такой период отвечает 400 всем четырём запросам экрана сразу,
    // и человек видит «Не получилось загрузить статистику» без слова о
    // причине. Сообщение обязано назвать и предел, и действие.
    return { kind: "error", message: `Не больше ${MAX_PERIOD_DAYS} дней — сузьте диапазон` };
  }
  return { kind: "period", period };
}
