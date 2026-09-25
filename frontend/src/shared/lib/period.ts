/**
 * Период статистики. Все бизнес-даты — по Москве (06 §0.1), поэтому «сегодня»
 * вычисляется не из локальной таймзоны браузера, а из Europe/Moscow: менеджер
 * из Владивостока и сервер должны понимать «сегодня» одинаково.
 */

export type PeriodPreset = "today" | "yesterday" | "last7" | "last30" | "this_month" | "prev_month" | "custom";

export interface Period {
  dateFrom: string; // YYYY-MM-DD, включительно
  dateTo: string; // YYYY-MM-DD, включительно
}

/** en-CA даёт ровно YYYY-MM-DD — без ручной сборки из частей. */
const MSK_ISO_DATE = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/Moscow",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export function moscowToday(now: Date = new Date()): string {
  return MSK_ISO_DATE.format(now);
}

/**
 * Date ↔ «YYYY-MM-DD» через ЛОКАЛЬНЫЕ части даты: `toISOString()` здесь нельзя —
 * у пользователя восточнее UTC локальная полночь уезжает на сутки назад.
 */
export function toIsoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** «2026-08-05» → локальная полночь этого дня (значение для date-пикера). */
export function parseIsoDate(s: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

/** Арифметика по календарным датам — в UTC-полдень, чтобы не ловить переходы. */
export function shiftDate(date: string, days: number): string {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Число суток в периоде, обе границы включительно (1 — «сегодня»). */
export function daysInPeriod(p: Period): number {
  const from = Date.parse(`${p.dateFrom}T12:00:00Z`);
  const to = Date.parse(`${p.dateTo}T12:00:00Z`);
  if (Number.isNaN(from) || Number.isNaN(to)) return 0;
  return Math.floor((to - from) / 86_400_000) + 1;
}

/** Группировка «по часам» разрешена только при периоде ≤ 7 дней (06 §4.2). */
export const HOUR_GROUP_MAX_DAYS = 7;

export function hourGroupAllowed(p: Period): boolean {
  const days = daysInPeriod(p);
  return days > 0 && days <= HOUR_GROUP_MAX_DAYS;
}

/** Валидация периода на клиенте (сервер ответит 400 period_too_long). */
export const MAX_PERIOD_DAYS = 366;

export function periodTooLong(p: Period): boolean {
  return daysInPeriod(p) > MAX_PERIOD_DAYS;
}

export function presetPeriod(preset: Exclude<PeriodPreset, "custom">, now: Date = new Date()): Period {
  const today = moscowToday(now);
  switch (preset) {
    case "today":
      return { dateFrom: today, dateTo: today };
    case "yesterday": {
      const y = shiftDate(today, -1);
      return { dateFrom: y, dateTo: y };
    }
    case "last7":
      return { dateFrom: shiftDate(today, -6), dateTo: today };
    case "last30":
      return { dateFrom: shiftDate(today, -29), dateTo: today };
    case "this_month":
      return { dateFrom: `${today.slice(0, 7)}-01`, dateTo: today };
    case "prev_month": {
      const firstOfThis = `${today.slice(0, 7)}-01`;
      const lastOfPrev = shiftDate(firstOfThis, -1);
      return { dateFrom: `${lastOfPrev.slice(0, 7)}-01`, dateTo: lastOfPrev };
    }
  }
}

export const PERIOD_PRESET_LABELS: Record<PeriodPreset, string> = {
  today: "Сегодня",
  yesterday: "Вчера",
  last7: "7 дней",
  last30: "30 дней",
  this_month: "Этот месяц",
  prev_month: "Прошлый месяц",
  custom: "Произвольный",
};

/** «1 – 4 авг.» / «4 авг.» — подпись выбранного периода. */
export function formatPeriodLabel(p: Period, formatDay: (d: string) => string): string {
  return p.dateFrom === p.dateTo ? formatDay(p.dateTo) : `${formatDay(p.dateFrom)} – ${formatDay(p.dateTo)}`;
}
