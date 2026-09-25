import type {
  KnownNotificationKind,
  NotificationDto,
  NotificationKind,
  NotificationSeverity,
} from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";

/**
 * Каталог событий (14 §2) в виде, нужном интерфейсу: иконка и человеческая
 * подпись типа для фильтра журнала. Ключи — реестр сервера
 * (`app/services/notifications.py:KINDS`), расходиться им нельзя.
 *
 * Заголовок, тело и надпись на кнопке фронт НЕ сочиняет — они приходят готовыми.
 * Здесь остаётся ровно то, что серверу знать незачем: чем рисовать строку и куда
 * вести ссылку на связанную сущность.
 */

/**
 * Право раздела уведомлений — то же, что проверяет сервер
 * (`services/notifications.SECTION_PERMISSION`): admin, head, manager.
 * Наблюдателю не приходит ничего, поэтому и колокольчика у него нет.
 */
export const NOTIFICATIONS_PERMISSION: Permission = "conversations:manage";

export const SEVERITY_LABELS: Record<NotificationSeverity, string> = {
  critical: "Критичное",
  warning: "Важное",
  info: "Обычное",
};

/** Класс цветного признака важности — цвет И слово, не только цвет. */
export function severityDotClass(severity: NotificationSeverity): string {
  return `lc-notify-severity lc-notify-severity--${severity}`;
}

/** Порядок важности: критичные первыми (сортировка плашек и строк одного времени). */
export const SEVERITY_RANK: Record<NotificationSeverity, number> = {
  critical: 0,
  warning: 1,
  info: 2,
};

/** WS-`notify` знает level (01 §11.3), таблица — severity (14 §4). Одно в другое. */
export function severityFromLevel(level: "info" | "warning" | "error"): NotificationSeverity {
  if (level === "error") return "critical";
  if (level === "warning") return "warning";
  return "info";
}

export type NotificationAction =
  /** Переход внутри приложения по связанной сущности. */
  | { mode: "link"; label: string; to: string }
  /** POST /notifications/{id}/action — работу делает сервер (14 §4). */
  | { mode: "request"; label: string; code: string }
  /** Подтвердить и убрать плашку: пометить прочитанным. */
  | { mode: "ack"; label: string };

interface KindMeta {
  icon: string;
  label: string;
}

/**
 * Иконка и подпись типа. Значения — из каталога 14 §2.
 *
 * Тип намеренно составной. `Record<KnownNotificationKind, …>` требует запись на
 * КАЖДЫЙ известный вид: забытый вид роняет `npm run typecheck`, а не тихо
 * доезжает до администратора строкой `system.unreachable` под общим 🔔 —
 * ровно в тот день, когда читать её будет некогда. `Record<string, …>` рядом
 * оставляет место видам, появившимся на сервере раньше фронта: они рисуются
 * общим видом (см. `FALLBACK`), и это осознанный запасной путь, а не пропуск.
 */
