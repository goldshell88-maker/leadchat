import type { ConversationDto, MessageDto } from "@/shared/api/types";
import {
  EMPTY_FLUSH_REPORT,
  type CachedSnapshot,
  type FlushReport,
  type OutboxEntry,
  type OutboxKind,
  type OutgoingDraft,
  type PlatformBridge,
} from "../bridge";
import { invoke, invokeSafe, listenSafe, type Unlisten } from "./ipc";

/**
 * Офлайн (04 §5): очередь `outbox` — единственные локально-авторитетные данные,
 * кэш диалогов — проекция сервера только для чтения (сервер всегда прав).
 * Открытых текстов в JS нет: шифрование/дешифрование живёт в Rust, сюда
 * приходят уже расшифрованные представления по запросу.
 */

/** Строка `outbox` как её отдаёт Rust (snake_case, 04 §5.2). */
interface OutboxRow {
  client_msg_id: string;
  dialog_id: string;
  kind: string;
  body: string;
  status: string;
  attempts: number;
  queued_at: string;
  last_error?: string | null;
}

/** `FlushReport` из 04 §8.1. */
interface FlushReportRaw {
  sent?: number;
  remaining?: number;
  failed?: Array<{ client_msg_id: string; dialog_id: string; error: string }>;
}

/**
 * Расшифрованный диалог из `cache_load_dialogs` — форма РОВНО ТАКАЯ, КАК В RUST
 * (`cache::store::DialogView`), а не «совпадает с ConversationDto».
 *
 * ⚠ ЗДЕСЬ СТОЯЛО `Partial<ConversationDto>`, И ЭТО ВРАЛО (28.08). Rust отдаёт
 * плоские колонки — `account_id`, `account_title`, `assignee_name`, `unread`,
 * `client_name`, `item_title`, — а разбор ниже читал `d.account`, `d.client`,
 * `d.assignee`, `d.item`, `d.unread_count`. Ни одного из этих имён в ответе нет,
 * поэтому срабатывали ВСЕ умолчания: список рисовался строками «Клиент» без
 * канала, без объявления, без превью и с нулём непрочитанных. Компилятор молчал
 * — у `Partial` все поля необязательные, и расхождение всей формы прошло мимо
 * него. Описываем настоящие имена: следующее расхождение упрётся в типы.
 */
type DialogView = {
  id: string;
  status?: ConversationDto["status"];
  account_id?: string | null;
  account_title?: string | null;
  assignee_id?: string | null;
  assignee_name?: string | null;
  unread?: number | null;
  last_message_at?: string | null;
  updated_at?: string | null;
  client_name?: string | null;
  client_phone?: string | null;
  item_title?: string | null;
  /** Полный элемент `GET /conversations`, сохранённый вместе со строкой. */
  card?: Partial<ConversationDto> | null;
};

type DialogsPayload = DialogView[] | { dialogs?: DialogView[]; saved_at?: string | null };

function toKind(raw: string): OutboxKind {
  return raw === "note" ? "note" : "message";
}

function toEntry(r: OutboxRow): OutboxEntry {
  return {
    clientMessageId: r.client_msg_id,
    conversationId: r.dialog_id,
    kind: toKind(r.kind),
    text: r.body,
    status: r.status === "failed" ? "failed" : r.status === "sending" ? "sending" : "queued",
    attempts: r.attempts ?? 0,
    queuedAt: r.queued_at,
    lastError: r.last_error ?? null,
  };
}

export function toFlushReport(raw: FlushReportRaw | null | undefined): FlushReport {
  if (!raw) return EMPTY_FLUSH_REPORT;
  return {
    sent: raw.sent ?? 0,
    remaining: raw.remaining ?? 0,
    failed: (raw.failed ?? []).map((f) => ({
      clientMessageId: f.client_msg_id,
      conversationId: f.dialog_id,
      error: f.error,
    })),
  };
}

/**
 * Строка кэша → элемент списка.
 *
 * ⚠ СНАЧАЛА `card` — ЭТО ГОТОВЫЙ СЕРВЕРНЫЙ ОТВЕТ. Rust сохраняет его рядом со
 * строкой ровно для этого: «полный JSON элемента списка — той же формы, что
 * отдаёт GET /conversations, чтобы UI рисовал офлайн-снапшот своим обычным
 * рендерером» (`cache::store::DialogView`). Его никто не читал, а DTO собирался
 * из полей, которых в ответе нет вовсе.
 *
 * Плоские колонки остаются запасным путём: строки, записанные старой версией
 * приложения, `card` не несут, и без него офлайн-старт у них пропал бы совсем.
 * Он же и объясняет, почему поля берутся так дословно — это те самые имена,
 * которые отдаёт Rust.
 */
