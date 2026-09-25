import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import { useRetryMessage } from "@/features/chats/hooks/useSendMessage";
import { patchMessageInCache, replaceMessageInCache } from "@/shared/realtime/applyWsEvent";
import type { MessageDto } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders, seedEmptyThread, threadMessages } from "./render";
import { queryClient as qc } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";

const TEMP_ID = "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59";

function failedTemp(): MessageDto {
  return {
    id: TEMP_ID, // temp: id === client_message_id (POST не дошёл)
    conversation_id: CONV_ID,
    direction: "out",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: "Замена экрана — от 8 900 ₽",
    attachments: [],
    delivery_status: "failed",
    client_message_id: TEMP_ID,
    created_at: "2026-08-05T10:12:00Z",
  };
}

function failedServer(): MessageDto {
  return { ...failedTemp(), id: "srv-9", client_message_id: "other-uuid", delivery_error: "Авито: чат недоступен" };
}

/** Хост-компонент: кнопка «Повторить» из ленты, подключённая к реальной мутации. */
function RetryHost({ msg }: { msg: MessageDto }) {
  const retry = useRetryMessage(CONV_ID);
  return (
    <MessageBubble msg={msg} prev={null} clientId="client-1" clientName="Иван" onRetry={(m) => retry.mutate(m)} />
  );
}

describe("«Повторить» и идемпотентность client_message_id (01 §1.6, 03 §3.4)", () => {
  const bodies: Array<Record<string, unknown>> = [];
  const urls: string[] = [];

  beforeEach(() => {
    bodies.length = 0;
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
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        urls.push(String(input));
        bodies.push(init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {});
        return jsonResponse(200, { ...failedTemp(), id: "srv-1", delivery_status: "pending" });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("повтор temp-сообщения шлёт ТОТ ЖЕ client_message_id — сервер не создаст дубль", async () => {
    const user = userEvent.setup();
    renderWithProviders(<RetryHost msg={failedTemp()} />);

    expect(screen.getByText(/Не отправилось/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).toContain(`/conversations/${CONV_ID}/messages`);
    expect(bodies[0].client_message_id).toBe(TEMP_ID);
  });

  it("повтор серверного failed идёт в POST /messages/{id}/retry", async () => {
    const user = userEvent.setup();
    renderWithProviders(<RetryHost msg={failedServer()} />);

    // Причина из message:status.error видна оператору (11 §2.2).
    expect(screen.getByText(/Не доставлено: Авито: чат недоступен/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).toContain("/messages/srv-9/retry");
  });

  it("кнопки «Повторить» нет без onRetry (роли без messages:send — 03 §5.2)", () => {
    renderWithProviders(
      <MessageBubble msg={failedServer()} prev={null} clientId="client-1" clientName="Иван" />,
    );
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
  });

  it("«обгоняющий» message:status применяется после подстановки серверного id (03 §3.4)", () => {
    // temp-пузырь в ленте, серверного id ещё нет.
    qc.setQueryData(qk.messages.list(CONV_ID), {
      pages: [
        {
          items: [{ ...failedTemp(), delivery_status: "pending" }],
          page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
        },
      ],
      pageParams: [null],
    });

    // Воркер оказался быстрее сети: статус пришёл раньше ответа POST.
    act(() => {
      patchMessageInCache(CONV_ID, "srv-fast", { delivery_status: "delivered" });
    });
    expect(threadMessages()[0].delivery_status).toBe("pending"); // пока некуда применять

    act(() => {
      replaceMessageInCache(CONV_ID, TEMP_ID, {
        ...failedTemp(),
        id: "srv-fast",
        delivery_status: "pending",
      });
    });

    const [msg] = threadMessages();
    expect(msg.id).toBe("srv-fast");
    expect(msg.delivery_status).toBe("delivered"); // буфер применился
  });
});
