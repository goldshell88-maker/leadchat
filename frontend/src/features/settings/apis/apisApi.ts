import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { http } from "@/shared/api/http";

/**
 * Монитор внешних сервисов (`/settings/apis`, 16.09).
 *
 * Встроенные сервисы считает сервер: ключ, переключатель, потолок, расход за
 * сегодня и простой — из окружения, настроек и счётчиков воркера. Свои записи
 * владельца — имя, адрес, лимиты, заметка — хранятся в базе и правятся здесь.
 */

export type ApiState = "ok" | "off" | "no_key" | "limit" | "down" | "unknown";

export interface ApiCheck {
  ok: boolean;
  status: number | null;
  ms: number | null;
  error: string | null;
  at: string;
}

export interface ApiRow {
  key: string;
  kind: "builtin" | "custom";
  name: string;
  purpose: string;
  url: string | null;
  docs_url: string | null;
  /** Имя переменной окружения с ключом; null — ключ не нужен. */
  env_var: string | null;
  /** null — ключ не нужен. */
  key_present: boolean | null;
  enabled: boolean;
  daily_limit: number | null;
  monthly_limit: number | null;
  /** null — расход не считается (свои записи, OSM). */
  used_today: number | null;
  state: ApiState;
  state_note: string;
  notes: string;
  checked: ApiCheck | null;
  editable: boolean;
}

export interface ApiEntryInput {
  name: string;
  purpose: string;
  url: string | null;
  docs_url: string | null;
  daily_limit: number | null;
  monthly_limit: number | null;
  notes: string;
}

export interface ApiEntryPatch {
  name?: string;
  purpose?: string;
  url?: string | null;
  docs_url?: string | null;
  daily_limit?: number | null;
  monthly_limit?: number | null;
  unlimited_daily?: boolean;
  unlimited_monthly?: boolean;
  notes?: string;
  enabled?: boolean;
}

interface ApisResponse {
  items: ApiRow[];
}

export const apisKey = ["settings", "apis"] as const;

export function useApis() {
  return useQuery({
    queryKey: apisKey,
    queryFn: () => http.get<ApisResponse>("/settings/apis"),
  });
}

function useRefreshOnSuccess() {
  const qc = useQueryClient();
  return (data: ApisResponse) => qc.setQueryData(apisKey, data);
}

export function useAddApi() {
  const onSuccess = useRefreshOnSuccess();
  return useMutation({
    mutationFn: (body: ApiEntryInput) =>
      http.post<ApisResponse & { key: string }>("/settings/apis", body),
    onSuccess,
  });
}

export function usePatchApi() {
  const onSuccess = useRefreshOnSuccess();
  return useMutation({
    mutationFn: ({ key, patch }: { key: string; patch: ApiEntryPatch }) =>
      http.patch<ApisResponse>(
        `/settings/apis/${encodeURIComponent(key)}`,
        patch,
      ),
    onSuccess,
  });
}

export function useDeleteApi() {
  const onSuccess = useRefreshOnSuccess();
  return useMutation({
    mutationFn: (key: string) =>
      http.del<ApisResponse>(`/settings/apis/${encodeURIComponent(key)}`),
    onSuccess,
  });
}

export function useCheckApi() {
  const onSuccess = useRefreshOnSuccess();
  return useMutation({
    mutationFn: (key: string) =>
      http.post<ApisResponse & { key: string; checked: ApiCheck }>(
        `/settings/apis/${encodeURIComponent(key)}/check`,
        {},
      ),
    onSuccess,
  });
}
