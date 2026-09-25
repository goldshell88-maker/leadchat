import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useInfiniteQuery, useQuery, type InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { fetchConversations, fetchTabCounts } from "@/features/chats/api";
import { BlockClientDialog } from "@/features/chats/components/card/BlockClientButton";
import { TransferBar } from "@/features/chats/components/thread/TransferBar";
import { qk, type ConversationFilters } from "@/shared/api/queryKeys";
import type {
  ConversationDetailDto,
  ConversationDto,
  ConversationsPage,
  TabCounts,
} from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * ПЕРЕДАЧА И ПОМЕТКА КЛИЕНТА — ОДИН КРУГ, А НЕ ТРИ (замер 06.09).
 *
 * В обоих местах стояло: POST → `await` инвалидации детали → `await`
 * инвалидации корня `conversations` — три последовательных круга ≈105 мс +
 * 3×RTT со спиннером на кнопке, а корень накрывал все выдачи и деталь разом:
 * 3–4 лишних запроса на одно нажатие. Ответ ручки при этом уже нёс всё, что
 * нужно экрану.
 *
 * ⚠ НА ЭКРАНЕ ЖИВУТ НАБЛЮДАТЕЛИ СПИСКА И ЧИСЕЛ (`ListProbe`), И ЭТО НЕ
 * УКРАШЕНИЕ. Без них любая инвалидация молчит — ключ без наблюдателя не
 * перезапрашивается, — и проверка «ни одного GET» зеленела бы даже с прежней
 * цепочкой. Диверсия «вернуть инвалидацию корня» обязана красить её.
 */
const GIVER = { id: "u-giver", full_name: "Анна Отдающая" };
const TAKER = { id: "u-taker", full_name: "Борис Принимающий" };
const МОИ: ConversationFilters = { tab: "mine" };
const КЛИЕНТ = "client-1";
const ДРУГОЙ_КЛИЕНТ = "client-2";

function сПредложением(): ConversationDetailDto {
  return makeConversation({
    assignee: GIVER,
    transfer: {
      to: TAKER,
      by: GIVER,
      at: "2026-09-06T09:00:00Z",
      comment: "Клиент из Балашихи, это твой район",
    },
  });
}

/**
 * Предложенный мне диалог, как его отдаёт сервер в «Моих» получателя: хозяин
 * прежний, предложение висит, клиент ждёт. Сервер держит такие в «Моих» и
 * считает в числах ещё ДО принятия (`mine_condition`/`own_condition`:
 * `transfer_to_id == me`).
 */
function ожидающийПредложения(): ConversationDto {
  return {
    ...сПредложением(),
    last_message_at: "2026-09-06T09:30:00Z",
    waiting_since: "2026-09-06T09:20:00Z",
  };
}

function строка(id: string, последнее: string, clientId = КЛИЕНТ): ConversationDto {
  const base = makeConversation();
  return { ...base, id, last_message_at: последнее, client: { ...base.client, id: clientId } };
}

function засеять(filters: ConversationFilters, rows: ConversationDto[]): void {
  queryClient.setQueryData<InfiniteData<ConversationsPage>>(qk.conversations.list(filters), {
    pages: [{ items: rows, page: { limit: 50, offset: 0, total: rows.length } }],
    pageParams: [0],
  });
}

function строки(filters: ConversationFilters): ConversationDto[] {
  return (
    queryClient
      .getQueryData<InfiniteData<ConversationsPage>>(qk.conversations.list(filters))
      ?.pages.flatMap((p) => p.items) ?? []
  );
}

function деталь(): ConversationDetailDto | undefined {
  return queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail(CONV_ID));
}

