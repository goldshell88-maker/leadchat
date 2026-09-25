import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, renderHook, screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { qk } from "@/shared/api/queryKeys";
import type { Permission } from "@/shared/auth/usePermissions";
import type { ConversationDetailDto } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread, wrap } from "./render";
import { useChangeStatus } from "@/features/chats/hooks/useConversationActions";

/*
 * Переход после закрытия проверяем перехватом `useNavigate`, а не адресом в
 * MemoryRouter: хук живёт вне дерева страницы, и читать его положение было бы
 * не проще, чем поймать сам вызов.
 */
const navigateSpy = vi.fn();
vi.mock("react-router-dom", async () => {
  const real = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...real, useNavigate: () => navigateSpy };
});

/** Обёртка для renderHook: те же провайдеры и тот же singleton queryClient. */
function Wrapper({ children }: { children: React.ReactNode }) {
  return wrap(<>{children}</>);
}

const HISTORY = {
  client: { id: "client-1", name: "Иван Петров" },
  items: [
    {
      id: "conv-old",
      status: "closed",
      item: { title: "Ремонт MacBook Air" },
      account: { title: "LP-Москва" },
      assignee: { full_name: "Пётр Ковалёв" },
      last_message_at: "2026-05-11T14:00:00Z",
      messages_count: 18,
    },
  ],
};

function seedDetail(conv: ConversationDetailDto) {
  queryClient.setQueryData(qk.conversations.detail(conv.id), conv);
}

