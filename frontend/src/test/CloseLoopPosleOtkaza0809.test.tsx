/**
 * Отказ на закрытие не заводит круг (бой 08.09).
 *
 * ⚠ ЧТО БЫЛО В БОЮ. За три часа боевого журнала — 70 ответов 403, из них 69 на
 * `PATCH /conversations/{id}/status` с одного офисного адреса; 62 из них за
 * ОДНУ минуту, по 4 запроса в секунду в один и тот же диалог. В журнале api
 * это два разных человека, то есть дело было не в одной сломанной вкладке.
 *
 * ⚠ ОТКУДА КРУГ. Закрытие уводит человека к следующему диалогу ещё до ответа
 * сервера, а отказ возвращает назад. Панель ленты рождается на каждый диалог
 * заново (`<ThreadView key={convId} />`), и отметка «эту просьбу я уже сделал»
 * жила ССЫЛКОЙ ВНУТРИ ПАНЕЛИ — то есть умирала вместе с ней. Второе рождение
 * находило в шине всё ту же просьбу и исполняло её снова. Круг рвался только
 * тем, что отказ переставал быть отказом: в бою коллега принял диалог, и
 * следующая попытка ЗАКРЫЛА живой разговор, взятый им за 32 мс до этого.
 *
 * ⚠ ПРОВЕРЯЕТСЯ ПРОВОДКА, А НЕ ФУНКЦИЯ: монтируется настоящая панель, дёргается
 * настоящая шина и считаются настоящие запросы. «Написано, но не подключено» в
 * этом проекте случалось не раз.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { act } from "react";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { useActionBus, ЖИЗНЬ_ПРОСЬБЫ_МС } from "@/features/hotkeys/actionBus";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => vi.fn() };
});

function деталь(): ConversationDetailDto {
  return {
    ...makeConversation(),
    status: "in_progress",
    assignee: { id: fakeUser.id, full_name: "Я" },
    participants: [],
  } as ConversationDetailDto;
}

function положить(conv: ConversationDetailDto) {
  queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [
      {
        items: [],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

describe("Ctrl+D после отказа сервера", () => {
  let закрытий = 0;

  beforeEach(() => {
    закрытий = 0;
    queryClient.clear();
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, drafts: {} });
    useActionBus.setState({ closeRequest: null });
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
        const method = (init?.method ?? "GET").toUpperCase();
        if (method === "PATCH" && url.includes("/status")) {
          закрытий += 1;
          /*
           * Тот самый отказ из боя: обращение ещё никто не взял. Важно, что он
           * ПОСТОЯННЫЙ — сервер отвечает так же и на вторую попытку, и на
           * шестидесятую. Круг в бою этим и держался.
           */
          return jsonResponse(
            403,
            errorEnvelope("forbidden", "Обращение ещё никто не взял", {
              reason: "queued_close_forbidden",
            }),
          );
        }
        if (url.includes("/messages")) {
          return jsonResponse(200, {
            items: [],
            page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
          });
        }
        if (url.includes(`/conversations/${CONV_ID}`)) return jsonResponse(200, деталь());
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  /*
   * ⚠ ДИВЕРСИЯ: вернуть память о просьбе внутрь панели — завести
   * `const seenClose = useRef(false)` и снимать просьбу им, не трогая шину, —
   * проверка краснеет: закрытий становится два, а в бою их было 62.
   */
  it("рождение панели заново не повторяет отказанное закрытие", async () => {
    положить(деталь());
    const { unmount } = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    await act(async () => {
      useActionBus.getState().requestClose(CONV_ID);
    });
    await waitFor(() => expect(закрытий).toBe(1));

    // Ровно то, что делает откат: человек возвращён в тот же диалог, и панель
    // рождается второй раз.
    unmount();
    положить(деталь());
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });

    expect(закрытий).toBe(1);
    // И причина, по которой второго закрытия нет: просьбу сняли с шины, а не
    // запомнили внутри панели, которая умирает вместе с диалогом.
    expect(useActionBus.getState().closeRequest).toBeNull();
  });

  /*
   * Просьба, которую некому было исполнить, не ждёт своего часа сутками.
   *
   * ⚠ ДИВЕРСИЯ: убрать проверку срока в эффекте — краснеет: панель закрывает
   * диалог, который человек всего лишь открыл посмотреть.
   */
  it("протухшая просьба не исполняется и снимается с шины", async () => {
    положить(деталь());
    useActionBus.setState({
      closeRequest: { convId: CONV_ID, в: Date.now() - ЖИЗНЬ_ПРОСЬБЫ_МС - 1 },
    });
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });

    expect(закрытий).toBe(0);
    expect(useActionBus.getState().closeRequest).toBeNull();
  });
});
