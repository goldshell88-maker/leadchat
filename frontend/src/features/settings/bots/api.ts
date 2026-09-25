import { ApiError, http, request } from "@/shared/api/http";
import type {
  BotAccountsResponse,
  BotCreateInput,
  BotDetail,
  BotScenarioIssue,
  BotUpdateInput,
  BotsPage,
  SandboxStartBody,
  SandboxStartResponse,
  SandboxTurnResponse,
} from "@/shared/api/types";

/**
 * Слой доступа к `/bots/*` (01 §8) и песочнице `/bots/sandbox/*` (01 §8.6).
 * Право на всё — `bots:manage` (только admin), guard стоит в роутере.
 */

export function fetchBots(): Promise<BotsPage> {
  return http.get<BotsPage>("/bots");
}

export function fetchBot(id: string): Promise<BotDetail> {
  return http.get<BotDetail>(`/bots/${id}`);
}

/** POST /bots (01 §8.3) — 201 с деталью нового бота. */
export function createBot(body: BotCreateInput): Promise<BotDetail> {
  return http.post<BotDetail>("/bots", body);
}

/** PUT /bots/{id} (01 §8.3) — полная замена: сценарий атомарен, PATCH не даём. */
export function updateBot(id: string, body: BotUpdateInput): Promise<BotDetail> {
  return request<BotDetail>(`/bots/${id}`, { method: "PUT", body });
}

/** Вкл/выкл — отдельные endpoint'ы (01 §8.4), без сохранения формы. */
export function setBotEnabled(id: string, enabled: boolean): Promise<BotDetail> {
  return http.post<BotDetail>(`/bots/${id}/${enabled ? "enable" : "disable"}`);
}

/** PUT /bots/{id}/accounts (01 §8.5) — полный список; отвязанные обнуляются. */
export function setBotAccounts(id: string, accountIds: string[]): Promise<BotAccountsResponse> {
  return request<BotAccountsResponse>(`/bots/${id}/accounts`, {
    method: "PUT",
    body: { account_ids: accountIds },
  });
}

/* --------------------------------- Песочница ------------------------------ */

export function sandboxStart(body: SandboxStartBody): Promise<SandboxStartResponse> {
  return http.post<SandboxStartResponse>("/bots/sandbox/start", body);
}

export function sandboxMessage(sessionId: string, text: string): Promise<SandboxTurnResponse> {
  return http.post<SandboxTurnResponse>(`/bots/sandbox/${sessionId}/message`, { text });
}

/** «⏩ Промотать таймаут» — эмуляция `bot_ask_timeout` без ожидания 24 ч. */
export function sandboxFireTimeout(sessionId: string): Promise<SandboxTurnResponse> {
  return http.post<SandboxTurnResponse>(`/bots/sandbox/${sessionId}/fire-timeout`);
}

export function sandboxStop(sessionId: string): Promise<void> {
  return http.del<void>(`/bots/sandbox/${sessionId}`);
}

/* ------------------------------ Ошибки валидации -------------------------- */

interface RawIssue {
  level?: string;
  code?: string;
  reason?: string;
  step_id?: string | null;
  field?: string | null;
  message?: string;
}

function toIssue(raw: RawIssue, fallbackMessage: string): BotScenarioIssue {
  const code = raw.code ?? raw.reason ?? "bot_scenario_invalid";
  return {
    level: raw.level === "warning" ? "warning" : "error",
    code,
    step_id: raw.step_id ?? null,
    field: raw.field ?? null,
    message: raw.message ?? fallbackMessage,
  };
}

/**
 * Разбор `422 bot_scenario_invalid`. Сервер — истина (02 §5.2), и он может
 * прислать как список `details.issues[]` (02 §5.2), так и одну проблему
 * плоскими полями `details.step_id/reason` (пример из 01 §8.3). Понимаем оба;
 * что угодно другое сводим к одной ошибке «на сценарий целиком».
 */
export function parseScenarioIssues(error: unknown): BotScenarioIssue[] {
  if (!(error instanceof ApiError)) return [];
  if (error.code !== "bot_scenario_invalid" && error.status !== 422) return [];
  const details = error.details ?? {};
  const list = (details as { issues?: unknown }).issues;
  if (Array.isArray(list) && list.length > 0) {
    return list.map((raw) => toIssue((raw ?? {}) as RawIssue, error.message));
  }
  return [toIssue(details as RawIssue, error.message)];
}
