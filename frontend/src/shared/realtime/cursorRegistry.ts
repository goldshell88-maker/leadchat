/**
 * next_cursor последнего «свежего» ответа ленты по диалогу (01 §11.7 п.3):
 * после reconnect хвост докачивается GET .../messages?after=<cursor>.
 * Курсор непрозрачен (01 §1.4) — клиент его не конструирует, только запоминает
 * из REST-ответов; перекрытие безопасно, сообщения дедуплицируются по id.
 */
const cursors = new Map<string, string>();

export function rememberCursor(conversationId: string, nextCursor: string | null | undefined): void {
  if (nextCursor) cursors.set(conversationId, nextCursor);
}

export function lastKnownCursor(conversationId: string): string | null {
  return cursors.get(conversationId) ?? null;
}

export function clearCursors(): void {
  cursors.clear();
}
