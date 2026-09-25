import type { NotificationAction, NotifyRequest, ToastKind } from "../bridge";
import { наЭкранеЛи } from "../наЭкранеЛи";
import { invoke, listenSafe, type Unlisten } from "./ipc";

/**
 * Тосты Windows (04 §4). Решение «показывать ли» принимает фронт — только он
 * знает, какой диалог открыт и в фокусе ли окно; рисует тост Rust
 * (`notify_reply` → WinRT, 04 §4.2), потому что кнопка «Ответить» с инлайн-полем
 * плагину недоступна.
 */

/**
 * Константы троттлинга (04 §4.3) — ЗЕРКАЛО значений в Rust (`notify.rs`).
 * Рабочий троттлинг живёт там: тосты просит и Rust-активатор (быстрый ответ),
 * поэтому единственный счётчик должен быть на его стороне. `ToastThrottle`
 * ниже — эталонная модель для тестов и веб-моста, в IPC-пути не участвует.
 */
export const THROTTLE = {
  perDialogCooldownMs: 30_000,
  globalWindowMs: 10_000,
  globalMax: 3,
  summaryEveryMs: 60_000,
  quietResumeMs: 30_000,
};

/** Тело тоста режем — Action Center всё равно обрежет (04 §8.1). */
export const TOAST_BODY_LIMIT = 120;

export interface ToastEnv {
  /** Окно видимо И в фокусе: тогда тоста нет — только звук и бейдж (04 §4.1). */
  focused: boolean;
  activeConversationId: string | null;
}

/**
 * Матрица 04 §4.1 — СПРАВОЧНАЯ реализация. Источник истины — Rust
 * (`notify::decide`), который видит те же поля; здесь она нужна только как
 * дешёвый локальный отсев «окно в фокусе → обычному сообщению тост не нужен»
 * (тот же критерий, что и `foreground` у ядра) и как контракт для тестов.
 * handoff и needs_reauth — приоритетные, показываются даже в фокусе.
 */
export function shouldToast(n: NotifyRequest, env: ToastEnv): boolean {
  // handoff, needs_reauth и системные события центра — приоритетные:
  // их видно и в фокусе (04 §4.1, 14 §4).
  if (n.kind === "handoff" || n.kind === "account" || n.kind === "system") return true;
  return !env.focused;
}

export type ToastDecision = "toast" | "summary" | "skip";

/** Троттлинг потока (04 §4.3): 3 тоста за 10 с → сводный режим, выход через 30 с тишины. */
export class ToastThrottle {
  private perDialog = new Map<string, number>();
  private recent: number[] = [];
  private summaryMode = false;
  private lastSummaryAt = 0;
  private lastEventAt = 0;
  private pendingMessages = 0;
  private pendingDialogs = new Set<string>();

  decide(conversationId: string, now: number = Date.now()): ToastDecision {
    if (this.summaryMode && now - this.lastEventAt > THROTTLE.quietResumeMs) {
      this.summaryMode = false;
      this.resetSummary();
    }
    this.lastEventAt = now;
    this.recent = this.recent.filter((t) => now - t < THROTTLE.globalWindowMs);

    if (this.summaryMode) {
      this.pendingMessages += 1;
      this.pendingDialogs.add(conversationId);
      if (now - this.lastSummaryAt < THROTTLE.summaryEveryMs) return "skip";
      this.lastSummaryAt = now;
      return "summary";
    }

    const last = this.perDialog.get(conversationId);
    if (last !== undefined && now - last < THROTTLE.perDialogCooldownMs) return "skip";

    if (this.recent.length >= THROTTLE.globalMax) {
      this.summaryMode = true;
      this.pendingMessages += 1;
      this.pendingDialogs.add(conversationId);
      this.lastSummaryAt = now;
      return "summary";
    }

    this.recent.push(now);
    this.perDialog.set(conversationId, now);
    return "toast";
  }

  /** Звук — только у первого тоста серии (04 §4.3). */
  silentNow(): boolean {
    return this.recent.length > 1;
  }

  summaryText(): string {
    const m = this.pendingMessages;
    const d = this.pendingDialogs.size;
    return `${m} ${plural(m, "новое сообщение", "новых сообщения", "новых сообщений")} в ${d} ${plural(
      d,
      "диалоге",
      "диалогах",
      "диалогах",
    )}`;
  }

  resetSummary(): void {
    this.pendingMessages = 0;
    this.pendingDialogs.clear();
  }
}

function plural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/**
 * Payload команды `notify_show` (04 §8.1). Rust принимает поля и в camelCase,
 * и в snake_case; шлём camelCase — Tauri так и передаёт аргументы.
 *
 * ВАЖНО: показывает тост именно `notify_show`. `notify_reply` — это ДРУГАЯ
 * команда (быстрый ответ из тоста, `{conversation_id, text}` → client_msg_id);
 * вызов её с этим payload'ом валится на разборе аргументов и тост не выходит.
 */