export const KIND_CATALOG: Record<KnownNotificationKind, KindMeta> & Record<string, KindMeta> = {
  /*
   * ПОДПИСИ СВЕРЕНЫ С ЗАГОЛОВКАМИ СЕРВЕРА (TEXT-11, `services/notifications.KINDS`).
   *
   * В журнале колонки «Событие» (эта подпись) и «Что произошло» (заголовок с
   * сервера) стоят в одной строке рядом, и по семи видам они говорили разное:
   * «Аккаунт требует переподключения» против «Аккаунт Авито требует
   * переподключения», «Планировщик не отвечает» против «Планировщик не подаёт
   * признаков жизни». Человек читает одну строку и видит два названия одного
   * события — и не может понять, одно это событие или два.
   *
   * Правило: подпись — ИМЯ ВИДА, поэтому в ней нет ни порогов, ни «вашего»
   * («Диалог без ответа», а не «Диалог больше 30 минут без ответа»), но слова
   * берутся серверные. Сверка ключей — `tests/unit/test_notification_catalog.py`;
   * текстов она не знает, и это её честное ограничение.
   */
  // 14 §2.1 — системные события, получатели: все администраторы
  "account.needs_reauth": { icon: "🔌", label: "Аккаунт Авито требует переподключения" },
  "inbound.stalled": { icon: "📵", label: "Приём сообщений остановился" },
  "backup.failed": { icon: "💾", label: "Резервное копирование не выполнилось" },
  "scheduler.down": { icon: "⏱", label: "Планировщик не подаёт признаков жизни" },
  "system.unreachable": { icon: "🛰", label: "Система не отвечает снаружи" },
  "queue.backlog": { icon: "📥", label: "Очередь входящих не разбирается" },
  "delivery.failures": { icon: "📤", label: "Сообщения не уходят клиентам" },
  "disk.space": { icon: "🗄", label: "На диске мало места" },
  "cert.expiring": { icon: "🔒", label: "Сертификат скоро истекает" },
  "ai.unavailable": { icon: "🤖", label: "AI временно недоступен" },
  "leadbot.down": { icon: "🛑", label: "Лид-бот не отвечает" },
  "avito.unreachable": { icon: "📡", label: "Авито не отвечает" },
  "gateway.down": { icon: "🌐", label: "Шлюз внешних сервисов не отвечает" },
  "inbound.dropped_disabled": { icon: "🚫", label: "Клиенты пишут в выключенный канал" },
  "bot.phantom_reply": { icon: "👻", label: "Бот считает отправленным то, чего нет" },
  "client_merge.autostopped": { icon: "🧩", label: "Автообъединение карточек остановлено" },
  "address.funnel_dropped": { icon: "📉", label: "Адреса стали реже попадать в карточку" },
  // Лестница политик правил адреса (пакет 6.0а): понижение/отключение и повышение
  "address.rule_degraded": { icon: "⬇️", label: "Правило адреса понижено" },
  "address.rule_promoted": { icon: "⬆️", label: "Правило адреса повышено" },

  // 14 §2.2 — просьбы сотрудников, получатели: все администраторы
  "support.password_reset": { icon: "🔑", label: "Сотрудник просит новый пароль" },
  "support.message": { icon: "✉️", label: "Сообщение администратору" },
  "auth.account_locked": { icon: "🚫", label: "Учётная запись заблокирована" },

  // 14 §2.3 — рабочие события, получатели: руководитель и ответственный менеджер
  "conversation.negative": { icon: "😠", label: "Клиент недоволен" },
  "conversation.no_reply": { icon: "⏳", label: "Диалог без ответа" },
  "conversation.reopened": { icon: "🔁", label: "Клиент вернулся в закрытый диалог" },
  "conversation.assigned": { icon: "⚑", label: "Вам передали диалог" },
  "conversation.invited": { icon: "👋", label: "Вас позвали в диалог" },
  /*
   * Диалог закрыл не тот, кто его вёл (03.09). Без строки в колокольчике
   * хозяин видит только, что поле заперлось и диалог ушёл из «Моих», — и
   * читает чужое действие как поломку.
   */
  "conversation.closed_by_other": { icon: "✔", label: "Ваш диалог закрыл коллега" },

  /*
   * ЧЕТЫРЕ ВИДА, КОТОРЫЕ ЗАВЕЛИ НА СЕРВЕРЕ И ЗАБЫЛИ ЗДЕСЬ.
   *
   * Каталог не падает на незнакомом виде — он подставляет колокольчик и слово
   * «Уведомление». Тем и опасен: беды не видно, просто самое личное и самое
   * срочное уведомление оператора выглядело как любое другое, а в фильтре
   * журнала стояло машинное `conversation.awaiting_you` вместо русской
   * подписи. Ни одна проверка этого не ловила.
   *
   * Ниже — тест, который теперь ловит: tests/unit/test_notification_catalog.py
   * сверяет виды сервера с этим файлом и падает на каждом новом.
   *
   * Часы у «клиент ждёт» — те же, что у «диалога без ответа»: событие одно и
   * то же, разница в получателе. Оператору — про свой диалог, руководителю —
   * про чужой.
   */
  "conversation.awaiting_you": { icon: "⏳", label: "Клиент ждёт вашего ответа" },
  // ЗДЕСЬ БЫЛО ЛИЦО `conversation.snooze_due` («Отложенный диалог вернулся»,
  // docs/38 §7). Снято 12 августа вместе с самим видом уведомления: сторожа
  // возврата и статуса «Отложен» больше нет, слать его некому. Оставить лицо
  // было нельзя — `test_notification_catalog.py` сверяет каталог с серверным
  // реестром в обе стороны, и лишний вид здесь означает «такое уведомление
  // бывает», из-за чего его ищут в журнале.
  "conversation.unclaimed": { icon: "🙈", label: "Диалог никто не принял" },
  "conversation.transfer_declined": { icon: "↩️", label: "От передачи отказались" },
  "conversation.transfer_expired": { icon: "↩️", label: "Передачу не приняли" },
  // Передающий забрал предложение назад. Значок «отмены», а не общий возврат:
  // от соседних двух («отказались», «не приняли») событие отличается тем, что
  // решение принял не получатель и не часы, а сам передающий.
  "conversation.transfer_cancelled": { icon: "🚫", label: "Передачу отменили" },
  "message.undelivered": { icon: "📤", label: "Ответ не дошёл до клиента" },
  // Авито прислал то, чего мы не понимаем. Иконка «сломанного письма», а не
  // общая тревога: беда не в системе, а в форме входящего — и разбирать её
  // будут по сохранённому сырцу, а не по логам.
  "inbound.unparsed": { icon: "📩", label: "Сообщения от клиентов не разбираются" },
  // Подписку на канал перебила чужая система. Замок, а не колокол: канал
  // не сломался, его забрали. Формулировка — серверная (TEXT-11); «канал»
  // вместо «аккаунта Авито» здесь не наш выбор, а слово из заголовка сервера,
  // и разойтись с ним хуже, чем разойтись со словарём (см. notes воркера).
  "webhook.lost": { icon: "🔒", label: "Канал отобрали: подписка на события пропала" },
};

