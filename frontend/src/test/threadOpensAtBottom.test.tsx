import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { MessageDto, MessagesPage } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * ЛЕНТА ОТКРЫВАЕТСЯ В КОНЦЕ.
 *
 * ЧТО БЫЛО. Начальная прокрутка делалась одним вызовом `scrollToIndex` в
 * layout-эффекте — то есть ДО того, как пузыри измерены: виртуализатор в этот
 * момент считает каждую строку по `estimateSize` (64px), а высоту колонки
 * берёт из `initialRect` (600px). Смещение вычислялось по этим числам и
 * больше не пересчитывалось. Замер со стенда (1440×600, диалог с Ольгой
 * Никитиной): `.thread-scroll` открывался с `scrollTop` 13 при максимуме 204 —
 * свежее служебное сообщение Авито оставалось под обрезом, а кнопки «↓ новые»
 * не было, потому что новых сообщений и не приходило.
 *
 * Дефект незаметен: экран выглядит целым, просто последним видно письмо
 * недельной давности. Именно поэтому он и прожил до аудита.
 *
 * ЧЕМ МЕРЯЕМ В JSDOM. Раскладки здесь нет, поэтому размеры прокручиваемого
 * блока подменяются: `scrollHeight`/`clientHeight` отдаёт наш объект, а
 * `scrollTop` — с тем же ограничением сверху, что и в браузере (дальше
 * `scrollHeight − clientHeight` не уехать). Рост высоты после первого кадра —
 * это и есть «строки измерились и оказались выше оценки».
 */

const BOX = { scrollHeight: 800, clientHeight: 600, scrollTop: 0 };

function isThreadScroll(el: HTMLElement): boolean {
  return el.classList.contains("thread-scroll");
}

const ORIGINAL = {
  scrollHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight"),
  clientHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight"),
  scrollTop: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTop"),
};

function mockScrollBox() {
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return isThreadScroll(this) ? BOX.scrollHeight : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return isThreadScroll(this) ? BOX.clientHeight : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get(this: HTMLElement) {
      return isThreadScroll(this) ? BOX.scrollTop : 0;
    },
    set(this: HTMLElement, value: number) {
      if (!isThreadScroll(this)) return;
      // Ровно как браузер: за нижнюю границу прокрутки не пускает.
      BOX.scrollTop = Math.max(0, Math.min(value, BOX.scrollHeight - BOX.clientHeight));
    },
  });
}

function restoreScrollBox() {
  for (const [name, descriptor] of Object.entries(ORIGINAL)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
  }
}

function message(i: number, body: string): MessageDto {
  return {
    id: `m-${i}`,
    conversation_id: CONV_ID,
    direction: i % 2 === 0 ? "in" : "out",
    sender_type: i % 2 === 0 ? "client" : "operator",
    sender: i % 2 === 0 ? null : { id: fakeUser.id, full_name: fakeUser.full_name },
    body,
    attachments: [],
    delivery_status: "delivered",
    client_message_id: null,
    created_at: `2026-08-05T10:${String(10 + i).padStart(2, "0")}:00Z`,
  };
}

function seedThread(count: number) {
  const items = Array.from({ length: count }, (_, i) =>
    message(i, i === count - 1 ? "Клиент оформил заказ, ожидает подтверждения" : `Сообщение ${i}`),
  );
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

describe("Открытие ленты", () => {
  beforeEach(() => {
    BOX.scrollHeight = 800;
    BOX.clientHeight = 600;
    BOX.scrollTop = 0;
    mockScrollBox();
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedThread(12);
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/messages")) {
          return jsonResponse(200, {
            items: [],
            page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
          });
        }
        return jsonResponse(200, makeConversation());
      }),
    );
  });

  afterEach(() => {
    restoreScrollBox();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("доезжает до низа, даже когда высота ленты дорастает уже после первого кадра", async () => {
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    // Первый кадр отработал по ОЦЕНКАМ размеров — низ на тот момент был здесь.
    expect(BOX.scrollTop).toBe(200);

    // А теперь строки измерились и оказались выше оценки: ровно этот рост и
    // оставлял ленту на месте — прокрутка была вычислена по старой высоте.
    BOX.scrollHeight = 1500;

    await waitFor(() => {
      expect(BOX.scrollTop).toBe(900); // 1500 − 600, то есть самый низ
    });
  });

  it("не тащит ленту вниз, если оператор увёл её вверх сам", async () => {
    // Прокрутка вверх — типовое действие смены («что мы обещали по цене»).
    // Догоняющие кадры не имеют права вырывать ленту из рук.
    const { container } = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    const scroller = container.querySelector(".thread-scroll") as HTMLElement;

    BOX.scrollTop = 0;
    scroller.dispatchEvent(new Event("scroll", { bubbles: false }));
    BOX.scrollHeight = 1500;

    await new Promise((r) => setTimeout(r, 120));
    expect(BOX.scrollTop).toBe(0);
  });

  it("«Закрыть диалог» живёт в меню «…» и называет то же сочетание, что реестр клавиш", async () => {
    /*
     * ДВА УТВЕРЖДЕНИЯ В ОДНОМ ТЕСТЕ, И ОБА ПРО ОДНО МЕСТО.
     *
     * Первое — про сочетание. Три места обещали разное: кнопка —
     * Ctrl+Shift+Enter, шпаргалка под полем ввода и окно «?» — Ctrl+D.
     * Работает Ctrl+D (useChatHotkeys), и подпись обязана говорить его.
     *
     * Второе — про место. С перекройки 12 августа закрытие живёт в меню «…»
     * вместе с остальным вторичным, а не отдельной зелёной кнопкой в шапке:
     * рядом с «Пометить как нежелательного» она провоцировала промах между
     * двумя действиями с очень разными последствиями. Отдельной кнопки в
     * шапке быть не должно — иначе действие снова окажется в двух местах.
     */
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(screen.queryByRole("button", { name: "Закрыть" })).not.toBeInTheDocument();

    await userEvent.click(await screen.findByLabelText("Действия с диалогом"));
    expect(await screen.findByRole("menuitem", { name: /Закрыть диалог/ })).toHaveTextContent(
      "Ctrl+D",
    );
  });
});
