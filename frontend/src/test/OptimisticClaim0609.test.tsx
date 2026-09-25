import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useInfiniteQuery, useQuery, type InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { fetchConversations, fetchTabCounts } from "@/features/chats/api";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import { qk, type ConversationFilters } from "@/shared/api/queryKeys";
import type {
  ConversationDetailDto,
  ConversationDto,
  ConversationsPage,
  TabCounts,
} from "@/shared/api/types";
import { refetchListsNow } from "@/shared/realtime/listRefetch";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

vi.mock("@/features/chats/inbox/sound", () => ({ playInboxChime: vi.fn() }));

/*
 * Сверка с сервером здесь заглушена целиком — и своя (`refetchListsNow`), и
 * отложенная из патча строки (`scheduleListRefetch`): она есть, но строка её
 * не ждёт. Любой ДРУГОЙ поход за списком или числами на живых наблюдателях
 * ниже станет лишним GET — и покрасит проверку «ровно один POST».
 */
vi.mock("@/shared/realtime/listRefetch", async (importOriginal) => {
  const настоящий = await importOriginal<typeof import("@/shared/realtime/listRefetch")>();
  return { ...настоящий, refetchListsNow: vi.fn(), scheduleListRefetch: vi.fn() };
});

/**
 * ПРИНЯЛ — СТРОКА В «МОИХ» И ЧИСЛА НАД ВКЛАДКОЙ ИЗ ОТВЕТА РУЧКИ (замер 06.09).
 *
 * nginx за 8 часов боя: `POST /claim` 552 раза, p50 43 мс; строка в «Моих»
 * появлялась только после ВТОРОГО круга — список плюс числа, — 3–5 запросов
 * на каждый приём. Ответ ручки при этом везёт диалог целиком.
 *
 * Здесь стережётся, что после ОДНОГО POST строка стоит на своём месте по
 * порядку сервера и «Мои N» сдвинуто — без единого GET. Сверка с сервером
 * остаётся, но в фоне: она подтверждает, а не показывает впервые.
 *
 * ⚠ САМ POST ОСТАЁТСЯ БЛОКИРУЮЩИМ: гонку двух нажатий решает сервер, и
 * показать «взял», чтобы через 200 мс отобрать, хуже ожидания ответа. Строка
 * появляется по ответу, а не по нажатию, — это проверяется тем, что до ответа
 * кэш пуст.
 */
const МОИ: ConversationFilters = { tab: "mine" };
const СТАРАЯ = "conv-old";
const MANAGER = fakeMe.permissions as never;

function строка(id: string, последнее: string): ConversationDto {
  return { ...makeConversation(), id, last_message_at: последнее };
}

function засеять(filters: ConversationFilters, rows: ConversationDto[]): void {
  queryClient.setQueryData<InfiniteData<ConversationsPage>>(qk.conversations.list(filters), {
    pages: [{ items: rows, page: { limit: 50, offset: 0, total: rows.length } }],
    pageParams: [0],
  });
}

function строки(filters: ConversationFilters): string[] {
  return (
    queryClient
      .getQueryData<InfiniteData<ConversationsPage>>(qk.conversations.list(filters))
      ?.pages.flatMap((p) => p.items.map((r) => r.id)) ?? []
  );
}

function числа(): TabCounts | undefined {
  return queryClient.getQueryData<TabCounts>(qk.conversations.counts);
}

/**
 * Живые наблюдатели «Моих» и чисел — как `ChatListPane` на экране (ревью 06.09).
 *
 * ⚠ БЕЗ НИХ «НИ ОДНОГО GET» — ПУСТЫЕ СЛОВА: ключ без наблюдателя не
 * перезапрашивается, и прямая инвалидация списков в пути принятия оставалась
 * бы зелёной. Данные засеяны до монтирования и свежи — сами по себе они ничего
 * не спрашивают.
 */
function ListProbe() {
  useInfiniteQuery({
    queryKey: qk.conversations.list(МОИ),
    queryFn: ({ pageParam }) => fetchConversations(МОИ, pageParam),
    initialPageParam: 0,
    getNextPageParam: () => undefined,
  });
  useQuery({ queryKey: qk.conversations.counts, queryFn: fetchTabCounts });
  return null;
}

/** Дать отработать любому отложенному перезапросу (окно схлопывания — 200 мс). */
const подождатьОкно = () => new Promise((r) => setTimeout(r, 300));

/** Диалог в очереди — так его видит низ панели: `in_inbox` и без хозяина. */
const вОчереди = () => makeConversation({ status: "new", assignee: null, in_inbox: true } as never);

/** Ответ `POST /claim`: тот же диалог, уже мой, свежее строк «Моих» и ждёт ответа. */
function взятый(over: Partial<ConversationDetailDto> = {}): ConversationDetailDto {
  return makeConversation({
    status: "in_progress",
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    last_message_at: "2026-09-06T10:00:00Z",
    waiting_since: "2026-09-06T09:59:00Z",
    ...over,
  });
}

