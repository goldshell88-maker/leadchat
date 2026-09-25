import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { queryClient } from "@/app/queryClient";
import {
  applyOutboxReport,
  dedupeOptimisticRows,
  dropOptimisticRow,
  installOptimisticDedup,
} from "@/features/chats/hooks/outboxSync";
import { qk } from "@/shared/api/queryKeys";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import type { MessageDto } from "@/shared/api/types";
import { fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, seedEmptyThread, threadMessages } from "./render";

/**
 * Дедупликация ⏳-строки по `client_message_id` (04 §5.4).
 * Локальная строка — временная; серверная, пришедшая с тем же
 * `client_message_id`, всегда побеждает: и когда приходит ответом API, и когда
 * приезжает событием WS `message:new` (в десктопе — только так).
 */

const TEMP_ID = "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59";

function tempRow(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: TEMP_ID,
    conversation_id: CONV_ID,
    direction: "out",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: "Замена экрана — от 8 900 ₽",
    attachments: [],
    delivery_status: "pending",
    client_message_id: TEMP_ID,
    created_at: "2026-08-05T10:12:00Z",
    ...overrides,
  };
}

function serverTwin(): MessageDto {
  return { ...tempRow(), id: "srv-1", delivery_status: "pending", created_at: "2026-08-05T10:12:01Z" };
}

function seedThread(items: MessageDto[]): void {
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [
      { items, page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false } },
    ],
    pageParams: [null],
  });
}

describe("Дедупликация оптимистичной строки по client_message_id (04 §5.4)", () => {
  let uninstall: (() => void) | null = null;

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    seedEmptyThread();
  });

  afterEach(() => {
    uninstall?.();
    uninstall = null;
  });

  it("ответ API: серверная строка вытесняет ⏳-близнеца", () => {
    seedThread([tempRow(), serverTwin()]);

    expect(dedupeOptimisticRows(CONV_ID)).toBe(true);

    const rows = threadMessages();
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe("srv-1");
  });

  it("WS message:new: ⏳-строка исчезает сама, дубля в ленте нет", () => {
    seedThread([tempRow()]);
    uninstall = installOptimisticDedup();

    applyWsEvent({
      type: "message:new",
      data: { conversation_id: CONV_ID, message: serverTwin() },
    } as never);

    const rows = threadMessages();
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe("srv-1");
  });

  it("⏳ без серверного близнеца остаётся на месте", () => {
    seedThread([tempRow()]);
    uninstall = installOptimisticDedup();

    applyWsEvent({
      type: "message:new",
      data: {
        conversation_id: CONV_ID,
        message: { ...serverTwin(), id: "srv-2", client_message_id: "другой-uuid" },
      },
    } as never);

    const rows = threadMessages();
    expect(rows).toHaveLength(2);
    expect(rows.some((m) => m.id === TEMP_ID)).toBe(true);
  });

  it("отчёт outbox_flush красит строку в ✗ с причиной (04 §8.2)", () => {
    seedThread([tempRow()]);

    applyOutboxReport({
      sent: 0,
      remaining: 0,
      failed: [{ clientMessageId: TEMP_ID, conversationId: CONV_ID, error: "HTTP 409" }],
    });

    const [row] = threadMessages();
    expect(row.delivery_status).toBe("failed");
    expect(row.delivery_error).toBe("HTTP 409");
  });

  it("«Удалить» убирает строку из ленты", () => {
    seedThread([tempRow()]);
    dropOptimisticRow(CONV_ID, TEMP_ID);
    expect(threadMessages()).toHaveLength(0);
  });
});
