import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import type { ConversationDto } from "@/shared/api/types";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * ЧУЖОЙ ДИАЛОГ ОТКРЫТ ДЛЯ РАБОТЫ, А ЧЕЙ ОН — СКАЗАНО ОТДЕЛЬНОЙ СТРОКОЙ.
 *
 * ⚠ ЗДЕСЬ ЖИЛ ЗАСЛОН, И ОН СНЯТ 03.09 ПО ПРОСЬБЕ ВЛАДЕЛЬЦА: «сделай так,
 * чтобы можно было спокойно заходить в чужой диалог, который в работе у
 * другого человека, и он появлялся так же у тебя в „Мои". Сделай только
 * индикацию, что диалог в работе у … Чтобы оба человека понимали, что у кого
 * в работе».
 *
 * Заслон закрывал поле ввода до нажатия «Всё равно написать». Защищал он от
 * настоящей беды — случайного ответа в чужой разговор, — но мешал работать
 * вдвоём каждый раз, а узнать, чей это диалог, можно было только упёршись в
 * него. Теперь поле живое сразу, а ответ на вопрос «чей диалог» стоит под
 * шапкой ленты и виден обоим (`WorkingOnStrip`).
 */

const Я = fakeUser.id;
const КОЛЛЕГА = { id: "u-vorontsov", full_name: "Воронцов Александр" };

function диалог(over: Partial<ConversationDto> = {}): ConversationDto {
  return makeConversation({ status: "in_progress", ...over }) as unknown as ConversationDto;
}

describe("Чужой диалог", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  it("в чужой рабочий диалог поле ввода открыто сразу", () => {
    renderWithProviders(
      <ThreadFooter convId="conv-1" conversation={диалог({ assignee: КОЛЛЕГА })} />,
    );

    expect(
      screen.queryByPlaceholderText(/Напишите сообщение/),
      "в чужом диалоге снова нужно нажимать кнопку, чтобы написать",
    ).not.toBeNull();
    expect(screen.queryByText("Всё равно написать")).toBeNull();
  });

  it("ЗАКРЫТЫЙ диалог ничей: заслона нет, поле ввода живое", () => {
    /*
     * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 03.09: «когда диалог закрыт, то он по факту не чей…
     * сейчас из-за этого себе в работу нельзя забрать диалог».
     *
     * Ответственный у закрытого диалога остаётся в записи — по нему видно, кто
     * вёл разговор. Но работа закончена, и заслон, поставленный против
     * случайного вмешательства в ЧУЖУЮ работу, здесь мешал начать свою: клиент
     * вернулся, ответить может любой свободный, а поле закрыто — и закрепить
     * диалог за собой нечем, потому что закрепление происходит при наборе.
     */
    renderWithProviders(
      <ThreadFooter
        convId="conv-1"
        conversation={диалог({ assignee: КОЛЛЕГА, status: "closed" })}
      />,
    );

    expect(screen.queryByText(/Диалог ведёт/)).toBeNull();
    /*
     * Кнопка «Вернуть в работу» живёт ВНУТРИ композера, то есть за заслоном.
     * Пока заслон считал закрытый диалог чужим, до неё было не добраться: ни
     * написать, ни вернуть в работу, ни закрепить за собой.
     */
    expect(screen.getByRole("button", { name: "Вернуть в работу" })).toBeTruthy();
  });

  it("свой диалог заслон не трогает", () => {
    renderWithProviders(
      <ThreadFooter
        convId="conv-1"
        conversation={диалог({ assignee: { id: Я, full_name: fakeUser.full_name } })}
      />,
    );
    expect(screen.getByPlaceholderText(/Напишите сообщение/)).toBeTruthy();
  });

  it("ничей диалог тоже: там писать — значит брать его себе", () => {
    /*
     * Путь «ответил — значит принял» (01 §6.2) намеренно оставлен без вопросов:
     * ничей диалог никто не ведёт, и перебивать некого.
     */
    renderWithProviders(<ThreadFooter convId="conv-1" conversation={диалог({ assignee: null })} />);
    expect(screen.getByPlaceholderText(/Напишите сообщение/)).toBeTruthy();
  });

});
