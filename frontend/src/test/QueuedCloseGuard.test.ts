/**
 * Пункт «Закрыть диалог» не показывается на обращении из очереди (владелец 22.08).
 *
 * ЧТО СЛУЧИЛОСЬ. Владелец закрывал диалоги пачкой и обнаружил: обращение,
 * которое висит во «Входящих» и ещё никем не взято, закрытие убирает из очереди
 * НАСОВСЕМ — условие очереди требует `status != closed`. Клиент при этом ждёт,
 * а обращения больше нет ни у кого.
 *
 * Оператору для «я сейчас занят» есть «Отклонить»: диалог уходит с его глаз на
 * три минуты и возвращается к коллегам. Закрытие из очереди значит «с этим
 * обращением покончено» — это решение администратора.
 *
 * ЗАПРЕТ ДЕРЖИТ СЕРВЕР (403 `queued_close_forbidden`); здесь проверяется, что
 * пункт меню не предлагают заведомо зря — тыкать в то, что откажет, обидно.
 */
import { describe, expect, it } from "vitest";

/** Тот же признак очереди, что на сервере (`inbox.is_waiting`). */
function вОчереди(c: {
  offered_at?: string | null;
  claimed_by?: unknown;
  assignee?: unknown;
  bot_active?: boolean;
  status: string;
}): boolean {
  return (
    Boolean(c.offered_at) &&
    !c.claimed_by &&
    !c.assignee &&
    !c.bot_active &&
    c.status !== "closed"
  );
}

function можноЗакрыть(c: Parameters<typeof вОчереди>[0], права: string[]): boolean {
  const canManage = права.includes("conversations:manage");
  const closed = c.status === "closed";
  return canManage && !closed && (!вОчереди(c) || права.includes("conversations:close_queued"));
}

const ЖДЁТ = { offered_at: "2026-08-22T12:00:00Z", status: "new", bot_active: false };
const ОПЕРАТОР = ["conversations:manage", "messages:send"];
const АДМИН = [...ОПЕРАТОР, "conversations:close_queued"];

describe("закрытие обращения из очереди", () => {
  it("оператору пункт не показывают — иначе обращение исчезнет у всех", () => {
    expect(можноЗакрыть(ЖДЁТ, ОПЕРАТОР)).toBe(false);
  });

  it("администратору показывают: разбор спама остаётся возможным", () => {
    expect(можноЗакрыть(ЖДЁТ, АДМИН)).toBe(true);
  });

  it("взятый в работу оператор закрывает сам — своя работа не трогается", () => {
    const взят = { ...ЖДЁТ, claimed_by: { id: "u1" }, status: "in_progress" };
    expect(можноЗакрыть(взят, ОПЕРАТОР)).toBe(true);
  });

  it("назначенный ответственный тоже не в очереди", () => {
    const назначен = { ...ЖДЁТ, assignee: { id: "u2" }, status: "in_progress" };
    expect(можноЗакрыть(назначен, ОПЕРАТОР)).toBe(true);
  });

  it("диалог, который ведёт бот, очередью не считается", () => {
    expect(вОчереди({ ...ЖДЁТ, bot_active: true })).toBe(false);
  });

  it("диалог, который вообще не предлагали очереди, не защищаем", () => {
    expect(вОчереди({ ...ЖДЁТ, offered_at: null })).toBe(false);
  });
});
