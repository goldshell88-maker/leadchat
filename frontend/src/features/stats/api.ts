import { http } from "@/shared/api/http";
import type { StatsQuery } from "@/shared/api/queryKeys";
import type {
  ExportJob,
  ExportJobCreated,
  ExportRequest,
  HeatmapResponse,
  ManagersResponse,
  ManagersSort,
  MyTodayStats,
  SortOrder,
  StatsSummary,
  TimeseriesGroup,
  TimeseriesMetric,
  TimeseriesResponse,
} from "@/shared/api/types";

/**
 * Клиент раздела `/stats/*` (контракт — 06 §4, права — 01 §9).
 * Все длительности приходят целыми секундами, форматирование — на фронте.
 */

/** Общие query-параметры: даты по Москве включительно + повторяемый manager_id. */
function baseParams(q: StatsQuery): URLSearchParams {
  const p = new URLSearchParams();
  p.set("date_from", q.dateFrom);
  p.set("date_to", q.dateTo);
  if (q.accountId) p.set("account_id", q.accountId);
  for (const id of q.managerIds ?? []) p.append("manager_id", id);
  return p;
}

/** GET /stats/summary (06 §4.1) — карточки-метрики с прошлым периодом. */
export function fetchStatsSummary(q: StatsQuery): Promise<StatsSummary> {
  return http.get<StatsSummary>(`/stats/summary?${baseParams(q).toString()}`);
}

/** GET /stats/timeseries (06 §4.2) — ряд, занулённый до сплошного бэкендом. */
export function fetchTimeseries(
  q: StatsQuery,
  metric: TimeseriesMetric,
  group: TimeseriesGroup,
): Promise<TimeseriesResponse> {
  const p = baseParams(q);
  p.set("metric", metric);
  p.set("group", group);
  return http.get<TimeseriesResponse>(`/stats/timeseries?${p.toString()}`);
}

/**
 * GET /stats/heatmap (06 §4.3) — ровно 168 ячеек.
 * `manager_id` сюда не передаётся: входящие сообщения менеджеру не принадлежат.
 */
export function fetchHeatmap(q: Omit<StatsQuery, "managerIds">): Promise<HeatmapResponse> {
  return http.get<HeatmapResponse>(`/stats/heatmap?${baseParams(q).toString()}`);
}

/** GET /stats/managers (06 §4.4) — сортировка серверная, пагинации нет. */
export function fetchManagers(q: StatsQuery, sort: ManagersSort, order: SortOrder): Promise<ManagersResponse> {
  const p = baseParams(q);
  p.set("sort", sort);
  p.set("order", order);
  return http.get<ManagersResponse>(`/stats/managers?${p.toString()}`);
}

/** POST /stats/export → 202 {job_id} (06 §4.5); 409/429 прилетают ApiError'ом. */
export function createExport(body: ExportRequest): Promise<ExportJobCreated> {
  return http.post<ExportJobCreated>("/stats/export", body);
}

/** GET /stats/export/{job_id} — статус job'а, поллинг раз в 2 с (11 §6.3). */
export function fetchExportJob(jobId: string): Promise<ExportJob> {
  return http.get<ExportJob>(`/stats/export/${encodeURIComponent(jobId)}`);
}

/** GET /stats/my/today (06 §6.1) — только свои цифры, user_id берётся из JWT. */
export function fetchMyToday(): Promise<MyTodayStats> {
  return http.get<MyTodayStats>("/stats/my/today");
}
