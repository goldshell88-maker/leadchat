import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InfiniteQueryObserver, type InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { qk, type ConversationFilters } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage, MessageDto, TabCounts } from "@/shared/api/types";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import {
  __resetListRefetch,
  COUNTS_REFETCH_WINDOW_MS,
  LIST_REFETCH_MAX_WAIT_MS,
} from "@/shared/realtime/listRefetch";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";

vi.mock("@/shared/realtime/notify", () => ({ notifyNewMessage: vi.fn(), notifyAssignedToMe: vi.fn() }));
vi.mock("@/features/chats/inbox/sound", () => ({ playInboxChime: vi.fn() }));

/**
 * СРЕЗ «ЖДУТ ОТВЕТА» ТЕРЯЕТ ОТВЕЧЕННУЮ СТРОКУ СРАЗУ.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «снова начали долго уходить отвеченные
 * диалоги… нужно нажимать или ждать». Уточнение на вопрос — «В „Моих“
 * отвеченный не уходит из списка».
 *
 * ЧТО БЫЛО. Членство строки в срезе считает СЕРВЕР (`waiting_only`), а
 * перезапрос списка после кадра назначался ровно по двум признакам — статус и
 * хозяин (`вкладкиМоглиСмениться`). Ответ оператора не меняет ни того, ни
 * другого: диалог уже «в работе» и уже мой. Значит список не перезапрашивался
 * никогда, и отвеченная строка оставалась в срезе до тихой сверки или до
 * нажатия на вкладку. Число «не отвечено» при этом уезжало вниз мгновенно —
 * дельтой из кадра: под надписью «ждут 3» лежали четыре строки.
 *
 * Это тот самый класс, на котором проект попадался много раз: одно и то же
 * поле считают два пути, и один из них про изменение не узнаёт.
 *
 * ⚠ ЧЕГО ЗДЕСЬ НЕ ПРОВЕРЯЕТСЯ И ПОЧЕМУ. Вкладка «Мои» БЕЗ среза отвеченный
 * диалог держит до закрытия — это замысел, а не поломка: диалог остаётся
 * твоим, пока ты его не закрыл. Срез и есть тот инструмент, который отвечает
 * на вопрос «что мне сейчас делать».
 */

const МОЙ_ID = "11111111-1111-4111-8111-111111111111";
const ЧУЖОЙ_ID = "22222222-2222-4222-8222-222222222222";
const ПОЛНОЕ_ОКНО = LIST_REFETCH_MAX_WAIT_MS + COUNTS_REFETCH_WINDOW_MS + 100;

const СРЕЗ: ConversationFilters = { tab: "mine", waitingOnly: true };
const БЕЗ_СРЕЗА: ConversationFilters = { tab: "mine" };
const СРЕЗ_ВСЕ: ConversationFilters = { tab: "all", waitingOnly: true };

function строка(id: string, overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: "Иван Петров", phone: null, avito_rating: null },
    assignee: { id: МОЙ_ID, full_name: "Я" },
    item: null,
    last_message: { body: "старое", direction: "in", created_at: "2026-09-09T09:00:00Z" },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-09T09:00:00Z",
    waiting_since: "2026-09-09T09:00:00Z",
    ...overrides,
  };
}

function сообщение(direction: MessageDto["direction"]): MessageDto {
  return {
    id: `m-${direction}`,
    conversation_id: "conv-1",
    direction,
    sender_type: direction === "in" ? "client" : "operator",
    sender: null,
    body: "Приедем завтра с 10 до 12",
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-09-09T10:00:00Z",
  };
}

function страница(items: ConversationDto[]): InfiniteData<ConversationsPage> {
  return {
    pages: [{ items, page: { limit: 50, offset: 0, total: items.length } }],
    pageParams: [0],
  };
}

/** Наблюдатель на списке — буквально «на этот список смотрят». */
function наблюдатель(фильтры: ConversationFilters) {
  const счёт = { список: 0 };
  const obs = new InfiniteQueryObserver(queryClient, {
    queryKey: qk.conversations.list(фильтры),
    queryFn: async () => {
      счёт.список += 1;
      return { items: [], page: { limit: 50, offset: 0, total: 0 } };
    },
    initialPageParam: 0,
    getNextPageParam: () => undefined,
  });
  const отписка = obs.subscribe(() => {});
  return { счёт, стоп: отписка };
}

function кадр(patch: Record<string, unknown>, direction: MessageDto["direction"] = "out") {
  applyWsEvent({
    type: "message:new",
    ts: "2026-09-09T10:00:00.100Z",
    data: {
      conversation_id: "conv-1",
      message: сообщение(direction),
      conversation_patch: {
        last_message_at: "2026-09-09T10:00:00Z",
        status: "in_progress",
        assignee_id: МОЙ_ID,
        unread_delta: 0,
        ...patch,
      },
    },
  });
}

