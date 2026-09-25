import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import { useDiscardMessage } from "@/features/chats/hooks/useSendMessage";
import type { MessageDto } from "@/shared/api/types";
import { qk } from "@/shared/api/queryKeys";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders, seedEmptyThread, threadMessages } from "./render";

/**
 * «Удалить» рядом с «Повторить» у красной ⏳-строки (04 §5.3). Кнопка есть
 * ТОЛЬКО у локальных строк офлайн-очереди: серверное сообщение из ленты не
 * выбрасывают — у него свои ретраи на бэкенде.
 */

const TEMP_ID = "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59";

function failedTemp(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: TEMP_ID, // temp: id === client_message_id
    conversation_id: CONV_ID,
    direction: "out",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: "Замена экрана — от 8 900 ₽",
    attachments: [],
    delivery_status: "failed",
    delivery_error: "Нет соединения",
    client_message_id: TEMP_ID,
    created_at: "2026-08-05T10:12:00Z",
    ...overrides,
  } as MessageDto;
}

function DiscardHost({ msg }: { msg: MessageDto }) {
  const discard = useDiscardMessage(CONV_ID);
  return (
    <MessageBubble
      msg={msg}
      prev={null}
      clientId="client-1"
      clientName="Иван"
      onRetry={() => {}}
      onDiscard={(m) => discard.mutate(m)}
    />
  );
}

describe("«Удалить» у строки офлайн-очереди (04 §5.3)", () => {
  let calls: ReturnType<typeof makeFakeTauriBridge>["calls"];

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedEmptyThread();
    const fake = makeFakeTauriBridge();
    calls = fake.calls;
    enterTauriRuntime(fake.bridge);
  });

  afterEach(() => {
    leaveTauriRuntime();
  });

  it("клик убирает строку из очереди Rust и гасит пузырь в ленте", async () => {
    const msg = failedTemp();
    queryClient.setQueryData(qk.messages.list(CONV_ID), {
      pages: [
        {
          items: [msg],
          page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
        },
      ],
      pageParams: [null],
    });

    renderWithProviders(<DiscardHost msg={msg} />);
    await userEvent.click(screen.getByRole("button", { name: "Удалить" }));

    await waitFor(() => expect(calls.remove).toHaveBeenCalledWith(TEMP_ID));
    await waitFor(() => expect(threadMessages()).toHaveLength(0));
  });

  it("у серверного сообщения кнопки «Удалить» нет — только «Повторить»", () => {
    const serverMsg = failedTemp({ id: "srv-9", client_message_id: "other-uuid" });
    renderWithProviders(<DiscardHost msg={serverMsg} />);

    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Удалить" })).toBeNull();
  });
});