interface ToastRequest {
  conversationId: string;
  title: string;
  body: string;
  attribution: string;
  kind: RustNotifyKind;
  silent: boolean;
  direction?: string;
  senderType?: string;
  isForYou?: boolean;
  canReply?: boolean;
  /** Подсказка о фокусе окна: WebView2 знает про visibility точнее, чем ядро. */
  foreground: boolean;
}

/** Что вернула команда: `shown` нужен, чтобы не играть свой звук поверх системного (10 §5.4 п.4). */
export interface ToastOutcome {
  shown: boolean;
  mode: string;
  reason: string;
  silent: boolean;
}

/** `NotifyKind` в Rust: message | assigned | summary | system (04 §4.1). */
type RustNotifyKind = "message" | "assigned" | "summary" | "system";

/** Наши «продуктовые» типы → варианты enum'а Rust; чужое значение он не разберёт. */
export function toRustKind(kind: ToastKind | undefined): RustNotifyKind {
  switch (kind) {
    case "handoff":
      return "assigned";
    case "account":
    case "system":
      return "system";
    default:
      return "message";
  }
}

function currentEnv(activeConversationId: string | null): ToastEnv {
  /*
   * Предикат переехал в `platform/наЭкранеЛи.ts` БЕЗ изменения смысла: тот же
   * `visible && hasFocus()`, то же «нет документа — считаем, что смотрит».
   * Переехал потому, что веб-мост считал по одной видимости и молчал у
   * диспетчера с CRM поверх окна; третьей копии предиката быть не должно.
   */
  return { focused: наЭкранеЛи(), activeConversationId };
}

export interface Notifier {
  notify(n: NotifyRequest, activeConversationId?: string | null): Promise<void>;
  onNotificationAction(cb: (a: NotificationAction) => void): Unlisten;
}

/** Событие deep-link/клика по тосту: Rust шлёт путь роутера (04 §4.2). */
export const NAVIGATE_EVENT = "navigate";
/** Быстрый ответ из тоста: `{ conversation_id, reply_text }` (04 §4.2). */
export const REPLY_EVENT = "notification:reply";

/** «leadchat://chats/{id}» или «/chats/{id}» → id диалога. */
export function conversationIdFromPath(path: string): string | null {
  const m = /chats\/([^/?#]+)/.exec(path);
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Статус «отошёл» из меню трея (`presence:set`, 04 §3.2): все тосты уходят
 * без звука до возврата в «здесь». Живёт в памяти процесса — это состояние
 * сессии, а не настройка пользователя.
 */
let awayMode = false;

export function setAwayMode(v: boolean): void {
  awayMode = v;
}

export function isAwayMode(): boolean {
  return awayMode;
}

export function createNotifier(): Notifier {
  return {
    async notify(n, activeConversationId = null) {
      const env = currentEnv(activeConversationId);
      // Дешёвый локальный отсев (тот же критерий, что у ядра): не дёргаем IPC,
      // когда окно в фокусе и событие не приоритетное. Матрица и троттлинг
      // 04 §4.1/§4.3 — на стороне Rust, здесь их дублировать нельзя: два
      // независимых счётчика гасили бы валидные тосты.
      if (!shouldToast(n, env)) return;

      const req: ToastRequest = {
        // Пустая строка = «диалога нет»: Rust открывает окно без перехода.
        conversationId: n.conversationId ?? "",
        title: n.title,
        body: n.body.slice(0, TOAST_BODY_LIMIT),
        attribution: n.attribution ?? "",
        kind: toRustKind(n.kind),
        silent: n.silent === true || awayMode,
        direction: n.direction,
        senderType: n.senderType,
        isForYou: n.isForYou,
        canReply: n.canReply,
        foreground: env.focused,
      };

      try {
        await invoke<ToastOutcome>("notify_show", { req });
      } catch (e) {
        console.warn("[platform] тост не показан:", e);
      }
    },

    onNotificationAction(cb) {
      const offNavigate = listenSafe<string>(NAVIGATE_EVENT, (path) => {
        const id = conversationIdFromPath(String(path ?? ""));
        if (id) cb({ conversationId: id });
      });
      const offReply = listenSafe<{ conversation_id?: string; reply_text?: string }>(REPLY_EVENT, (p) => {
        if (!p?.conversation_id) return;
        cb({ conversationId: p.conversation_id, replyText: p.reply_text ?? undefined });
      });
      return () => {
        offNavigate();
        offReply();
      };
    },
  };
}
