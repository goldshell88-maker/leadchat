import { http } from "@/shared/api/http";
import type {
  MarkReadResult,
  NotificationActionResult,
  NotificationUnreadCount,
  NotificationsPage,
} from "@/shared/api/types";
import type { NotificationFilters } from "@/shared/api/queryKeys";

/** Программный интерфейс центра уведомлений — 14 §4. */

/** Размер страницы журнала: как в журнале аудита — таблица читается глазами. */
export const NOTIFICATIONS_PAGE_SIZE = 50;

/** Сколько строк показывает выпадающий список колокольчика. */
export const RECENT_PAGE_SIZE = 10;

export function fetchNotifications(f: NotificationFilters): Promise<NotificationsPage> {
  const p = new URLSearchParams();
  p.set("limit", String(NOTIFICATIONS_PAGE_SIZE));
  p.set("offset", String(f.offset));
  if (f.severity !== "all") p.set("severity", f.severity);
  if (f.kind) p.set("kind", f.kind);
  if (f.dateFrom) p.set("date_from", f.dateFrom);
  if (f.dateTo) p.set("date_to", f.dateTo);
  if (f.unreadOnly) p.set("unread_only", "true");
  return http.get<NotificationsPage>(`/notifications?${p.toString()}`);
}

export function fetchRecentNotifications(): Promise<NotificationsPage> {
  return http.get<NotificationsPage>(`/notifications?limit=${RECENT_PAGE_SIZE}&offset=0`);
}

/**
 * Сколько неподтверждённых критичных догружаем отдельно. Больше десятки
 * последних строк намеренно: красных плашек одновременно бывает три, и
 * запас нужен на подтверждённые, которые в эту выборку ещё попадают.
 */
export const CRITICAL_PAGE_SIZE = 20;

/**
 * Непрочитанное критичное — ОТДЕЛЬНЫМ ЗАПРОСОМ, и вот почему.
 *
 * Колокольчик грузит десять ПОСЛЕДНИХ строк, а красная плашка рисуется по
 * тому, что доехало. С 6 по 11 августа «Приём сообщений остановился» дал 112
 * строк, и два настоящих «Резервное копирование не выполнилось» (ночи 6 и 8
 * августа) в эту десятку не попали — их не показали никому, и копий за те ночи
 * действительно нет. Одной понижённой важности шумного вида мало: критичное
 * обязано доезжать до экрана независимо от того, сколько строк натикало сверху.
 */
export function fetchUnreadCriticalNotifications(): Promise<NotificationsPage> {
  return http.get<NotificationsPage>(
    `/notifications?limit=${CRITICAL_PAGE_SIZE}&offset=0&severity=critical&unread_only=true`,
  );
}

/** Счётчик колокольчика с разбивкой по важности. */
export function fetchUnreadCount(): Promise<NotificationUnreadCount> {
  return http.get<NotificationUnreadCount>("/notifications/unread-count");
}

export function markNotificationRead(id: string): Promise<MarkReadResult> {
  return http.post<MarkReadResult>(`/notifications/${encodeURIComponent(id)}/read`);
}

export function markAllNotificationsRead(): Promise<MarkReadResult> {
  return http.post<MarkReadResult>("/notifications/read-all");
}

/**
 * Действие по уведомлению (14 §4). `code` — из поля `action` самой строки:
 * что именно делать, решает сервер, фронт лишь просит выполнить.
 */
export function runNotificationAction(id: string, code?: string): Promise<NotificationActionResult> {
  return http.post<NotificationActionResult>(
    `/notifications/${encodeURIComponent(id)}/action`,
    code ? { action: code } : undefined,
  );
}
