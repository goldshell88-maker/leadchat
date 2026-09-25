import { qk } from "@/shared/api/queryKeys";
import type { ExportRequest } from "@/shared/api/types";
import { useExportJob } from "@/shared/export/useExportJob";
import { createExport, fetchExportJob } from "../api";

/** Выгрузка статистики — общая фоновая выгрузка со своим тостом. */
export function useStatsExport() {
  return useExportJob<ExportRequest>({
    toastId: "stats-export",
    create: createExport,
    fetchJob: fetchExportJob,
    jobKey: qk.stats.exportJob,
  });
}