/**
 * Живые наблюдатели списка «Моих» и чисел — как `ChatListPane` на экране.
 * Данные засеяны до монтирования и свежи, поэтому сами по себе они ничего не
 * запрашивают; запросят — только если кто-то их инвалидирует.
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

describe("Передача и пометка клиента — один круг", () => {
  let запросы: Array<{ url: string; method: string }> = [];
  /** Что сервер добавит к ответу на отказ — ожидание, участники. */
  let ответНаОтказ: Partial<ConversationDetailDto> = {};
  const как = (user: { id: string; full_name: string }) =>
    resetSessionStore({
      user: { ...fakeUser, id: user.id, full_name: user.full_name },
      permissions: ["messages:send", "conversations:read", "conversations:manage"],
      accessToken: "t",
      bootstrapped: true,
    });

  beforeEach(() => {
    queryClient.clear();
    запросы = [];
    ответНаОтказ = {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        запросы.push({ url, method });
        if (method === "POST" && url.includes("/transfer/accept")) {
          return jsonResponse(200, {
            ...сПредложением(),
            assignee: TAKER,
            transfer: null,
            status_since: "2026-09-06T09:05:00Z",
            last_message_at: "2026-09-06T10:00:00Z",
          });
        }
        if (method === "POST" && url.includes("/transfer/")) {
          return jsonResponse(200, { ...сПредложением(), transfer: null, ...ответНаОтказ });
        }
        if (method === "POST" && url.endsWith("/block")) {
          return jsonResponse(200, {
            id: КЛИЕНТ,
            name: "Иван Петров",
            phone: null,
            blocked: true,
            blocked_at: "2026-09-06T10:00:00Z",
            blocked_by: null,
            blocked_reason: "Спам",
          });
        }
        if (method === "POST" && url.endsWith("/unblock")) {
          return jsonResponse(200, {
            id: КЛИЕНТ,
            name: "Иван Петров",
            phone: null,
            blocked: false,
            blocked_at: null,
            blocked_by: null,
            blocked_reason: null,
          });
        }
        if (url.includes("/conversations/counts")) return jsonResponse(200, { mine: 0, mine_waiting: 0 });
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const методы = () => запросы.map((r) => r.method);

  it("принял передачу: деталь и строка из ответа, дубля в «Моих» нет, числа не тронуты, ни одного GET", async () => {
    как(TAKER);
    // Деталь закреплена мной: `_transfer_reply` собирает деталь без `viewer`
    // (`pinned: false` всем), и ответ ручки закрепление снять не вправе.
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), { ...сПредложением(), pinned: true });
    // Предложенный мне диалог в «Моих» УЖЕ стоит, и числа его уже считают.
    засеять(МОИ, [ожидающийПредложения(), строка("conv-old", "2026-09-06T09:00:00Z")]);
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 2, mine_waiting: 1 });
    renderWithProviders(
      <>
        <ListProbe />
        <TransferBar conversation={сПредложением()} />
      </>,
    );
    expect(запросы, "наблюдатели запросили свежие данные сами").toEqual([]);

    await userEvent.click(screen.getByRole("button", { name: "Принять диалог" }));

    await waitFor(() => expect(деталь()?.transfer).toBeNull());
    expect(деталь()?.assignee).toEqual(TAKER);
    expect(деталь()?.pinned, "ответ ручки снял закрепление").toBe(true);
    // Строка одна, уже моя и без предложения — из ответа, а не из перезапроса.
    expect(строки(МОИ).map((r) => r.id)).toEqual([CONV_ID, "conv-old"]);
    expect(строки(МОИ)[0].assignee).toEqual(TAKER);
    expect(строки(МОИ)[0].transfer).toBeNull();
    // Сервер посчитал диалог ещё при предложении — второй раз не считаем.
    expect(
      queryClient.getQueryData<TabCounts>(qk.conversations.counts),
      "числа сдвинуты второй раз",
    ).toEqual({ mine: 2, mine_waiting: 1 });
    // Спиннер погас по ответу POST, а не после цепочки инвалидаций.
    expect(screen.getByRole("button", { name: "Принять диалог" })).toBeEnabled();

    await подождатьОкно();
    expect(методы(), "к одному POST добавились GET списка или чисел").toEqual(["POST"]);
  });

  it("принял, а в «Моих» строки ещё не было (кэш старше предложения): встаёт из ответа, числа не гадаем", async () => {
    как(TAKER);
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), сПредложением());
    засеять(МОИ, [строка("conv-old", "2026-09-06T09:00:00Z")]);
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 1, mine_waiting: 0 });
    renderWithProviders(
      <>
        <ListProbe />
        <TransferBar conversation={сПредложением()} />
      </>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Принять диалог" }));

    await waitFor(() => expect(строки(МОИ).map((r) => r.id)).toEqual([CONV_ID, "conv-old"]));
    // Кэш старше предложения — и числа такие же; чинит их ближайшая сверка.
    expect(queryClient.getQueryData<TabCounts>(qk.conversations.counts)).toEqual({
      mine: 1,
      mine_waiting: 0,
    });
    await подождатьОкно();
    expect(методы()).toEqual(["POST"]);
  });

  it("отказался от передачи: строка ушла из моих «Моих», числа −1, во «Всех» хозяин прежний, ни одного GET", async () => {
    как(TAKER);
    ответНаОтказ = { waiting_since: "2026-09-06T09:20:00Z" };
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), сПредложением());
    const ВСЕ: ConversationFilters = { tab: "all" };
    засеять(ВСЕ, [ожидающийПредложения()]);
    // В моих «Моих» диалог стоял как предложенный — после отказа ему там не место.
    засеять(МОИ, [ожидающийПредложения(), строка("conv-old", "2026-09-06T09:00:00Z")]);
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 2, mine_waiting: 1 });
    renderWithProviders(
      <>
        <ListProbe />
        <TransferBar conversation={сПредложением()} />
      </>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Отказаться" }));

    await waitFor(() => expect(деталь()?.transfer).toBeNull());
    expect(строки(ВСЕ)[0].transfer).toBeNull();
    expect(строки(ВСЕ)[0].assignee).toEqual(GIVER);
    expect(строки(МОИ).map((r) => r.id), "чужой диалог остался в «Моих» отказавшегося").toEqual([
      "conv-old",
    ]);
    expect(queryClient.getQueryData<TabCounts>(qk.conversations.counts)).toEqual({
      mine: 1,
      mine_waiting: 0,
    });

    await подождатьОкно();
    expect(методы()).toEqual(["POST"]);
  });

  it("отказался, но я позван в диалог: строка в «Моих» остаётся, «на руках» прежнее, «не отвечено» −1", async () => {
    как(TAKER);
    ответНаОтказ = {
      waiting_since: "2026-09-06T09:20:00Z",
      participants: [{ ...TAKER, reason: null, invited_at: null, kind: "invited" }],
    };
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), сПредложением());
    засеять(МОИ, [ожидающийПредложения(), строка("conv-old", "2026-09-06T09:00:00Z")]);
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 2, mine_waiting: 1 });
    renderWithProviders(
      <>
        <ListProbe />
        <TransferBar conversation={сПредложением()} />
      </>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Отказаться" }));

    await waitFor(() => expect(деталь()?.transfer).toBeNull());
    expect(строки(МОИ).map((r) => r.id)).toEqual([CONV_ID, "conv-old"]);
    expect(queryClient.getQueryData<TabCounts>(qk.conversations.counts)).toEqual({
      mine: 2,
      mine_waiting: 0,
    });
    await подождатьОкно();
    expect(методы()).toEqual(["POST"]);
  });

  it("пометил клиента: деталь и все его строки из ответа, окно закрылось, ни одного GET", async () => {
    как(TAKER);
    const onClose = vi.fn();
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    засеять(МОИ, [
      строка(CONV_ID, "2026-09-06T10:00:00Z"),
      строка("conv-same-client", "2026-09-06T09:30:00Z"),
      строка("conv-other", "2026-09-06T09:00:00Z", ДРУГОЙ_КЛИЕНТ),
    ]);
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 3, mine_waiting: 0 });
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <ListProbe />
        <BlockClientDialog clientId={КЛИЕНТ} convId={CONV_ID} blocked={false} opened onClose={onClose} />
      </>,
    );

    await user.type(await screen.findByLabelText("Почему"), "Спам");
    await user.click(screen.getByRole("button", { name: "Пометить" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(деталь()?.client.blocked).toBe(true);
    expect(деталь()?.client.blocked_reason).toBe("Спам");
    const [своя, тогоЖе, чужая] = строки(МОИ);
    expect(своя.client.blocked).toBe(true);
    expect(тогоЖе.client.blocked, "второй диалог того же клиента остался без пометки").toBe(true);
    expect(чужая.client.blocked, "пометка задела чужого клиента").toBeFalsy();

    await подождатьОкно();
    expect(методы()).toEqual(["POST"]);
  });

  it("снял пометку: та же дорога назад", async () => {
    как(TAKER);
    const onClose = vi.fn();
    const помеченный = makeConversation();
    помеченный.client = { ...помеченный.client, blocked: true, blocked_reason: "Спам" };
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), помеченный);
    засеять(МОИ, [{ ...строка(CONV_ID, "2026-09-06T10:00:00Z"), client: помеченный.client }]);
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 1, mine_waiting: 0 });
    renderWithProviders(
      <>
        <ListProbe />
        <BlockClientDialog clientId={КЛИЕНТ} convId={CONV_ID} blocked opened onClose={onClose} />
      </>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Снять пометку" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(деталь()?.client.blocked).toBe(false);
    expect(деталь()?.client.blocked_reason).toBeNull();
    expect(строки(МОИ)[0].client.blocked).toBe(false);

    await подождатьОкно();
    expect(методы()).toEqual(["POST"]);
  });
});