describe("срез «ждут ответа» и отвеченный диалог", () => {
  let стоп: Array<() => void> = [];

  beforeEach(() => {
    vi.useFakeTimers();
    queryClient.clear();
    __resetListRefetch();
    useUnreadStore.getState().clear();
    useInboxStore.getState().clear();
    useChatUiStore.setState({ activeConversationId: null, filters: { tab: "mine" } });
    useSessionStore.setState({
      user: { id: МОЙ_ID, full_name: "Я", email: "me@leadchat.local", role: "manager" } as never,
      permissions: ["messages:send"] as never,
    });
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 5, mine_waiting: 3 });
    стоп = [];
  });

  afterEach(() => {
    for (const f of стоп) f();
    __resetListRefetch();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("ответил — строка уходит из среза, а не ждёт нажатия", async () => {
    queryClient.setQueryData(qk.conversations.list(СРЕЗ), страница([строка("conv-1")]));
    const { счёт, стоп: off } = наблюдатель(СРЕЗ);
    стоп.push(off);

    кадр({ waiting_since: null });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "отвеченный диалог остался в срезе до перезапроса").toBe(1);
  });

  it("срез не открыт — сервера не касаемся вовсе", async () => {
    /*
     * ⚠ ЗАЩИТА ОТ ВОЗВРАТА ШТОРМА 06.09. Кадр приходит всем тринадцати на
     * каждое сообщение компании (1 126 в час), и ожидание переключается у
     * каждого второго. Безусловный перезапрос здесь — это ровно те 344 ответа
     * 429 в сутки, из-за которых на экране появлялось «Не получилось
     * загрузить».
     */
    queryClient.setQueryData(qk.conversations.list(БЕЗ_СРЕЗА), страница([строка("conv-1")]));
    const { счёт, стоп: off } = наблюдатель(БЕЗ_СРЕЗА);
    стоп.push(off);

    кадр({ waiting_since: null });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "список перезапрошен там, где среза нет").toBe(0);
    expect(
      queryClient.getQueryData<TabCounts>(qk.conversations.counts)?.mine_waiting,
      "число «не отвечено» обязано двигаться дельтой и без среза",
    ).toBe(2);
  });

  it("клиент написал — строка обязана в срезе ПОЯВИТЬСЯ", async () => {
    // Обратное направление того же правила: срез показывает не «мои долги
    // на момент загрузки», а тех, кто ждёт сейчас.
    queryClient.setQueryData(qk.conversations.list(СРЕЗ), страница([строка("conv-1", { waiting_since: null })]));
    const { счёт, стоп: off } = наблюдатель(СРЕЗ);
    стоп.push(off);

    кадр({ waiting_since: "2026-09-09T10:00:00Z", unread_delta: 1 }, "in");
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "заждавшийся клиент не попал в срез").toBe(1);
  });

  it("чужой отвеченный диалог уходит из среза на «Всех»", async () => {
    /*
     * ⚠ ПОЧЕМУ ЗДЕСЬ НЕ ГОДИТСЯ ДЕЛЬТА СЧЁТА. Она считает МОЙ долг и на чужом
     * диалоге всегда ноль. Срез же от хозяина не зависит — он живёт и на
     * вкладке «Все». Считай мы членство дельтой, свой отвеченный уходил бы, а
     * чужой оставался, и разницы никто бы не объяснил.
     */
    const чужой = строка("conv-1", { assignee: { id: ЧУЖОЙ_ID, full_name: "Коллега" } });
    queryClient.setQueryData(qk.conversations.list(СРЕЗ_ВСЕ), страница([чужой]));
    const { счёт, стоп: off } = наблюдатель(СРЕЗ_ВСЕ);
    стоп.push(off);

    кадр({ waiting_since: null, assignee_id: ЧУЖОЙ_ID });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "чужой отвеченный застрял в срезе").toBe(1);
  });

  it("кадр, не трогающий ожидание, списка не заказывает даже при открытом срезе", async () => {
    // Отрицательная проверка с живым остатком: кадр доехал и строку поправил,
    // а сервер не спрошен — иначе «перезапрашиваем при срезе» выродилось бы в
    // «перезапрашиваем всегда, пока срез открыт».
    queryClient.setQueryData(qk.conversations.list(СРЕЗ), страница([строка("conv-1")]));
    const { счёт, стоп: off } = наблюдатель(СРЕЗ);
    стоп.push(off);

    кадр({ waiting_since: "2026-09-09T09:00:00Z" });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    const строки = queryClient.getQueryData<InfiniteData<ConversationsPage>>(
      qk.conversations.list(СРЕЗ),
    );
    expect(строки?.pages[0].items[0].last_message_at, "кадр не доехал до строки").toBe(
      "2026-09-09T10:00:00Z",
    );
    expect(счёт.список, "лишний перезапрос при неизменном ожидании").toBe(0);
  });

  it("строка старого кэша без «ждёт» — спрашиваем сервер, а не гадаем", async () => {
    const старая = строка("conv-1");
    delete (старая as Partial<ConversationDto>).waiting_since;
    queryClient.setQueryData(qk.conversations.list(СРЕЗ), страница([старая]));
    const { счёт, стоп: off } = наблюдатель(СРЕЗ);
    стоп.push(off);

    кадр({ waiting_since: null });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "по неизвестной строке решили молча").toBe(1);
  });
});
