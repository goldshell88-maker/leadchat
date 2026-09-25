/**
 * The ONLY place query keys are declared (03 §2.1). No literal arrays in components.
 * Filters enter the key whole: switching a tab is a different key = its own cache.
 */

import type { Role } from "@/shared/auth/usePermissions";

/**
 * Вкладка = ЧЕЙ диалог. «new» и «closed» остаются в типе, потому что сервер
 * их по-прежнему принимает (ими пользуется десктопная дельта-синхронизация и
 * старые сохранённые фильтры), но в ряду кнопок их больше нет: состояние
 * диалога переехало в отдельный фильтр `status`. Два признака в одном
 * переключателе читались как дубли — «Новые» и «Закрытые» суть срезы внутри
 * «Всех».
 */
export type ConversationTab = "mine" | "all" | "new" | "closed";

/**
 * Состояние диалога — измерение, ортогональное вкладке.
 *
 * ВТОРОЕ ОБЪЯВЛЕНИЕ ТОГО ЖЕ ТИПА ОТСЮДА УБРАНО (docs/38 §0). Оно жило здесь
 * рядом с копией в `shared/api/types.ts`, и обе надо было править вместе;
 * TypeScript про это молчит — два одинаковых литеральных объединения
 * совместимы, пока не разъедутся.
 */
import type { ConversationStatus } from "@/shared/lib/conversationStatus";

export type { ConversationStatus };

export type ConversationFilters = {
  tab: ConversationTab; // вкладки DESIGN 3.3
  status?: ConversationStatus; // «Статус ▾» — рядом с «Каналом» и «Менеджером»
  q?: string; // имя / текст / телефон (trim + debounce 300 мс до попадания в ключ)
  accountId?: string; // фильтр по аккаунту Авито
  assigneeId?: string; // фильтр «по менеджеру» (head/admin)
  /**
   * «Без ответственного» — диалоги, которые не ведёт никто (`unassigned=true`).
   *
   * ⚠ ОТДЕЛЬНЫЙ ПРИЗНАК, А НЕ СПЕЦЗНАЧЕНИЕ В `assigneeId`, И ЭТО ПОВТОРЯЕТ
   * РЕШЕНИЕ СЕРВЕРА (`services/conversations.py`, ветка `unassigned`): там
   * пояснено, что тип поля обязан остаться uuid, иначе опечатка вместо
   * честного 422 давала бы «показал всё». Здесь тип тоже строка-uuid, и
   * магическое «__none__» в ней жило бы ровно до первой правки сборки запроса.
   *
   * На экране это ОДИН контрол «Ответственный ▾» с пунктом «Без
   * ответственного»: измерение одно, и двумя контролами его выразить значит
   * пустить человека выбрать «Пётр Ковалёв» + «ничей» — срез, который всегда
   * пуст. Разъезжается это только на границе с сетью, где так и надо.
   */
  unassigned?: boolean;
  tag?: string; // фильтр по тегу (tag= в 01 §5.1), напр. «негатив»
  /**
   * Показывать И закрытые тоже (требование заказчика 13 августа: «во вкладке „Все"
   * чтобы можно было вся история целиком, а не только в „Разборе диалогов"»).
   *
   * ⚠ ВЫКЛЮЧЕНО ПО УМОЛЧАНИЮ, И ЭТО НЕ ПОЛУМЕРА. Импортированная история Авито
   * заводится закрытой, и закрытых на порядки больше открытых (у Jivo та же
   * история насчитывала сотни тысяч диалогов), — а «Все»
   * это вкладка по умолчанию у владельца и руководителя. Включи архив всегда, и
   * первый экран после входа станет архивом: закрытое сегодня встанет в верхние
   * строки вперемешку с рабочим, потому что список сортируется по последнему
   * сообщению. Заказчик просил, чтобы историю МОЖНО БЫЛО посмотреть, а не чтобы
   * она заслонила работу.
   *
   * ⚠ И ЦЕНА ЗАПРОСА. Частичный индекс `ix_conversations_open_last_message`
   * (миграция 0028) построен ровно под «кроме закрытых»; с архивом планировщик его
   * не возьмёт, а `COUNT(*)` считается на каждую страницу бесконечного списка.
   * Пока это по требованию — платит тот, кто попросил, и тогда, когда попросил.
   */
  withClosed?: boolean;
  /**
   * «Ждут ответа» — срез по клиентам, которые ждут нас (жалоба владельца 02.09).
   *
   * Парный счётчику «не отвечено» на вкладке: на число нажимают и попадают
   * сюда. Оба считаются ОДНИМ предикатом на сервере (`_waiting_condition`),
   * поэтому «не отвечено: 3» и три строки в списке — это одно и то же число, а
   * не два похожих.
   */
  waitingOnly?: boolean;
};

/**
 * Общие параметры раздела `/stats/*` (06 §4): даты по Москве включительно,
 * необязательные фильтры по аккаунту и менеджерам (manager_id повторяемый).
 */
export type StatsQuery = {
  dateFrom: string;
  dateTo: string;
  accountId?: string;
  managerIds?: string[];
};

/** Фильтры журнала аудита (01 §9.7). */
export type AuditFilters = {
  dateFrom?: string;
  dateTo?: string;
  userId?: string;
  action?: string;
  offset: number;
};

export type NotificationSeverityFilter = "all" | "critical" | "warning" | "info";

