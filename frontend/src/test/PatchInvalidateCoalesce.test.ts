/**
 * Пачка изменений — ОДИН перезапрос списка, а не пятьдесят (жалобы владельца 22.08).
 *
 * ЧТО БЫЛО. Обработчики событий звали
 * `invalidateQueries({ queryKey: conversations.root, refetchType: "active" })`
 * на каждое изменение: смену статуса, принятие диалога из очереди, возврат в
 * очередь, передачу. Строка списка к этому моменту УЖЕ поправлена локально
 * (`patchRowEverywhere`), и перезапрос нужен ровно для одного: диалог мог
 * переехать между вкладками.
 *
 * На одном событии незаметно. На пачке — нет: владелец закрывал диалоги подряд
 * и получил всё сразу — «зависания страницы», «диалог визуально пропадает» и
 * три тоста «Статус не изменился». Складывается это так: пятьдесят закрытий
 * дают пятьдесят PATCH плюс пятьдесят полных перезапросов тяжёлого списка, а
 * поток запросов упирается в лимит nginx (30 в секунду на адрес) — часть
 * возвращается 429. Кадры очереди хуже вдвойне: они широковещательные, и в
 * смену из тринадцати диспетчеров чужое «Принять» перезапрашивало список у всех.
 *
 * ЧТО СТАЛО. Перезапрос собирается в один: последний в пачке выигрывает. И
 * ключ у него — СПИСКИ, а не корень `["conversations"]`: корень накрывал и
 * открытую деталь, добавляя второй запрос на ровном месте.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import { scheduleListRefetch, __resetListRefetch } from "@/shared/realtime/listRefetch";

function следитьЗаПерезапросом() {
  return vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
}

/**
 * Сколько было ПАЧЕК — считаем по ключу списка.
 *
 * ⚠ С 03.09 один таймер сбрасывает ДВА ключа: строки списка и число над
 * вкладкой. Они обязаны обновляться вместе — иначе число отстаёт от строк, из
 * которых сложено (жалоба владельца про медленные индикаторы). Считать все
 * вызовы подряд значило бы проверять их количество, а проверяем мы схлопывание.
 */
function пачек(шпион: { mock: { calls: unknown[][] } }): number {
  return шпион.mock.calls.filter(
    (c) =>
      JSON.stringify((c[0] as { queryKey?: readonly unknown[] } | undefined)?.queryKey) ===
      JSON.stringify(CONVERSATIONS_LIST_KEY),
  ).length;
}

describe("схлопывание перезапросов списка", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    __resetListRefetch();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("одно изменение — один перезапрос", () => {
    const spy = следитьЗаПерезапросом();
    scheduleListRefetch();
    expect(spy).not.toHaveBeenCalled(); // ждём хвост
    vi.advanceTimersByTime(1000);
    expect(пачек(spy)).toBe(1);
  });

  it("пятьдесят изменений подряд — тоже один перезапрос", () => {
    const spy = следитьЗаПерезапросом();
    for (let i = 0; i < 50; i += 1) scheduleListRefetch();
    vi.advanceTimersByTime(1000);
    expect(пачек(spy)).toBe(1);
  });

  it("пачка, пауза, пачка — два перезапроса, а не один на всё", () => {
    const spy = следитьЗаПерезапросом();
    for (let i = 0; i < 10; i += 1) scheduleListRefetch();
    vi.advanceTimersByTime(1000);
    for (let i = 0; i < 10; i += 1) scheduleListRefetch();
    vi.advanceTimersByTime(1000);
    expect(пачек(spy)).toBe(2);
  });

  it("перезапрос не теряется: хвост срабатывает обязательно", () => {
    const spy = следитьЗаПерезапросом();
    scheduleListRefetch();
    vi.advanceTimersByTime(10_000);
    expect(пачек(spy)).toBe(1);
  });

  it("задержка человеку незаметна — не больше секунды", () => {
    const spy = следитьЗаПерезапросом();
    scheduleListRefetch();
    vi.advanceTimersByTime(1000);
    expect(пачек(spy)).toBe(1);
  });

  /*
   * Ключ проверяем прямо: `["conversations"]` совпадает и со списками, и с
   * `["conversations","detail",id]`. То есть прежний вызов заодно тянул с
   * сервера открытый диалог — деталь, которую обработчик только что поправил
   * в кэше сам. Два запроса вместо одного и лишняя перерисовка шапки.
   */
  it("перезапрашиваются СПИСКИ, а открытая деталь — нет", () => {
    const spy = следитьЗаПерезапросом();
    scheduleListRefetch();
    vi.advanceTimersByTime(1000);

    const [аргумент] = spy.mock.calls[0];
    expect(аргумент).toMatchObject({ queryKey: CONVERSATIONS_LIST_KEY, refetchType: "active" });

    // Тот же ключ через сопоставитель react-query: список накрыт, деталь — нет.
    const подходит = (key: readonly unknown[]) =>
      JSON.stringify(key).startsWith(JSON.stringify(CONVERSATIONS_LIST_KEY).slice(0, -1));
    expect(подходит(qk.conversations.list({ tab: "mine" }))).toBe(true);
    expect(подходит(qk.conversations.detail("conv-1"))).toBe(false);
  });
});
