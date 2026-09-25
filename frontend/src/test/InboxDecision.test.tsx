import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { notifications } from "@mantine/notifications";
import type { InfiniteData } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import { useConversationDetail } from "@/features/chats/hooks/useConversationActions";
import { useFocusBus } from "@/features/hotkeys/focusBus";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

vi.mock("@/features/chats/inbox/sound", () => ({ playInboxChime: vi.fn() }));

const NEXT_ID = "q-2";
const PETR = { id: "u-petr", full_name: "Пётр Ковалёв" };

const MANAGER: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "notes:read",
  "notes:write",
  "templates:own",
];

/**
 * Диалог в очереди. `GET /conversations/{id}` полей очереди НЕ отдаёт — деталь
 * приходит без `in_inbox`, и признаком служит состав `GET /inbox` (стор `ids`,
 * его заполняет `seedQueue`). Фикстура повторяет это буквально: если бы она
 * подкладывала `in_inbox: true` в деталь, тест проверял бы контракт, которого
 * у сервера нет.
 */
const queued = () => makeConversation({ status: "new", assignee: null });

const taken = () =>
  makeConversation({
    status: "in_progress",
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
  });

function queueRow(id: string): ConversationDto {
  return { ...queued(), id, client: { id: `client-${id}`, name: "Сергей Орлов", phone: null, avito_rating: null } };
}

/** Кэш очереди: текущий диалог и следующий за ним — есть куда переходить. */
function seedQueue(ids: string[]): void {
  const items = ids.map((id) => (id === CONV_ID ? queued() : queueRow(id)));
  queryClient.setQueryData<InfiniteData<ConversationsPage>>(qk.inbox.list, {
    pages: [{ items, page: { limit: 50, offset: 0, total: items.length } } satisfies ConversationsPage],
    pageParams: [0],
  });
  useInboxStore.getState().seedFromServer(ids, ids.length);
}

function LocationProbe() {
  const { pathname } = useLocation();
  return <div data-testid="loc">{pathname}</div>;
}

/** Низ ленты берёт диалог из кэша детали — как в приложении (03 §2.1). */
function FooterHost() {
  const detail = useConversationDetail(CONV_ID);
  return (
    <>
      <ThreadFooter convId={CONV_ID} conversation={detail.data} />
      <LocationProbe />
    </>
  );
}

