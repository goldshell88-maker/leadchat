import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import type { ConversationDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * ЧУЖОЕ ЗАКРЫТИЕ НЕ ЗАБИРАЕТ ПОЛЕ ИЗ-ПОД РУКИ.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 03.09: «сделай так, чтобы другой человек мог спокойно у
 * меня его закрыть и чтобы это никак не помешало другому человеку».
 *
 * Ветка «Диалог закрыт» — ранний выход ДО всей разметки подвала: фоновый кадр
 * о закрытии уносил с экрана поле вместе с набранным ответом, полосу быстрых
 * ответов, вложения и фокус. Текст оставался цел в черновике, но человек этого
 * не знает — он видит, что панель под руками стала другой.
 *
 * Тот же урок в подвале уже записан для очереди (30.08); тогда ветку закрытия
 * не тронули, потому что закрытие считали действием самого хозяина.
 */
function диалог(over: Partial<ConversationDto> = {}): ConversationDto {
  return { ...makeConversation(), status: "in_progress", ...over } as ConversationDto;
}

/**
 * Подвал плюс кнопка «пришёл кадр о закрытии». Имя латиницей: правило
 * react-hooks/rules-of-hooks узнаёт компонент по заглавной ЛАТИНСКОЙ букве.
 *
 * `rerender` из хелпера не годится: он подставляет голый элемент мимо
 * провайдеров, и запрос заготовок падает без клиента.
 */
function Harness() {
  const [закрыт, setЗакрыт] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setЗакрыт(true)}>
        закрыл коллега
      </button>
      <Composer
        convId={CONV_ID}
        conversation={диалог(закрыт ? { status: "closed" } : {})}
      />
    </>
  );
}

describe("Диалог закрыли, пока человек писал", () => {
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
      vi.fn(async () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } })),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it("набранный ответ и поле остаются на месте", async () => {
    renderWithProviders(<Harness />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Текст сообщения"), "Сейчас уточню и вернусь");
    await user.click(screen.getByRole("button", { name: "закрыл коллега" }));

    const поле = screen.queryByLabelText("Текст сообщения") as HTMLTextAreaElement | null;
    expect(поле, "поле ввода исчезло вместе с набранным ответом").not.toBeNull();
    expect(поле?.value).toBe("Сейчас уточню и вернусь");
  });

  it("отправить нельзя, и сказано почему", async () => {
    renderWithProviders(<Harness />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Текст сообщения"), "Сейчас уточню");
    await user.click(screen.getByRole("button", { name: "закрыл коллега" }));

    expect(screen.getByText(/Диалог закрыт — верните в работу/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить сообщение" })).toBeDisabled();
  });

  it("ЗАЩЁЛКА: стёр набранное — поле не выдёргивается из-под пальцев", async () => {
    /*
     * ⚠ БЕЗ ЗАЩЁЛКИ РЕШЕНИЕ ПЕРЕСЧИТЫВАЛОСЬ БЫ НА КАЖДУЮ БУКВУ. Человек стирает
     * написанное, чтобы начать заново, — и панель под пальцами превращается в
     * плашку. Решение принимается один раз на диалог.
     */
    renderWithProviders(<Harness />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Текст сообщения"), "Сейчас");
    await user.click(screen.getByRole("button", { name: "закрыл коллега" }));
    await user.clear(screen.getByLabelText("Текст сообщения"));

    expect(screen.queryByLabelText("Текст сообщения")).not.toBeNull();
  });

  it("а на пустом поле всё как раньше: плашка «Диалог закрыт»", () => {
    /*
     * Обратная половина. Оставь мы поле всегда — человек, открывший закрытый
     * диалог просто посмотреть, получал бы живое поле, из которого нельзя
     * отправить, вместо честной плашки с кнопкой возврата.
     */
    renderWithProviders(<Composer convId={CONV_ID} conversation={диалог({ status: "closed" })} />);

    expect(screen.getByText("Диалог закрыт.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Текст сообщения")).toBeNull();
  });
});
