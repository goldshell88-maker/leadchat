import { beforeEach, describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/query-core";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import { applyConversationPatch } from "@/shared/realtime/applyWsEvent";
import { patchInboxRow } from "@/features/chats/inbox/api";

/**
 * КАДР СОКЕТА НЕ ИМЕЕТ ПРАВА «ОМОЛАЖИВАТЬ» ЧУЖОЙ КЭШ.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 31.08, ДОСЛОВНО: «я могу взять диалоги, но когда я зайду
 * во вкладку "Мои" — их не будет, и появятся только после обновления
 * страницы»; «постоянно требуется обновлять страницу, я не хочу вообще её
 * обновлять».
 *
 * МЕХАНИЗМ. Патч строки шёл во ВСЕ закэшированные варианты фильтров. Там, где
 * строки нет, апдейтер возвращал тот же `old` — но это не `undefined`, и
 * TanStack Query всё равно записывал данные и слал `success`. А тот ставит
 * `isInvalidated: false` и двигает `dataUpdatedAt` на «сейчас».
 *
 * Дальше складывалось так: приём диалога помечал список «Моих» устаревшим, но
 * перезапросить его в тот момент нельзя — запрос выключен, открыта очередь.
 * Через доли секунды приходили собственные кадры принятия, гасили пометку и
 * обнуляли часы свежести. Человек жал «Мои» — `staleTime` 30 секунд ещё не
 * вышел, пометки нет, запроса нет, — и видел список без только что взятых
 * диалогов. Помогала одна перезагрузка: она стирает кэш целиком.
 *
 * В живую смену кадры идут чаще раза в тридцать секунд, поэтому кэш не
 * протухал НИКОГДА.
 */

const CONV_В_КЭШЕ = "aaaa1111-0000-0000-0000-000000000001";
const CONV_ЧУЖОЙ = "bbbb2222-0000-0000-0000-000000000002";
const КЛЮЧ_МОИХ = [...CONVERSATIONS_LIST_KEY, { tab: "mine" }] as const;

function положитьСписок(convId: string): void {
  queryClient.setQueryData(КЛЮЧ_МОИХ, {
    pages: [
      {
        items: [{ id: convId, unread_count: 0, tags: [], last_message_at: "2026-08-31T10:00:00Z" }],
        page: { limit: 50, has_more: false, next_cursor: null },
      },
    ],
    pageParams: [null],
  });
}

function запись() {
  return queryClient.getQueryCache().find({ queryKey: КЛЮЧ_МОИХ });
}

describe("Пометка «устарело» переживает кадры по чужим диалогам", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  it("⚠ кадр по диалогу, которого нет в этом списке, не снимает пометку", () => {
    положитьСписок(CONV_В_КЭШЕ);
    void queryClient.invalidateQueries({ queryKey: CONVERSATIONS_LIST_KEY, refetchType: "none" });
    expect(запись()?.state.isInvalidated, "пометка не встала — проверка бессмысленна").toBe(true);

    applyConversationPatch(CONV_ЧУЖОЙ, { status: "closed" });

    expect(
      запись()?.state.isInvalidated,
      "кадр по чужому диалогу снял пометку — переход на «Мои» не сделает запроса",
    ).toBe(true);
  });

  it("и не обнуляет часы свежести", () => {
    положитьСписок(CONV_В_КЭШЕ);
    const было = запись()?.state.dataUpdatedAt;

    applyConversationPatch(CONV_ЧУЖОЙ, { status: "closed" });

    expect(
      запись()?.state.dataUpdatedAt,
      "часы свежести сдвинуты кадром, которого этот список не касался",
    ).toBe(было);
  });

  it("а по СВОЕЙ строке — обновляет, как и должен", () => {
    положитьСписок(CONV_В_КЭШЕ);
    applyConversationPatch(CONV_В_КЭШЕ, { status: "closed" });
    const строка = queryClient.getQueryData<{ pages: { items: { status?: string }[] }[] }>(КЛЮЧ_МОИХ);
    expect(строка?.pages[0].items[0].status, "своя строка перестала обновляться кадром").toBe("closed");
  });

  it("кэш очереди тоже не омолаживается чужими кадрами", () => {
    queryClient.setQueryData(qk.inbox.list, {
      pages: [{ items: [{ id: CONV_В_КЭШЕ, unread_count: 0, tags: [] }], page: { limit: 50 } }],
      pageParams: [null],
    });
    void queryClient.invalidateQueries({ queryKey: qk.inbox.list, refetchType: "none" });

    patchInboxRow(CONV_ЧУЖОЙ, (r) => r);

    expect(
      queryClient.getQueryCache().find({ queryKey: qk.inbox.list })?.state.isInvalidated,
      "кадр по чужому диалогу снял пометку с очереди",
    ).toBe(true);
  });

  it("сама библиотека ведёт себя так, как здесь предполагается", () => {
    /*
     * ⚠ ПРОВЕРКА ПОСЫЛКИ, А НЕ КОДА. Всё выше построено на утверждении
     * «возврат прежнего объекта — это ЗАПИСЬ, а возврат undefined — нет».
     * Утверждение про чужую библиотеку, и оно обязано быть проверено, а не
     * принято на веру: поменяется поведение query-core — проверки выше начнут
     * сторожить пустоту, и никто этого не заметит.
     */
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000 } } });
    const ключ = ["k"] as const;
    qc.setQueryData(ключ, { v: 1 });
    const q = () => qc.getQueryCache().find({ queryKey: ключ });

    void qc.invalidateQueries({ queryKey: ключ, refetchType: "none" });
    qc.setQueriesData({ queryKey: ключ }, (old) => old);
    expect(q()?.state.isInvalidated, "возврат прежнего объекта перестал быть записью").toBe(false);

    void qc.invalidateQueries({ queryKey: ключ, refetchType: "none" });
    qc.setQueriesData({ queryKey: ключ }, () => undefined);
    expect(q()?.state.isInvalidated, "возврат undefined перестал быть «не трогать»").toBe(true);
    expect(q()?.state.data, "возврат undefined стёр данные").toEqual({ v: 1 });
  });
});
