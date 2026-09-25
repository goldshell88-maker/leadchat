import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ThreadActions } from "@/features/chats/components/thread/ThreadActions";
import { fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

/**
 * «Вернуть в очередь» — исправление принятого по ошибке диалога (#33).
 *
 * Ручка `POST /conversations/{id}/release` жила на сервере с самого начала, а
 * нажать её было нечем: в интерфейсе не было ни кнопки, ни вызова. Диспетчер,
 * ткнувший «Принять» не в ту строку — а очередь обновляется на глазах, и
 * строки съезжают из-под пальца, — оставался с чужим диалогом в «Моих».
 * У остальных двенадцати диалог из очереди исчезал, и клиент ждал человека,
 * который не собирался ему отвечать. Обойти это можно было только через
 * руководителя, то есть отвлечь второго человека ради чужой описки.
 *
 * Проверяется здесь не только наличие пункта, но и ГРАНИЦЫ: у кого его быть не
 * должно. Пункт, который всегда отвечает 403, хуже отсутствующего — он
 * обещает возможность, которой нет.
 *
 * С 12 августа действие живёт в меню «…», а не отдельной иконкой в шапке,
 * поэтому каждая проверка сперва открывает меню. Это не обход теста, а часть
 * проверяемого: пункт обязан быть ДОСТИЖИМ, а не просто существовать.
 */
describe("ThreadActions — вернуть в очередь", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      /*
       * ⚠ `conversations:release` — ОТДЕЛЬНОЕ ПРАВО С 28.08 (решение владельца:
       * «вернуть в очередь мог только бот или администратор, у менеджеров эту
       * функцию отключи и удали, чтобы её не было»). Раньше пункт висел на
       * `messages:send`, то есть был у каждого, кто умеет отвечать клиенту.
       */
      permissions: ["conversations:read", "messages:send", "conversations:release"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  const noop = () => {};

  function renderActions(conversation = makeConversation()) {
    return renderWithProviders(
      <ThreadActions
        conversation={conversation}
        onInvite={noop}
        onTransfer={noop}
        onBlock={noop}
        onClose={noop}
        closePending={false}
      />,
    );
  }

  /** Меню «…» — единственный вход во вторичные действия над диалогом. */
  async function openMenu() {
    await userEvent.click(screen.getByLabelText("Действия с диалогом"));
  }

  it("держатель диалога видит пункт и возвращает диалог одним нажатием", async () => {
    const fetchMock = vi.fn(
      async () =>
        ({
          ok: true,
          status: 200,
          json: async () => ({
            conversation: makeConversation({ status: "new", assignee: null }),
            count: 4,
            escalated: 0,
          }),
        }) as Response,
    );
    vi.stubGlobal("fetch", fetchMock);

    renderActions();
    await openMenu();
    await userEvent.click(await screen.findByRole("menuitem", { name: "Вернуть в очередь" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = String(fetchMock.mock.calls.at(0)?.at(0));
    expect(url).toContain("/release");
  });

  it("позванный коллега пункта не видит: держит диалог не он", async () => {
    renderActions(
      makeConversation({
        assignee: { id: "u-другой", full_name: "Пётр Ковалёв" },
        participants: [{ id: fakeUser.id, full_name: fakeUser.full_name }],
      } as never),
    );
    await openMenu();
    // Меню открыто и не пусто — значит проверка смотрит на живое меню, а не на
    // отсутствие меню вообще.
    expect(await screen.findByRole("menuitem", { name: "Позвать коллегу" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Вернуть в очередь" })).not.toBeInTheDocument();
  });

  it("у закрытого диалога пункта нет: возвращать в очередь нечего", async () => {
    renderActions(makeConversation({ status: "closed" }));
    await openMenu();
    expect(await screen.findByRole("menuitem", { name: "Позвать коллегу" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Вернуть в очередь" })).not.toBeInTheDocument();
  });

  it("у МЕНЕДЖЕРА пункта нет — даже в своём диалоге", async () => {
    /*
     * ⚠ РЕШЕНИЕ ВЛАДЕЛЬЦА 28.08 дословно: «сделай, чтобы вернуть в очередь мог
     * только бот или администратор, у менеджеров эту функцию отключи и удали,
     * чтобы её не было».
     *
     * Возврат снимает ответственного и отдаёт тринадцати диалог, с которым
     * человек уже поговорил: клиент получает второго собеседника с нуля, а
     * история разговора остаётся за прежним. Для «я сейчас занят» у оператора
     * есть «Отклонить» — оно про диалог, ЕЩЁ не начатый.
     */
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send"], // без conversations:release
      accessToken: "t",
      bootstrapped: true,
    });
    renderActions();
    await openMenu();
    // ⚠ СНАЧАЛА ДОЖДАТЬСЯ ПРИСУТСТВУЮЩЕГО, ПОТОМ ОТРИЦАТЬ ОТСУТСТВУЮЩЕЕ.
    // Первая редакция спрашивала про «Вернуть в очередь» сразу после клика — а
    // меню Mantine к этому моменту ещё пустое, и проверка зеленела ВСЕГДА:
    // диверсия «вернуть пункт всем отвечающим» её не роняла.
    expect(await screen.findByRole("menuitem", { name: "Позвать коллегу" })).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: "Вернуть в очередь" }),
      "менеджер снова видит возврат в очередь",
    ).not.toBeInTheDocument();
  });

  it("наблюдатель не видит даже кнопки «…» — он не может брать диалоги вовсе", () => {
    // Было FUNC-21: скринридер объявлял группу «Действия с диалогом», внутри
    // которой не было ни одного действия. Пустое меню обещает возможность и
    // тут же в ней отказывает.
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    // Диалог ЧУЖОЙ — так наблюдатель их и видит. Свой диалог у наблюдателя не
    // заводится, а закрепить у себя он вправе (личная отметка, сервер её
    // разрешает всем с `conversations:read`), и меню тогда законно осталось бы.
    renderActions(makeConversation({ assignee: { id: "u-другой", full_name: "Пётр Ковалёв" } }));
    expect(screen.queryByLabelText("Действия с диалогом")).not.toBeInTheDocument();
  });
});
