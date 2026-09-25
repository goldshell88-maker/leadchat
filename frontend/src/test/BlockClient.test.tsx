import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { BlockClientDialog } from "@/features/chats/components/card/BlockClientButton";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";
import { ThreadActions } from "@/features/chats/components/thread/ThreadActions";

/**
 * Пометка «нежелательный клиент» (чёрный список, docs/19).
 *
 * Проверяется не «уходит ли запрос». Проверяется, ЧТО ЧЕЛОВЕК ПОНИМАЕТ,
 * нажимая кнопку. Слово «заблокировать» читается как «он больше не сможет
 * писать», и оператор с таким пониманием перестанет следить за диалогом
 * вовсе — а сообщения продолжат приходить. Окно обязано сказать правду до
 * нажатия, а не после.
 */

describe("Пометка нежелательного клиента", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const posts = () =>
    fetchMock.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "POST")
      .map((c) => ({ url: String(c[0]), body: String((c[1] as RequestInit).body ?? "") }));

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:manage", "messages:send"],
      accessToken: "t",
      bootstrapped: true,
    });
    fetchMock = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("окно объясняет, что сообщения НЕ потеряются", async () => {
    // Половина смысла пометки. Без этой фразы человек понимает её как
    // «блокировку» и перестаёт смотреть на диалог — а сообщения приходят.
    renderWithProviders(
      <BlockClientDialog clientId="c-1" blocked={false} convId="conv-1" opened onClose={() => {}} />,
    );

    expect(await screen.findByText(/будут приходить и сохраняться/)).toBeInTheDocument();
    expect(screen.getByText(/не встанет в очередь/)).toBeInTheDocument();
  });

  it("причина уходит вместе с пометкой", async () => {
    // «Через полгода никто не вспомнит, кого и за что пометили» — поэтому
    // причина спрашивается сразу, а не «когда понадобится».
    const user = userEvent.setup();
    renderWithProviders(
      <BlockClientDialog clientId="c-1" blocked={false} convId="conv-1" opened onClose={() => {}} />,
    );

    await user.type(await screen.findByLabelText("Почему"), "Спам");
    await user.click(screen.getByRole("button", { name: "Пометить" }));

    await waitFor(() => {
      const p = posts().find((x) => x.url.endsWith("/clients/c-1/block"));
      expect(p).toBeTruthy();
      expect(JSON.parse(p!.body)).toEqual({ reason: "Спам" });
    });
  });

  it("снятие объясняет, что вернётся, а не спрашивает «точно?»", () => {
    // Состояние «нежелательный» с причиной теперь показывает карточка клиента
    // (это информация), а окно отвечает на другой вопрос: что произойдёт, если
    // пометку снять.
    renderWithProviders(
      <BlockClientDialog clientId="c-1" blocked convId="conv-1" opened onClose={() => {}} />,
    );

    expect(screen.getByText(/снова станет обычным/)).toBeInTheDocument();
    expect(screen.getByText(/вставать в очередь/)).toBeInTheDocument();
  });

  it("снятие пометки не спрашивает подтверждения", async () => {
    // Вернуть клиента в обычную работу безопасно: худшее, что случится, —
    // диалог снова встанет в очередь. Подтверждение у безопасного действия
    // приучает нажимать «да» не читая, и тогда оно не сработает там, где нужно.
    const user = userEvent.setup();
    renderWithProviders(
      <BlockClientDialog clientId="c-1" blocked convId="conv-1" opened onClose={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: "Снять пометку" }));

    await waitFor(() =>
      expect(posts().some((x) => x.url.endsWith("/clients/c-1/unblock"))).toBe(true),
    );
    // Второго шага нет: одно нажатие — и пометка снята. Закрытием окна
    // управляет тот, кто его открыл, поэтому здесь проверяется действие.
    expect(posts().filter((x) => x.url.includes("/clients/")).length).toBe(1);
  });

  it("наблюдателю кнопки нет вовсе", () => {
    // Не «есть, но запрещена»: пустая кнопка-обманка хуже её отсутствия.
    //
    // Пункт переехал в меню «…» шапки ленты (ThreadActions), и право
    // проверяется там же — вместе с соседними действиями. Проверка живёт
    // рядом с пунктом, а не рядом с окном, которое этот пункт открывает.
    //
    // У наблюдателя не рисуется даже сама кнопка «…»: меню, открывающееся
    // пустым, обещает возможность и тут же в ней отказывает (было FUNC-21 —
    // скринридер объявлял группу «Действия с диалогом» без единого действия).
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    renderWithProviders(
      <ThreadActions
        /*
          Диалог ЧУЖОЙ — так наблюдатель их и видит. Свой диалог у наблюдателя
          не заводится: назначить его некому, а закрепить у себя он вправе
          (это личная отметка, сервер её разрешает всем с `conversations:read`),
          и меню тогда законно покажет единственный пункт «Закрепить».
        */
        conversation={makeConversation({
          assignee: { id: "u-другой", full_name: "Пётр Ковалёв" },
        })}
        onInvite={() => {}}
        onTransfer={() => {}}
        onBlock={() => {}}
        onClose={() => {}}
        closePending={false}
      />,
    );
    expect(screen.queryByLabelText("Действия с диалогом")).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /нежелательного/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Позвать/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Передать/ })).not.toBeInTheDocument();
  });
});
