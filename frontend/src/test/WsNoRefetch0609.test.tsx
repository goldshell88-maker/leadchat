import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InfiniteQueryObserver, QueryObserver, type InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { inboxRows } from "@/features/chats/inbox/api";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage, MessageDto, TabCounts } from "@/shared/api/types";
import { applyNewMessage, applyWsEvent } from "@/shared/realtime/applyWsEvent";
import {
  __resetListRefetch,
  COUNTS_RECONCILE_MS,
  COUNTS_REFETCH_WINDOW_MS,
  LIST_REFETCH_MAX_WAIT_MS,
} from "@/shared/realtime/listRefetch";
import { resyncNow } from "@/shared/realtime/quietResync";
import type { WsServerEvent } from "@/shared/realtime/wsEvents";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { jsonResponse } from "./helpers";

// Звук и уведомления — факт вызова, в jsdom играть нечего.
vi.mock("@/shared/realtime/notify", () => ({ notifyNewMessage: vi.fn(), notifyAssignedToMe: vi.fn() }));
vi.mock("@/features/chats/inbox/sound", () => ({ playInboxChime: vi.fn() }));

/**
 * КАДР СОКЕТА НЕ ЗАКАЗЫВАЕТ СПИСОК У КАЖДОЙ ВКЛАДКИ (аудит 06.09).
 *
 * ⚠ ЗАМЕР. `GET /conversations/counts` 55–60 тыс. и `GET /conversations`
 * 52–56 тыс. в сутки — 54–57 % ВСЕГО трафика, в пик 47 запросов на человека в
 * минуту. Причина: `patchRowEverywhere` после КАЖДОГО широковещательного
 * кадра (`message:new` всем — 1 126 сообщений в час, `conversation:updated`,
 * `inbox:claimed/released`, `assigned`, личный кадр на `POST /read`)
 * заказывал у каждой из 11–13 вкладок список плюс числа через 200 мс – 3 с.
 * Вкладки били в одно окно, nginx (30 запросов в секунду на адрес) отдавал
 * 344 ответа 429 в сутки — все с офисного адреса; react-query 4xx не
 * повторяет, и человек видел «Не получилось загрузить» и сорванное «Принять».
 *
 * ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ — ПО СЧЁТЧИКУ НАСТОЯЩИХ ЗАПРОСОВ, а не по шпиону на
 * `invalidateQueries`: список и числа живут под наблюдателями, как на экране,
 * и каждый вызов `queryFn` — это запрос, который ушёл бы в nginx.
 *
 * ДИВЕРСИИ (все проведены, все дали красный, всё восстановлено байт в байт):
 *  1. в `applyNewMessage` вместо `сверитьсяПослеПатча` вернуть безусловный
 *     `scheduleListRefetch()` — «входящее в мой диалог» красный: список
 *     запрошен;
 *  2. в `сверитьсяПослеПатча` убрать ветку `сдвинутьСчёт` — «не отвечено +1»
 *     красный: число не сдвинулось;
 *  3. `вкладкиМоглиСмениться` всегда `false` — «смена статуса» красный:
 *     список не спрошен;
 *  4. в `scheduleCountsRefetch` снять проверку `таймерЧисел !== null` —
 *     «один запрос на окно» красный: три запроса вместо одного;
 *  5. в `quietResync.resyncNow` вернуть сплошной `invalidateQueries({
 *     refetchType: "active" })` — «шаблоны не трогает» красный.
 */

const МОЙ_ID = "11111111-1111-4111-8111-111111111111";
const ЧУЖОЙ_ID = "22222222-2222-4222-8222-222222222222";
const ФИЛЬТРЫ = { tab: "mine" } as const;
const ПОЛНОЕ_ОКНО = LIST_REFETCH_MAX_WAIT_MS + COUNTS_REFETCH_WINDOW_MS + 100;

function строка(id: string, overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: "Иван Петров", phone: null, avito_rating: null },
    assignee: { id: МОЙ_ID, full_name: "Я" },
    item: null,
    last_message: { body: "старое", direction: "in", created_at: "2026-09-06T09:00:00Z" },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-06T09:00:00Z",
    waiting_since: null,
    ...overrides,
  };
}

