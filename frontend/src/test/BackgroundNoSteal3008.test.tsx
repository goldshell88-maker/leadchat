import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { поПлану } from "@/features/chats/components/list/listOrder";
import { useFocusBus } from "@/features/hotkeys/focusBus";
import type { ConversationDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * ФОНОВОЕ СОБЫТИЕ НЕ ТРОГАЕТ ТОГО, КТО ПИШЕТ (разбор 30.08).
 *
 * ⚠ ЗДЕСЬ ИСПРАВЛЯЕТСЯ МОЙ СОБСТВЕННЫЙ НЕВЕРНЫЙ ВЫВОД. Разбирая жалобу «чат
 * сам переключается на другого клиента», я заключил, что фоновые события экран
 * увести не могут: активный диалог пишется всего из двух мест. Рассуждение было
 * верным, вывод — нет. Экран уводит не только запись активного диалога, но и
 * ПОДМЕНА ТОГО, ЧТО ПОД НИМ НАРИСОВАНО:
 *
 *  • сервер сам возвращает диалог в очередь (протухший ключ присутствия, три
 *    минуты без пинга, свёрнутая вкладка) — поле ввода подменяется панелью
 *    «Принять/Отклонить», набранное исчезает с экрана, фокус падает на страницу;
 *  • руководитель раскидывает смену — кадр проставляет ответственного, и заслон
 *    чужого диалога захлопывает поле прямо под руками;
 *  • композер, смонтировавшись заново от такого кадра, забирает фокус себе — в
 *    том числе из поля поиска, где человек набирал фамилию;
 *  • список пересортировывается под курсором, и щелчок попадает в соседа.
 *
 * Ни одно из четырёх не требует нажатия. Исходная формулировка владельца —
 * «никакие фоновые запросы не должны переключать экран или фокус ввода» —
 * описывала происходящее точнее моего разбора.
 */

function строка(id: string, at: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: id === "conv-A" ? "Ольга" : "Пётр", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: { body: "текст", direction: "in", created_at: at },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: at,
  };
}

