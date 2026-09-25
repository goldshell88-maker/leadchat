import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { useSendMessage, type SendVars } from "@/features/chats/hooks/useSendMessage";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders, seedEmptyThread, threadMessages } from "./render";

/**
 * УПАВШЕЕ СООБЩЕНИЕ ЖИВЁТ В ОДНОМ МЕСТЕ.
 *
 * ЧТО БЫЛО. При отказе (например 409 `account_needs_reauth`) делалось и то и
 * другое сразу: в ленте появлялся пузырь «Не отправилось · Повторить ·
 * Удалить», и тот же текст возвращался в пустое поле ввода. Рядом оказывались
 * два способа повторить одно сообщение, и это РАЗНЫЕ отправки: у той, что из
 * поля, новый `client_message_id`, то есть идемпотентность сервера её не
 * сдержит — клиент получает один ответ дважды. Плюс два места живут разное
 * время: пузырь оптимистичный, сервер сообщения не создавал, и F5 стирает его
 * бесследно, а поле переживает перезагрузку (черновики в localStorage).
 *
 * ЧТО ТЕПЕРЬ. Вернули текст в поле — пузырь убираем. Оставили пузырь — поле не
 * трогаем. Две ветки «пузырь остаётся» проверены в draftSurvival (поле уже
 * занято новым текстом; заметка из карточки клиента); здесь — основная и та,
 * что молча теряла бы файлы.
 */

const TEXT = "Мастер будет в 14:00, устроит?";

function SendHost({ vars }: { vars: SendVars }) {
  const send = useSendMessage(CONV_ID);
  return (
    <button type="button" onClick={() => send.mutate(vars)}>
      отправить
    </button>
  );
}

function renderAndSend(vars: SendVars) {
  const { container } = renderWithProviders(<SendHost vars={vars} />);
  act(() => (container.querySelector("button") as HTMLButtonElement).click());
}

describe("Отказ отправки", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(409, {
          error: { code: "account_needs_reauth", message: "Аккаунт Авито отключён" },
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("текст вернулся в поле — пузыря с «Повторить» в ленте больше нет", async () => {
    renderAndSend({ text: TEXT, isNote: false, tempId: "tmp-1", ownsDraft: true });

    await waitFor(() => expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe(TEXT));
    expect(threadMessages()).toHaveLength(0);
  });

  it("причина отказа названа словами сервера, а не молчанием", async () => {
    // Текст молча возвращался в поле, и человек жал Enter снова и снова.
    const тост = vi.spyOn(await import("@/shared/ui/toast"), "showToast");
    renderAndSend({ text: TEXT, isNote: false, tempId: "tmp-why", ownsDraft: true });

    await waitFor(() =>
      expect(тост).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Сообщение не отправлено",
          message: "Аккаунт Авито отключён",
        }),
      ),
    );
  });

  it("с вложением пузырь остаётся: переотправить файл умеет только «Повторить»", async () => {
    // Поле хранит один текст. Вернув его и убрав пузырь, мы бы молча потеряли
    // уже загруженный файл — а к заметке его прикладывают ради содержимого.
    renderAndSend({
      text: "смета",
      isNote: true,
      tempId: "tmp-2",
      ownsDraft: true,
      attachments: [{ media_id: "m-1", kind: "image", url: "/api/v1/media/m-1", name: "смета.png", size: 10 }],
    });

    await waitFor(() => expect(threadMessages()[0]?.delivery_status).toBe("failed"));
    expect(threadMessages()[0].attachments).toHaveLength(1);
    expect(useChatUiStore.getState().drafts[CONV_ID]?.text ?? "").toBe("");
  });
});