function сообщение(id: string, convId: string, direction: MessageDto["direction"]): MessageDto {
  return {
    id,
    conversation_id: convId,
    direction,
    sender_type: direction === "in" ? "client" : "operator",
    sender: null,
    body: "Сколько стоит замена экрана?",
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-09-06T10:00:00Z",
  };
}

function страница(items: ConversationDto[]): InfiniteData<ConversationsPage> {
  return {
    pages: [{ items, page: { limit: 50, offset: 0, total: items.length } }],
    pageParams: [0],
  };
}

/** Настоящие запросы, как у экрана: наблюдатели на списке, числах и очереди. */
function наблюдатели() {
  const счёт = { список: 0, числа: 0, очередь: 0 };
  const список = new InfiniteQueryObserver(queryClient, {
    queryKey: qk.conversations.list(ФИЛЬТРЫ),
    queryFn: async () => {
      счёт.список += 1;
      return { items: [], page: { limit: 50, offset: 0, total: 0 } };
    },
    initialPageParam: 0,
    getNextPageParam: () => undefined,
  });
  const числа = new QueryObserver<TabCounts>(queryClient, {
    queryKey: qk.conversations.counts,
    queryFn: async () => {
      счёт.числа += 1;
      return { mine: 5, mine_waiting: 2 };
    },
  });
  const очередь = new InfiniteQueryObserver(queryClient, {
    queryKey: qk.inbox.list,
    queryFn: async () => {
      счёт.очередь += 1;
      return { items: [], page: { limit: 50, offset: 0, total: 0 } };
    },
    initialPageParam: 0,
    getNextPageParam: () => undefined,
  });
  const отписки = [список.subscribe(() => {}), числа.subscribe(() => {}), очередь.subscribe(() => {})];
  return { счёт, стоп: () => отписатьВсе(отписки) };
}

function отписатьВсе(отписки: Array<() => void>): void {
  for (const f of отписки) f();
}

function числа(): TabCounts | undefined {
  return queryClient.getQueryData<TabCounts>(qk.conversations.counts);
}

function входящее(convId: string, patch: Record<string, unknown>) {
  applyWsEvent({
    type: "message:new",
    ts: "2026-09-06T10:00:00.100Z",
    data: { conversation_id: convId, message: сообщение(`m-${convId}`, convId, "in"), conversation_patch: patch },
  });
}

