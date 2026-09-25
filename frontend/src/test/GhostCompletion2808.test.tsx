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
 * ПОЛЕ ДОРИСОВЫВАЕТ ПРОДОЛЖЕНИЕ САМО.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 28.08: «сделай так, чтобы быстрые сообщения сами
 * автоматически заполнялись».
 *
 * ⚠ ПОЧЕМУ ПРИЗРАК, А НЕ НАСТОЯЩАЯ ПОДСТАНОВКА В ТЕКСТ — ГЛАВНОЕ РЕШЕНИЕ ЭТОГО
 * ФАЙЛА. Дописать в поле по-настоящему значит однажды отправить клиенту то,
 * чего человек не писал: набрал «Здра», отвлёкся на ленту, нажал Enter — и ушло
 * чужое предложение целиком. Отозвать сообщение в Авито НЕЛЬЗЯ.
 *
 * Поэтому продолжение видно, но текстом становится только по Tab. Внешне это то
 * самое «само заполняется»; по сути — ни одной буквы в поле без нажатия.
 */

const ШАБЛОН = {
  id: "t1",
  owner_id: "u1",
  title: "Здравствуйте",
  body: "Здравствуйте! Чем можем помочь?",
  folder: null,
};

const ДРУГОЙ = {
  id: "t2",
  owner_id: "u1",
  title: "Выезд",
  body: "Мастер приедет сегодня",
  folder: null,
};

describe("Призрачное продолжение в поле", () => {
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
      items: [ШАБЛОН, ДРУГОЙ],
      page: { limit: 50, offset: 0, total: 2 },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => ({
          items: [ШАБЛОН, ДРУГОЙ],
          page: { limit: 50, offset: 0, total: 2 },
        }),
      }) as unknown as Response),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const поле = () => screen.getByRole("textbox", { name: "Текст сообщения" }) as HTMLTextAreaElement;
  const призрак = () => document.querySelector(".composer__ghost-tail");

  function открыть() {
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
  }

  it("после «Здра» поле показывает продолжение серым", async () => {
    открыть();
    await userEvent.type(поле(), "Здра");
    await waitFor(() => {
      expect(призрак()?.textContent).toBe("вствуйте! Чем можем помочь?");
    });
  });

  it("ПОЛЕ ПРИ ЭТОМ НЕ ТРОНУТО — в нём ровно то, что набрали", async () => {
    /*
     * Сердце проверки. Призрак живёт в зеркале под полем; окажись он в
     * `value`, отправка ушла бы клиенту целиком по случайному Enter.
     */
    открыть();
    await userEvent.type(поле(), "Здра");
    await waitFor(() => expect(призрак()).not.toBeNull());
    expect(поле().value, "продолжение попало в поле — Enter отправит его клиенту").toBe("Здра");
  });

  it("Tab принимает продолжение — и вот теперь оно в поле", async () => {
    открыть();
    await userEvent.type(поле(), "Здра");
    await waitFor(() => expect(призрак()).not.toBeNull());

    await userEvent.tab();
    await waitFor(() => {
      expect(поле().value).toBe("Здравствуйте! Чем можем помочь?");
    });
  });

  it("совпало НЕ С НАЧАЛА — призрака нет", async () => {
    /*
     * «Выез» находит «Мастер приедет сегодня» по слову в середине. Серые буквы,
     * не продолжающие набранное, читались бы как опечатка поля: человек видит
     * «Выезмастер приедет сегодня».
     */
    открыть();
    await userEvent.type(поле(), "приед");
    await new Promise((r) => setTimeout(r, 30));
    expect(призрак()).toBeNull();
  });

  it("одной буквы мало и для призрака", async () => {
    открыть();
    await userEvent.type(поле(), "З");
    await new Promise((r) => setTimeout(r, 30));
    expect(призрак()).toBeNull();
  });

  it("призрак не читается программой чтения с экрана", async () => {
    /*
     * Иначе она объявила бы непринятое предложение как уже набранный текст, и
     * незрячий оператор отправил бы чужую фразу, будучи уверенным, что написал
     * своё.
     */
    открыть();
    await userEvent.type(поле(), "Здра");
    await waitFor(() => expect(призрак()).not.toBeNull());
    expect(document.querySelector(".composer__ghost")?.getAttribute("aria-hidden")).toBe("true");
  });
});
