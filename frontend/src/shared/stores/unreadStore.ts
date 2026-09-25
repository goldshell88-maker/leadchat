import { create } from "zustand";
import type { ConversationDto, ConversationStatus } from "@/shared/api/types";
import { useSessionStore } from "./sessionStore";

/**
 * Счётчики непрочитанных (03 §2.2/§3.5). Не персистится — сервер отдаёт
 * unread_count в каждой строке списка, стор лишь агрегирует их для бейджей:
 * title «(N) LeadChat», иконка «Чаты» в rail, счётчики вкладок «Мои»/«Новые»
 * (клиентский подсчёт, 11 §2.1). assigneeId/status нужны именно для вкладок.
 *
 * ЧТО ЗДЕСЬ ЛЕЖИТ НА САМОМ ДЕЛЕ — «всё, что я успел пролистать», а не «мои
 * диалоги»: `seedFromRows` зовётся на КАЖДУЮ загруженную страницу любого
 * списка, включая чужие и закрытые (`features/chats/api.ts`,
 * `features/chats/inbox/api.ts`, догон после реконнекта). Строки отсюда не
 * убираются никогда — сменил фильтр, и в сторе осело содержимое обеих выдач.
 * Поэтому суммировать его целиком нельзя: получится число, зависящее от того,
 * куда человек смотрел (см. :func:`selectMineUnread`).
 */
export interface UnreadEntry {
  count: number;
  assigneeId: string | null;
  status: ConversationStatus;
}

interface UnreadState {
  byConversation: Record<string, UnreadEntry>;

  /** Сервер — истина: строки списка перезаписывают счётчики целиком. */
  seedFromRows(rows: ConversationDto[]): void;
  increment(convId: string, delta: number, meta?: Partial<Omit<UnreadEntry, "count">>): void;
  /** Метаданные строки изменились (статус/ответственный) без изменения счётчика. */
  patchMeta(convId: string, meta: Partial<Omit<UnreadEntry, "count">>): void;
  setCount(convId: string, count: number): void;
  reset(convId: string): void;
  clear(): void;
}

export const useUnreadStore = create<UnreadState>()((set) => ({
  byConversation: {},

  seedFromRows: (rows) =>
    set((s) => {
      const next = { ...s.byConversation };
      for (const row of rows) {
        next[row.id] = {
          count: row.unread_count,
          assigneeId: row.assignee?.id ?? null,
          status: row.status,
        };
      }
      return { byConversation: next };
    }),

  increment: (convId, delta, meta) =>
    set((s) => {
      const prev = s.byConversation[convId];
      const entry: UnreadEntry = {
        count: Math.max(0, (prev?.count ?? 0) + delta),
        assigneeId: meta?.assigneeId !== undefined ? meta.assigneeId : (prev?.assigneeId ?? null),
        status: meta?.status ?? prev?.status ?? "new",
      };
      return { byConversation: { ...s.byConversation, [convId]: entry } };
    }),

  patchMeta: (convId, meta) =>
    set((s) => {
      const prev = s.byConversation[convId];
      if (!prev) return s;
      return {
        byConversation: {
          ...s.byConversation,
          [convId]: {
            ...prev,
            assigneeId: meta.assigneeId !== undefined ? meta.assigneeId : prev.assigneeId,
            status: meta.status ?? prev.status,
          },
        },
      };
    }),

  setCount: (convId, count) =>
    set((s) => {
      const prev = s.byConversation[convId];
      if (!prev && count === 0) return s;
      return {
        byConversation: {
          ...s.byConversation,
          [convId]: {
            count: Math.max(0, count),
            assigneeId: prev?.assigneeId ?? null,
            status: prev?.status ?? "new",
          },
        },
      };
    }),

  reset: (convId) =>
    set((s) => {
      const prev = s.byConversation[convId];
      if (!prev || prev.count === 0) return s;
      return { byConversation: { ...s.byConversation, [convId]: { ...prev, count: 0 } } };
    }),

  clear: () => set({ byConversation: {} }),
}));

