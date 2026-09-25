import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, MessageDto, MessagesPage } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * В ШАПКЕ ЛЕНТЫ И В СТРОКЕ СПИСКА — ОДНО ЧИСЛО ОЖИДАНИЯ (CHATS-07 / FUNC-14).
 *
 * Шапка считала САМА: брала последнее видимое сообщение из загруженного куска
 * ленты и отсчитывала от него. Список тем временем показывал серверный
 * `waiting_since`. На одном экране получалось «ждёт 40 мин» слева и «ждёт 10
 * мин» сверху — два ответа на один вопрос про один диалог. Диспетчер верит
 * тому, что ближе к глазу, и по меньшему числу решает «успею, отвечу позже».
 *
 * Расходились они закономерно: своё «последнее видимое» — это последнее из
 * ДОЗАГРУЖЕННОГО, и ответ коллеги из соседней вкладки сдвигал отсчёт молча.
 * Поэтому проверка построена так, чтобы два источника заведомо спорили:
 * канонический якорь говорит 40 минут, а хвост ленты — 10.
 */

const NOW = Date.parse("2026-08-12T14:00:00Z");
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString();

/** Диалог с сервера: ожидание с 13:20, то есть сорок минут. */
function conversation(over: Record<string, unknown> = {}): ConversationDetailDto {
  return { ...makeConversation(), waiting_since: ago(40), ...over } as ConversationDetailDto;
}

/** Хвост ленты: последним лежит входящее десятиминутной давности. */
function seedThread() {
  const items: MessageDto[] = [
    {
      id: "m-1",
      conversation_id: CONV_ID,
      direction: "in",
      sender_type: "client",
      sender: null,
      body: "Так вы приедете?",
      attachments: [],
      delivery_status: "delivered",
      client_message_id: null,
      created_at: ago(10),
    },
  ];
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [
      {
        items,
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

function open(conv: ConversationDetailDto) {
  queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, conv)));
  return renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
}

function chipText(container: HTMLElement): string {
  return (container.querySelector(".thread-header__status") as HTMLElement).textContent ?? "";
}

describe("Ожидание в шапке ленты", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedThread();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("берётся из серверного якоря, а не из хвоста загруженной ленты", () => {
    const { container } = open(conversation());

    // Хвост ленты сказал бы «10 мин» — и говорил, пока шапка считала сама.
    expect(chipText(container)).toBe("В работе · ждёт 40 мин");
  });

  it("говорит ровно то же, что строка того же диалога в списке", () => {
    /*
     * Обе половины экрана видны ОДНОВРЕМЕННО: список слева, лента справа.
     * Поэтому проверяем не «шапка права», а «шапка и список согласны» — дефект
     * был в расхождении, и вернуться он может с любой из сторон.
     */
    const conv = conversation();
    const { container } = open(conv);

    const list = renderWithProviders(
      <ConversationListItem row={conv} active now={NOW} onOpen={() => {}} showChannel />,
    );
    const gauge = list.container.querySelector(".wait-gauge") as HTMLElement | null;

    expect(chipText(container)).toContain("ждёт 40 мин");
    // Подпись в ячейке короче («40м» против «ждёт 40 мин») — это разное
    // оформление ОДНОГО числа, ради которого и заведён общий `waitingFor`.
    expect(gauge?.getAttribute("aria-label")).toBe("Ждёт 40 мин");
  });

  it("«не ждёт» от сервера шапка не переспоривает", () => {
    /*
     * Мы уже ответили — сервер снял отметку, а последним в ленте всё ещё лежит
     * сообщение клиента (наш ответ ушёл другой вкладкой). Прежний расчёт по
     * хвосту зажигал в шапке «ждёт 10 мин» и требовал ответить второй раз.
     */
    const { container } = open(conversation({ waiting_since: null }));

    expect(chipText(container)).toBe("В работе");
  });

  it("у закрытого диалога ожидания нет, даже если якорь ещё не убран", () => {
    // Закрытие приезжает кадром `conversation:updated` — статусом, без якоря.
    const { container } = open(conversation({ status: "closed" }));
    const chip = container.querySelector(".thread-header__status") as HTMLElement;

    expect(chip.textContent).toBe("Закрыт");
    expect(chip.getAttribute("data-late")).toBeNull();
  });

  it("просрочка красит весь чип: сорок минут — это уже поздно", () => {
    const { container } = open(conversation());
    const chip = container.querySelector(".thread-header__status") as HTMLElement;

    expect(chip.getAttribute("data-late")).toBe("true");
    // Порог общий с остальным интерфейсом (WAIT_LATE_MIN = 15 мин).
    const fresh = open(conversation({ waiting_since: ago(3) }));
    expect(
      (fresh.container.querySelector(".thread-header__status") as HTMLElement).getAttribute(
        "data-late",
      ),
    ).toBeNull();
  });
});
