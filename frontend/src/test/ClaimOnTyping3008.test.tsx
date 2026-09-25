import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import type { ConversationDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * «НАЧАЛ ПИСАТЬ — ДИАЛОГ ТВОЙ» (просьба владельца 30.08, шаг 1 п. 1).
 *
 * ЗАЧЕМ. Незакреплённый диалог открыт у двоих сразу: заслон перед чужим
 * диалогом молчит, пока хозяина нет, и поле ввода живое у обоих. Оба набирают
 * ответ одному клиенту, и лишним оказывается тот, кто нажал «отправить»
 * вторым. Сервер закрепляет диалог отправкой, но окно между «начал печатать»
 * и «отправил» — самое длинное — ничем не закрыто.
 *
 * ⚠ ПОЧЕМУ НЕ `onFocus`, КАК ПРОСИЛИ. Фокус в этом приложении ставится
 * программно из шести мест: после приёма из очереди, горячей клавишей «фокус в
 * поле», при включении режима заметки, при возврате в свой же диалог. Правило
 * «фокус = закрепление» раздавало бы клиентов от нажатия клавиши — ровно тот
 * класс дефекта, что разбирался тем же утром, когда случайная клавиша приняла
 * на менеджера постороннего человека. Набранный символ намерение выражает
 * однозначно; отдельная проверка ниже стережёт, что один фокус ничего не берёт.
 */

function незакреплённый(): ConversationDto {
  return { ...makeConversation(), assignee: null };
}

describe("Закрепление по началу набора", () => {
  const urls: string[] = [];

  beforeEach(() => {
    urls.length = 0;
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
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        if (url.includes("/claim")) {
          return jsonResponse(200, { conversation: { ...незакреплённый(), assignee: { id: fakeUser.id, full_name: fakeUser.full_name } }, inbox_count: 0 });
        }
        return jsonResponse(200, { items: [], page: { limit: 50, has_more: false, next_cursor: null } });
      }),
    );
  });

  const приёмов = () => urls.filter((u) => u.includes(`/conversations/${CONV_ID}/claim`)).length;

  it("набранный символ закрепляет незакреплённый диалог", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={незакреплённый()} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Здр");

    expect(приёмов(), "диалог не закрепился — двое продолжат писать одному клиенту").toBe(1);
  });

  it("один фокус без набора не берёт диалог", async () => {
    /*
     * ⚠ ГЛАВНАЯ ПРОВЕРКА. Владелец просил закреплять по `onFocus`; здесь
     * записано, почему этого делать нельзя. Фокус ставится программно из шести
     * мест, и «фокус = закрепление» раздавало бы клиентов от нажатия клавиши.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={незакреплённый()} />);

    await user.click(screen.getByLabelText("Текст сообщения"));

    expect(приёмов(), "диалог взят от одного лишь щелчка в поле ввода").toBe(0);
  });

  it("свой диалог второй раз не принимают", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Здр");

    expect(приёмов(), "лишний запрос приёма на каждый свой диалог").toBe(0);
  });

  it("заметку пишут без взятия клиента на себя", async () => {
    // Заметку видят только сотрудники: написать её про чужой разговор — не то
    // же самое, что взять клиента себе.
    useChatUiStore.setState({ drafts: { [CONV_ID]: { text: "", isNote: true } } });
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={незакреплённый()} />);

    await user.type(screen.getByLabelText("Текст заметки"), "Перезвонить");

    expect(приёмов(), "заметка закрепила клиента").toBe(0);
  });

  it("длинный текст закрепляет ровно один раз", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={незакреплённый()} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Здравствуйте, мастер подъедет");

    expect(приёмов(), "приём уходит на каждый символ — это шквал запросов").toBe(1);
  });
});
