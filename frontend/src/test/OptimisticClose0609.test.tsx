import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { notifications } from "@mantine/notifications";
import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { useChangeStatus } from "@/features/chats/hooks/useConversationActions";
import { inboxRows } from "@/features/chats/inbox/api";
import { qk, type ConversationFilters } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, ConversationDto, ConversationsPage } from "@/shared/api/types";
import { refetchListsNow } from "@/shared/realtime/listRefetch";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, wrap } from "./render";

/** Как `ChatsPage`: адрес сменился — панель сообщает, какой диалог открыт теперь. */
const navigateSpy = vi.fn((to: unknown) => {
  const m = /^\/chats\/([^/?#]+)/.exec(String(to));
  useChatUiStore.getState().setActive(m ? m[1] : null);
});
vi.mock("react-router-dom", async () => {
  const real = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...real, useNavigate: () => navigateSpy };
});

vi.mock("@/shared/realtime/listRefetch", async (importOriginal) => {
  const настоящий = await importOriginal<typeof import("@/shared/realtime/listRefetch")>();
  return { ...настоящий, refetchListsNow: vi.fn() };
});

/**
 * ЗАКРЫТИЕ — ВСЁ ВИДИМОЕ ДЕЛАЕТСЯ ДО ОТВЕТА СЕРВЕРА (замер 06.09).
 *
 * nginx за 8 часов боя: `PATCH /status` 377 раз, p50 35 мс. Оптимистично
 * правилась только деталь; строка списка и переход к следующему ждали
 * `onSuccess`, уход строки из «Моих» — ещё круг `refetchListsNow`. Человек
 * ждал RTT + 35 мс до перехода и ещё ~50 мс, пока строка исчезнет; при 429 или
 * обрыве — тост и никакого перехода.
 *
 * Здесь заперты обе половины: переход и уход строки ДО ответа — и честный
 * откат, когда сервер отказал (гость с 403 `guest_cannot_close` обязан
 * прочитать слова сервера, а не «что-то не вышло»).
 *
 * ⚠ ОТВЕТ СЕРВЕРА ЗДЕСЬ ПОД НАШИМ КОНТРОЛЕМ: `fetch` на PATCH отдаёт обещание,
 * которое разрешаем сами. «До ответа» — не гонка с таймером, а порядок вызовов.
 */
const МОИ: ConversationFilters = { tab: "mine" };
const ВСЕ: ConversationFilters = { tab: "all" };
const СЛЕДУЮЩИЙ = "conv-next";
const ТРЕТИЙ = "conv-third";

function строка(id: string, последнее: string): ConversationDto {
  return { ...makeConversation(), id, last_message_at: последнее };
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

function деталь(id = CONV_ID): ConversationDetailDto | undefined {
  return queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail(id));
}

/** Три строки «Моих» в порядке сервера: закрываемая сверху, за ней — следующий. */
function триСтроки(): ConversationDto[] {
  return [
    строка(CONV_ID, "2026-09-06T10:05:00Z"),
    строка(СЛЕДУЮЩИЙ, "2026-09-06T10:00:00Z"),
    строка(ТРЕТИЙ, "2026-09-06T09:00:00Z"),
  ];
}

function Wrapper({ children }: { children: React.ReactNode }) {
  return wrap(<>{children}</>);
}

describe("Закрытие диалога — до ответа сервера", () => {
  let ответить: (r: Response) => void = () => {};
  let запросы: Array<{ url: string; method: string }> = [];

  beforeEach(() => {
    queryClient.clear();
    navigateSpy.mockClear();
    vi.mocked(refetchListsNow).mockClear();
    запросы = [];
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({
      filters: МОИ,
      inboxOpen: false,
      drafts: {},
      activeConversationId: CONV_ID,
    });
    useInboxStore.getState().clear();
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        запросы.push({ url, method });
        if (method === "PATCH" && url.includes("/status")) {
          return new Promise<Response>((resolve) => {
            ответить = resolve;
          });
        }
        return Promise.resolve(
          jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }),
        );
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const патчУшёл = () => запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"));

  it("к следующему уходим ДО ответа сервера, и строка уже ушла из «Моих»", async () => {
    засеять(МОИ, триСтроки());
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    const { result } = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });

    result.current.mutate({ status: "closed" });

    await waitFor(() =>
      expect(navigateSpy).toHaveBeenCalledWith(`/chats/${СЛЕДУЮЩИЙ}`, { replace: true }),
    );
    await waitFor(() => expect(патчУшёл()).toBe(true));
    // Сервер ещё молчит (`ответить` не звали), а экран уже на месте:
    expect(строки(МОИ).map((r) => r.id), "строка ждёт ответа сервера").toEqual([
      СЛЕДУЮЩИЙ,
      ТРЕТИЙ,
    ]);
    expect(деталь()?.status).toBe("closed");
    expect(vi.mocked(refetchListsNow), "сверка ушла вместе с PATCH").not.toHaveBeenCalled();

    ответить(
      jsonResponse(200, {
        ...makeConversation(),
        status: "closed",
        status_since: "2026-09-06T10:06:00Z",
      }),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // Сверка с сервером — ПОСЛЕ ответа и в фоне; второго перехода нет.
    expect(vi.mocked(refetchListsNow)).toHaveBeenCalledTimes(1);
    expect(navigateSpy).toHaveBeenCalledTimes(1);
  });

  it("сервер отказал — строка вернулась на своё место, человек вернулся в диалог, тост словами сервера", async () => {
    const show = vi.spyOn(notifications, "show").mockImplementation(() => "id");
    засеять(МОИ, триСтроки());
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    const { result } = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });

    result.current.mutate({ status: "closed" });
    await waitFor(() =>
      expect(navigateSpy).toHaveBeenCalledWith(`/chats/${СЛЕДУЮЩИЙ}`, { replace: true }),
    );
    await waitFor(() => expect(патчУшёл()).toBe(true));
    expect(строки(МОИ).map((r) => r.id)).toEqual([СЛЕДУЮЩИЙ, ТРЕТИЙ]);

    // Гость закрывает чужой диалог — сервер называет хозяина и подсказывает кнопку.
    const слова = "Диалог ведёт Зуев Денис. Чтобы убрать его у себя, нажмите «Выйти»";
    ответить(jsonResponse(403, errorEnvelope("forbidden", слова, { reason: "guest_cannot_close" })));
    await waitFor(() => expect(result.current.isError).toBe(true));

    // Ровно то место, где стояла, — не хвост и не верх.
    expect(строки(МОИ).map((r) => r.id), "строка не вернулась на место").toEqual([
      CONV_ID,
      СЛЕДУЮЩИЙ,
      ТРЕТИЙ,
    ]);
    expect(строки(МОИ)[0].status, "строка вернулась закрытой").toBe("in_progress");
    expect(деталь()?.status).toBe("in_progress");
    expect(navigateSpy, "человека не вернули в диалог").toHaveBeenLastCalledWith(
      `/chats/${CONV_ID}`,
      { replace: true },
    );
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Диалог ведёт коллега", message: слова }),
    );
    expect(vi.mocked(refetchListsNow), "после отказа сверять нечего").not.toHaveBeenCalled();
  });

  it("человек сам ушёл дальше, пока сервер думал, — назад его не дёргаем", async () => {
    /*
     * Ответ на закрытие едет, а человек уже прошёл по списку дальше (или
     * закрыл и следующий). Вернуть его в первый диалог после отказа — то самое
     * «самовольное переключение», которое владелец просил убрать. Строка
     * вернулась, тост назвал причину — этого достаточно.
     */
    const show = vi.spyOn(notifications, "show").mockImplementation(() => "id");
    засеять(МОИ, триСтроки());
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    const { result } = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });

    result.current.mutate({ status: "closed" });
    await waitFor(() =>
      expect(navigateSpy).toHaveBeenCalledWith(`/chats/${СЛЕДУЮЩИЙ}`, { replace: true }),
    );
    await waitFor(() => expect(патчУшёл()).toBe(true));
    // Пока ответ ехал — открыл третий сам.
    useChatUiStore.getState().setActive(ТРЕТИЙ);

    ответить(jsonResponse(429, errorEnvelope("rate_limited", "Слишком часто")));
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(строки(МОИ).map((r) => r.id)).toEqual([CONV_ID, СЛЕДУЮЩИЙ, ТРЕТИЙ]);
    expect(navigateSpy, "человека дёрнули из диалога, который он открыл сам").toHaveBeenCalledTimes(
      1,
    );
    expect(show).toHaveBeenCalled();
  });

  it("во «Всех» строка остаётся, но уже закрытой; перехода нет — и откат её перекрашивает назад", async () => {
    /*
     * «Все» — витрина, закрытые там показываются; уносить человека в соседнюю
     * строку значит потерять то, что он читал. Строка при этом обязана
     * перекраситься сразу — иначе «закрыл, а в списке живой».
     */
    useChatUiStore.setState({ filters: ВСЕ });
    засеять(ВСЕ, триСтроки());
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    const { result } = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });

    result.current.mutate({ status: "closed" });
    await waitFor(() => expect(патчУшёл()).toBe(true));

    expect(navigateSpy).not.toHaveBeenCalled();
    expect(строки(ВСЕ).map((r) => r.id)).toEqual([CONV_ID, СЛЕДУЮЩИЙ, ТРЕТИЙ]);
    expect(строки(ВСЕ)[0].status, "строка во «Всех» не перекрасилась").toBe("closed");

    ответить(jsonResponse(429, errorEnvelope("rate_limited", "Слишком часто")));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(строки(ВСЕ)[0].status, "после отказа строка осталась закрытой").toBe("in_progress");
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("из очереди: строка уходит из «Входящих» сразу, а при отказе возвращается со счётчиком", async () => {
    useChatUiStore.setState({ inboxOpen: true });
    const вОчереди = [
      { ...строка(CONV_ID, "2026-09-06T10:05:00Z"), in_inbox: true, offered_at: "2026-09-06T09:00:00Z" },
      { ...строка("q-2", "2026-09-06T10:00:00Z"), in_inbox: true, offered_at: "2026-09-06T09:30:00Z" },
    ];
    queryClient.setQueryData<InfiniteData<ConversationsPage>>(qk.inbox.list, {
      pages: [{ items: вОчереди, page: { limit: 50, offset: 0, total: 2 } }],
      pageParams: [0],
    });
    useInboxStore.getState().seedFromServer([CONV_ID, "q-2"], 2);
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    const { result } = renderHook(() => useChangeStatus(CONV_ID), { wrapper: Wrapper });

    result.current.mutate({ status: "closed" });
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith("/chats/q-2", { replace: true }));
    expect(inboxRows().map((r) => r.id)).toEqual(["q-2"]);
    expect(useInboxStore.getState().count).toBe(1);

    await waitFor(() => expect(патчУшёл()).toBe(true));
    // Необработанное закрывает только администратор — обычный отказ очереди.
    ответить(
      jsonResponse(
        403,
        errorEnvelope("forbidden", "Это обращение ещё никто не взял", {
          reason: "queued_close_forbidden",
        }),
      ),
    );
    await waitFor(() => expect(result.current.isError).toBe(true));
    // Ждёт дольше — стоит выше: строка встала на своё место по `offered_at`.
    expect(inboxRows().map((r) => r.id), "строка не вернулась в очередь").toEqual([CONV_ID, "q-2"]);
    expect(useInboxStore.getState().count, "счётчик очереди не вернулся").toBe(2);
    expect(navigateSpy).toHaveBeenLastCalledWith(`/chats/${CONV_ID}`, { replace: true });
  });

  it("ответ на закрытие чинит ПРЕЖНИЙ диалог, а не тот, что открыт теперь", async () => {
    /*
     * ⚠ `ChatThreadPane` не перемонтируется при переходе — ему приходит новый
     * `convId`, и хук перерисовывается. TanStack Query подменяет колбэки
     * висящей мутации на свежие при каждой перерисовке наблюдателя: без
     * защиты ответ на закрытие старого диалога прилетал бы в `onSuccess` с
     * `convId` НОВОГО, и «закрытым» в кэше оказывался бы тот, что человек
     * только что открыл. Здесь хук перерисовывается с новым id ровно так, как
     * это делает панель.
     */
    засеять(МОИ, триСтроки());
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    queryClient.setQueryData(
      qk.conversations.detail(СЛЕДУЮЩИЙ),
      makeConversation({ id: СЛЕДУЮЩИЙ, status: "in_progress" }),
    );
    const { result, rerender } = renderHook(({ id }) => useChangeStatus(id), {
      wrapper: Wrapper,
      initialProps: { id: CONV_ID },
    });

    result.current.mutate({ status: "closed" });
    await waitFor(() => expect(navigateSpy).toHaveBeenCalled());
    await waitFor(() => expect(патчУшёл()).toBe(true));

    rerender({ id: СЛЕДУЮЩИЙ });
    // Новому диалогу чужой ответ не мешает: «Закрыть» и Ctrl+D у него не ждут.
    expect(result.current.isPending, "спиннер прежнего закрытия перешёл на новый диалог").toBe(
      false,
    );

    ответить(
      jsonResponse(200, {
        ...makeConversation(),
        status: "closed",
        status_since: "2026-09-06T10:06:00Z",
      }),
    );
    await waitFor(() => expect(деталь(CONV_ID)?.status_since).toBe("2026-09-06T10:06:00Z"));
    expect(деталь(СЛЕДУЮЩИЙ)?.status, "закрытым стал только что открытый диалог").toBe(
      "in_progress",
    );
    expect(vi.mocked(refetchListsNow)).toHaveBeenCalledTimes(1);
  });
});
