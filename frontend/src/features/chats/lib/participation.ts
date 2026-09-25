import type { ConversationDto, ConversationDetailDto } from "@/shared/api/types";

/**
 * Кем человек приходится ЭТОМУ диалогу — одним ответом на все экраны.
 *
 * ⚠ ОДНА ФУНКЦИЯ, А НЕ ТРИ ПОХОЖИХ УСЛОВИЯ. Признак «я тут гость» нужен в трёх
 * местах сразу: подвал решает, показывать ли поле ввода, меню — показывать ли
 * «Закрыть диалог», Ctrl+D — закрыть или выйти. Разъедься они хоть на слово, и
 * получится знакомая пара «два пути считают одно поле по-разному»: подвал
 * пускает писать, а меню считает тебя гостем.
 *
 * Правило то же, что на сервере (`participants.is_guest`): гость — это участие
 * вида `self` при живом ЧУЖОМ ответственном. Позванный (`invited`) гостем не
 * считается: его позвали помогать, и права он получил осознанно.
 */

/** Участники приехали? У строки списка их нет вовсе — это НЕ «их нет». */
export function участникиИзвестны(conversation?: ConversationDto): boolean {
  return Array.isArray((conversation as ConversationDetailDto | undefined)?.participants);
}

function участники(conversation?: ConversationDto) {
  return (conversation as ConversationDetailDto | undefined)?.participants ?? [];
}

/** Диалог ведёт кто-то другой, и он живой (закрытый диалог ничей). */
export function чужойЖивой(conversation?: ConversationDto, myId?: string | null): boolean {
  const хозяин = conversation?.assignee;
  return Boolean(хозяин && myId && хозяин.id !== myId && conversation?.status !== "closed");
}

/** Я в этом диалоге есть — неважно, позвали меня или зашёл сам. */
export function яУчастник(conversation?: ConversationDto, myId?: string | null): boolean {
  return Boolean(myId) && участники(conversation).some((p) => p.id === myId);
}

/**
 * Я ЗАШЁЛ СЮДА САМ в чужой живой диалог.
 *
 * Пока участники неизвестны (приехала только строка списка), ответ `false`:
 * гостевые ограничения не вводят по догадке.
 */
export function яГость(conversation?: ConversationDto, myId?: string | null): boolean {
  if (!чужойЖивой(conversation, myId)) return false;
  return участники(conversation).some((p) => p.id === myId && p.kind === "self");
}
