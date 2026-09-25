import { beforeEach, describe, expect, it } from "vitest";
import { queryClient } from "@/app/queryClient";
import { inboxAhead, nextInboxId } from "@/features/chats/inbox/api";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { useInboxStore } from "@/shared/stores/inboxStore";

/**
 * «Следующий в очереди» — обход ПО КРУГУ (разбор боевой сборки от 12 августа).
 *
 * ЧТО БЫЛО НА ПРОДЕ. Выбор следующего делался так: `rows[idx + 1] ?? rows[idx - 1]`.
 * На последней строке очереди `idx + 1` — это `undefined`, срабатывал запасной
 * вариант, и «следующим» оказывался ПРЕДЫДУЩИЙ. Из предыдущего «следующим»
 * снова становился последний: два диалога перекидывали оператора друг другу,
 * и до начала очереди кнопкой было не дойти вовсе.
 *
 * Тест держит все положения, в которых эта функция работает: она же считает
 * переход после принятия, отказа и закрытия — там текущий диалог из очереди
 * УХОДИТ, и переход на первого корректен ровно потому же, почему корректен круг.
 */

function row(id: string, offeredAt: string): ConversationDto {
  return {
    id,
    status: "new",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: null,
    item: null,
    last_message: { body: "Здравствуйте!", direction: "in", created_at: offeredAt },
    unread_count: 1,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: offeredAt,
    in_inbox: true,
    offered_at: offeredAt,
    waiting_seconds: 60,
    waiting_human: "1 мин",
    declined_count: 0,
    escalated: false,
  };
}

/** Засеять кэш очереди и стор так, как это делает `fetchInboxQueue`. */
function seedQueue(ids: string[], total = ids.length): void {
  const items = ids.map((id, i) => row(id, `2026-08-12T09:0${i}:00Z`));
  const page: ConversationsPage = { items, page: { limit: 50, offset: 0, total } };
  queryClient.setQueryData(qk.inbox.list, { pages: [page], pageParams: [0] });
  useInboxStore.getState().seedFromServer(ids, total);
}

describe("Следующий в очереди", () => {
  beforeEach(() => {
    queryClient.clear();
    useInboxStore.getState().clear();
  });

  it("с ПОСЛЕДНЕГО в очереди уводит на ПЕРВЫЙ, а не на предыдущий", () => {
    seedQueue(["q-1", "q-2", "q-3", "q-4"]);
    expect(nextInboxId("q-4")).toBe("q-1");
  });

  it("не зацикливается между двумя последними: круг проходится целиком", () => {
    seedQueue(["q-1", "q-2", "q-3", "q-4"]);
    const visited: string[] = [];
    let at = "q-1";
    for (let step = 0; step < 4; step += 1) {
      const next = nextInboxId(at);
      expect(next).not.toBeNull();
      at = next as string;
      visited.push(at);
    }
    // Четыре шага по кругу из четырёх строк обязаны обойти ВСЕ и вернуться в начало.
    expect(visited).toEqual(["q-2", "q-3", "q-4", "q-1"]);
  });

  it("из середины идёт к соседу снизу", () => {
    seedQueue(["q-1", "q-2", "q-3"]);
    expect(nextInboxId("q-2")).toBe("q-3");
  });

  it("единственный диалог в очереди: следующего НЕТ (кнопку прятать)", () => {
    seedQueue(["q-1"]);
    expect(nextInboxId("q-1")).toBeNull();
  });

  it("пустая очередь — некуда идти", () => {
    seedQueue([]);
    expect(nextInboxId("q-1")).toBeNull();
  });

  it("текущего диалога в очереди нет (уже принят) — идём к первому ждущему", () => {
    seedQueue(["q-1", "q-2"]);
    expect(nextInboxId("conv-mine")).toBe("q-1");
  });

  /*
   * Счётчик на кнопке. Он показывал РАЗМЕР ОЧЕРЕДИ целиком, поэтому при
   * четырёх ждущих и одном из них открытом на кнопке стояло «Следующий (4)»,
   * хотя перейти можно было к трём. Считаем то, что кнопка обещает: сколько
   * диалогов ЕЩЁ ждёт, кроме открытого.
   */
  it("счётчик не считает открытый диалог: 4 в очереди, один открыт — осталось 3", () => {
    seedQueue(["q-1", "q-2", "q-3", "q-4"]);
    expect(inboxAhead("q-2")).toBe(3);
  });

  it("открытый диалог не из очереди — счётчик показывает всю очередь", () => {
    seedQueue(["q-1", "q-2", "q-3", "q-4"]);
    expect(inboxAhead("conv-mine")).toBe(4);
  });

  it("в очереди один диалог, он же открыт — счётчик ноль, кнопке нечего обещать", () => {
    seedQueue(["q-1"]);
    expect(inboxAhead("q-1")).toBe(0);
    expect(nextInboxId("q-1")).toBeNull();
  });

  it("счётчик берёт ВСЮ очередь, а не загруженную страницу", () => {
    // Первая страница из 50, всего в очереди 120 — кнопка обязана обещать 119.
    seedQueue(["q-1", "q-2"], 120);
    expect(inboxAhead("q-1")).toBe(119);
  });
});
