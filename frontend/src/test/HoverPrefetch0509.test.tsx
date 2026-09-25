/**
 * ДИАЛОГ СПРАШИВАЕТСЯ ДО НАЖАТИЯ (просьба владельца 05.09: «сделай моментальную
 * подгрузку диалога, есть небольшой тайминг»).
 *
 * Сервер к «таймингу» отношения не имеет — замер боя за шесть часов: деталь
 * 55 мс в среднем, лента 42 мс, и оба запроса уходят разом (сторож
 * `ThreadOpenParallel0509`). Круг до сервера остаётся, но его можно сдвинуть в
 * прошлое: курсор стоит на строке за 250–400 мс до нажатия, и это время
 * достаётся нам даром.
 *
 * СТОРОЖИМ ТРИ ВЕЩИ, И ТРЕТЬЯ ВАЖНЕЕ ДВУХ ПЕРВЫХ:
 *  1) постоял на строке — деталь и первая страница ленты уже в кэше;
 *  2) провёл мышью мимо — не ушло ни одного запроса (иначе проводка по колонке
 *     из пятидесяти строк превращается в шквал, а операторов в смене 13);
 *  3) открытие после наведения не показывает скелет ВООБЩЕ — то есть человек
 *     не видит «диалог собирается» ни на кадр.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import { runAction } from "@/features/hotkeys/dispatch";
import { ПОРОГ_НАВЕДЕНИЯ_МС, навелись, увели } from "@/features/chats/предзагрузка";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

const СОСЕД = "conv-2";

function строка(id: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-05T10:00:00Z",
  } as ConversationDto;
}

function лента(): MessagesPage {
  return {
    items: [],
    page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
  };
}

let запросы: Array<{ url: string; method: string }> = [];

/**
 * ЧТЕНИЯ диалога, и только они. `POST /read` уходит всегда и человека не ждёт:
 * он двигает маркер прочтения, а не рисует экран.
 */
function чтения(id: string): string[] {
  return запросы
    .filter((з) => з.method === "GET" && з.url.includes(`/conversations/${id}`))
    .map((з) => з.url);
}

function ответ(url: string): Response {
  const тело = url.includes("/messages") ? лента() : makeConversation({ id: СОСЕД });
  return new Response(JSON.stringify(тело), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

/** Подождать реальным таймером: порог предзагрузки живёт на `setTimeout`. */
async function подождать(мс: number): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, мс));
  });
}

