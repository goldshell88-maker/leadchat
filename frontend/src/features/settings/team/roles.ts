import type { Role } from "@/shared/auth/usePermissions";

/** Подписи ролей и однострочные описания прав (DESIGN §5.2, 11 §4.2). */

export const ROLE_LABELS: Record<Role, string> = {
  admin: "Администратор",
  head: "Руководитель",
  manager: "Менеджер",
  observer: "Наблюдатель",
};

export const ROLE_HINTS: Record<Role, string> = {
  admin: "Весь интерфейс: сотрудники, аккаунты Авито, боты, статистика, журнал",
  head: "Все диалоги без отправки, статистика всех сотрудников, журнал аудита",
  manager: "Переписка с клиентами, личные быстрые ответы и своя статистика за день",
  observer: "Только чтение диалогов: без ответов, заметок и смены статусов",
};

export const ROLE_ORDER: Role[] = ["admin", "head", "manager", "observer"];

export const ROLE_OPTIONS = ROLE_ORDER.map((value) => ({ value, label: ROLE_LABELS[value] }));

/**
 * Умеет ли роль отвечать клиенту.
 *
 * Список повторяет `ASSIGNABLE_ROLES` сервера, и дубль здесь осознанный:
 * интерфейсу нужно решить, показывать ли живой тумблер «ведёт диалоги», а
 * спрашивать об этом сервер ради каждой строки таблицы — лишний запрос.
 * Разъезд ловится тестом.
 */
export function canAnswer(role: Role): boolean {
  return role === "admin" || role === "manager";
}