describe("Фоновое событие не трогает того, кто пишет", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID, filters: { tab: "all" } });
    useInboxStore.getState().clear?.();
    seedEmptyThread();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } })));
  });

  it("композер не отнимает каретку у другого живого поля", () => {
    /*
     * ⚠ ГЛАВНОЕ ПРО ФОКУС. Эффект фокусировки срабатывает и на монтировании, а
     * счётчик просьб после первого открытия диалога за смену навсегда больше
     * нуля. Значит фокус забирало любое перемонтирование — а его устраивает
     * серверный кадр, без участия человека.
     */
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    const поиск = document.createElement("input");
    document.body.appendChild(поиск);
    поиск.focus();

    /*
     * ⚠ ЧЕРЕЗ `act`, И ЭТО НЕ ФОРМАЛЬНОСТЬ. Без него обновление стора не
     * доводится до эффекта, фокус никуда не встаёт — и проверка проходит,
     * ничего не проверив. Поймано соседней проверкой ниже, которая ждала
     * ОБРАТНОГО и потому упала.
     */
    act(() => useFocusBus.getState().requestComposerFocus());

    expect(
      document.activeElement,
      "каретку выдернуло из чужого поля — остаток набранного уйдёт клиенту",
    ).toBe(поиск);
  });

  it("но с пустого места фокус берёт — «принял, сразу пиши» цело", () => {
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);
    (document.activeElement as HTMLElement | null)?.blur();

    act(() => useFocusBus.getState().requestComposerFocus());

    expect(document.activeElement, "после приёма каретка больше не встаёт в поле").toBe(
      screen.getByLabelText("Текст сообщения"),
    );
  });

  it("возврат диалога в очередь не забирает поле с набранным ответом", () => {
    useChatUiStore.setState({ drafts: { [CONV_ID]: { text: "Здравствуйте, мастер", isNote: false } } });
    useInboxStore.setState({ ids: { [CONV_ID]: true } } as never);

    renderWithProviders(
      <ThreadFooter convId={CONV_ID} conversation={{ ...makeConversation(), assignee: null, in_inbox: true } as ConversationDto} />,
    );

    expect(
      screen.queryByLabelText("Текст сообщения"),
      "поле ввода исчезло из-под набора: дальше любая клавиша работает как команда",
    ).not.toBeNull();
  });

  it("в чужой рабочий диалог поле ввода открыто сразу", async () => {
    /*
     * ⚠ ЗДЕСЬ БЫЛО ДВА ТЕСТА ЗАСЛОНА, И ОБА СНЯТЫ ВМЕСТЕ С НИМ (03.09).
     *
     * Заслон закрывал поле ввода в чужом диалоге до нажатия «Всё равно
     * написать». Владелец решил иначе: «сделай так, чтобы можно было спокойно
     * заходить в чужой диалог… сделай только индикацию, что диалог в работе у
     * …». Проверка перевёрнута: поле обязано быть живым сразу, а кто ведёт
     * диалог — сказано отдельной строкой под шапкой (`WorkingOnStrip`).
     */
    const чужой = {
      ...makeConversation(),
      assignee: { id: "другой-человек", full_name: "Пётр Петров" },
    } as ConversationDto;

    renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={чужой} />);

    expect(
      screen.queryByLabelText("Текст сообщения"),
      "в чужом диалоге снова нужно нажимать кнопку, чтобы написать",
    ).not.toBeNull();
    expect(screen.queryByText("Всё равно написать")).toBeNull();
  });

  it("порядок под курсором держится, новые строки уходят в конец", () => {
    const A = строка("conv-A", "2026-08-30T10:00:00Z");
    const B = строка("conv-B", "2026-08-30T09:00:00Z");
    const C = строка("conv-C", "2026-08-30T11:00:00Z");

    // Пришло сообщение — сервер отдал B первой, а C появилась впервые.
    const свежие = [B, A, C];
    const порядок = поПлану(свежие, ["conv-A", "conv-B"]);

    expect(порядок.map((r) => r.id), "строки разъехались под уже занесённой рукой").toEqual([
      "conv-A",
      "conv-B",
      "conv-C",
    ]);
  });

  it("исчезнувшая строка из плана не воскресает", () => {
    const A = строка("conv-A", "2026-08-30T10:00:00Z");
    const порядок = поПлану([A], ["conv-B", "conv-A"]);
    expect(порядок.map((r) => r.id)).toEqual(["conv-A"]);
  });

  it("без плана порядок сервера не трогаем", () => {
    const A = строка("conv-A", "2026-08-30T10:00:00Z");
    const B = строка("conv-B", "2026-08-30T09:00:00Z");
    expect(поПлану([B, A], null).map((r) => r.id)).toEqual(["conv-B", "conv-A"]);
  });

  it("колонка списка подписана на наведение курсора", () => {
    /*
     * ⚠ БЕЗ ЭТОЙ ПРОВЕРКИ ТРИ ПРЕДЫДУЩИЕ СТОРОЖИЛИ БЫ МЁРТВУЮ ФУНКЦИЮ: они
     * зовут `поПлану` напрямую и остались бы зелёными, забудь кто-нибудь
     * подписать колонку. Механизм, написанный и не подключённый, — самая
     * частая поломка в этом проекте.
     */
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });
    const { container } = render(
      <QueryClientProvider client={qc}>
        <MantineProvider theme={theme} defaultColorScheme="light">
          <MemoryRouter initialEntries={["/chats"]}>
            <ChatListPane />
          </MemoryRouter>
        </MantineProvider>
      </QueryClientProvider>,
    );
    const колонка = container.querySelector(".chat-list-pane__scroll");
    expect(колонка, "колонка списка не найдена").not.toBeNull();
    // Наведение не должно ронять отрисовку и обязано быть обработано.
    expect(() => fireEvent.mouseEnter(колонка as Element)).not.toThrow();
    expect(() => fireEvent.mouseLeave(колонка as Element)).not.toThrow();
    const исходник = ChatListPane.toString();
    expect(исходник, "заморозка порядка не подключена к наведению курсора").toContain("поПлану");
  });
});
