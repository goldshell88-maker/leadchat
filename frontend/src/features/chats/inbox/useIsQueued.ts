import type { ConversationDto } from "@/shared/api/types";
import { useInboxStore } from "@/shared/stores/inboxStore";

/**
 * «Этот диалог ждёт МОЕГО решения» — единственный признак, по которому низ
 * ленты показывает кнопки «Принять диалог»/«Отклонить» вместо поля ввода.
 *
 * Признаков два, и порядок между ними важен.
 *
 * 1. `in_inbox` — слово сервера. Приходит в патчах строки (`queue_patch` /
 *    `claimed_patch`, 01 §11.3) и в элементах `GET /inbox`. `false` — диалог
 *    уже занят, и это перевешивает всё остальное: именно так у остальных
 *    двенадцати гаснет кнопка в ту же секунду, когда кто-то нажал «Принять».
 *
 * 2. Состав очереди из `GET /inbox` (стор `ids`). Это тоже слово сервера, а не
 *    догадка: попасть в выдачу `/inbox` может только диалог, ждущий решения
 *    именно этого человека. Признак нужен потому, что `GET /conversations/{id}`
 *    полей очереди не отдаёт вовсе — на детали `in_inbox` просто нет.
 *
 * Чего здесь НЕТ и быть не должно — вывода «ответственного нет, статус новый,
 * значит очередь». Диалоги, заведённые до 7.1, выглядят ровно так же и обязаны
 * вести себя как раньше: поле ввода на месте, «кто ответил, тот и ведёт».
 */
export function useIsQueued(convId: string, conversation?: ConversationDto): boolean {
  const inQueueList = useInboxStore((s) => Boolean(s.ids[convId]));
  if (typeof conversation?.in_inbox === "boolean") return conversation.in_inbox;
  return inQueueList;
}
