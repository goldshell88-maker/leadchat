import type { ConversationTab } from "@/shared/api/queryKeys";
import type { ConversationDto, MessageDto, TabCounts } from "@/shared/api/types";
import type { Role } from "@/shared/auth/usePermissions";

/**
 * Единственное «платформо-осознанное» место фронта (03 §7, 04 §1.1).
 * Фичи зовут ТОЛЬКО этот интерфейс; ни один компонент не знает слова «Tauri».
 *
 * Подключение реализации — строго динамическим импортом: в веб-бандле чанк
 * `platform/tauri/*` никогда не запрашивается, а сами пакеты `@tauri-apps/*`
 * не импортируются вовсе (мост говорит с ядром через инжектируемый
 * `window.__TAURI_INTERNALS__`, см. `tauri/ipc.ts`).
 */

/** Tauri 2 инжектит `__TAURI_INTERNALS__` в WebView (03 §7). */
export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Сборка `vite --mode desktop` (04 §1.1): VITE_IS_DESKTOP=1 + абсолютный VITE_API_BASE. */
export function isDesktopBuild(): boolean {
  return (import.meta.env.VITE_IS_DESKTOP as string | undefined) === "1";
}

export type OutboxKind = "message" | "note";
export type OutboxStatus = "queued" | "sending" | "failed";

/** Исходящее, поставленное в очередь (04 §5.3). */
export interface OutgoingDraft {
  conversationId: string;
  kind: OutboxKind;
  text: string;
  /**
   * Ключ идемпотентности (01 §1.6). Фронт генерирует его ДО оптимистичной
   * строки и передаёт в `outbox_push`, чтобы ⏳-пузырь и строка очереди имели
   * один и тот же id; «Повторить» переиспользует тот же — дубля не будет.
   */
  clientMessageId?: string;
}

/** Строка `outbox` глазами UI (04 §5.2). */
export interface OutboxEntry {
  clientMessageId: string;
  conversationId: string;
  kind: OutboxKind;
  text: string;
  status: OutboxStatus;
  attempts: number;
  queuedAt: string;
  lastError?: string | null;
}

export interface OutboxFailure {
  clientMessageId: string;
  conversationId: string;
  error: string;
}

/** `FlushReport` из 04 §8.1 в JS-виде. */
export interface FlushReport {
  sent: number;
  failed: OutboxFailure[];
  remaining: number;
}

export const EMPTY_FLUSH_REPORT: FlushReport = { sent: 0, failed: [], remaining: 0 };

/** Снапшот кэша для мгновенного старта (04 §5.4 шаг 1). */
export interface CachedSnapshot {
  dialogs: ConversationDto[];
  /** ISO-8601 `meta.last_sync_at`; null — кэш ни разу не синхронизировался. */
  savedAt: string | null;
  /**
   * Поля ниже несёт ВЕБ-снимок (IndexedDB, `platform/снимокСписка.ts`); Rust о
   * них не знает и в десктопе они не приходят — потому и необязательные.
   * Проверки на стороне `warmup.ts` написаны «есть поле — проверяем», чтобы
   * десктопная ветка вела себя ровно как прежде.
   */
  /** Кому принадлежит снимок: чужой не показываем (компьютер сменный). */
  ownerId?: string | null;
  /** Роль владельца — из неё вкладка по умолчанию (`defaultTabForRole`). */
  role?: Role | null;
  /**
   * Вкладка, открытая в момент записи. Хранится, чтобы запись описывала себя
   * сама: срез на экране НЕ берётся отсюда — его пересчитывает `matchesTab`
   * под текущие фильтры, потому что фильтры перезагрузку не переживают.
   */
  tab?: ConversationTab | null;
  /** Числа над вкладкой «Мои» на первый кадр. */
  counts?: TabCounts | null;
}

/** Клик по тосту / быстрый ответ из тоста (04 §4.2). */
export interface NotificationAction {
  conversationId: string;
  replyText?: string;
  /**
   * Что нажали: `undefined` — само тело уведомления, иначе `action` кнопки
   * («принять», «отклонить»). Кнопки бывают только у уведомлений из
   * сервис-воркера, поэтому поле необязательное; переход в диалог одинаков для
   * любого нажатия, а отдельное действие разбирает тот, кто эти кнопки завёл.
   */
  action?: string;
}

/**
 * Продуктовые типы тоста. `account` и `system` оба ложатся на `NotifyKind::System`
 * в Rust, но различать их на фронте полезно: у первого есть свой экран
 * (/settings/accounts), у второго — только центр уведомлений (14 §4).
 *
 * `inbox` — диалог встал в очередь. До 07.09 такого вида не было вовсе: на
 * кадр `inbox:new` звенел только звук, а уведомления не получал никто (замер
 * 05.09: 1357 новых диалогов и 625 возвратов в сутки).
 */
