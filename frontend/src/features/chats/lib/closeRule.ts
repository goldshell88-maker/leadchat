import type { ConversationDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { яГость } from "./participation";

/**
 * Можно ли этому человеку закрыть диалог — то же правило, что у сервера
 * (`conversations.change_status`), чтобы не предлагать заведомый отказ.
 *
 * * нужно `conversations:manage`;
 * * гость (зашёл сам в чужой диалог) не закрывает: закрытие закрыло бы
 *   разговор хозяину, а убирает у себя кнопка «Выйти»;
 * * обращение, которое ещё никто не взял, закрывает только администратор
 *   (`conversations:close_queued`): закрытие убрало бы его из очереди насовсем.
 */
export function canCloseConversation(
  conversation: ConversationDto,
  myId: string | null,
  can: (permission: Permission) => boolean,
): boolean {
  if (conversation.status === "closed" || !can("conversations:manage")) return false;
  if (яГость(conversation, myId)) return false;
  const вОчереди =
    Boolean(conversation.offered_at) &&
    !conversation.claimed_by &&
    !conversation.assignee &&
    !conversation.bot_active;
  return !вОчереди || can("conversations:close_queued");
}
