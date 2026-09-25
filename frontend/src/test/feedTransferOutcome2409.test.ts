import { describe, expect, it } from "vitest";
import { describeFrame } from "@/features/feed/describe";
import type { WsServerEvent } from "@/shared/realtime/wsEvents";

/**
 * ЖИВАЯ ЛЕНТА ГОВОРИТ ПРАВДУ О ПЕРЕДАЧЕ (проверка 24.09).
 *
 * С 07.08 передача — предложение: диалог остаётся у прежнего, пока получатель
 * не примет. Лента же писала «передал диалог — теперь ведёт Пётр» в момент
 * ПРЕДЛОЖЕНИЯ и молчала о принятии, отказе, отмене и истечении: минимум 101
 * строка за 45 дней утверждала неправду, а все 518 появлялись раньше времени.
 */

const ANNA = { id: "u-anna", full_name: "Анна Ведущая" };
const PETR = { id: "u-petr", full_name: "Пётр Берущий" };

function assigned(offer: boolean): WsServerEvent {
  return {
    type: "conversation:assigned",
    ts: "2026-09-24T10:00:00Z",
    data: {
      conversation_id: "c-1",
      assignee: PETR,
      assigned_by: ANNA,
      comment: null,
      is_for_you: false,
      offer,
    },
  } as WsServerEvent;
}

function outcome(
  kind: "accepted" | "declined" | "cancelled" | "expired",
  actor: typeof ANNA | null,
): WsServerEvent {
  return {
    type: "conversation:updated",
    ts: "2026-09-24T10:05:00Z",
    data: {
      conversation_id: "c-1",
      patch: { transfer: null },
      transfer_outcome: kind,
      transfer_actor: actor,
    },
  } as WsServerEvent;
}

describe("Передача в Живой ленте", () => {
  it("предложение названо предложением, а не свершившейся передачей", () => {
    const line = describeFrame(assigned(true));
    expect(line?.text).toBe("Анна Ведущая предлагает диалог Пётр Берущий — ждёт ответа");
    expect(line?.tone).toBe("warn");
  });

  it("прямое назначение по-прежнему «теперь ведёт»", () => {
    expect(describeFrame(assigned(false))?.text).toBe(
      "Анна Ведущая передал диалог — теперь ведёт Пётр Берущий",
    );
  });

  it.each([
    ["accepted", PETR, "Пётр Берущий принял(а) переданный диалог", "good"],
    ["declined", PETR, "Пётр Берущий отказался(ась) от передачи — диалог остаётся у прежнего", "warn"],
    ["cancelled", ANNA, "Анна Ведущая отменил(а) передачу", "neutral"],
    ["expired", null, "передачу никто не принял за 15 минут — диалог остался у прежнего", "warn"],
  ] as const)("исход «%s» — своей строкой", (kind, actor, text, tone) => {
    const line = describeFrame(outcome(kind, actor));
    expect(line?.text).toBe(text);
    expect(line?.tone).toBe(tone);
  });

  it("обычная заплатка без исхода и без смены статуса в ленту не идёт", () => {
    const frame = {
      type: "conversation:updated",
      ts: "2026-09-24T10:06:00Z",
      data: { conversation_id: "c-2", patch: { tags: ["срочно"] } },
    } as WsServerEvent;
    expect(describeFrame(frame)).toBeNull();
  });
});