describe("Принятие из очереди — строка и числа из ответа", () => {
  let запросы: Array<{ url: string; method: string }> = [];
  let ответНаПриём: ConversationDetailDto;

  beforeEach(() => {
    queryClient.clear();
    vi.mocked(refetchListsNow).mockClear();
    запросы = [];
    ответНаПриём = взятый();
    resetSessionStore({ user: fakeUser, permissions: MANAGER, accessToken: "t", bootstrapped: true });
    useChatUiStore.setState({ filters: МОИ, inboxOpen: true, activeConversationId: CONV_ID, drafts: {} });
    useInboxStore.getState().clear();
    useInboxStore.getState().seedFromServer([CONV_ID], 1);
    засеять(МОИ, [строка(СТАРАЯ, "2026-09-06T09:00:00Z")]);
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 3, mine_waiting: 1 });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        запросы.push({ url, method });
        if (method === "POST" && url.endsWith("/claim")) {
          return jsonResponse(200, { conversation: ответНаПриём, count: 0, escalated: 0 });
        }
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
  });

  afterEach(() => {
    useInboxStore.getState().clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function принять() {
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <ListProbe />
        <ThreadFooter convId={CONV_ID} conversation={вОчереди()} />
      </>,
    );
    await user.click(await screen.findByRole("button", { name: "Принять диалог" }));
    await waitFor(() => expect(запросы.some((r) => r.url.endsWith("/claim"))).toBe(true));
  }

  it("строка встаёт в «Мои» на своё место, числа сдвинуты — и ни одного GET", async () => {
    await принять();

    // Свежее старой строки — значит выше неё, как расставил бы сервер.
    await waitFor(() => expect(строки(МОИ)).toEqual([CONV_ID, СТАРАЯ]));
    expect(числа(), "числа над вкладкой ждут перезапроса").toEqual({ mine: 4, mine_waiting: 2 });
    // Один запрос — и это POST. Список и числа сервера не спрашивали.
    expect(запросы.map((r) => r.method)).toEqual(["POST"]);
    // Сверка с сервером остаётся — в фоне, а не как источник строки.
    expect(vi.mocked(refetchListsNow)).toHaveBeenCalledTimes(1);
    // И после окна схлопывания наблюдатели ничего не заказали.
    await подождатьОкно();
    expect(запросы.map((r) => r.method), "к POST добавились GET списка или чисел").toEqual([
      "POST",
    ]);
  });

  it("не ждёт ответа — строка уже своя; дубля при повторе не плодится", async () => {
    /*
     * Строка «Моих» из прошлой выдачи уже могла содержать этот диалог (кадр
     * `conversation:updated` приехал раньше ответа). Вставка обязана это
     * заметить, иначе одна и та же строка стоит дважды.
     */
    засеять(МОИ, [строка(CONV_ID, "2026-09-06T10:00:00Z"), строка(СТАРАЯ, "2026-09-06T09:00:00Z")]);
    await принять();

    await waitFor(() => expect(числа()?.mine).toBe(4));
    expect(строки(МОИ)).toEqual([CONV_ID, СТАРАЯ]);
  });

  it("в поисковую выдачу и в чужой срез не вставляется", async () => {
    /*
     * Поиск — про запрос человека, «ждут ответа» — про долг: угадать, попадёт
     * ли туда взятый диалог, нельзя без сервера. Такие выдачи не трогаем,
     * фоновая сверка их поправит сама.
     */
    const ПОИСК: ConversationFilters = { tab: "mine", q: "иван" };
    const ЧУЖОЙ_АККАУНТ: ConversationFilters = { tab: "mine", accountId: "acc-other" };
    засеять(ПОИСК, [строка(СТАРАЯ, "2026-09-06T09:00:00Z")]);
    засеять(ЧУЖОЙ_АККАУНТ, [строка(СТАРАЯ, "2026-09-06T09:00:00Z")]);
    await принять();

    await waitFor(() => expect(строки(МОИ)).toEqual([CONV_ID, СТАРАЯ]));
    expect(строки(ПОИСК)).toEqual([СТАРАЯ]);
    expect(строки(ЧУЖОЙ_АККАУНТ)).toEqual([СТАРАЯ]);
  });

  it("если ответ отдал диалог не мне — в «Мои» не вставляем и числа не трогаем", async () => {
    /*
     * Ответ ручки — правда о том, чей теперь диалог. Ставить строку в СВОИ по
     * одному факту нажатия значило бы показать чужой диалог как свой.
     */
    ответНаПриём = взятый({ assignee: { id: "u-other", full_name: "Пётр Ковалёв" } });
    await принять();

    await waitFor(() => expect(vi.mocked(refetchListsNow)).toHaveBeenCalled());
    expect(строки(МОИ)).toEqual([СТАРАЯ]);
    expect(числа()).toEqual({ mine: 3, mine_waiting: 1 });
  });
});
