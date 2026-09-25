import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { fetchConversations, fetchTabCounts } from "@/features/chats/api";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import {
  __resetListRefetch,
  refetchListsNow,
  scheduleListRefetch,
} from "@/shared/realtime/listRefetch";
import { jsonResponse } from "./helpers";

/**
 * ЧИСЛА «ВЗЯТО / НЕ ОТВЕЧЕНО» НАД ВКЛАДКОЙ «МОИ» (жалоба владельца 02.09).
 *
 * ⚠ ГЛАВНОЕ, ЧТО СТЕРЕЖЁТ ЭТОТ ФАЙЛ, — ЧТО ЧИСЛО СПРАШИВАЮТ У СЕРВЕРА.
 * Бейдж у «Моих» здесь уже стоял и был снят: он считался по загруженным
 * строкам, а список идёт страницами по 50, и «Мои 3» превращалось в «Мои 17»
 * от одной прокрутки колеса. Посчитать по кэшу заново — соблазн бесплатный и
 * ровно та же ошибка, поэтому проверка смотрит на адрес запроса, а не на число.
 */

describe("счёт спрашивают у сервера", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => jsonResponse(200, { mine: 27, mine_waiting: 11 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  it("ходит в отдельную ручку, а не считает список", async () => {
    const итог = await fetchTabCounts();
    expect(String(fetchMock.mock.calls[0][0])).toContain("/conversations/counts");
    expect(итог).toEqual({ mine: 27, mine_waiting: 11 });
  });
});

describe("фильтр «Ждут ответа» уезжает на сервер", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => jsonResponse(200, { items: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  function запрос(): URLSearchParams {
    return new URL(String(fetchMock.mock.calls[0][0]), "http://x").searchParams;
  }

  it("включённый — параметром", async () => {
    await fetchConversations({ tab: "mine", waitingOnly: true }, 0);
    expect(запрос().get("waiting_only")).toBe("true");
  });

  it("выключённый — параметра нет вовсе", async () => {
    await fetchConversations({ tab: "mine" }, 0);
    expect(запрос().has("waiting_only")).toBe(false);
  });

  it("на «Всех» тоже работает: закрытые отсекает предикат, а не вкладка", async () => {
    await fetchConversations({ tab: "all", waitingOnly: true }, 0);
    expect(запрос().get("tab")).toBe("any");
    expect(запрос().get("waiting_only")).toBe("true");
  });
});

describe("число едет вместе со строками (жалоба владельца 03.09)", () => {
  let счёт: number;
  let списки: number;

  beforeEach(() => {
    __resetListRefetch();
    vi.useFakeTimers();
    счёт = 0;
    списки = 0;
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation((арг?: unknown) => {
      const ключ = (арг as { queryKey?: readonly unknown[] } | undefined)?.queryKey;
      const текст = JSON.stringify(ключ);
      if (текст === JSON.stringify(qk.conversations.counts)) счёт += 1;
      if (текст === JSON.stringify(CONVERSATIONS_LIST_KEY)) списки += 1;
      return Promise.resolve();
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    __resetListRefetch();
  });

  it("СВОЁ НАЖАТИЕ обновляет число немедленно", () => {
    /*
     * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА: «очень долго обновляется вкладка/поля/индикаторы».
     *
     * Своё нажатие счётчика не касалось ВООБЩЕ: `refetchListsNow` сбрасывал
     * только ключ списка, а число живёт под соседним ключом и ждало своего
     * окна в пятнадцать секунд. Человек принимал диалог, видел его в списке —
     * и старое число над вкладкой ещё четверть минуты.
     */
    refetchListsNow();
    expect(счёт).toBe(1);
    expect(списки).toBe(1);
  });

  it("чужая пачка событий даёт ОДИН запрос числа, и не позже полусекунды", () => {
    for (let i = 0; i < 100; i++) scheduleListRefetch();
    expect(счёт).toBe(0);
    vi.advanceTimersByTime(500);
    expect(счёт).toBe(1);
  });

  it("число и строки сбрасываются ОДНИМ таймером — число не может отстать", () => {
    /*
     * ⚠ ГЛАВНОЕ СВОЙСТВО ПРАВКИ. Раньше у числа был свой схлопыватель с окном
     * 15 с, у строк — свой с 0,5 с. Список перестраивался сразу, число
     * держалось старым ещё четырнадцать секунд — ровно то, на что жаловался
     * владелец. Теперь ключ один таймер сбрасывает вместе.
     */
    scheduleListRefetch();
    vi.advanceTimersByTime(500);
    expect(счёт).toBe(списки);
    expect(счёт).toBe(1);
  });

  it("непрерывный поток не отодвигает число дальше потолка", () => {
    // Потолок пачки — три секунды: иначе живой поток кадров откладывал бы
    // перезапрос бесконечно (жалоба оператора 28.08 про «подгрузились через
    // три минуты»).
    for (let i = 0; i < 20; i++) {
      scheduleListRefetch();
      vi.advanceTimersByTime(400);
    }
    expect(счёт).toBeGreaterThanOrEqual(1);
  });
});
