import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { http } from "@/shared/api/http";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";

/**
 * Раздел «Лид-бот» — связь, настройки, проверка, журнал работы.
 *
 * ⚠ ТОКЕНА В ЭТИХ ТИПАХ НЕТ И НЕ ДОЛЖНО ПОЯВИТЬСЯ. Сервер отдаёт только
 * `token_set` — «задан или нет». Токен открывает чужому сервису всю переписку
 * клиентов; приехав во вкладку, он ложится в историю запросов, в кэш и в чужие
 * снимки экрана. Наружу он уходит только в одну сторону — при сохранении.
 */

export interface LeadbotConnection {
  url: string;
  token_set: boolean;
  /** `db` — задано из интерфейса, `env` — тем, что вписал инженер при развёртывании. */
  source: string;
}

export interface LeadbotAccountOption {
  id: string;
  title: string;
  /** Источник («В95») — подпись собирает `shared/lib/channelLabel`. */
  lead_origin?: string | null;
  /** Канал уже обслуживает ДРУГОЙ бот — подключение отберёт его, и это видно до нажатия. */
  busy_with_other_bot: boolean;
}

export interface LeadbotOverview {
  connection: LeadbotConnection;
  is_ready: boolean;
  enabled: boolean;
  /** `suggest` — ответ ложится подсказкой оператору, `auto` — уходит клиенту. */
  mode: "suggest" | "auto";
  context_messages: number;
  account_ids: string[];
  accounts: LeadbotAccountOption[];
}

export interface LeadbotProbe {
  ok: boolean;
  error: string | null;
  ms: number;
  status?: number | null;
  reply: string | null;
  layer: string | null;
}

export interface LeadbotEscalation {
  reason: string | null;
  label: string | null;
  deadline_min: number | null;
}

export interface LeadbotTestResult {
  ok: boolean;
  error: string | null;
  ms: number;
  status?: number | null;
  reply?: string | null;
  confidence?: number | null;
  needs_operator?: boolean | null;
  layer?: string | null;
  flag?: string | null;
  escalation?: LeadbotEscalation | null;
  lead_ready?: boolean;
  warnings?: string[];
}

export interface LeadbotCall {
  id: string;
  at: string;
  conversation_id: string | null;
  account_id: string | null;
  request_id: string | null;
  question: string | null;
  reply: string | null;
  layer: string | null;
  flag: string | null;
  confidence: number | null;
  needs_operator: boolean | null;
  outcome: string;
  /** Подпись исхода приходит с сервера — один словарь на сервер и на экран. */
  outcome_label: string;
  escalation: LeadbotEscalation | null;
  lead_ready: boolean;
  warnings: string[];
  ms: number | null;
  error: string | null;
}

const KEY = ["leadbot"] as const;
const CALLS_KEY = ["leadbot", "calls"] as const;

export function useLeadbot() {
  return useQuery({
    queryKey: KEY,
    queryFn: () => http.get<LeadbotOverview>("/leadbot"),
  });
}

export function useLeadbotCalls(params: { onlyTrouble: boolean; accountId: string | null }) {
  return useQuery({
    queryKey: [...CALLS_KEY, params.onlyTrouble, params.accountId],
    queryFn: () => {
      const query = new URLSearchParams({ limit: "100" });
      if (params.onlyTrouble) query.set("only_trouble", "true");
      if (params.accountId) query.set("account_id", params.accountId);
      return http.get<{ items: LeadbotCall[] }>(`/leadbot/calls?${query.toString()}`);
    },
    // Журнал смотрят, когда что-то случилось, и обновление раз в полминуты
    // избавляет от привычки жать F5. Живой ленты здесь не нужно: обращения к
    // лид-боту идут не чаще, чем пишут клиенты.
    refetchInterval: 30_000,
  });
}

export function useSaveLeadbot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      enabled?: boolean;
      mode?: "suggest" | "auto";
      context_messages?: number;
      account_ids?: string[];
    }) => http.patch<LeadbotOverview>("/leadbot", body),
    onSuccess: (data) => qc.setQueryData(KEY, data),
  });
}

export function useSaveConnection() {
  const qc = useQueryClient();
  return useMutation({
    /**
     * `token` отсутствует в теле — «оставить прежний».
     *
     * Экран не знает нынешнего значения и показать его не может. Значит пустое
     * поле обязано означать «не трогали»: трактуй его как «убрать», и любое
     * исправление опечатки в адресе молча разорвало бы связь.
     */
    mutationFn: (body: { url: string; token?: string }) =>
      http.put<LeadbotOverview>("/leadbot/connection", body),
    onSuccess: (data) => qc.setQueryData(KEY, data),
  });
}

/*
 * ОТКАЗ НАШЕЙ РУЧКИ — ТОСТОМ (проверка 24.09). Отказ самого лид-бота приходит
 * 200 с `ok: false` и показывается на экране, а HTTP-отказ (выкатка, 429,
 * истёкшая сессия) молчал: крутилка гасла, и человек считал, что ничего не
 * случилось. Обработчик — в хуке, чтобы его не забыл ни один вызов.
 */

export function useResetConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => http.post<LeadbotOverview>("/leadbot/connection/reset", {}),
    onSuccess: (data) => qc.setQueryData(KEY, data),
    onError: (error) =>
      showToast(describeError({ where: "Возврат настроек лид-бота как при установке", error })),
  });
}

export function useProbe() {
  return useMutation({
    mutationFn: () => http.post<LeadbotProbe>("/leadbot/probe", {}),
    onError: (error) => showToast(describeError({ where: "Проверка связи с лид-ботом", error })),
  });
}

export function useTestChat() {
  return useMutation({
    mutationFn: (body: { dialog: Array<{ role: string; content: string }>; item_title?: string }) =>
      http.post<LeadbotTestResult>("/leadbot/test", body),
    onError: (error) => showToast(describeError({ where: "Вопрос лид-боту", error })),
  });
}

/**
 * Почему бот молчит (жалоба владельца 19.08: «включил — он ничего не взял»).
 *
 * Бот входит только в СВЕЖИЙ диалог и только туда, где человек ещё не
 * отвечал. Когда новых диалогов нет, он честно молчит — но узнать это было
 * неоткуда: журнал обращений пуст, а пустота читается как поломка. Причину
 * считает тот же код, который решает пускать бота.
 */
export interface LeadbotSilence {
  channels: { id: string; title: string; bot_attached: boolean; status: string }[];
  in_progress: { conversation_id: string; status: string; last_message_at: string | null }[];
  not_taken: {
    conversation_id: string;
    status: string;
    last_message_at: string | null;
    reason: string | null;
    reason_label: string;
  }[];
}

export function useLeadbotSilence() {
  return useQuery({
    queryKey: ["leadbot", "silence"],
    queryFn: () => http.get<LeadbotSilence>("/leadbot/silence?limit=20"),
    refetchInterval: 30_000,
  });
}