describe("Кадры по знакомой строке не ходят на сервер", () => {
  let стоп: () => void;
  let счёт: { список: number; числа: number; очередь: number };

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
    // Всё свежее: наблюдатель при подписке не ходит за данными, и каждый
    // запрос ниже — следствие кадра, а не подписки.
    queryClient.setQueryData(qk.conversations.list(ФИЛЬТРЫ), страница([строка("conv-1")]));
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 5, mine_waiting: 2 });
    queryClient.setQueryData(qk.inbox.list, страница([]));
    ({ стоп, счёт } = наблюдатели());
  });

  afterEach(() => {
    стоп();
    __resetListRefetch();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("⚠ входящее в мой диалог: список НЕ перезапрашивается, «не отвечено» +1 без запроса", async () => {
    входящее("conv-1", {
      last_message_at: "2026-09-06T10:00:00Z",
      status: "in_progress",
      waiting_since: "2026-09-06T10:00:00Z",
      assignee_id: МОЙ_ID,
      unread_delta: 1,
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "кадр по знакомой строке снова заказал список").toBe(0);
    expect(счёт.числа, "число спросили у сервера, хотя дельта известна из кадра").toBe(0);
    expect(числа()?.mine_waiting, "«не отвечено» не сдвинулось на входящее").toBe(3);
    expect(числа()?.mine, "«на руках» от сообщения не меняется").toBe(5);
  });

  it("мой ответ гасит «не отвечено» — тоже дельтой", async () => {
    queryClient.setQueryData(
      qk.conversations.list(ФИЛЬТРЫ),
      страница([строка("conv-1", { waiting_since: "2026-09-06T09:00:00Z" })]),
    );
    applyWsEvent({
      type: "message:new",
      ts: "2026-09-06T10:00:00.100Z",
      data: {
        conversation_id: "conv-1",
        message: сообщение("m-out", "conv-1", "out"),
        conversation_patch: {
          last_message_at: "2026-09-06T10:00:00Z",
          status: "in_progress",
          waiting_since: null,
          assignee_id: МОЙ_ID,
          unread_delta: 0,
        },
      },
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список + счёт.числа).toBe(0);
    expect(числа()?.mine_waiting).toBe(1);
  });

  /*
   * ⚠ ПОТОЛОК РАСХОЖДЕНИЯ ЧИСЛА (жалоба владельца 07.09: «ответил клиенту, а
   * диалог так и висит в неотвеченных»).
   *
   * До 06.09 число сверялось с сервером после каждого кадра — дорого, но
   * самоисправлялось за секунды. Дельта дешевле, но читает строку из кэша, а
   * вариантов кэша столько, сколько наборов фильтров человек открывал; они
   * грузятся в разное время и расходятся. Ошибку дельты нельзя доказать
   * отсутствующей, поэтому здесь не заплатка под найденный случай, а потолок:
   * раз в полминуты число спрашивают у сервера в любом случае.
   *
   * ⚠ ДИВЕРСИЯ: убрать `scheduleCountsReconcile()` из `сверитьсяПослеПатча` —
   * проверка краснеет: после кадра сервер не спрашивают никогда.
   */
  it("числа сверяются с сервером и тогда, когда дельта сошлась", async () => {
    queryClient.setQueryData(
      qk.conversations.list(ФИЛЬТРЫ),
      страница([строка("conv-1", { waiting_since: "2026-09-06T09:00:00Z" })]),
    );
    applyWsEvent({
      type: "message:new",
      ts: "2026-09-06T10:00:00.100Z",
      data: {
        conversation_id: "conv-1",
        message: сообщение("m-out", "conv-1", "out"),
        conversation_patch: {
          last_message_at: "2026-09-06T10:00:00Z",
          status: "in_progress",
          waiting_since: null,
          assignee_id: МОЙ_ID,
          unread_delta: 0,
        },
      },
    });

    // Дельта сошлась: число сдвинуто на месте, сервер сразу не спрошен.
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);
    expect(числа()?.mine_waiting).toBe(1);
    expect(счёт.числа, "сервер спрошен сразу — дельта тогда не нужна вовсе").toBe(0);

    // А к концу окна сверки — спрошен, сколько бы кадров ни пришло.
    await vi.advanceTimersByTimeAsync(COUNTS_RECONCILE_MS + 100);
    expect(счёт.числа, "число не сверяется с сервером никогда — расхождение навсегда").toBe(1);
  });

  it("повторное входящее в уже ждущий диалог число не двигает", async () => {
    queryClient.setQueryData(
      qk.conversations.list(ФИЛЬТРЫ),
      страница([строка("conv-1", { waiting_since: "2026-09-06T09:00:00Z" })]),
    );
    входящее("conv-1", {
      status: "in_progress",
      waiting_since: "2026-09-06T09:00:00Z",
      assignee_id: МОЙ_ID,
      unread_delta: 1,
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список + счёт.числа).toBe(0);
    expect(числа()?.mine_waiting, "второе сообщение того же клиента посчитано как второй диалог").toBe(2);
  });

  it("чужой диалог: ни запроса, ни сдвига — числа только про мои", async () => {
    queryClient.setQueryData(
      qk.conversations.list(ФИЛЬТРЫ),
      страница([строка("conv-1", { assignee: { id: ЧУЖОЙ_ID, full_name: "Коллега" } })]),
    );
    входящее("conv-1", {
      status: "in_progress",
      waiting_since: "2026-09-06T10:00:00Z",
      assignee_id: ЧУЖОЙ_ID,
      unread_delta: 1,
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список + счёт.числа).toBe(0);
    expect(числа()?.mine_waiting).toBe(2);
  });

  it("личный кадр отметки прочтения (`POST /read`) сервера не касается", async () => {
    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-09-06T10:00:00Z",
      data: { conversation_id: "conv-1", patch: { unread_count: 0 } },
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);
    expect(счёт.список + счёт.числа).toBe(0);
  });

  it("патч, где статус ПРИСУТСТВУЕТ, но не менялся, — тоже без запроса", async () => {
    // `_publish_change` кладёт `status` почти в каждый патч; спрашивать список
    // на каждую метку или телефон клиента — та же беда с другого входа.
    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-09-06T10:00:00Z",
      data: { conversation_id: "conv-1", patch: { status: "in_progress", tags: ["негатив"] } },
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);
    expect(счёт.список + счёт.числа).toBe(0);
  });

  it("строки нет в кэше — список перезапрашивается: её место знает только сервер", async () => {
    входящее("conv-новый", { status: "new", waiting_since: "2026-09-06T10:00:00Z", unread_delta: 1 });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);
    expect(счёт.список, "новый диалог не приехал бы до перезагрузки").toBe(1);
  });

  it("⚠ смена статуса — перезапрос списка вместе с числами", async () => {
    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-09-06T10:00:00Z",
      data: { conversation_id: "conv-1", patch: { status: "closed" } },
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);
    expect(счёт.список, "закрытый диалог остался бы во вкладке").toBe(1);
    expect(счёт.числа, "«на руках» после закрытия не пересчитано").toBe(1);
  });

  it("хозяин в кадре не совпал со строкой — перезапрос: диалог переехал", async () => {
    входящее("conv-1", {
      status: "in_progress",
      waiting_since: "2026-09-06T10:00:00Z",
      assignee_id: ЧУЖОЙ_ID,
      unread_delta: 1,
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);
    expect(счёт.список).toBe(1);
  });

  it("⚠ строка старого кэша без `waiting_since`: спрашивают ОДНИ числа, один раз на окно", async () => {
    const старая = строка("conv-1");
    delete старая.waiting_since; // так выглядит строка, записанная до 12.08
    queryClient.setQueryData(qk.conversations.list(ФИЛЬТРЫ), страница([старая]));

    for (let i = 0; i < 3; i++) {
      входящее("conv-1", { status: "in_progress", assignee_id: МОЙ_ID, unread_delta: 1 });
    }
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "неизвестная дельта заказала весь список").toBe(0);
    expect(счёт.числа, "числа спрошены на каждый кадр, а не раз на окно").toBe(1);
  });

  it("inbox:new: строка встаёт в очередь из кадра, и следом никто ничего не перезапрашивает", async () => {
    applyWsEvent({
      type: "inbox:new",
      ts: "2026-09-06T10:00:00Z",
      data: {
        conversation_id: "conv-q",
        conversation: строка("conv-q", {
          assignee: null,
          status: "new",
          in_inbox: true,
          offered_at: "2026-09-06T10:00:00Z",
        }),
        can_claim: true,
      },
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(inboxRows().map((r) => r.id), "строка из кадра не попала в очередь").toEqual(["conv-q"]);
    expect(счёт.очередь, "очередь перезапрошена следом за вставкой из кадра").toBe(0);
    expect(счёт.список, "новый диалог в очереди заказал список вкладок").toBe(0);
  });

  /*
   * ДОБАВЛЕНО РЕВЬЮ 06.09 — диверсии, которые прошли мимо сторожей выше
   * (каждая дала зелёный на сломанном коде, теперь красный):
   *  6. в `tabCounts.отвечаю` убрать получателя передачи — «передают мне» красный;
   *  7. в `дельтаНеОтвечено` без сессии вернуть 0 вместо null — «без сессии» красный;
   *  8. в `сбросить()` не гасить окно чисел — «окно чисел гаснет» красный;
   *     (а вот проверка таймера списка в `scheduleCountsRefetch` по поведению
   *     неотличима — окно всё равно гасит `сбросить()`; тест «список уже в
   *     очереди» держит наблюдаемое: один запрос чисел на пачку, — и на её
   *     снятии остаётся зелёным, это честно записано здесь);
   *  9. (см. 8);
   * 10. в `вкладкиМоглиСмениться` не сравнивать получателя передачи —
   *     «предложение передачи мне» красный;
   * 11. в `applyWsEvent` снять перезапрос по кадру без патча — «позвали в
   *     диалог» красный.
   */

  it("передают мне, клиент пишет — «не отвечено» +1 дельтой: за передаваемый отвечаю уже я", async () => {
    // `own_condition` сервера — ответственный ИЛИ получатель предложенной
    // передачи; ответственный при этом ещё прежний (двухфазность, 01 §5.5).
    queryClient.setQueryData(
      qk.conversations.list(ФИЛЬТРЫ),
      страница([
        строка("conv-1", {
          assignee: { id: ЧУЖОЙ_ID, full_name: "Коллега" },
          transfer: {
            to: { id: МОЙ_ID, full_name: "Я" },
            by: { id: ЧУЖОЙ_ID, full_name: "Коллега" },
            at: "2026-09-06T09:30:00Z",
            comment: null,
          },
        }),
      ]),
    );
    входящее("conv-1", {
      status: "in_progress",
      waiting_since: "2026-09-06T10:00:00Z",
      assignee_id: ЧУЖОЙ_ID,
      unread_delta: 1,
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список + счёт.числа).toBe(0);
    expect(числа()?.mine_waiting, "ожидание в передаваемом мне диалоге не зачлось").toBe(3);
  });

  it("без сессии дельту не выводят — числа спрашивают у сервера, а не гасят нулём", async () => {
    useSessionStore.setState({ user: null });
    входящее("conv-1", {
      status: "in_progress",
      waiting_since: "2026-09-06T10:00:00Z",
      assignee_id: МОЙ_ID,
      unread_delta: 1,
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список).toBe(0);
    expect(счёт.числа, "неизвестно чей диалог — а число молча оставили прежним").toBe(1);
  });

  it("список уже в очереди — просьба про одни числа лишняя: их привезёт список", async () => {
    const старая = строка("conv-1");
    delete старая.waiting_since;
    queryClient.setQueryData(qk.conversations.list(ФИЛЬТРЫ), страница([старая]));

    // 1) смена статуса ставит таймер списка (200 мс);
    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-09-06T10:00:00Z",
      data: { conversation_id: "conv-1", patch: { status: "waiting_client" } },
    });
    // 2) следом кадр, по которому дельту не вывести (строка без `waiting_since`).
    входящее("conv-1", { status: "waiting_client", assignee_id: МОЙ_ID, unread_delta: 1 });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список).toBe(1);
    expect(счёт.числа, "числа спрошены дважды: списком и отдельным окном").toBe(1);
  });

  it("окно чисел гаснет, когда следом едет список: числа спрашивают один раз", async () => {
    const старая = строка("conv-1");
    delete старая.waiting_since;
    queryClient.setQueryData(qk.conversations.list(ФИЛЬТРЫ), страница([старая]));

    // 1) неизвестная дельта — окно чисел (3 с);
    входящее("conv-1", { status: "in_progress", assignee_id: МОЙ_ID, unread_delta: 1 });
    // 2) через мгновение смена статуса — список с числами через 200 мс.
    await vi.advanceTimersByTimeAsync(50);
    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-09-06T10:00:00Z",
      data: { conversation_id: "conv-1", patch: { status: "waiting_client" } },
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список).toBe(1);
    expect(счёт.числа, "окно чисел дожило до своего срока поверх списка").toBe(1);
  });

  it("⚠ предложение передачи мне — перезапрос: диалог входит в «Мои», хотя статус и хозяин прежние", async () => {
    // Кадр `_publish_change` при передаче: статус и ответственный те же, новое
    // только `transfer`. У получателя на вкладке «Все» строка знакома — без
    // этого правила ни «Мои», ни число «на руках» о передаче не узнали бы.
    queryClient.setQueryData(
      qk.conversations.list(ФИЛЬТРЫ),
      страница([строка("conv-1", { assignee: { id: ЧУЖОЙ_ID, full_name: "Коллега" } })]),
    );
    applyWsEvent({
      type: "conversation:updated",
      ts: "2026-09-06T10:00:00Z",
      data: {
        conversation_id: "conv-1",
        patch: {
          status: "in_progress",
          assignee: { id: ЧУЖОЙ_ID, full_name: "Коллега" },
          in_inbox: false,
          transfer: {
            to: { id: МОЙ_ID, full_name: "Я" },
            by: { id: ЧУЖОЙ_ID, full_name: "Коллега" },
            at: "2026-09-06T10:00:00Z",
            comment: "посмотри, пожалуйста",
          },
        },
      },
    });
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "передача мне не дошла до списка «Моих»").toBe(1);
    expect(счёт.числа, "«на руках» после передачи мне не пересчитано").toBe(1);
  });

  it("⚠ «позвали в диалог» — кадр без патча — спрашивает список: состав «Моих» сменился", async () => {
    // `routes/conversations.py`: приглашение и выход шлют `message:new` с
    // системной записью и БЕЗ `conversation_patch`; по строке перемены не видно.
    const системная: MessageDto = {
      ...сообщение("m-sys", "conv-1", "system"),
      sender_type: "system",
      body: "Коллега позвал(а) в диалог: Я",
    };
    applyWsEvent({
      type: "message:new",
      ts: "2026-09-06T10:00:00Z",
      data: { conversation_id: "conv-1", message: системная },
    } as unknown as WsServerEvent);
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список, "позванный не увидит диалог в «Моих» до тихой сверки").toBe(1);
  });

  it("а своя отправка — тот же `applyNewMessage` без патча — сервера не касается", async () => {
    // `useSendMessage.onMutate` кладёт временное сообщение этим же путём и
    // без патча; правило «кадр без патча спрашивает список» живёт в
    // `applyWsEvent`, а не здесь, — иначе каждая отправка заказывала бы список.
    applyNewMessage("conv-1", сообщение("m-temp", "conv-1", "out"));
    await vi.advanceTimersByTimeAsync(ПОЛНОЕ_ОКНО);

    expect(счёт.список + счёт.числа, "своя отправка пошла за списком").toBe(0);
  });
});