/** Фильтры журнала `/notifications` (14 §3): важность, тип, период, страница. */
export interface NotificationFilters {
  severity: NotificationSeverityFilter;
  kind?: string;
  dateFrom?: string;
  dateTo?: string;
  /** Непрочитанные — отдельный переключатель, в журнале удобно. */
  unreadOnly?: boolean;
  offset: number;
}

/** Фильтры вкладки «Сотрудники» (01 §3.1, 11 §4.2). */
export interface TeamFilters {
  q?: string;
  role?: Role;
  /** Показывать отключённых сотрудников (по умолчанию — только активные). */
  includeInactive: boolean;
  offset: number;
}

export const qk = {
  me: ["me"] as const,
  health: ["health"] as const,
  invite: (token: string) => ["invite", token] as const,

  conversations: {
    root: ["conversations"] as const,
    list: (f: ConversationFilters) => ["conversations", "list", f] as const, // infinite, offset
    detail: (id: string) => ["conversations", "detail", id] as const,
    /**
     * Числа над вкладкой «Мои»: сколько взято и сколько не отвечено.
     * Считает СЕРВЕР (`GET /conversations/counts`) — см. довод в
     * `services/conversations.py:tab_counts`.
     */
    counts: ["conversations", "counts"] as const,
  },

  messages: {
    root: ["messages"] as const,
    list: (conversationId: string) => ["messages", conversationId] as const, // infinite, cursor
  },

  clients: {
    /** Прошлые диалоги клиента: GET /conversations/{id}/client-history (01 §5.6). */
    history: (conversationId: string) => ["clients", "history", conversationId] as const,
  },

  templates: {
    root: ["templates"] as const,
    list: (scope: TemplateScope) => ["templates", scope] as const,
    folders: ["templates", "folders"] as const,
  },

  /**
   * Сотрудники (01 §3.1). `root` — общий префикс: правка сотрудника делает
   * несвежими и таблицу «Команда», и список для «→ Передать».
   */
  users: {
    root: ["users"] as const,
    /** Таблица /settings/team; фильтры входят в ключ целиком. */
    list: (f: TeamFilters) => ["users", "list", f] as const,
    /** GET /users/assignable — список для «→ Передать». */
    assignable: ["users", "assignable"] as const,
  },

  accounts: ["accounts"] as const, // /settings/accounts

  /** Статистика (06 §4). Фильтры входят в ключ целиком — свой кэш на каждый срез. */
  stats: {
    root: ["stats"] as const,
    summary: (q: StatsQuery) => ["stats", "summary", q] as const,
    timeseries: (q: StatsQuery, metric: string, group: string) =>
      ["stats", "timeseries", q, metric, group] as const,
    /** Фильтр по менеджеру на карту не действует (06 §4.3) — в ключ не входит. */
    heatmap: (q: Omit<StatsQuery, "managerIds">) => ["stats", "heatmap", q] as const,
    managers: (q: StatsQuery, sort: string, order: string) =>
      ["stats", "managers", q, sort, order] as const,
    myToday: ["stats", "my", "today"] as const,
    exportJob: (jobId: string) => ["stats", "export", jobId] as const,
  },

  /** GET /conversations/table/export/{job_id} — статус выгрузки «Разбора». */
  tableExportJob: (jobId: string) => ["table-export", jobId] as const,

  /** GET /audit-log (01 §9.7) — журнал аудита в /settings/team. */
  auditLog: (f: AuditFilters) => ["audit-log", f] as const,
  /** GET /audit-log/filters — пункты фильтров «Действие» и «Сотрудник». */
  auditFilterOptions: ["audit-log-filters"] as const,

  /** Центр уведомлений (14 §3–§4). */
  notifications: {
    root: ["notifications"] as const,
    /** Префикс всех страниц журнала независимо от фильтров. */
    listRoot: ["notifications", "list"] as const,
    list: (f: NotificationFilters) => ["notifications", "list", f] as const,
    /** Колокольчик: последние N без фильтров — свой ключ, не делит кэш с журналом. */
    recent: ["notifications", "recent"] as const,
    /**
     * Неподтверждённое критичное — своя выборка, а не страница журнала: в
     * последние десять строк оно не помещается, когда шумит что-то другое
     * (так на бою и потерялись два «Резервное копирование не выполнилось»).
     */
    unreadCritical: ["notifications", "unread-critical"] as const,
    unreadCount: ["notifications", "unread-count"] as const,
  },

  /**
   * Очередь «Входящие» (7.1). Свой префикс, а не фильтр `conversations.list`:
   * у очереди своя выборка (ничей диалог, ждущий решения), свой порядок
   * (`offered_at ASC` — кто дольше ждёт, тот выше) и своя судьба строки
   * (приняли — она исчезает у всех сразу). Пересечения с `conversations.root`
   * нет намеренно: события очереди не должны инвалидировать вкладки
   * «Мои/Все/Новые/Закрытые», а их `invalidate` — трогать очередь.
   */
  inbox: {
    list: ["inbox", "list"] as const,
    /** Предпросмотр разгрузки (блок 8.3) — зависит от выбранного срока. */
    stale: (days: number) => ["inbox", "stale", days] as const,
  },

  /** Боты (01 §8) — список и деталь сценария в /settings/bots. */
  bots: {
    root: ["bots"] as const,
    list: ["bots", "list"] as const,
    detail: (id: string) => ["bots", "detail", id] as const,
  },
} as const;

export type TemplateScope = "all" | "shared" | "personal";

/** Prefix matching every cached conversations list regardless of filters. */
export const CONVERSATIONS_LIST_KEY = ["conversations", "list"] as const;
