import type { HeatmapCell } from "@/shared/api/types";

/**
 * Шкала тепловой карты. Интенсивность — по КВАНТИЛЯМ выборки, а не по максимуму
 * (06 §3.4): один пиковый час не должен «выжигать» карту. Уровень 0 — ноль
 * входящих (нейтральный фон), 1…5 — оттенки от `--lc-green-50` (#d8f3d8) к
 * `--lc-green-600` (#2ea32d) — цвета живут в stats.css по `data-level`.
 */

export const HEAT_LEVELS = 5;

export interface HeatScale {
  /** 0 — пусто, 1…5 — растущая интенсивность. */
  level(value: number): number;
  thresholds: number[];
  max: number;
}

function quantile(sortedAsc: number[], p: number): number {
  if (sortedAsc.length === 0) return 0;
  const idx = Math.min(sortedAsc.length - 1, Math.max(0, Math.round(p * (sortedAsc.length - 1))));
  return sortedAsc[idx];
}

/**
 * ⚠ КВАНТИЛИ СХЛОПЫВАЮТСЯ НА ТИХОЙ ВЫБОРКЕ (находка 23.08 №7).
 *
 * Пороги — это значения из самой выборки, и когда выборка почти вся из единиц
 * (двадцать часов по одному обращению, потом 2, 3 и 40), все четыре квантиля
 * равны единице. Уровень считается числом превышенных порогов — и час с ДВУМЯ
 * обращениями превышает все четыре, то есть красится ровно как час с сорока.
 * Карта, которая должна показывать, когда людям писать, показывает шум.
 *
 * Лечится не сменой квантилей, а признанием вырождения: если пороги не строго
 * растут, шкала строится линейно между наименьшим ненулевым и максимумом. На
 * той же выборке 2 попадает в первый уровень, 40 — в пятый.
 */
function лестница(nonZero: number[]): number[] {
  const квантили = [0.2, 0.4, 0.6, 0.8].map((p) => quantile(nonZero, p));
  const строго_растут = квантили.every((t, i) => i === 0 || t > квантили[i - 1]);
  if (строго_растут) return квантили;
  const min = nonZero[0] ?? 0;
  const max = nonZero[nonZero.length - 1] ?? 0;
  if (!(max > min)) return квантили; // все значения равны — делить нечего
  return [1, 2, 3, 4].map((k) => min + ((max - min) * k) / 5);
}

export function buildHeatScale(values: number[]): HeatScale {
  const nonZero = values.filter((v) => v > 0).sort((a, b) => a - b);
  const thresholds = лестница(nonZero);
  const max = nonZero.length ? nonZero[nonZero.length - 1] : 0;
  return {
    thresholds,
    max,
    level(value: number) {
      if (!(value > 0)) return 0;
      let lvl = 1;
      for (const t of thresholds) if (value > t) lvl += 1;
      return Math.min(lvl, HEAT_LEVELS);
    },
  };
}

/** Рабочее окно из настроек: приходит в ответе `/stats/summary` (FUNC-42). */
export interface WorkHours {
  start_hour: number;
  end_hour: number;
}

/**
 * ПОЛОСА РАБОЧЕГО ОКНА НА ШКАЛЕ ЧАСОВ.
 *
 * Вопрос к этой карте ровно один: сколько спроса приходит там, где дежурного
 * нет. Ответить на него можно было только держа в голове, во сколько смена
 * начинается, — часы на карте не отмечались вовсе, хотя приходят с сервера
 * (`work_hours`, настраиваются владельцем и не зашиты).
 *
 * Черта рисуется ОДНИМ элементом на все рабочие колонки: собери её из
 * подчёркиваний под каждым часом — и зазоры сетки (2px) разрежут её на
 * пунктир, который читается как «часть часов рабочие, часть нет».
 *
 * Колонка 1 в сетке ряда — подписи дней, часы начинаются со второй, поэтому
 * час H стоит в колонке H + 2.
 *
 * Значения из настроек проверяем: окно вне суток или вывернутое (start ≥ end)
 * молча остаётся без черты. Нарисованная наугад полоса хуже отсутствующей —
 * по ней будут ставить дежурного.
 */
export function workBand(hours: WorkHours | undefined): { column: number; span: number } | null {
  if (!hours) return null;
  const { start_hour: start, end_hour: end } = hours;
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  if (start < 0 || end > 24 || start >= end) return null;
  return { column: start + 2, span: end - start };
}

/** Час внутри рабочего окна: подпись такого часа набрана ярче соседних. */
export function isWorkHour(hour: number, hours: WorkHours | undefined): boolean {
  const band = workBand(hours);
  if (!band) return false;
  // Из колонки обратно в час — той же арифметикой, что и вперёд: колонка = час + 2.
  const начало = band.column - 2;
  return hour >= начало && hour < начало + band.span;
}

/** ISO-дни недели: 1 = понедельник … 7 = воскресенье (06 §4.3). */
export const DOW_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"] as const;

/** «Вт 14:00 — 37 входящих» (11 §6.2). */
export function heatTooltip(dow: number, hour: number, value: number): string {
  const day = DOW_SHORT[dow - 1] ?? "";
  const dayCap = day ? day[0].toUpperCase() + day.slice(1) : "";
  return `${dayCap} ${String(hour).padStart(2, "0")}:00 — ${value} ${pluralIncoming(value)}`;
}

function pluralIncoming(n: number): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return "входящее";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "входящих";
  return "входящих";
}

/**
 * Матрица 7×24 из ответа API. Бэкенд обещает ровно 168 ячеек, но UI не должен
 * падать на неполном ответе — недостающие считаем нулями.
 */
export function toMatrix(cells: HeatmapCell[] | undefined): number[][] {
  const grid: number[][] = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 0));
  for (const c of cells ?? []) {
    const row = c.dow - 1;
    if (row < 0 || row > 6 || c.hour < 0 || c.hour > 23) continue;
    grid[row][c.hour] = c.value;
  }
  return grid;
}
