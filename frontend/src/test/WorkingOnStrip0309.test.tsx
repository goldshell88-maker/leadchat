import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { WorkingOnStrip } from "@/features/chats/components/thread/WorkingOnStrip";
import type { ConversationDetailDto } from "@/shared/api/types";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

/**
 * «В РАБОТЕ У …» — ОДНА СТРОКА, КОТОРУЮ ВИДЯТ ОБА.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 03.09: «сделай только индикацию, что диалог в работе у
 * … Чтобы оба человека понимали, что у кого в работе».
 *
 * До этого единственная надпись про хозяина диалога жила в заслоне над полем
 * ввода: её видел только тот, кто в заслон упёрся, и только в двух ветках
 * подвала из четырёх. Снять заслон и не поставить эту строку значило бы
 * оставить экран, на котором ответа «чей это диалог» нет вообще.
 */
const КОЛЛЕГА = { id: "u-anna", full_name: "Анна Иванова" };
const ВТОРОЙ = { id: "u-boris", full_name: "Борис Мастер", reason: null, invited_at: "2026-09-03T10:00:00Z" };

function диалог(over: Partial<ConversationDetailDto> = {}): ConversationDetailDto {
  return { ...makeConversation(), status: "in_progress", ...over } as ConversationDetailDto;
}

function стенд(conv: ConversationDetailDto) {
  resetSessionStore({
    user: fakeUser,
    permissions: fakeMe.permissions as never,
    accessToken: "t",
    bootstrapped: true,
  });
  renderWithProviders(<WorkingOnStrip conversation={conv} />);
}

describe("Строка «в работе у …»", () => {
  it("в чужом диалоге называет хозяина", () => {
    стенд(диалог({ assignee: КОЛЛЕГА }));
    expect(screen.getByText(/В работе у Анна Иванова/)).toBeInTheDocument();
  });

  it("вошедший видит, что он тут же", () => {
    стенд(диалог({ assignee: КОЛЛЕГА, participants: [{ id: fakeUser.id, full_name: "Я", reason: null, invited_at: "2026-09-03T10:00:00Z" }] }));
    expect(screen.getByText(/В работе у Анна Иванова · здесь же вы/)).toBeInTheDocument();
  });

  it("ХОЗЯИН ВИДИТ ГОСТЯ — вторая половина «оба понимают»", () => {
    /*
     * Без этой ветки индикация была бы односторонней: гость знает, к кому
     * зашёл, а хозяин не знает, что к нему зашли. Владелец просил обратного.
     */
    стенд(диалог({ assignee: { id: fakeUser.id, full_name: "Я" }, participants: [ВТОРОЙ] }));
    expect(screen.getByText(/В работе у вас · здесь же Борис Мастер/)).toBeInTheDocument();
  });

  it("свой диалог без гостей строки не показывает", () => {
    /*
     * Строка появляется, когда есть новость. Постоянная подпись «в работе у
     * вас» над собственным диалогом — шум, который перестают замечать, и
     * вместе с ним перестают замечать случай, когда диалог всё-таки чужой.
     */
    стенд(диалог({ assignee: { id: fakeUser.id, full_name: "Я" } }));
    expect(screen.queryByText(/В работе/)).toBeNull();
  });

  it("у закрытого диалога хозяина нет — строки тоже", () => {
    /* Закрытый диалог ничей: его ответственный — память о том, кто вёл. */
    стенд(диалог({ assignee: КОЛЛЕГА, status: "closed" }));
    expect(screen.queryByText(/В работе/)).toBeNull();
  });

  it("у ничьего диалога строки нет", () => {
    стенд(диалог({ assignee: null }));
    expect(screen.queryByText(/В работе/)).toBeNull();
  });
});