/**
 * Сумма непрочитанных В МОИХ ЖИВЫХ ДИАЛОГАХ — title «(N) LeadChat» и бейдж
 * рейки (03 §3.5).
 *
 * ЧТО БЫЛО НА БОЮ (12 августа). «(10)» в заголовке вкладки появлялось и
 * пропадало от смены ФИЛЬТРА: число выскочило, только когда в выдачу попал
 * закрытый диалог с десятью непрочитанными — тот, которого в обычных списках
 * не видно вовсе. Причина в устройстве стора: он засеивается строками КАЖДОЙ
 * загруженной страницы (`seedFromRows` в `features/chats/api.ts` и в очереди),
 * то есть хранит не «мою работу», а «всё, что я успел пролистать». Что
 * пролистал — то и посчиталось.
 *
 * ПОЧЕМУ ФИЛЬТР ПО «МОИМ», А НЕ ПРОСТО ВЫЧЕРКНУТЬ ЗАКРЫТЫЕ. Убрать закрытые
 * значило бы починить ровно тот случай, который заметили, оставив весь класс:
 * зашёл на вкладку «Все» — и в заголовок приехали непрочитанные двенадцати
 * коллег; вернулся к «Моим» — уехали. Число обязано отвечать на один вопрос —
 * «сколько работы у МЕНЯ» — и не зависеть от того, куда человек посмотрел.
 * Ровно это и написано у бейджа в `stores/badges.ts`: «Непрочитанное — мне
 * написали, я не прочитал, работа уже моя». Условие теперь совпадает со
 * словами.
 *
 * Закрытые не в счёт по той же причине, по которой их нет в
 * :func:`selectMineUnreadDialogs`: закрытый диалог ничего не требует, а
 * непрочитанное в нём — след истории, а не работа.
 *
 * Ничей диалог из очереди сюда не попадает (он ещё не мой) — и не должен: у
 * очереди свой бейдж `[N]`, а складывать эти две величины запрещено (см.
 * `stores/badges.ts`). Раньше они складывались молча.
 *
 * `myUserId` пустой (сессия ещё не поднялась) — ноль, а не «всё подряд».
 */
export function selectMineUnread(
  s: { byConversation: Record<string, UnreadEntry> },
  myUserId: string | undefined,
): number {
  if (!myUserId) return 0;
  let total = 0;
  for (const id in s.byConversation) {
    const e = s.byConversation[id];
    if (e.assigneeId === myUserId && e.status !== "closed") total += e.count;
  }
  return total;
}

/**
 * Тот же счётчик для трёх мест, которые его показывают: заголовок вкладки
 * (`stores/badges.ts`), значок «Чаты» в рейке (`app/AppLayout.tsx`) и бейдж
 * трея десктопа (`platform/index.ts`).
 *
 * ИМЯ И СИГНАТУРА ОСТАВЛЕНЫ ПРЕЖНИМИ НАМЕРЕННО: все три места спрашивают одно и
 * то же — «сколько работы у меня», — и разное поведение у них было бы третьим
 * подряд числом про одно и то же. Своего `myUserId` ни у рейки, ни у трея нет,
 * а заводить его в двух чужих файлах ради одного и того же чтения сессии
 * значит развести три копии правила.
 */
export function selectTotalUnread(s: { byConversation: Record<string, UnreadEntry> }): number {
  return selectMineUnread(s, useSessionStore.getState().user?.id);
}

// ЗДЕСЬ ЖИЛИ `selectMineUnreadDialogs` и `selectNewUnreadDialogs` — селекторы
// под бейджи вкладок «Мои» и «Новые». Сняты 23.08 вместе с самими бейджами:
// вкладок с числами в списке больше нет, потребителей у селекторов не осталось
// ни одного. Живые соседи рядом — `selectMineUnread` и `selectTotalUnread`.

