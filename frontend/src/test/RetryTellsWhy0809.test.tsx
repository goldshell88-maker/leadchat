/**
 * ОТКАЗ ПОВТОРА ОБЪЯСНЯЕТ СЕБЯ (правка 08.09, найдено разбором).
 *
 * ЧТО БЫЛО. `useRetryMessage.onError` только возвращал сообщению крестик.
 * Со стороны диспетчера: нажал «Повторить», крестик мигнул на ⏳ и вернулся
 * крестиком, ни строчки о причине. Хуже всего это там, где повтор бесполезен
 * по своей природе: сервер отвечает 409 `account_needs_reauth` — канал Авито
 * отвалился, и никакое число нажатий сообщение не отправит. Человек жмёт
 * снова и снова, клиент ждёт.
 *
 * ПОЧЕМУ ТЕКСТ БЕРЁМ У СЕРВЕРА. Заготовка «проверьте связь» врала бы на
 * отвалившемся канале, на потерянных правах и на любой пятисотке. Своя фраза
 * остаётся только там, где ответа сервера нет вовсе.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import { useRetryMessage } from "@/features/chats/hooks/useSendMessage";
import { showToast } from "@/shared/ui/toast";
import type { MessageDto } from "@/shared/api/types";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders, seedEmptyThread } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

const ID = "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59";

/** Серверный failed: у него `id` не равен `client_message_id` — повтор идёт в /retry. */
function failedServer(): MessageDto {
  return {
    id: "srv-9",
    conversation_id: CONV_ID,
    direction: "out",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: "Замена экрана — от 8 900 ₽",
    attachments: [],
    delivery_status: "failed",
    client_message_id: ID,
    created_at: "2026-08-05T10:12:00Z",
    delivery_error: "Авито: чат недоступен",
  };
}

function RetryHost({ msg }: { msg: MessageDto }) {
  const retry = useRetryMessage(CONV_ID);
  return (
    <MessageBubble msg={msg} prev={null} clientId="client-1" clientName="Иван" onRetry={(m) => retry.mutate(m)} />
  );
}

describe("Отказ повтора говорит словами, а не крестиком", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedEmptyThread();
    vi.mocked(showToast).mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("на 409 «канал требует переподключения» показывает причину от сервера", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          409,
          errorEnvelope(
            "account_needs_reauth",
            "Аккаунт Авито требует переподключения — сообщения не уходят",
          ),
        ),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<RetryHost msg={failedServer()} />);
    await user.click(screen.getByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const аргумент = vi.mocked(showToast).mock.calls[0][0];
    expect(
      аргумент.message,
      "человеку не сказали причину — повтор снова выглядит как «мигнуло и всё»",
    ).toContain("требует переподключения");
    expect(аргумент.color).toBe("red");
  });

  it("когда сервер не ответил вовсе — своя фраза, а не пустота", async () => {
    /*
     * ⚠ ВТОРАЯ ВЕТКА ОБЯЗАТЕЛЬНА. Текст берётся у сервера, а при обрыве связи
     * ответа нет: без этой ветки человек снова остался бы без объяснения —
     * ровно в том случае, где повтор как раз и помогает.
     */
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const user = userEvent.setup();
    renderWithProviders(<RetryHost msg={failedServer()} />);
    await user.click(screen.getByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const аргумент = vi.mocked(showToast).mock.calls[0][0];
    expect(String(аргумент.message).length, "при обрыве связи сообщение пустое").toBeGreaterThan(10);
  });
});
