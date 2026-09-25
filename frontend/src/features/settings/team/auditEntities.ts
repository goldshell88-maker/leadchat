/**
 * Подписи объектов (`entity`) — колонка «Объект».
 *
 * Подписей ДЕЙСТВИЙ здесь нет намеренно: их даёт сервер — `description` строки
 * и реестр `GET /audit-log/filters`. Второй словарь на фронте расходился с
 * реестром и по составу, и по словам.
 */
export const AUDIT_ENTITY_LABELS: Record<string, string> = {
  user: "Сотрудник",
  account: "Аккаунт",
  avito_account: "Аккаунт",
  conversation: "Диалог",
  client: "Клиент",
  message: "Сообщение",
  template: "Быстрый ответ",
  bot: "Бот",
  stats: "Статистика",
  settings: "Настройки",
  api_registry: "Внешний сервис",
  dialogs: "Разбор диалогов",
};

export function auditEntityLabel(entity: string | null): string {
  if (!entity) return "—";
  return AUDIT_ENTITY_LABELS[entity] ?? entity;
}
