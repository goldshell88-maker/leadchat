import { http } from "@/shared/api/http";
import type { ConversationStatus } from "@/shared/lib/conversationStatus";

/** Чем кончился диалог у бота (docs/45 §4.2). */
export interface BotOutcome {
  /**
   * Вид сбоя из `handoff.OUTCOME_GROUPS`, либо `null`.
   *
   * `null` — это НЕ «неизвестно». Это «сбоем не является»: бот передал диалог
   * по плану, либо ещё ведёт его, либо записи о передаче нет вовсе. Подпись
   * при этом всё равно осмысленная — см. `label`.
   */
  group: string | null;
  /** Причина словами: «сработала защита от зацикливания», «ещё ведёт». */
  label: string;
}

export interface BotDialogRow {
  id: string;
  status: ConversationStatus;
  client_name: string | null;
  account_title: string | null;
  assignee_name: string | null;
  messages_count: number;
  last_message_at: string | null;
  first_response_sec: number | null;
  outcome: BotOutcome;
}

export interface BotCounter {
  group: string;
  label: string;
  count: number;
  /**
   * `failure` — сбой: зацикливание, отказ, зависание.
   * `capacity` — «не смог сам»: бот сработал правильно, просто не потянул.
   *
   * ⚠ Экран ОБЯЗАН их различать. «AI не уверен» — 210 передач из 325 на боевом
   * трафике; поставь его в ряд со сбоями, и экран скажет «210 сбоев» про самое
   * здоровое поведение бота, а единственный настоящий отказ по регламенту
   * потеряется на этом фоне совсем.
   */
  kind: "failure" | "capacity";
}

/** Строка живой полосы: кто у бота в руках прямо сейчас. */
export interface BotLiveRow {
  id: string;
  client_name: string | null;
  account_title: string | null;
  /** С какого момента диалог в текущем состоянии — из него считаем «сколько идёт». */
  since: string | null;
  last_message_at: string | null;
  bot_replies: number;
  last_bot_text: string | null;
}

export interface BotLive {
  count: number;
  items: BotLiveRow[];
}

export function fetchBotDialogsLive(): Promise<BotLive> {
  return http.get<BotLive>("/bot-dialogs/live");
}

export interface BotDialogsPage {
  items: BotDialogRow[];
  page: { limit: number; offset: number; total: number };
  counters: BotCounter[];
}

export interface BotDialogsQuery {
  accountId?: string;
  /** Выбранная плитка. Пусто — показываем все диалоги бота. */
  outcome?: string;
  dateFrom?: string;
  dateTo?: string;
  offset: number;
}

export function fetchBotDialogs(qy: BotDialogsQuery): Promise<BotDialogsPage> {
  const p = new URLSearchParams();
  if (qy.accountId) p.set("account_id", qy.accountId);
  if (qy.outcome) p.set("outcome", qy.outcome);
  if (qy.dateFrom) p.set("date_from", qy.dateFrom);
  if (qy.dateTo) p.set("date_to", qy.dateTo);
  p.set("offset", String(qy.offset));
  return http.get<BotDialogsPage>(`/bot-dialogs?${p}`);
}