export type ToastKind = "message" | "handoff" | "account" | "system" | "inbox";

export interface NotifyRequest {
  title: string;
  body: string;
  /**
   * Диалог, к которому ведёт клик по тосту. У системного уведомления центра
   * (14 §2.1–2.2) диалога нет — Rust в этом случае открывает просто окно
   * (`leadchat://chats`), поэтому поле необязательное.
   */
  conversationId?: string;
  /** «LP-Москва» — аккаунт-получатель, строка attribution тоста (04 §8.1). */
  attribution?: string;
  /** handoff показывается всегда, тосты бота не показываются вовсе (04 §4.1). */
  kind?: ToastKind;
  /** Статус «отошёл» — тост без звука (04 §4.1). */
  silent?: boolean;
  /**
   * Поля матрицы 04 §4.1: решение «показывать ли» принимает Rust, но без них
   * фильтр «только от клиента» у него не отработает (нужны `direction` и
   * `senderType`), а у head/observer в тосте останется кнопка «Ответить».
   */
  direction?: "in" | "out" | "note" | "system";
  senderType?: "client" | "operator" | "bot" | "system";
  /** Для `kind: "handoff"` — назначили именно текущему пользователю. */
  isForYou?: boolean;
  /** false у head/observer — тост без «Ответить» (11 §7.2). */
  canReply?: boolean;
  /**
   * Ключ склейки: браузер ЗАМЕНЯЕТ показанное уведомление с тем же тегом, а не
   * кладёт рядом. Значение по умолчанию считает `дополнитьСклейкой` — руками
   * его передают только там, где вид события шире `kind`.
   */
  tag?: string;
  /** Не гасить само: уведомление ждёт человека (передача диалога). */
  requireInteraction?: boolean;
  /**
   * Кнопки уведомления. ⚠ Работают ТОЛЬКО через сервис-воркер
   * (`registration.showNotification`); у `new Notification` их нет, и он это
   * поле молча игнорирует.
   */
  actions?: Array<{ action: string; title: string }>;
}

/**
 * Склейка — ОДНО правило на веб-мост и сервис-воркер. Разводить их нельзя:
 * тег решает, заменит ли уведомление предыдущее, и два пути, считающие один
 * ключ по-разному, дали бы «десять уведомлений от одного клиента» ровно в том
 * браузере, где сработал не тот путь.
 *
 * Теги: сообщение — `lc-msg-<диалог>`, очередь — `lc-inbox` ОДИН на всю
 * очередь (в ней важно «есть новые», а не сколько их), передача —
 * `lc-handoff-<диалог>`.
 *
 * Системные записи центра тега не получают намеренно: их мало, каждая про
 * своё, и затирать одну другой значит потерять сообщение о поломке.
 */
export function дополнитьСклейкой(n: NotifyRequest): NotifyRequest {
  return {
    ...n,
    tag: n.tag ?? тегПоВиду(n),
    // Передача ждёт решения человека и сама гаснуть не должна: за 30 дней их
    // 119 — редко и дорого, пропущенная стоит диалога.
    requireInteraction: n.requireInteraction ?? n.kind === "handoff",
  };
}

function тегПоВиду(n: NotifyRequest): string | undefined {
  switch (n.kind) {
    case "account":
    case "system":
      return undefined;
    case "inbox":
      return "lc-inbox";
    case "handoff":
      return n.conversationId ? `lc-handoff-${n.conversationId}` : undefined;
    default:
      // `message` и не указанный вид — он же `message` (см. `toRustKind`).
      return n.conversationId ? `lc-msg-${n.conversationId}` : undefined;
  }
}

/** Веб-мост офлайна не эмулирует: Composer честно говорит «нет соединения» (03 §7). */
export class OfflineUnsupported extends Error {
  constructor() {
    super("Офлайн-очередь доступна только в приложении для Windows");
    this.name = "OfflineUnsupported";
  }
}

/** Версия и канал для блока «О приложении» (04 §6.3). */
export interface AppInfo {
  version: string;
  channel: "stable";
}

export interface UpdateInfo {
  version: string;
  notes?: string | null;
  pubDate?: string | null;
}

/**
 * Десктоп-только возможности: автообновление и автозапуск (04 §6).
 * В интерфейсе 03 §7 их нет — поэтому поле необязательное, веб его не имеет,
 * а UI («О приложении», баннер) проверяет наличие перед показом.
 */