describe("предзагрузка диалога", () => {
  beforeEach(() => {
    queryClient.clear();
    запросы = [];
    увели();
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, activeConversationId: "conv-1" });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
      pages: [
        { items: [строка("conv-1"), строка(СОСЕД)], page: { limit: 2, offset: 0, total: 2 } },
      ],
      pageParams: [0],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        запросы.push({ url: String(url), method: init?.method ?? "GET" });
        return Promise.resolve(ответ(String(url)));
      }),
    );
  });

  afterEach(() => {
    увели();
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  /**
   * ЧЕРЕЗ САМУ СТРОКУ СПИСКА, А НЕ ВЫЗОВОМ ФУНКЦИИ. Всё остальное в этом файле
   * зовёт `навелись` напрямую — то есть проверяет модуль, но не проводку. Сними
   * обработчик со строки, и все прочие проверки останутся зелёными, а
   * предзагрузки в бою не будет ни одной.
   */
  it("наведение на строку списка запускает предзагрузку", async () => {
    const { container } = renderWithProviders(
      <ConversationListItem
        row={строка(СОСЕД)}
        active={false}
        now={Date.now()}
        onOpen={vi.fn()}
        showChannel
      />,
    );
    fireEvent.pointerEnter(container.querySelector(".conv-card") as HTMLElement);
    await подождать(ПОРОГ_НАВЕДЕНИЯ_МС + 40);

    expect(queryClient.getQueryData(qk.conversations.detail(СОСЕД))).toBeDefined();
  });

  it("постояли на строке — деталь и первая страница уже в кэше", async () => {
    навелись(СОСЕД);
    await подождать(ПОРОГ_НАВЕДЕНИЯ_МС + 40);

    expect(queryClient.getQueryData(qk.conversations.detail(СОСЕД))).toBeDefined();
    expect(queryClient.getQueryData(qk.messages.list(СОСЕД))).toBeDefined();
  });

  /**
   * ⚠ ЧИСЛО ПОРОГА — ЭТО И ЕСТЬ ЗАЩИТА ОТ ШКВАЛА. Строка списка 84 px; порог
   * 150 мс означает, что проводка быстрее 560 px/s не рождает ни одного
   * запроса. Опусти порог до 30–50 мс — и один человек, ведущий мышь по
   * колонке, выдаёт полсотни запросов; в смене таких тринадцать.
   */
  it("провели мышью мимо — не ушло ни одного запроса", async () => {
    навелись(СОСЕД);
    await подождать(Math.round(ПОРОГ_НАВЕДЕНИЯ_МС / 3));
    увели();
    await подождать(ПОРОГ_НАВЕДЕНИЯ_МС + 40);

    expect(чтения(СОСЕД)).toEqual([]);
    expect(queryClient.getQueryData(qk.conversations.detail(СОСЕД))).toBeUndefined();
  });

  it("открытый диалог сам себя не предзагружает", async () => {
    навелись("conv-1"); // он же activeConversationId
    await подождать(ПОРОГ_НАВЕДЕНИЯ_МС + 40);

    expect(чтения("conv-1")).toEqual([]);
  });

  /**
   * Ради этого всё и затевалось: к моменту нажатия ждать нечего, и панель
   * рисует переписку сразу — без скелета, который человек читает как «диалог
   * пропал» (жалоба владельца 22.08).
   */
  it("после наведения открытие идёт из кэша: ни скелета, ни запросов", async () => {
    навелись(СОСЕД);
    await подождать(ПОРОГ_НАВЕДЕНИЯ_МС + 40);
    запросы = [];

    useChatUiStore.setState({ activeConversationId: СОСЕД });
    const { container } = renderWithProviders(<ChatThreadPane convId={СОСЕД} />);

    expect(container.querySelector(".thread-skeleton")).toBeNull();
    expect(чтения(СОСЕД)).toEqual([]);
  });

  /**
   * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ОБЯЗАНА ПАДАТЬ ПО СВОЕЙ ПРИЧИНЕ. Без наведения та
   * же панель обязана показать скелет — иначе проверка выше зеленела бы и от
   * панели, которая скелет не рисует вовсе.
   */
  it("без наведения скелет на месте — проверка выше не зеленеет впустую", () => {
    useChatUiStore.setState({ activeConversationId: СОСЕД });
    const { container } = renderWithProviders(<ChatThreadPane convId={СОСЕД} />);

    expect(container.querySelector(".thread-skeleton")).not.toBeNull();
  });

  /**
   * У КЛАВИАТУРЫ ФОРЫ НЕТ: `listNext` открывает диалог в тот же миг, поэтому
   * форой становится сам шаг — раз пошли вниз, спрашиваем следующего вниз.
   */
  it("шаг по списку клавишей спрашивает следующего в ту же сторону", async () => {
    const rows = [строка("conv-1"), строка(СОСЕД), строка("conv-3")];
    queryClient.setQueryData(qk.conversations.list(DEFAULT_FILTERS), {
      pages: [{ items: rows, page: { limit: 3, offset: 0, total: 3 } }],
      pageParams: [0],
    });

    runAction("listNext", { navigate: vi.fn(), rows: () => rows, can: () => true });
    await подождать(20);

    // Шагнули на conv-2 — вперёд спрошен conv-3, а не тот, на который шагнули.
    expect(queryClient.getQueryData(qk.conversations.detail("conv-3"))).toBeDefined();
  });
});
