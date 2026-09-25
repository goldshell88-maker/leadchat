import { beforeEach, describe, expect, it } from "vitest";
import { InfiniteQueryObserver, QueryClient } from "@tanstack/query-core";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import { свернутьЛентыКХвосту } from "@/shared/realtime/applyWsEvent";

/**
 * ЛЕНТА НЕ СХЛОПЫВАЕТСЯ В СТАРЫЙ КУСОК.
 *
 * ⚠ ЭТО И БЫЛО «ДИАЛОГ ВНЕЗАПНО ПЕРЕСКАКИВАЕТ». Жалоба возвращалась трижды, и
 * трижды её искали среди переходов — а никакой навигации здесь нет: адрес и
 * имя клиента прежние, меняется СОДЕРЖИМОЕ ленты.
 *
 * МЕХАНИЗМ. Лента — бесконечный запрос с `getNextPageParam: () => null`: вниз
 * не догружаем, низ живёт кадрами. Пока страница одна, перезапрос приносит
 * свежую полусотню. Но оператор листает вверх («что мы обещали по цене» —
 * типовое движение смены), догрузка кладёт курсор САМОГО СТАРОГО куска в
 * `pageParams[0]`, а полный перезапрос идёт только вперёд: берёт нулевую
 * страницу по этому курсору и обрывается. Вся лента заменяется старым куском.
 *
 * Дальше самоподдерживается: `pageParams[0]` навсегда старый, каждая сверка
 * схлопывает туда же, а новые кадры дописываются в конец старого куска —
 * лента с дырой посередине.
 */

const CONV = "eeee0000-0000-0000-0000-00000000000e";

function лента(страницы: unknown[], params: (string | null)[]) {
  queryClient.setQueryData(qk.messages.list(CONV), { pages: страницы, pageParams: params });
}

function текущая() {
  return queryClient.getQueryData<{ pages: { items: { id: string }[] }[]; pageParams: unknown[] }>(
    qk.messages.list(CONV),
  );
}

describe("Свёртка ленты перед перезапросом", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  it("⚠ оставляет СВЕЖИЙ хвост, а не старый кусок", () => {
    // Догрузка старого кладёт страницы в НАЧАЛО: pages[0] — древняя.
    лента(
      [{ items: [{ id: "древнее" }] }, { items: [{ id: "свежее" }] }],
      ["курсор-старый", null],
    );

    свернутьЛентыКХвосту();

    const д = текущая();
    expect(д?.pages.length, "страниц осталось больше одной — перезапрос снова уедет в старое").toBe(1);
    expect(
      д?.pages[0].items[0].id,
      "оставили древний кусок вместо свежего — это и есть «перескочило»",
    ).toBe("свежее");
  });

  it("и возвращает курсор в начало, иначе перезапрос уедет туда же", () => {
    лента([{ items: [{ id: "древнее" }] }, { items: [{ id: "свежее" }] }], ["курсор-старый", null]);
    свернутьЛентыКХвосту();
    expect(текущая()?.pageParams, "курсор остался старым — беда вернётся на следующей сверке").toEqual([
      null,
    ]);
  });

  it("одну страницу не трогает вовсе", () => {
    лента([{ items: [{ id: "свежее" }] }], [null]);
    const было = текущая();
    свернутьЛентыКХвосту();
    expect(текущая(), "тронули кэш там, где сворачивать нечего").toBe(было);
  });

  it("библиотека действительно ведёт себя так, как здесь предполагается", async () => {
    /*
     * ⚠ ПРОВЕРКА ПОСЫЛКИ, А НЕ КОДА. Всё выше держится на утверждении о чужой
     * библиотеке: «полный перезапрос бесконечного запроса идёт только вперёд и
     * берёт нулевую страницу по pageParams[0]». Поменяется поведение
     * query-core — проверки выше начнут сторожить пустоту, и никто не заметит.
     */
    const qc = new QueryClient();
    const запросы: (string | null)[] = [];
    const obs = new InfiniteQueryObserver(qc, {
      queryKey: ["m"],
      queryFn: async ({ pageParam }) => {
        запросы.push(pageParam as string | null);
        return { items: [pageParam ?? "свежие"], page: { has_more_before: true, prev_cursor: "стар" } };
      },
      initialPageParam: null as string | null,
      getNextPageParam: () => null,
      getPreviousPageParam: (first: { page: { has_more_before: boolean; prev_cursor: string } }) =>
        first.page.has_more_before ? first.page.prev_cursor : null,
    });
    const off = obs.subscribe(() => {});
    await obs.refetch();
    await obs.fetchPreviousPage();
    запросы.length = 0;
    await obs.refetch();
    off();

    const d = qc.getQueryData<{ pages: unknown[]; pageParams: unknown[] }>(["m"]);
    expect(запросы, "перезапрос перестал ходить по старому курсору").toEqual(["стар"]);
    expect(d?.pages.length, "перезапрос перестал схлопывать ленту").toBe(1);
  });

  it("свёртка подключена ДО перезапроса в обоих местах", () => {
    /*
     * ⚠ ПОРЯДОК — ЧАСТЬ ПОЧИНКИ. Свернуть ПОСЛЕ перезапроса значит не сделать
     * ничего: лента к тому моменту уже подменена старым куском.
     */
    /*
     * Смотрим ТЕЛО нужной функции, а не весь файл: в `applyWsEvent` есть и
     * другие перезапросы, и первый попавшийся `invalidateQueries` стоит выше
     * самой свёртки — сторож по файлу целиком был бы бессмысленным.
     */
    for (const [путь, функция] of [
      ["src/shared/realtime/quietResync.ts", "export function resyncNow"],
      ["src/shared/realtime/applyWsEvent.ts", "function refetchOpenScreens"],
    ] as const) {
      const весь = readFileSync(путь, "utf-8") as string;
      const начало = весь.indexOf(функция);
      expect(начало, `${путь}: не найдена ${функция}`).toBeGreaterThan(-1);
      const исходник = весь.slice(начало, весь.indexOf("\n}", начало));
      const свёртка = исходник.indexOf("свернутьЛентыКХвосту()");
      /*
       * ⚠ ПЕРЕЗАПРОС ИЩЕМ ПО ЛЮБОЙ ЕГО ФОРМЕ. Тихая сверка 31.08 перестала
       * перечислять ключи и стала сплошной (`invalidateQueries({refetchType:
       * "active"})`) — сторож, привязанный к строке `qk.messages.root`, начал
       * падать на верном коде. Порядок при этом важен по-прежнему: свернуть
       * ПОСЛЕ перезапроса значит не сделать ничего.
       */
      const перезапрос = исходник.indexOf("invalidateQueries(");
      expect(свёртка, `${путь}: свёртка не подключена`).toBeGreaterThan(-1);
      expect(перезапрос, `${путь}: перезапроса нет вовсе`).toBeGreaterThan(-1);
      expect(свёртка, `${путь}: свёртка идёт после перезапроса — она бесполезна`).toBeLessThan(
        перезапрос,
      );
    }
  });
});