describe("Тихая сверка сверяет чаты, а не всё подряд", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    queryClient.clear();
    useChatUiStore.setState({ activeConversationId: "conv-1" });
    useSessionStore.setState({ permissions: ["messages:send"] as never });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { count: 0, escalated: 0 })),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("⚠ шаблоны (staleTime 5 мин) сверка не трогает; ключи чатов — да", async () => {
    /*
     * До 06.09 сверка шла сплошным `invalidateQueries({ refetchType: "active" })`:
     * на экране чатов это 10–17 запросов раз в две минуты с каждой из 11–13
     * вкладок, включая шаблоны, которые не меняются неделями.
     */
    let шаблоныЗапрошены = 0;
    queryClient.setQueryData(qk.templates.list("all"), { items: [] });
    const шаблоны = new QueryObserver(queryClient, {
      queryKey: qk.templates.list("all"),
      queryFn: async () => {
        шаблоныЗапрошены += 1;
        return { items: [] };
      },
      staleTime: 5 * 60_000,
    });
    const отписка = шаблоны.subscribe(() => {});
    const шпион = vi.spyOn(queryClient, "invalidateQueries");

    resyncNow(true);
    await vi.advanceTimersByTimeAsync(100);

    const ключи = шпион.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(шпион.mock.calls.some((c) => c[0]?.queryKey === undefined), "сплошная сверка вернулась").toBe(false);
    expect(ключи).toContain(JSON.stringify(CONVERSATIONS_LIST_KEY));
    expect(ключи).toContain(JSON.stringify(qk.conversations.counts));
    expect(ключи).toContain(JSON.stringify(qk.inbox.list));
    expect(ключи, "открытая лента не сверяется — потерянный кадр останется потерянным").toContain(
      JSON.stringify(qk.messages.list("conv-1")),
    );
    expect(шаблоныЗапрошены, "сверка сходила за шаблонами").toBe(0);
    отписка();
  });

});
