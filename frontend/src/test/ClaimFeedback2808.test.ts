// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY } from "@/shared/api/queryKeys";
import {
  LIST_REFETCH_DELAY_MS,
  LIST_REFETCH_MAX_WAIT_MS,
  __resetListRefetch,
  refetchListsNow,
  scheduleListRefetch,
} from "@/shared/realtime/listRefetch";

/**
 * ОБРАТНАЯ СВЯЗЬ ОПЕРАТОРОВ 28.08: «взял 4 диалога, по статистике в работе 14,
 * а вижу 10, 4 пропали. Подгрузились спустя 3 минуты».
 *
 * ⚠ СХЛОПЫВАНИЕ ГОЛОДАЛО. Таймер перезапроса списка сбрасывался КАЖДЫМ
 * вызовом, а кадр `inbox:claimed` приходит всем тринадцати диспетчерам на
 * каждое чужое «Принять». В живую смену события идут чаще, чем раз в
 * полсекунды, — и перезапрос не наступал вообще, пока поток не стихнет. Три
 * минуты в жалобе — это не задержка списка, это время до первой паузы.
 *
 * ⚠ И СВОЁ НАЖАТИЕ ЖДАЛО В ОБЩЕЙ ОЧЕРЕДИ. Взятый диалог в «Моих» не появляется
 * патчем: `applyConversationPatch` правит строку там, где она уже есть, а
 * взятый лежал в очереди. Строка ждала перезапроса — того самого, который
 * голодал. Схлопывание защищает от ЧУЖИХ событий, а результат собственного
 * нажатия человек ждёт глазами.
 */

/** Сколько раз сбрасывали ключ СПИСКА — то есть сколько было пачек. */
function пачек(): number {
  const вызовы = (queryClient.invalidateQueries as ReturnType<typeof vi.fn>).mock.calls;
  return вызовы.filter(
    (c) =>
      JSON.stringify((c[0] as { queryKey?: readonly unknown[] } | undefined)?.queryKey) ===
      JSON.stringify(CONVERSATIONS_LIST_KEY),
  ).length;
}

describe("Перезапрос списка не голодает", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    __resetListRefetch();
    /*
     * ⚠ СЧИТАЕМ ПАЧКИ, А НЕ ВЫЗОВЫ (03.09). С этого дня один таймер сбрасывает
     * ДВА ключа — строки списка и число над вкладкой: они обязаны обновляться
     * вместе, иначе число отстаёт от строк, из которых сложено. Считать все
     * вызовы подряд значило бы проверять их количество, а проверяем мы
     * схлопывание пачек.
     */
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation(async () => {});
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    __resetListRefetch();
  });

  it("тихо — перезапрос через полсекунды после последнего события", () => {
    scheduleListRefetch();
    vi.advanceTimersByTime(LIST_REFETCH_DELAY_MS - 1);
    expect(пачек()).toBe(0);
    vi.advanceTimersByTime(1);
    expect(пачек()).toBe(1);
  });

  it("непрерывный поток событий больше не откладывает перезапрос навсегда", () => {
    /*
     * Событие каждые 200 мс — типичная смена из тринадцати диспетчеров. Без
     * потолка таймер сбрасывался бы бесконечно.
     */
    for (let прошло = 0; прошло < LIST_REFETCH_MAX_WAIT_MS + 500; прошло += 200) {
      scheduleListRefetch();
      vi.advanceTimersByTime(200);
    }
    expect(
      пачек(),
      "под непрерывным потоком список не перезапрашивается вовсе",
    ).toBeGreaterThan(0);
  });

  it("под потоком перезапрос наступает не позже потолка", () => {
    /*
     * ⚠ ПЕРВАЯ РЕДАКЦИЯ ЭТОЙ ПРОВЕРКИ БЫЛА НЕВЕРНОЙ: она звала расписание ОДИН
     * раз и ждала, что оно промолчит до потолка. Одиночный вызов срабатывает
     * через полсекунды — это и есть обычное схлопывание. Потолок проверяется
     * только ПОТОКОМ: событие чаще, чем хвост, иначе таймер не сбрасывается.
     */
    let прошло = 0;
    while (пачек() === 0) {
      scheduleListRefetch();
      vi.advanceTimersByTime(200);
      прошло += 200;
      if (прошло > LIST_REFETCH_MAX_WAIT_MS * 3) break; // не зацикливаемся на красном
    }
    expect(пачек(), "перезапрос не наступил вовсе").toBeGreaterThan(0);
    expect(
      прошло,
      `перезапрос наступил через ${прошло} мс — позже потолка ${LIST_REFETCH_MAX_WAIT_MS} мс`,
    ).toBeLessThanOrEqual(LIST_REFETCH_MAX_WAIT_MS + 200);
  });

  it("после срабатывания начинается новая пачка со своим потолком", () => {
    scheduleListRefetch();
    vi.advanceTimersByTime(LIST_REFETCH_DELAY_MS);
    expect(пачек()).toBe(1);

    scheduleListRefetch();
    vi.advanceTimersByTime(LIST_REFETCH_DELAY_MS);
    expect(пачек()).toBe(2);
  });

  it("своё нажатие перезапрашивает СРАЗУ и снимает отложенный", () => {
    scheduleListRefetch();
    refetchListsNow();
    expect(пачек()).toBe(1);
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({
      queryKey: CONVERSATIONS_LIST_KEY,
      refetchType: "active",
    });

    // Отложенный снят: иначе за немедленным пришёл бы второй через полсекунды.
    vi.advanceTimersByTime(LIST_REFETCH_MAX_WAIT_MS * 2);
    expect(пачек()).toBe(1);
  });
});

describe("Проводка взятия", () => {
  const без_комментариев = (t: string) =>
    (t as string).replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("успех принятия зовёт немедленный перезапрос, а не отложенный", () => {
    /*
     * Без этой проверки предыдущие зеленеют впустую: потолок можно поставить, а
     * взятие оставить в общей очереди — и дефект вернётся целиком.
     */
    const src = без_комментариев(
      readFileSync("src/features/chats/inbox/useInbox.ts", "utf-8") as string,
    );
    const i = src.indexOf("export function принятоУспешно");
    expect(i, "общая обвязка успеха исчезла").toBeGreaterThan(-1);
    const тело = src.slice(i, src.indexOf("export function useClaimConversation"));
    expect(тело, "взятый диалог снова ждёт хвоста чужой пачки").toContain("refetchListsNow()");
  });

  it("сочетание клавиш берёт диалог из ЛЮБОЙ вкладки", () => {
    /*
     * Жалоба: «приходится заходить во входящие, тыкать мышкой на диалог и после
     * нажимать комбинацию». Здесь стояло `if (!store.activeConversationId)
     * return false` — сочетание молчало, пока диалог не открыт.
     */
    const src = без_комментариев(
      readFileSync("src/features/hotkeys/dispatch.ts", "utf-8") as string,
    );
    const i = src.indexOf('case "claim"');
    expect(i, "действие «принять» исчезло из обработчика").toBeGreaterThan(-1);
    const ветка = src.slice(i, src.indexOf('case "close"', i));
    expect(ветка, "сочетание снова молчит без открытого диалога").toContain("inboxRows()");
    expect(ветка, "взятый из очереди диалог не открывается").toContain("claimFromQueue");
  });
});
