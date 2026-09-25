import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * СОХРАНИТЬ ФРАЗУ В БЫСТРЫЕ ОТВЕТЫ ПРЯМО ИЗ ЧАТА — как в Jivo.
 *
 * ⚠ ЗАЧЕМ. Прежний путь: запомнить фразу, уйти в настройки, найти раздел,
 * нажать «Создать», набрать её заново, придумать название, сохранить,
 * вернуться в диалог. Результат виден в данных боя: личные заготовки за всё
 * время завели двое из пятидесяти семи человек.
 */
describe("Сохранение быстрого ответа из чата", () => {
  let создано: unknown = null;

  beforeEach(() => {
    создано = null;
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
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/templates") && init?.method === "POST") {
          создано = JSON.parse(String(init.body));
          return jsonResponse(201, { id: "new", owner_id: fakeUser.id, ...(создано as object) });
        }
        if (url.includes("/templates")) {
          return jsonResponse(200, { items: [], page: { limit: 200, offset: 0, total: 0 } });
        }
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("кнопки нет на обрывке и она есть на фразе", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const input = screen.getByLabelText("Текст сообщения");

    await user.click(input);
    await user.type(input, "ок");
    expect(screen.queryByLabelText("Сохранить как быстрый ответ")).toBeNull();

    await user.clear(input);
    await user.type(input, "Здравствуйте, чем могу помочь?");
    expect(await screen.findByLabelText("Сохранить как быстрый ответ")).toBeInTheDocument();
  });

  it("сохраняет набранное БЕЗ названия — оно берётся из текста", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.click(screen.getByLabelText("Текст сообщения"));
    await user.type(screen.getByLabelText("Текст сообщения"), "Подскажите модель телевизора");
    await user.click(await screen.findByLabelText("Сохранить как быстрый ответ"));

    // Текст уже в редакторе — набирать заново нечего, название необязательно.
    await user.click(await screen.findByRole("button", { name: "Сохранить" }));

    await waitFor(() =>
      expect(создано).toEqual({
        title: "Подскажите модель телевизора",
        body: "Подскажите модель телевизора",
        folder: null,
        shared: false,
      }),
    );
  });
});
