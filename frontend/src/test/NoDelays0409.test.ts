import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import {
  __resetListRefetch,
  COUNTS_REFETCH_WINDOW_MS,
  LIST_REFETCH_DELAY_MS,
  scheduleListRefetch,
} from "@/shared/realtime/listRefetch";
import {
  applyConversationPatch,
  applyLocalRead,
  applyPinnedInLists,
} from "@/shared/realtime/applyWsEvent";
import { забытьСвои, свои } from "@/features/chats/components/list/listOrder";
import { принятоУспешно } from "@/features/chats/inbox/useInbox";
import type { ConversationDetailDto } from "@/shared/api/types";
import { DEFAULT_FILTERS } from "@/shared/stores/chatUiStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { makeConversation } from "./render";

/**
 * ЧТОБЫ НЕ БЫЛО ЗАДЕРЖЕК ВООБЩЕ (просьба владельца 04.09).
 *
 * Здесь заперты три места, где человек ждал зря: хвост схлопывания, пометка
 * «эта строка моя» и лишние походы к серверу на локальных путях.
 */
describe("Оставшиеся задержки", () => {
  let сбросы: string[];

  beforeEach(() => {
    __resetListRefetch();
    забытьСвои(["conv-1", "conv-2"]);
    vi.useFakeTimers();
    сбросы = [];
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation((арг?: unknown) => {
      сбросы.push(
        JSON.stringify((арг as { queryKey?: readonly unknown[] } | undefined)?.queryKey),
      );
      return Promise.resolve();
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    __resetListRefetch();
  });

  it("хвост схлопывания — двести миллисекунд, а не полсекунды", () => {
    /*
     * Прежние полсекунды обосновывались тем, что «дольше человек уже замечает».
     * Довод верен только сверху: снизу порог никто не проверял, а он и есть
     * задержка на КАЖДОЕ чужое событие. Схлопывание при этом остаётся
     * схлопыванием — пачка кадров об одном диалоге приходит за десятки
     * миллисекунд.
     */
    expect(LIST_REFETCH_DELAY_MS).toBe(200);

    scheduleListRefetch();
    vi.advanceTimersByTime(199);
    expect(сбросы).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(сбросы).toContain(JSON.stringify(CONVERSATIONS_LIST_KEY));
    expect(сбросы).toContain(JSON.stringify(qk.conversations.counts));
  });

  it("пачка кадров всё ещё склеивается в один перезапрос", () => {
    /* Обратная половина: ускорение не должно превратиться в запрос на кадр. */
    for (let i = 0; i < 30; i++) scheduleListRefetch();
    vi.advanceTimersByTime(200);
    expect(сбросы.filter((k) => k === JSON.stringify(CONVERSATIONS_LIST_KEY))).toHaveLength(1);
  });

  it("принятый диалог помечен своим — иначе он уедет в конец списка", () => {
    /*
     * ⚠ ПОРЯДОК СПИСКА ЗАМОРОЖЕН НА ЧЕТЫРЕ СЕКУНДЫ, пока курсор над колонкой:
     * строки не должны прыгать под рукой. Заморозка отличает знакомые строки от
     * новых, а взятый диалог для неё НОВЫЙ — в «Моих» его до сих пор не было.
     * Без пометки он вставал в хвост и висел там до конца заморозки.
     *
     * Пометка в проекте была, но стояла только на пути КЛАВИШИ; кнопка
     * «Принять» идёт сюда и её не ставила. Один диалог, взятый двумя
     * способами, вёл себя по-разному.
     */
    const conv = { ...makeConversation(), id: "conv-1" } as ConversationDetailDto;
    принятоУспешно("conv-1", { conversation: conv, count: 3, escalated: 0 } as never);

    expect(свои().has("conv-1"), "строка приедет в хвост списка").toBe(true);
  });

  /** Строка «Моих» в кэше — как её видит экран. */
  function положитьМою(row: Record<string, unknown>): void {
    queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
      pages: [{ items: [row], page: { limit: 50, offset: 0, total: 1 } }],
      pageParams: [0],
    });
  }

  it("а ожидание в ЧУЖОМ кадре двигает число ДЕЛЬТОЙ, без похода к серверу", () => {
    /*
     * ⚠ ПЕРЕПИСАНО 06.09. Здесь стояло «любой чужой патч спрашивает числа у
     * сервера» с патчем `{ unread_count: 3 }`, и это была половина беды из
     * замера 06.09: `GET /conversations/counts` 55–60 тыс. в сутки (54–57 %
     * трафика вместе со списком), 344 ответа 429 с офисного адреса. Само
     * непрочитанное числа не двигает: сервер считает «не отвечено» по
     * `own_condition` + статус + `awaiting_since` (`tab_counts`), а
     * непрочитанное — состояние оператора. Довод «числа двигает входящее
     * сообщение» верен, но входящее везёт в кадре `waiting_since` — отсюда
     * дельта, а не запрос.
     *
     * Обратная половина прежней правки — «число не может отстать от строк» —
     * держится теперь крепче: число меняется тем же движением, что и строка,
     * отставать ему негде.
     */
    resetSessionStore({ user: fakeUser });
    queryClient.setQueryData(qk.conversations.counts, { mine: 4, mine_waiting: 1 });
    положитьМою({ ...makeConversation(), id: "conv-3", waiting_since: null });

    applyConversationPatch("conv-3", { waiting_since: "2026-09-06T10:00:00Z" });
    vi.advanceTimersByTime(COUNTS_REFETCH_WINDOW_MS + LIST_REFETCH_DELAY_MS);

    expect(queryClient.getQueryData(qk.conversations.counts), "число не сдвинулось").toEqual({
      mine: 4,
      mine_waiting: 2,
    });
    expect(сбросы, "дельта известна из кадра, а сервер всё равно спросили").toEqual([]);
  });

  it("а где дельту не вывести — спрашивают ОДНИ числа, отложенно, и не список", () => {
    /*
     * Строка старого кэша без `waiting_since` (в типе отсутствие ≠ `null`):
     * ждал ли клиент до патча, по ней не узнать. Гадать нельзя — спрашиваем,
     * но только числа и одним запросом на окно: список здесь не менялся.
     */
    resetSessionStore({ user: fakeUser });
    положитьМою({ ...makeConversation(), id: "conv-3" });

    applyConversationPatch("conv-3", { waiting_since: "2026-09-06T10:00:00Z" });
    vi.advanceTimersByTime(COUNTS_REFETCH_WINDOW_MS);

    expect(сбросы, "числа не спросили — они отстанут от строк").toContain(
      JSON.stringify(qk.conversations.counts),
    );
    expect(сбросы, "ради чисел перезапросили весь список").not.toContain(
      JSON.stringify(CONVERSATIONS_LIST_KEY),
    );
  });

  it("закрепление и отметка прочтения не дёргают сервер", () => {
    /*
     * Оба пути локальные: закрепление меняет только порядок, отметка
     * прочтения — только бейдж. Числа над вкладкой считаются по владению и
     * ожиданию ответа, и ни то ни другое их не двигает. Docstring'и обоих
     * путей это и обещали, а общая строка перезапроса молча обещание отменяла.
     */
    applyPinnedInLists("conv-2", true);
    applyLocalRead("conv-2");
    vi.advanceTimersByTime(1000);
    expect(сбросы, "локальный путь сходил к серверу").toEqual([]);
  });
});
