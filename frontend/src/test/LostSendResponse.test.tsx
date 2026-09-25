import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { useSendMessage } from "@/features/chats/hooks/useSendMessage";
import { appendMessage } from "@/shared/realtime/applyWsEvent";
import type { MessageDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders, seedEmptyThread, threadMessages } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * ПОТЕРЯННЫЙ ОТВЕТ POST — ЭТО НЕ «СООБЩЕНИЕ НЕ УШЛО».
 *
 * Сервер публикует `message:new` ВНУТРИ обработчика POST, до ответа, и кадр
 * приходит в том числе автору. Если ответ потерялся по дороге — обрыв,
 * таймаут, 502 после коммита, — сообщение у клиента уже есть, а отправляющая
 * вкладка видит только отказ. Дальше `onError` делал худшее из возможного:
 * возвращал текст в поле ввода. На экране одновременно пузырь ответа и тот же
 * текст в поле, без единого слова о том, что произошло. Естественная реакция —
 * дожать Enter, и клиент получает один и тот же ответ дважды: у повторной
 * отправки НОВЫЙ `client_message_id`, так что идемпотентность её не сдержит, а
 * отозвать сообщение в Авито нельзя.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТ РАБОТАЕТ: убрали проверку близнеца в
 * `onError` — тест краснеет на «в поле пусто».
 */

const TEMP_ID = "018f3c2a-9b1e-7c4d-a5f6-000000000777";
const TEXT = "Приедем завтра с 10 до 12";

function SendHost() {
  const send = useSendMessage(CONV_ID);
  return (
    <button
      type="button"
      onClick={() => send.mutate({ text: TEXT, isNote: false, tempId: TEMP_ID, ownsDraft: true })}
    >
      Отправить
    </button>
  );
}

/** Серверный близнец, приехавший кадром `message:new` до отказа POST. */
function близнец(): MessageDto {
  return {
    id: "srv-777",
    conversation_id: CONV_ID,
    direction: "out",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: TEXT,
    attachments: [],
    delivery_status: "pending",
    client_message_id: TEMP_ID,
    created_at: "2026-08-28T10:12:00Z",
  };
}

describe("Потерянный ответ отправки", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ drafts: {} });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedEmptyThread();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("близнец дошёл — текст в поле не возвращается и пузырь не двоится", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        // Сервер сообщение создал и опубликовал, а ответ не доехал.
        appendMessage(CONV_ID, близнец());
        throw new Error("network");
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<SendHost />);
    await user.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => {
      const строки = threadMessages();
      expect(строки).toHaveLength(1); // остался только серверный
      expect(строки[0].id).toBe("srv-777");
    });
    expect(useChatUiStore.getState().drafts[CONV_ID]?.text ?? "").toBe("");
  });

  it("близнеца нет — прежнее поведение: текст возвращается в поле", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network");
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<SendHost />);
    await user.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() =>
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe(TEXT),
    );
    expect(threadMessages()).toHaveLength(0); // пузырь убран, текст в поле
  });
});
