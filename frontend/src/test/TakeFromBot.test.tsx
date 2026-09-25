import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * «Забрать себе» — вмешаться в диалог бота, ничего не написав клиенту.
 *
 * Как это выглядело у владельца 29 августа. Диалог ведёт бот, сверху плашка
 * «ваше сообщение отключит его». Владелец хочет забрать диалог — посмотреть
 * переписку, дособрать данные, позвонить, — но НЕ хочет писать клиенту ради
 * того, чтобы бот замолчал. Другого способа не было: `POST /bots/{id}/disable`
 * выключает бота целиком, на всех диалогах сразу.
 *
 * Кнопка делает то же, что делает ответ, минус сам ответ.
 */

describe("Забрать диалог у бота", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  function ботВедёт() {
    const conv = makeConversation({ status: "new", assignee: null, bot_active: true });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
    seedEmptyThread();
    return conv;
  }

  it("кнопка стоит в плашке, пока диалог ведёт бот", () => {
    ботВедёт();
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    expect(screen.getByRole("button", { name: "Забрать себе" })).toBeInTheDocument();
  });

  it("без бота кнопки нет — забирать не у кого", () => {
    const conv = makeConversation({ status: "new", assignee: null, bot_active: false });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
    seedEmptyThread();
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    expect(screen.queryByRole("button", { name: "Забрать себе" })).not.toBeInTheDocument();
  });

  it("нажатие зовёт ручку мьюта и НЕ отправляет сообщение", async () => {
    const user = userEvent.setup();
    ботВедёт();
    const вызовы: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string | URL | Request, init?: RequestInit) => {
        const путь = String(typeof url === "string" ? url : (url as Request).url ?? url);
        вызовы.push(`${init?.method ?? "GET"} ${путь}`);
        return Promise.resolve(jsonResponse(200, { bot_active: false, taken: true }));
      }),
    );

    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await user.click(screen.getByRole("button", { name: "Забрать себе" }));

    await waitFor(() => {
      expect(вызовы.some((в) => в.includes("/bot/mute"))).toBe(true);
    });
    // Главное свойство кнопки: клиент ничего не получил.
    expect(вызовы.some((в) => /POST .*\/messages\b/.test(в))).toBe(false);
  });
});
