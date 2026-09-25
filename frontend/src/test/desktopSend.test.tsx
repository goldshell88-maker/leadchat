import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { useSendMessage, useRetryMessage } from "@/features/chats/hooks/useSendMessage";
import type { MessageDto } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { CONV_ID, renderWithProviders, seedEmptyThread, threadMessages } from "./render";

/**
 * Выбор пути отправки (03 §7, 04 §5.3): в десктопе — ВСЕГДА через outbox моста,
 * в вебе — прямой POST. Ошибиться нельзя ни в ту, ни в другую сторону: в
 * браузере outbox'а нет вовсе, в приложении прямой POST потерял бы офлайн.
 */

const TEMP_ID = "018f3c2a-9b1e-7c4d-a5f6-000000000001";
const TEXT = "Замена экрана — от 8 900 ₽";

function SendHost({ isNote = false }: { isNote?: boolean }) {
  const send = useSendMessage(CONV_ID);
  return (
    <button type="button" onClick={() => send.mutate({ text: TEXT, isNote, tempId: TEMP_ID })}>
      Отправить
    </button>
  );
}

function RetryHost({ msg }: { msg: MessageDto }) {
  const retry = useRetryMessage(CONV_ID);
  return (
    <button type="button" onClick={() => retry.mutate(msg)}>
      Повторить
    </button>
  );
}

function failedOutboxRow(): MessageDto {
  return {
    id: TEMP_ID,
    conversation_id: CONV_ID,
    direction: "out",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: TEXT,
    attachments: [],
    delivery_status: "failed",
    client_message_id: TEMP_ID,
    created_at: "2026-08-05T10:12:00Z",
  };
}

describe("Отправка: мост в десктопе, HTTP в вебе", () => {
  const urls: string[] = [];

  beforeEach(() => {
    urls.length = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return jsonResponse(200, { ...failedOutboxRow(), id: "srv-1", delivery_status: "pending" });
      }),
    );
  });

  afterEach(() => {
    leaveTauriRuntime();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("в Tauri сообщение уходит в outbox моста, а не в сеть; UI сразу рисует ⏳", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    calls.push.mockResolvedValue(TEMP_ID);
    enterTauriRuntime(bridge);

    const user = userEvent.setup();
    renderWithProviders(<SendHost />);
    await user.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(calls.push).toHaveBeenCalledTimes(1));
    expect(calls.push).toHaveBeenCalledWith({
      conversationId: CONV_ID,
      kind: "message",
      text: TEXT,
      clientMessageId: TEMP_ID, // ключ идемпотентности 01 §1.6
    });
    // Ни одного HTTP-запроса: POST делает Rust при флаше (04 §8.2).
    expect(urls).toHaveLength(0);
    // Флаш дёргается сразу после push (04 §5.3 п.2).
    await waitFor(() => expect(calls.drain).toHaveBeenCalled());

    const [msg] = threadMessages();
    expect(msg.delivery_status).toBe("pending"); // ⏳ часы ожидания
    expect(msg.client_message_id).toBe(TEMP_ID);
  });

  it("заметка в Tauri тоже идёт через очередь, но с kind=note (01 §6.4)", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    enterTauriRuntime(bridge);

    const user = userEvent.setup();
    renderWithProviders(<SendHost isNote />);
    await user.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(calls.push).toHaveBeenCalledTimes(1));
    expect(calls.push.mock.calls[0][0]).toMatchObject({ kind: "note" });
    expect(urls).toHaveLength(0);
  });

  it("в вебе путь прежний: прямой POST /conversations/{id}/messages", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SendHost />);
    await user.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).toContain(`/conversations/${CONV_ID}/messages`);
  });

  it("«Повторить» в десктопе сбрасывает строку очереди, а не шлёт POST", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    enterTauriRuntime(bridge);

    const user = userEvent.setup();
    renderWithProviders(<RetryHost msg={failedOutboxRow()} />);
    await user.click(screen.getByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(calls.retry).toHaveBeenCalledWith(TEMP_ID));
    expect(urls).toHaveLength(0);
    expect(calls.drain).toHaveBeenCalled();
  });
});