export function toConversationDto(d: DialogView): ConversationDto {
  const карточка = d.card && typeof d.card === "object" ? d.card : null;
  const запасной: ConversationDto = {
    id: d.id,
    status: d.status ?? "new",
    channel: "avito",
    account: { id: d.account_id ?? "", title: d.account_title ?? "" },
    client: { id: "", name: d.client_name ?? "Клиент", phone: d.client_phone ?? null, avito_rating: null },
    assignee: d.assignee_id ? { id: d.assignee_id, full_name: d.assignee_name ?? "" } : null,
    item: d.item_title ? { title: d.item_title, url: null, price: null } : null,
    last_message: null,
    unread_count: d.unread ?? 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: d.last_message_at ?? null,
  };
  if (!карточка) return запасной;
  // Карточка правее: она и есть ответ сервера. Умолчания под ней — на случай,
  // если её записала версия, не знавшая какого-то поля.
  return { ...запасной, ...карточка, id: d.id };
}

/** Событие отчёта флаша (04 §8.2). */
export const OUTBOX_REPORT_EVENT = "outbox:report";

export function createOfflineQueue(): PlatformBridge["offlineQueue"] {
  return {
    async push(item: OutgoingDraft) {
      // Rust генерирует client_msg_id, если фронт не передал свой; передаём
      // всегда — оптимистичный ⏳-пузырь уже нарисован с этим id (03 §7).
      const id = await invoke<string>("outbox_push", {
        dialogId: item.conversationId,
        kind: item.kind,
        body: item.text,
        clientMsgId: item.clientMessageId ?? null,
      });
      return id || (item.clientMessageId ?? "");
    },

    async drain() {
      // HTTP делает Rust (04 §8.2) — callback веб-контракта здесь не нужен.
      const raw = await invokeSafe<FlushReportRaw | null>("outbox_flush", {}, null);
      return toFlushReport(raw);
    },

    async size() {
      const rows = await invokeSafe<OutboxRow[]>("outbox_list", {}, []);
      return rows.length;
    },

    async list() {
      const rows = await invokeSafe<OutboxRow[]>("outbox_list", {}, []);
      return rows.map(toEntry);
    },

    async retry(clientMessageId) {
      await invoke("outbox_retry", { clientMsgId: clientMessageId });
    },

    async remove(clientMessageId) {
      await invoke("outbox_delete", { clientMsgId: clientMessageId });
    },

    onReport(cb) {
      return listenSafe<FlushReportRaw>(OUTBOX_REPORT_EVENT, (raw) => cb(toFlushReport(raw)));
    },
  };
}

export function createConvCache(): PlatformBridge["convCache"] {
  return {
    async warmup(): Promise<CachedSnapshot | null> {
      const payload = await invokeSafe<DialogsPayload | null>("cache_load_dialogs", {}, null);
      if (!payload) return null;
      const rows = Array.isArray(payload) ? payload : (payload.dialogs ?? []);
      if (rows.length === 0) return null;
      return {
        dialogs: rows.filter((d) => d && d.id).map(toConversationDto),
        savedAt: Array.isArray(payload) ? null : (payload.saved_at ?? null),
      };
    },

    async persist(s: CachedSnapshot) {
      // Сервер прав: upsert перезаписывает локальные строки, prune 200/100 — в Rust (04 §5.4).
      await invokeSafe("cache_apply_sync", { dialogs: s.dialogs, messages: [] }, null);
    },

    async loadMessages(conversationId: string, limit = 100) {
      return invokeSafe<MessageDto[]>("cache_load_messages", { dialogId: conversationId, limit }, []);
    },

    async clear() {
      // Rust чистит диалоги, ленты, очередь и токен одним куском (`cache_clear`).
      await invokeSafe("cache_clear", {}, null);
    },
  };
}

/**
 * Триггеры флаша (04 §5.3 п.2): реконнект WS/возврат сети (`online`) и таймер
 * раз в 30 с, пока в очереди что-то есть. Флаш сразу после `push` делает
 * вызывающий код отправки.
 */
export function startOutboxSchedule(drain: () => Promise<unknown>): Unlisten {
  const tick = () => void drain();
  const timer = setInterval(tick, 30_000);
  window.addEventListener("online", tick);
  return () => {
    clearInterval(timer);
    window.removeEventListener("online", tick);
  };
}
