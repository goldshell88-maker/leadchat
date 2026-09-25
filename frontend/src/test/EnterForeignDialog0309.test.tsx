import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import { WorkingOnStrip } from "@/features/chats/components/thread/WorkingOnStrip";
import type { ConversationDetailDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * ВХОД В ЧУЖОЙ ДИАЛОГ — ПОЛОСОЙ ВНИЗУ, КАК «ПРИНЯТЬ ДИАЛОГ» (просьба владельца
 * 04.09: «кнопку „Войти в диалог" сделай по аналогии, как сделано „Принять
 * диалог", с такой же защитой от написания текста, как и было раньше»).
 *
 * ⚠ ДВЕ ПРЕДЫДУЩИЕ РЕДАКЦИИ И ПОЧЕМУ ОНИ НЕ ГОДИЛИСЬ. 03.09 вход стал
 * автоматическим — через две секунды чтения; «Мои» начали собираться из истории
 * просмотров, и закрытие такого диалога закрывало разговор клиенту у коллеги.
 * 04.09 вход стал кнопкой, но она стояла В СТРОКЕ ПОД ШАПКОЙ — то есть далеко
 * от того места, куда человек тянется, когда хочет ответить, и поле ввода при
 * этом оставалось открытым: защиты от случайного ответа в чужой разговор не
 * было вовсе.
 *
 * Теперь полоса стоит НА МЕСТЕ ПОЛЯ ВВОДА: она и объясняет, почему писать
 * нельзя, и даёт единственное действие, которое это меняет.
 */
function диалог(over: Partial<ConversationDetailDto> = {}): ConversationDetailDto {
  return {
    ...makeConversation(),
    status: "in_progress",
    assignee: { id: "u-anna", full_name: "Анна Иванова" },
    participants: [],
    ...over,
  } as ConversationDetailDto;
}

const МОЁ_УЧАСТИЕ = {
  id: fakeUser.id,
  full_name: "Я",
  reason: null,
  invited_at: "2026-09-04T10:00:00Z",
};

describe("Вход в чужой диалог", () => {
  let входы: string[] = [];

  beforeEach(() => {
    входы = [];
    queryClient.clear();
    useChatUiStore.setState({ drafts: {} });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "POST" && url.includes("/enter")) {
          входы.push(url);
          return jsonResponse(200, { entered: true, participants: [] });
        }
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  function подвал(conversation: ConversationDetailDto) {
    return renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={conversation} />);
  }

  it("в чужом рабочем диалоге поля ввода нет — вместо него полоса с кнопкой", () => {
    подвал(диалог());

    expect(screen.getByRole("button", { name: "Войти в диалог" })).toBeInTheDocument();
    // ⚠ ЗАЩИТА ОТ НАПИСАНИЯ — ЭТО ОТСУТСТВИЕ ПОЛЯ, А НЕ СЕРАЯ КНОПКА. Ровно так
    // же ведёт себя полоса очереди: композер не монтируется вовсе.
    expect(screen.queryByRole("textbox")).toBeNull();
    // Причина запрета названа там же, где запрет: сообщение в Авито не отзывают.
    expect(screen.getByText(/отозвать его в Авито нельзя/)).toBeInTheDocument();
    expect(screen.getByText(/Диалог ведёт Анна Иванова/)).toBeInTheDocument();
  });

  it("нажатие записывает вошедшего", async () => {
    const user = userEvent.setup();
    подвал(диалог());

    await user.click(screen.getByRole("button", { name: "Войти в диалог" }));

    await waitFor(() => expect(входы).toHaveLength(1));
    expect(входы[0]).toContain(`/conversations/${CONV_ID}/enter`);
  });

  it("НИЧЕГО НЕ ПРОИСХОДИТ САМО: пока не нажали — запросов нет", async () => {
    /*
     * ⚠ ГЛАВНОЕ СВОЙСТВО ПРАВКИ. Порядок 03.09 записывал человека через две
     * секунды чтения, и «Мои» собирались из истории просмотров: пролистал
     * список стрелками — набрал десяток чужих диалогов.
     */
    подвал(диалог());
    await new Promise((r) => setTimeout(r, 50));
    expect(входы).toEqual([]);
  });

  it("вошедшему полоса больше не мешает: поле ввода на месте", () => {
    подвал(диалог({ participants: [МОЁ_УЧАСТИЕ] }));

    expect(screen.queryByRole("button", { name: "Войти в диалог" })).toBeNull();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("в своём диалоге полосы нет — он и так мой", () => {
    подвал(диалог({ assignee: { id: fakeUser.id, full_name: "Я" } }));

    expect(screen.queryByRole("button", { name: "Войти в диалог" })).toBeNull();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("недописанный ответ полоса не забирает", () => {
    /*
     * ⚠ ТОТ ЖЕ ДОВОД, ЧТО У ОЧЕРЕДИ (30.08). Кадр о смене ответственного
     * приезжает посреди набора; подмени мы поле полосой — набранное исчезло бы
     * с экрана вместе с фокусом, и следующая клавиша сработала бы как команда.
     */
    useChatUiStore.setState({ drafts: { [CONV_ID]: { text: "уже написал половину" } } as never });
    подвал(диалог());

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Войти в диалог" })).toBeNull();
  });

  it("пока участники не приехали, поле не отбирают", () => {
    /*
     * У строки списка поля `participants` нет вовсе, и деталь приезжает вторым
     * запросом. Считай мы «нет поля» за «меня там нет» — вошедший терял бы поле
     * ввода на пол-секунды при каждом открытии.
     */
    const строкаСписка = диалог();
    delete (строкаСписка as Partial<ConversationDetailDto>).participants;
    подвал(строкаСписка);

    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("строка под шапкой осталась индикацией: кнопок в ней нет", () => {
    /*
     * «Войти» переехала вниз, «Выйти» снята вовсе (решение владельца 04.09:
     * «мне не нужна лишняя кнопка „Выйти", для себя я нажму Ctrl+D и выйду»).
     */
    renderWithProviders(<WorkingOnStrip conversation={диалог({ participants: [МОЁ_УЧАСТИЕ] })} />);

    expect(screen.getByText(/В работе у Анна Иванова/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
