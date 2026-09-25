/**
 * Семантика дельты карточек (11 §6.2): «стрелка и цвет — семантические: для
 * времени ответа снижение — зелёное („быстрее“), для остальных рост — зелёный».
 *
 * То есть у большинства метрик рост = хорошо, а у ИНВЕРТИРОВАННЫХ (время ответа)
 * рост = плохо. Один список — один источник истины и для цвета, и для подписи.
 */

export type DeltaTone = "good" | "bad" | "neutral";

/** Метрики, у которых рост — это ухудшение (время: чем меньше, тем лучше). */
export const INVERTED_METRICS: ReadonlySet<string> = new Set([
  "frt_operator",
  "frt_bot",
  "frt_median_sec",
  "frt_median_biz_sec",
  "frt_avg_sec",
  "frt_operator_median",
]);

export function isInvertedMetric(metricId: string): boolean {
  return INVERTED_METRICS.has(metricId);
}

/**
 * Цветовая семантика дельты. `null`/`0`/нечисло → нейтрально (snapshot-карточки
 * «В работе»/«Ждут ответа» приходят с `delta_pct: null` — сравнивать не с чем).
 */
export function deltaTone(metricId: string, deltaPct: number | null | undefined): DeltaTone {
  if (typeof deltaPct !== "number" || !Number.isFinite(deltaPct) || deltaPct === 0) return "neutral";
  const grew = deltaPct > 0;
  return grew === isInvertedMetric(metricId) ? "bad" : "good";
}

/** ▲ рост / ▼ снижение / пусто, если сравнивать не с чем. */
export function deltaArrow(deltaPct: number | null | undefined): "▲" | "▼" | "" {
  if (typeof deltaPct !== "number" || !Number.isFinite(deltaPct) || deltaPct === 0) return "";
  return deltaPct > 0 ? "▲" : "▼";
}

/**
 * Пояснение в скобках у инвертированных метрик: «(быстрее)» / «(медленнее)» —
 * без него «−21 %» у времени ответа читается как ухудшение (11 §6.1).
 */
export function deltaHint(metricId: string, deltaPct: number | null | undefined): string | null {
  if (!isInvertedMetric(metricId)) return null;
  if (typeof deltaPct !== "number" || !Number.isFinite(deltaPct) || deltaPct === 0) return null;
  return deltaPct < 0 ? "быстрее" : "медленнее";
}