export interface DesktopApi {
  appInfo(): Promise<AppInfo>;
  /** Ручная проверка кнопкой «Проверить обновления»; null — обновлений нет, сбой — исключение. */
  checkForUpdates(): Promise<UpdateInfo | null>;
  /** Скачать + поставить + перезапустить (клик по «Обновить» в баннере). */
  installUpdate(): Promise<void>;
  autostart: {
    isEnabled(): Promise<boolean>;
    set(enabled: boolean): Promise<void>;
  };
  /** Передать Rust текущий access-JWT для `outbox_flush` (04 §8.1). */
  setSessionToken(token: string | null): Promise<void>;
  /**
   * База API для Rust-стороны (`set_api_base`). У ядра свой дефолт —
   * прод-домен; если бандл собран под другой стенд (VITE_API_BASE), без этого
   * вызова JS и Rust ходят на РАЗНЫЕ хосты, а очередь уезжает не туда.
   */
  setApiBase(base: string): Promise<void>;
}

export interface PlatformBridge {
  readonly kind: "web" | "tauri";
  /** Нативный тост (Windows Action Center) или Notification API / no-op в вебе. */
  notify(n: NotifyRequest): Promise<void>;
  /** Бейдж непрочитанных: иконка трея в Tauri; в вебе — no-op (title меняется отдельно). */
  setBadge(count: number): Promise<void>;
  /** Клик по уведомлению / «Ответить» из тоста; возвращает отписку. */
  onNotificationAction(cb: (a: NotificationAction) => void): () => void;
  /** Офлайн-очередь исходящих (04 §5.3); в вебе — заглушка «офлайна нет». */
  offlineQueue: {
    /** Возвращает `client_msg_id` — он же `client_message_id` (03 §7). */
    push(item: OutgoingDraft): Promise<string>;
    /** Прогнать очередь (`outbox_flush`). Аргумент `send` веб-контракта не нужен: HTTP делает Rust. */
    drain(send?: (item: OutgoingDraft) => Promise<void>): Promise<FlushReport>;
    size(): Promise<number>;
    list(): Promise<OutboxEntry[]>;
    /** «Повторить»: сбросить attempts и вернуть в queued. */
    retry(clientMessageId: string): Promise<void>;
    /** «Удалить»: убрать строку из очереди насовсем. */
    remove(clientMessageId: string): Promise<void>;
    /** Отчёты Rust-флаша (событие `outbox:report`, 04 §8.2). */
    onReport(cb: (r: FlushReport) => void): () => void;
  };
  /** Локальный кэш последних 200 диалогов для мгновенного старта (SQLite). */
  convCache: {
    warmup(): Promise<CachedSnapshot | null>;
    persist(s: CachedSnapshot): Promise<void>;
    loadMessages(conversationId: string, limit?: number): Promise<MessageDto[]>;
    /**
     * Стереть всё локальное: диалоги, ленты и ОЧЕРЕДЬ ОТПРАВКИ. Зовётся при
     * выходе из аккаунта и при принудительном отзыве сессии.
     *
     * ⚠ БЕЗ ЭТОГО ЧУЖОЕ СООБЩЕНИЕ УХОДИТ ПОД ЧУЖИМ ИМЕНЕМ. Команда `cache_clear`
     * в Rust была написана, зарегистрирована и снабжена комментарием «очередь
     * принадлежит сессии, и оставлять чужие неотправленные сообщения следующему
     * пользователю на этой машине нельзя» — и не вызывалась НИОТКУДА. Выход
     * чистил только токен в памяти, база SQLite оставалась нетронутой, а
     * фоновый флаш раз в 30 секунд берёт ТЕКУЩИЙ токен: диспетчер A написал при
     * оборванной сети и ушёл, диспетчер B вошёл на том же компьютере — и через
     * полминуты сообщение A уехало клиенту от имени B.
     */
    clear(): Promise<void>;
  };
  openExternal(url: string): Promise<void>;
  desktop?: DesktopApi;
}

let bridge: PlatformBridge | null = null;
let initPromise: Promise<PlatformBridge> | null = null;

export function getBridge(): PlatformBridge {
  if (!bridge) throw new Error("bridge not initialized");
  return bridge;
}

/** Для мест, которые обязаны работать и до initBridge (ранние WS-события). */
export function getBridgeOrNull(): PlatformBridge | null {
  return bridge;
}

/**
 * Инициализация моста. Динамический импорт — ключевое требование 03 §7:
 * веб-бандл никогда не запрашивает чанк `tauri`.
 */
export function initBridge(): Promise<PlatformBridge> {
  initPromise ??= (async () => {
    bridge = isTauri()
      ? (await import("./tauri")).createTauriBridge()
      : (await import("./web")).createWebBridge();
    return bridge;
  })();
  return initPromise;
}

/** Только для тестов: подменить/сбросить мост. */
export function __setBridge(b: PlatformBridge | null): void {
  bridge = b;
  initPromise = b ? Promise.resolve(b) : null;
}
