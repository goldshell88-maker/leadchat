import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { qk } from "@/shared/api/queryKeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * ПОДСКАЗКА ЗАМЕНЯЕТ НАБРАННОЕ, А НЕ ДОПИСЫВАЕТСЯ К НЕМУ.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 28.08 дословно: «подсказки дописывают в начале слова».
 *
 * Полоса быстрых ответов работает автодополнением: человек пишет «Здра», и
 * набранное — это ЗАПРОС, а не текст сообщения. Вставка же шла общим путём с
 * пикером шаблонов, который дописывает в позицию курсора и набранное сохраняет.
 * Получалось «Здра Здравствуйте, чем помочь?» — и человек стирал первые четыре
 * буквы руками после каждой подсказки.
 *
 * У пикера (⚡) поведение остаётся прежним и это не оплошность: там набранное —
 * настоящий текст сообщения, а не запрос к списку.
 */

const ШАБЛОН = {
  id: "t1",
  owner_id: "u1",
  title: "Здравствуйте",
  body: "Здравствуйте! Чем можем помочь?",
  folder: null,
};

describe("Быстрый ответ и набранное слово", () => {
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
    queryClient.setQueryData(qk.templates.list("all"), {
      items: [ШАБЛОН],
      page: { limit: 50, offset: 0, total: 1 },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => ({ items: [ШАБЛОН], page: { limit: 50, offset: 0, total: 1 } }),
      }) as unknown as Response),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const поле = () => screen.getByRole("textbox", { name: "Текст сообщения" });

  function открыть() {
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
  }

  it("набранное слово ЗАМЕНЯЕТСЯ шаблоном, а не остаётся перед ним", async () => {
    открыть();
    await userEvent.type(поле(), "Здра");

    const строка = await screen.findByRole("option", { name: /Здравствуйте/ });
    await userEvent.click(строка);

    await waitFor(() => {
      expect((поле() as HTMLTextAreaElement).value).toBe("Здравствуйте! Чем можем помочь?");
    });
  });

  it("текст ПЕРЕД запросом сохраняется — заменяется только последнее слово", async () => {
    /*
     * Человек дописывает подсказку в середину мысли: «Добрый день. Здра» →
     * «Добрый день. Здравствуйте!…». Съешь мы всё поле, потерялось бы уже
     * написанное — а это хуже лишних букв: их видно, потерю нет.
     */
    открыть();
    await userEvent.type(поле(), "Спасибо за ожидание. Здра");

    await userEvent.click(await screen.findByRole("option", { name: /Здравствуйте/ }));

    await waitFor(() => {
      expect((поле() as HTMLTextAreaElement).value).toBe(
        "Спасибо за ожидание. Здравствуйте! Чем можем помочь?",
      );
    });
  });

  it("над пустым полем подсказки нет вовсе", async () => {
    /*
     * «Сейчас они перекрывают диалоги, когда открываешь диалог». Человек только
     * что открыл переписку и читает её — список ему на неё класть незачем.
     */
    открыть();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("listbox", { name: "Быстрые ответы" })).toBeNull();
  });
});