/** Принятие диалога из очереди (7.1 п.2–4). */
describe("Диалог из очереди: «Принять» / «Отклонить»", () => {
  let claimStatus: number;
  let posted: string[];
  /** Счётчик СВОЕЙ очереди после действия — сервер считает его сам (01, `*Out`). */
  let countAfterAction: number;

  beforeEach(() => {
    queryClient.clear();
    useInboxStore.getState().clear();
    useChatUiStore.setState({ activeConversationId: CONV_ID, filters: { tab: "all" }, inboxOpen: true });
    resetSessionStore({ user: fakeUser, permissions: MANAGER as never, accessToken: "t", bootstrapped: true });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), queued());
    seedQueue([CONV_ID, NEXT_ID]);
    claimStatus = 200;
    countAfterAction = 1;
    posted = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        const path = url.pathname;
        if (init?.method === "POST") posted.push(path);
        if (path.endsWith("/claim")) {
          return claimStatus === 200
            ? jsonResponse(200, { conversation: taken(), count: countAfterAction, escalated: 0 })
            : jsonResponse(409, errorEnvelope("already_claimed", "Диалог уже принят", { claimed_by: PETR }));
        }
        if (path.endsWith("/decline")) {
          return jsonResponse(200, {
            conversation_id: CONV_ID,
            declined: true,
            already_declined: false,
            reason: null,
            escalated_now: false,
            count: countAfterAction,
            escalated: 0,
          });
        }
        if (path.endsWith("/inbox/count")) {
          return jsonResponse(200, { count: countAfterAction, escalated: 0 });
        }
        if (path.endsWith("/read")) return jsonResponse(204, null);
        if (path.includes(`/conversations/${CONV_ID}`)) {
          return jsonResponse(200, posted.some((p) => p.endsWith("/claim")) ? taken() : queued());
        }
        if (path.endsWith("/inbox")) {
          return jsonResponse(200, { items: [queueRow(NEXT_ID)], page: { limit: 50, offset: 0, total: 1 } });
        }
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("вместо поля ввода — «Принять диалог» и «Отклонить», ленту при этом видно", async () => {
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    expect(await screen.findByRole("button", { name: "Принять диалог" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отклонить" })).toBeInTheDocument();
    // Писать в непринятый диалог нельзя — поля нет вовсе, а не «заблокировано».
    expect(screen.queryByLabelText("Текст сообщения")).toBeNull();
  });

  it("диалог вне очереди остаётся с полем ввода: старое поведение не тронуто", async () => {
    // Ничей диалог со статусом «новый» — так выглядит и очередь, и всё, что
    // заведено до 7.1. Разница ровно одна: этого нет в выдаче `GET /inbox`.
    useInboxStore.getState().clear();
    queryClient.removeQueries({ queryKey: qk.inbox.list });
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    expect(await screen.findByLabelText("Текст сообщения")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Принять диалог" })).toBeNull();
  });

  it("счётчик после отказа берётся из ответа сервера, а не вычитанием", async () => {
    const user = userEvent.setup();
    // Очередь персональная: сервер знает её размер точно, клиент — только
    // приблизительно. Здесь его −1 дал бы 1, а правда — 7.
    seedQueue([CONV_ID, NEXT_ID]);
    countAfterAction = 7;
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    await user.click(await screen.findByRole("button", { name: "Отклонить" }));

    await waitFor(() => expect(useInboxStore.getState().count).toBe(7));
  });

  it("руководитель кнопок не получает: его низ панели не меняется", async () => {
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "conversations:manage", "notes:write"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    expect(await screen.findByText(/Режим просмотра/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Принять диалог" })).toBeNull();
  });

  it("наблюдателю очередь не видна и низ панели остаётся пустым", async () => {
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: ["conversations:read"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    const { container } = renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    await waitFor(() => expect(screen.getByTestId("loc")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Принять диалог" })).toBeNull();
    expect(container.querySelector(".composer")).toBeNull();
    expect(container.querySelector(".inbox-decision")).toBeNull();
  });

  it("принял — появляется поле ввода с фокусом, а ОЧЕРЕДЬ ОСТАЁТСЯ на экране", async () => {
    // UX-аудит, docs/17 §Т6. Раньше принятие переключало вкладку на «Мои», а
    // переключение вкладки попутно закрывает очередь: список ждущих исчезал
    // ровно в тот момент, когда он растёт, и за следующим обращением
    // приходилось возвращаться руками — десятки раз за смену. Заодно пропадала
    // из виду цифра «сколько ещё ждёт» и пометка «никто не берёт».
    const user = userEvent.setup();
    const nonceBefore = useFocusBus.getState().composerNonce;
    useChatUiStore.getState().setInboxOpen(true);
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    await user.click(await screen.findByRole("button", { name: "Принять диалог" }));

    expect(await screen.findByLabelText("Текст сообщения")).toBeInTheDocument();
    expect(posted).toContain(`/api/v1/conversations/${CONV_ID}/claim`);
    expect(useChatUiStore.getState().inboxOpen).toBe(true);
    expect(useFocusBus.getState().composerNonce).toBeGreaterThan(nonceBefore);
    // Своей же строки в очереди больше нет — ни в кэше, ни в счётчике.
    expect(useInboxStore.getState().count).toBe(1);
  });

  it("опоздал: 409 already_claimed → тост именем принявшего и переход к следующему", async () => {
    const user = userEvent.setup();
    const show = vi.spyOn(notifications, "show").mockImplementation(() => "id");
    claimStatus = 409;
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    await user.click(await screen.findByRole("button", { name: "Принять диалог" }));

    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent(`/chats/${NEXT_ID}`));
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ message: `Диалог принял ${PETR.full_name}` }),
    );
  });

  it("отклонил — сразу следующий диалог очереди, а не пустой экран", async () => {
    const user = userEvent.setup();
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    await user.click(await screen.findByRole("button", { name: "Отклонить" }));

    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent(`/chats/${NEXT_ID}`));
    expect(posted).toContain(`/api/v1/conversations/${CONV_ID}/decline`);
    expect(useInboxStore.getState().count).toBe(1);
  });

  it("очередь опустела — отклонение возвращает к списку, а не в никуда", async () => {
    const user = userEvent.setup();
    seedQueue([CONV_ID]);
    countAfterAction = 0;
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });

    await user.click(await screen.findByRole("button", { name: "Отклонить" }));

    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chats"));
    expect(screen.getByTestId("loc")).not.toHaveTextContent(CONV_ID);
  });

  it("диалог забрали, пока его читали: плашка «Диалог принял …» вместо кнопок", async () => {
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });
    await screen.findByRole("button", { name: "Принять диалог" });

    act(() => {
      applyWsEvent({
        type: "inbox:claimed",
        ts: "2026-08-06T10:00:00Z",
        data: { conversation_id: CONV_ID, claimed_by: PETR },
      });
    });

    expect(await screen.findByText(/Диалог принял Пётр Ковалёв/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Принять диалог" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Отклонить" })).toBeNull();
    // Уйти к следующему можно одним нажатием — очередь не должна останавливаться.
    expect(screen.getByRole("button", { name: "Следующий в очереди" })).toBeInTheDocument();
  });

  it("свой же claim из другой вкладки плашку не показывает", async () => {
    renderWithProviders(<FooterHost />, { route: `/chats/${CONV_ID}` });
    await screen.findByRole("button", { name: "Принять диалог" });

    act(() => {
      applyWsEvent({
        type: "inbox:claimed",
        ts: "2026-08-06T10:00:00Z",
        data: { conversation_id: CONV_ID, claimed_by: { id: fakeUser.id, full_name: fakeUser.full_name } },
      });
    });

    await waitFor(() => expect(useInboxStore.getState().count).toBe(1));
    expect(screen.queryByText(/Диалог принял/)).toBeNull();
  });
});