const FALLBACK: KindMeta = { icon: "🔔", label: "Уведомление" };

export function kindIcon(kind: NotificationKind): string {
  return (KIND_CATALOG[kind] ?? FALLBACK).icon;
}

/** Подпись типа для фильтра журнала; неизвестный вид показывается как есть. */
export function kindLabel(kind: NotificationKind): string {
  return KIND_CATALOG[kind]?.label ?? kind;
}

/** Куда ведёт связанная сущность уведомления. */
export function entityLink(n: NotificationDto): { to: string; label: string } | null {
  const e = n.entity;
  if (!e) return null;
  if (e.type === "conversation" && e.id) return { to: `/chats/${e.id}`, label: "Открыть диалог" };
  if (e.type === "account") return { to: "/settings/accounts", label: "Открыть аккаунты" };
  if (e.type === "user") return { to: "/settings/team", label: "Открыть команду" };
  return null;
}

/**
 * Действие уведомления. Приоритет — за сервером: он знает, есть ли кнопка и
 * что на ней написано. Нет серверного действия — ведём на связанную сущность.
 * У критичного кнопка есть ВСЕГДА (14 §3): чинить нечего — значит «Подтвердить».
 */
export function resolveAction(n: NotificationDto): NotificationAction | null {
  if (n.action) return { mode: "request", label: n.action.label, code: n.action.code };
  const link = entityLink(n);
  if (link) return { mode: "link", label: link.label, to: link.to };
  if (n.severity === "critical") return { mode: "ack", label: "Подтвердить" };
  return null;
}

/** Непрочитанное — единственный признак «висит на человеке». */
export function isUnread(n: NotificationDto): boolean {
  return !n.is_read;
}

/**
 * Фильтр по `audience_hint` WS-события (01 §11.3). Хаб уже адресует кадр;
 * это вторая линия — чтобы рассылка по роли не всплыла не у той роли.
 * Соответствие ролей и рассылок — `services/notifications.AUDIENCE_PERMISSION`.
 */
export function audienceAllowed(
  permissions: readonly Permission[],
  hint?: string | null,
): boolean {
  if (!permissions.includes(NOTIFICATIONS_PERMISSION)) return false;
  if (!hint) return true; // адресное уведомление — сервер уже выбрал получателя
  if (hint === "admin") return permissions.includes("users:manage");
  if (hint === "head") return permissions.includes("audit:read");
  return true;
}
