import { create } from "zustand";
import { useSessionStore } from "@/shared/stores/sessionStore";

/**
 * Кто ещё смотрит открытый диалог (SCEN-48).
 *
 * ЧТО БЫЛО. Двое диспетчеров открывали один диалог и отвечали клиенту оба —
 * клиент получал два ответа подряд. Признака «диалог уже открыт коллегой» не
 * было ни на одном конце: сервер кадр `subscribe` принимал и запоминал, но
 * никому о нём не рассказывал, а браузер его не слал вовсе. Механизм был
 * объявлен в контракте и не работал целиком.
 *
 * Состав приходит с сервера ЦЕЛИКОМ (`conversation:viewers`) и заменяется
 * целиком же — не «пришёл такой-то». Собирать список из приращений на канале,
 * который рвётся и переподключается, значит однажды показать «вы тут один»
 * тому, кто не один; а признак, которому нельзя верить, хуже отсутствующего.
 */

export interface Viewer {
  id: string;
  full_name: string;
}

interface ViewersState {
  /** conversation_id -> кто в нём сейчас, в порядке прихода (первым — открывший раньше). */
  byConversation: Record<string, Viewer[]>;
  setViewers(conversationId: string, viewers: Viewer[]): void;
  /** Забыть один диалог — при уходе из него. */
  forget(conversationId: string): void;
  clear(): void;
}

export const useViewersStore = create<ViewersState>()((set) => ({
  byConversation: {},

  setViewers: (conversationId, viewers) =>
    set((s) => ({ byConversation: { ...s.byConversation, [conversationId]: viewers } })),

  forget: (conversationId) =>
    set((s) => {
      if (!(conversationId in s.byConversation)) return {};
      const next = { ...s.byConversation };
      delete next[conversationId];
      return { byConversation: next };
    }),

  clear: () => set({ byConversation: {} }),
}));

/** Пустой массив-одиночка: новая ссылка на каждый кадр перерисовывала бы подписчиков впустую. */
const NOBODY: Viewer[] = [];

/**
 * Кто ещё, КРОМЕ МЕНЯ, смотрит этот диалог.
 *
 * Себя из состава вычитаем здесь, а не на сервере: серверу список нужен один
 * на всех подписчиков, иначе кадр пришлось бы персонализировать под каждого —
 * тринадцать разных тел одного события ради вычёркивания одной строки.
 */
export function otherViewers(
  byConversation: Record<string, Viewer[]>,
  conversationId: string | null,
  meId: string | null,
): Viewer[] {
  if (!conversationId) return NOBODY;
  const all = byConversation[conversationId];
  if (!all || all.length === 0) return NOBODY;
  const others = all.filter((v) => v.id !== meId);
  return others.length === 0 ? NOBODY : others;
}

/**
 * Хук для экрана диалога: кто ещё в этом диалоге прямо сейчас.
 * Возвращает пустой массив, если никого, — вызывающему достаточно `length`.
 */
export function useOtherViewers(conversationId: string | null): Viewer[] {
  const byConversation = useViewersStore((s) => s.byConversation);
  const meId = useSessionStore((s) => s.user?.id ?? null);
  return otherViewers(byConversation, conversationId, meId);
}

/**
 * Строка для человека: «Диалог открыт: Пётр Петров».
 *
 * Живёт рядом со стором, а не в компоненте, потому что читают её двое: сам
 * признак на экране диалога и предупреждение, всплывающее в момент, когда
 * коллега только открыл диалог, — а два разных текста об одном и том же
 * заставляли бы гадать, одно это событие или разные.
 */
export function viewersLabel(viewers: Viewer[]): string {
  const names = viewers.map((v) => v.full_name).filter(Boolean);
  if (names.length === 0) return "";
  if (names.length === 1) return `Диалог открыт: ${names[0]}`;
  if (names.length === 2) return `Диалог открыт: ${names[0]} и ${names[1]}`;
  return `Диалог открыт: ${names[0]}, ${names[1]} и ещё ${names.length - 2}`;
}