describe("ClientCardPane — карточка клиента (11 §2.4)", () => {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  /** Что отдаёт сервер на GET /conversations/{id} — тесты его подменяют. */
  let detailResponse: ConversationDetailDto;

  beforeEach(() => {
    calls.length = 0;
    navigateSpy.mockClear();
    detailResponse = makeConversation();
    queryClient.clear();
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : {} });
        if (url.includes("client-history")) return jsonResponse(200, HISTORY);
        if (url.includes("/users/assignable")) return jsonResponse(200, { items: [] });
        if (url.includes("/status")) return jsonResponse(200, makeConversation({ status: "closed" }));
        return jsonResponse(200, detailResponse);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function renderCard(permissions: Permission[], conv = makeConversation()) {
    resetSessionStore({ user: fakeUser, permissions, accessToken: "t", bootstrapped: true });
    seedDetail(conv);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  it("показывает заявку, историю и ответственного — и ни одной кнопки-дубля", async () => {
    renderCard(fakeMe.permissions as Permission[]);

    // ИМЕНИ ЗДЕСЬ БОЛЬШЕ НЕТ (правка 10 от 12 августа). Сначала оно печаталось
    // дважды внутри самой карточки («Клиент» и «Клиент на Авито») — это убрала
    // правка 9. Разбор живого ЭКРАНА нашёл третий экземпляр, невидимый, пока
    // смотришь на карточку отдельно: заголовок чата и заголовок карточки
    // печатают одно имя в трёхстах пикселях друг от друга. Имя осталось шапке.
    // 17.08: имя дублируется в карточке по решению владельца
    expect(screen.queryAllByText("Иван Петров").length).toBeGreaterThan(0);
    expect(screen.getByText(/★ 4.9/)).toBeInTheDocument();
    expect(screen.getByText(/Ремонт iPhone 13/)).toBeInTheDocument();
    // «Передать» переехала в шапку ленты — второго экземпляра здесь нет.
    expect(screen.queryByRole("button", { name: "Передать" })).toBeNull();
    // Менеджеру «ответственный» — текст, свободный селект только у A/H (03 §5.2).
    expect(screen.getByText("Вы")).toBeInTheDocument();

    expect(await screen.findByText("Ремонт MacBook Air")).toBeInTheDocument();
  });

  it("телефона нет — вместо мёртвой строки ссылка «указать телефон» (правка 9)", () => {
    /*
     * ЧТО БЫЛО. Карточка печатала «телефон не указан» серым текстом и всё.
     * Автомат вычитывает номер из переписки, и на бою это 3 телефона из 37
     * обращений: люди диктуют номер голосом, пишут его в объявлении, называют
     * мастеру на пороге. Система ЗНАЛА, что номера нет, и не давала вписать
     * его — диспетчер держал номер в блокноте рядом с клавиатурой.
     */
    renderCard(fakeMe.permissions as Permission[]);

    expect(screen.getByRole("button", { name: /указать телефон/ })).toBeInTheDocument();
    expect(screen.queryByText("телефон не указан")).not.toBeInTheDocument();
  });

  it("заметки свёрнуты в одну строку со счётчиком (правка 9)", () => {
    /*
     * Пустая секция «Заметки» занимала столько же места, сколько блок с
     * личностью клиента, — а пуста она у подавляющего большинства диалогов.
     * Счётчик в свёрнутой строке обязателен: без него свёрнутый блок не
     * отличить от блока с тремя заметками, и сворачивание прятало бы работу
     * коллеги.
     */
    renderCard(fakeMe.permissions as Permission[]);

    const toggle = screen.getByRole("button", { name: /Заметки \(0\)/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Заметок пока нет")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.getByText("Заметок пока нет")).toBeInTheDocument();
  });

  it("смена статуса шлёт PATCH /status и оптимистично обновляет деталь (01 §5.4)", async () => {
    /*
     * Проверяем ХУК, а не контрол. Выбор статуса раньше стоял и здесь, и в
     * шапке ленты — два одинаковых элемента управления в трёхстах пикселях
     * друг от друга (UX-аудит, docs/17 §С1); из карточки он убран. Поведение
     * при этом никуда не делось, и привязывать его проверку к тому, где
     * сейчас нарисована кнопка, изначально было неверно: оно принадлежит
     * `useChangeStatus`, а не карточке.
     */
    seedDetail(makeConversation());
    const { result } = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });

    result.current.mutate({ status: "closed" });

    await waitFor(() => {
      expect(calls.some((c) => c.url.includes(`/conversations/${CONV_ID}/status`))).toBe(true);
    });
    const patch = calls.find((c) => c.url.includes("/status"));
    expect(patch?.body).toMatchObject({ status: "closed" });
    // Оптимистичность: деталь в кэше стала «закрыт» ещё до ответа сервера.
    expect(
      queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail(CONV_ID))?.status,
    ).toBe("closed");
  });

  it("телефон из conversation:updated подсвечивается на 2 с (WS)", async () => {
    const { container } = renderCard(fakeMe.permissions as Permission[]);
    expect(container.querySelector(".card-phone")).toBeNull();

    // Телефон извлёк бэкенд из текста — он же прилетит и в рефетче детали.
    detailResponse = makeConversation({
      client: { id: "client-1", name: "Иван Петров", phone: "+79261234567", avito_rating: 4.9 },
    });
    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-08-05T10:00:00Z",
      data: { conversation_id: CONV_ID, patch: { client: { phone: "+79261234567" } } },
    });

    // Ищем номер в ЧЕЛОВЕЧЕСКОМ виде: с 11 августа карточка показывает его по
    // группам (дефект аудита 17). Ссылка `tel:` при этом хранит исходные
    // одиннадцать цифр — это проверяет отдельный тест ниже.
    const phone = await screen.findByText("+7 926 123-45-67");
    expect(phone).toBeInTheDocument();
    await waitFor(() =>
      expect(container.querySelector('.card-phone[data-hot="true"]')).not.toBeNull(),
    );
  });

  it("закрыл во «Моих» — открывается следующий, а не пустое окно", async () => {
    /*
     * Замечание заказчика: «во вкладке "Мои" закрываешь диалог, а окно с
     * диалогом остаётся открытым, и приходится постоянно перекликивать на
     * новый». «Мои» — это своя нагрузка, её закрывают подряд.
     *
     * На «Всех» перехода нет намеренно: туда заходят посмотреть чужой диалог
     * или найти старый, и уносить человека в соседнюю строку значит потерять
     * то, что он читал. Обе стороны проверяются ниже.
     */
    const { useChatUiStore } = await import("@/shared/stores/chatUiStore");
    const { qk } = await import("@/shared/api/queryKeys");

    const seedList = (tab: "mine" | "all") => {
      const filters = { ...useChatUiStore.getState().filters, tab };
      useChatUiStore.setState({ filters, inboxOpen: false });
      queryClient.setQueryData(qk.conversations.list(filters), {
        pages: [
          {
            items: [makeConversation(), makeConversation({ id: "conv-next" })],
            page: { limit: 50, offset: 0, total: 2 },
          },
        ],
        pageParams: [0],
      });
      return filters;
    };

    seedList("mine");
    seedDetail(makeConversation());
    const mine = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });
    mine.result.current.mutate({ status: "closed" });
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith("/chats/conv-next", { replace: true }));

    navigateSpy.mockClear();
    seedList("all");
    seedDetail(makeConversation());
    const all = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });
    all.result.current.mutate({ status: "closed" });
    await waitFor(() => expect(calls.some((c) => c.url.includes("/status"))).toBe(true));
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("показывает, кого позвали в диалог (docs/19)", () => {
    // Ответ на вопрос «кто ещё здесь». Без этого списка позванный человек
    // существует только в системной записи, которая уехала вверх ленты через
    // десяток сообщений.
    renderCard(
      fakeMe.permissions as Permission[],
      makeConversation({
        participants: [
          { id: "u-petr", full_name: "Пётр Ковалёв", reason: "посмотри модель", invited_at: null },
        ],
      }),
    );

    expect(screen.getByText("Позваны")).toBeInTheDocument();
    expect(screen.getByText("Пётр Ковалёв")).toBeInTheDocument();
    // СОСТАВ — да, КНОПКА «Позвать» — нет (правка 10): звать зовут из шапки
    // ленты, а здесь показано, кто уже здесь. Список — это факт о диалоге,
    // кнопка — действие, и место у них разное.
    expect(screen.queryByRole("button", { name: "Позвать" })).toBeNull();
    expect(screen.getByRole("button", { name: "Убрать Пётр Ковалёв" })).toBeInTheDocument();
  });

  it("руководитель видит состав, но не зовёт (docs/19)", () => {
    // Право на приглашение — `messages:send`: зовёт тот, кто ведёт переписку.
    // Руководитель диалоги распределяет, для этого у него селект выше. Но
    // состав он видит — иначе не понять, почему в чужом диалоге двое.
    renderCard(
      ["conversations:read", "conversations:manage"],
      makeConversation({
        participants: [
          { id: "u-petr", full_name: "Пётр Ковалёв", reason: null, invited_at: null },
        ],
      }),
    );

    expect(screen.getByText("Пётр Ковалёв")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Позвать" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Убрать/ })).toBeNull();
  });

  it("наблюдатель видит факты, но не органы управления (11 §2.5.3)", () => {
    /*
     * ЧТО ИЗМЕНИЛОСЬ 12 АВГУСТА. Блок «Ход диалога» показывает ФАКТЫ всем, а
     * ДЕЙСТВИЯ — по праву. Раньше он целиком висел на `conversations:manage`,
     * то есть наблюдатель не видел даже статуса.
     *
     * Итог обращения из проверки ушёл: в тот же день владелец отказался от
     * «Чем закончилось» целиком, и показывать здесь стало нечего. Проверка
     * «наблюдатель видит статус, но не органы управления» от этого не
     * ослабла — она про право, а не про исход.
     */
    renderCard(["conversations:read"], makeConversation({ status: "closed" }));

    // Имя наблюдателю показывает шапка ленты — в карточке его нет ни у кого.
    // 17.08: имя дублируется в карточке по решению владельца
    expect(screen.queryAllByText("Иван Петров").length).toBeGreaterThan(0);
    // Селекта нет — статус показан текстом.
    expect(screen.queryAllByLabelText("Статус диалога")).toHaveLength(0);
    expect(screen.getByText("Закрыт")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Передать" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Заметки/ })).toBeNull();
    // Телефон наблюдателю не править: у него нет `conversations:manage`.
    expect(screen.queryByRole("button", { name: /указать телефон/ })).toBeNull();
    expect(screen.getByText("телефон не указан")).toBeInTheDocument();
  });
});
