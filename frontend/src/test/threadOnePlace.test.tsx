import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { MessageDto, MessagesPage } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * ОДНО ДЕЙСТВИЕ ЖИВЁТ В ОДНОМ МЕСТЕ, ОДНО ЗНАЧЕНИЕ ПОКАЗЫВАЕТСЯ ОДИН РАЗ.
 *
 * Перекройка от 12 августа, разбор живого экрана владельцем. Действия над
 * диалогом были продублированы В ЧЕТЫРЁХ местах: ряд иконок в шапке ленты,
 * кнопки в правой карточке, нижняя плашка и ховер-иконка в строке списка.
 * Плюс селект «Статус» делал то же, что кнопка «Взять в работу». Термины при
 * этом расходились: «Принять диалог» и «Взять в работу» — одно действие под
 * двумя именами.
 *
 * Этот файл сторожит ЦЕНТРАЛЬНУЮ КОЛОНКУ: шапку и ленту. Он проверяет не
 * «есть ли кнопка», а «сколько раз одно и то же показано на экране» — потому
 * что дефект был именно в количестве, а не в отсутствии.
 */

/**
 * РАЗМЕРЫ ДЛЯ ВИРТУАЛИЗАТОРА — ИНАЧЕ ЛЕНТА ПУСТА, И ТЕСТ ЗЕЛЕН ВСЕГДА.
 *
 * `@tanstack/virtual-core` меряет и колонку, и строки через `offsetHeight`
 * (см. `getRect` и `measureElement` в его исходниках), а jsdom отдаёт по нему
 * ноль. Итог без этой подмены: `.thread-virtual` высотой 0 и НИ ОДНОЙ строки в
 * документе — то есть проверка «журнал не показан» проходила бы и на сломанном
 * коде, потому что не показано вообще ничего. Проверено: до подмены дамп
 * ленты был `<div class="thread-virtual" style="height: 0px"></div>`.
 *
 * Подменяется только высота и только у двух известных классов; всё остальное
 * остаётся нулевым, как в jsdom и было.
 */
const REAL_OFFSET = {
  height: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight"),
  width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth"),
};

function mockVirtualSizes() {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
    configurable: true,
    get(this: HTMLElement) {
      if (this.classList.contains("thread-scroll")) return 600;
      if (this.classList.contains("thread-virtual__row")) return 64;
      return 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
    configurable: true,
    get: () => 800,
  });
}

function restoreVirtualSizes() {
  for (const [name, d] of [
    ["offsetHeight", REAL_OFFSET.height],
    ["offsetWidth", REAL_OFFSET.width],
  ] as const) {
    if (d) Object.defineProperty(HTMLElement.prototype, name, d);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
  }
}

let seq = 0;

function message(overrides: Partial<MessageDto> = {}): MessageDto {
  seq += 1;
  return {
    id: `m-${seq}`,
    conversation_id: CONV_ID,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "Сломалась стиральная машина",
    attachments: [],
    delivery_status: "delivered",
    client_message_id: null,
    created_at: `2026-08-12T10:${String(seq % 60).padStart(2, "0")}:00Z`,
    ...overrides,
  };
}

function sys(body: string): MessageDto {
  return message({ direction: "system", sender_type: "system", body });
}

function seed(items: MessageDto[]) {
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [
      {
        items,
        page: {
          prev_cursor: null,
          next_cursor: null,
          has_more_before: false,
          has_more_after: false,
        },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

describe("Шапка ленты — идентичность диалога и ничего сверх", () => {
  beforeEach(() => {
    seq = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seed([message()]);
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, makeConversation())));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function open(conversation = makeConversation()) {
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conversation);
    return renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
  }

  it("канал и объявление — ОДНОЙ строкой, и наведение показывает её же целиком", async () => {
    const { container } = open();

    const sub = container.querySelector(".thread-header__sub") as HTMLElement;
    expect(sub).not.toBeNull();
    // Одна строка — значит один потомок. Было четыре факта в ряд (аккаунт,
    // телефон, ответственный, ожидание), и на 768px от них оставалось ноль
    // пикселей: сжимался именно тот блок, который сжимать нельзя.
    expect(sub.children).toHaveLength(1);

    const line = sub.children[0] as HTMLElement;
    expect(line.textContent).toBe("LP-Москва · Ремонт iPhone 13");
    // `title` обязан совпадать с обрезаемым текстом слово в слово: иначе
    // человек наводит мышь на «LP-Мос…» и получает не то, что обрезано.
    expect(line.getAttribute("title")).toBe(line.textContent);
    await screen.findByText("Иван Петров");
  });

  it("без объявления остаётся один аккаунт, без висящей в воздухе точки", () => {
    const { container } = open(makeConversation({ item: null }));
    const line = container.querySelector(".thread-header__sub > *") as HTMLElement;
    expect(line.textContent).toBe("LP-Москва");
  });

  it("телефона и ответственного в шапке НЕТ — они живут в правой колонке", () => {
    // Одно значение показывается один раз. Телефон — блок «Клиент»,
    // ответственный — «Ход диалога»; вторая копия рядом заставляла бы сверять
    // их глазами и расходилась бы при первой же правке.
    const { container } = open(
      makeConversation({
        client: { id: "client-1", name: "Иван Петров", phone: "+7 905 123-45-67" } as never,
        assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
      }),
    );

    const header = container.querySelector(".thread-header") as HTMLElement;
    expect(within(header).queryByText("+7 905 123-45-67")).not.toBeInTheDocument();
    expect(within(header).queryByText(fakeUser.full_name)).not.toBeInTheDocument();
  });

  it("чип статуса ОДИН, и ожидание клиента живёт ВНУТРИ него", () => {
    /*
     * Клиент ждёт три часа. ЧИСЛО ЗДЕСЬ ОТ СЕРВЕРА (`waiting_since`), а не от
     * хвоста ленты: шапка считала по последнему видимому сообщению и спорила со
     * строкой списка, которая давно берёт канонический якорь. Хвост ленты
     * оставлен ровно такой же — чтобы проверка не зависела от того, чем именно
     * считают.
     */
    seed([message({ created_at: new Date(Date.now() - 3 * 60 * 60_000).toISOString() })]);
    const { container } = open(
      makeConversation({
        status: "in_progress",
        waiting_since: new Date(Date.now() - 3 * 60 * 60_000).toISOString(),
      } as never),
    );

    const chips = container.querySelectorAll(".thread-header__status");
    // Двумя отдельными значками статус и ожидание спорили: «В работе» рядом
    // с «ждёт 3 ч» читается как две новости, хотя новость одна.
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent).toBe("В работе · ждёт 3 ч");
    expect(chips[0].getAttribute("data-late")).toBe("true");
  });

  it("у закрытого диалога чип не обещает ожидания", () => {
    // Раньше закрытый диалог с последним словом клиента показывал «ждёт 3 ч»
    // рядом с плашкой «Диалог закрыт» — два утверждения об одном диалоге,
    // прямо противоречащие друг другу, на расстоянии полуэкрана.
    // Якорь ожидания у закрытого диалога может ещё не приехать снятым: закрытие
    // прилетает кадром `conversation:updated` — статусом, без `waiting_since`.
    seed([message({ created_at: new Date(Date.now() - 3 * 60 * 60_000).toISOString() })]);
    const { container } = open(
      makeConversation({
        status: "closed",
        waiting_since: new Date(Date.now() - 3 * 60 * 60_000).toISOString(),
      } as never),
    );

    const chip = container.querySelector(".thread-header__status") as HTMLElement;
    expect(chip.textContent).toBe("Закрыт");
    expect(chip.getAttribute("data-late")).toBeNull();
  });

  it("вторичные действия — только в меню «…», ряда иконок в шапке больше нет", async () => {
    const { container } = open();

    // Ряд иконок убран целиком: подписи у них появлялись подсказкой лишь
    // через 400 мс наведения (SCEN-07), и «пометить нежелательным» было не
    // отличить от «закрыть» — а перепутать их дорого.
    expect(container.querySelector(".thread-actions__btn")).toBeNull();

    await userEvent.click(await screen.findByLabelText("Действия с диалогом"));

    for (const name of [
      /Позвать коллегу/,
      /Передать диалог/,
      /Пометить как нежелательного/,
      /Закрыть диалог/,
    ]) {
      expect(await screen.findByRole("menuitem", { name })).toBeInTheDocument();
    }
  });
});

describe("Лента — только переписка и свёрнутые группы событий", () => {
  beforeEach(() => {
    seq = 0;
    mockVirtualSizes();
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, makeConversation())));
  });

  afterEach(() => {
    restoreVirtualSizes();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("двенадцать служебных плашек схлопываются в одну строку с разворотом", async () => {
    /*
     * ЖИВОЙ СЛУЧАЙ. Двенадцать записей подряд вытеснили сообщения клиента за
     * пределы экрана: человек открывал диалог и читал историю нажатий коллег
     * вместо вопроса, на который надо ответить.
     */
    seed([
      message({ body: "Сломалась стиральная машина" }),
      ...Array.from({ length: 12 }, (_, i) => sys(`Диалог передан: А → Б ${i}`)),
      message({ body: "Так вы приедете?" }),
    ]);

    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    // Свёрнуто: журнала на экране нет, оба сообщения клиента есть.
    expect(await screen.findByText("Сломалась стиральная машина")).toBeInTheDocument();
    expect(screen.getByText("Так вы приедете?")).toBeInTheDocument();
    expect(screen.queryByText(/Диалог передан: А → Б 0/)).not.toBeInTheDocument();

    const toggle = screen.getByRole("button", { name: /История статусов: 12 событий/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(toggle);
    expect(screen.getByText("Диалог передан: А → Б 0")).toBeInTheDocument();
    expect(screen.getByText("Диалог передан: А → Б 11")).toBeInTheDocument();
  });

  it("«Ждал N» из служебной записи не показывается вовсе", async () => {
    // Метрика диалога, а не событие: сколько клиент ждёт, живёт в чипе
    // статуса, где значение тикает. Застывшее число в ленте спорило с шапкой.
    seed([message(), sys("Диалог принят: Ольга Никитина. Ждал 7 мин")]);

    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(await screen.findByText(/Диалог принят: Ольга Никитина/)).toBeInTheDocument();
    expect(screen.queryByText(/Ждал 7 мин/)).not.toBeInTheDocument();
  });

  it("отменённая пара видна, но погашена", async () => {
    seed([
      message(),
      sys("Диалог принят: Ольга"),
      sys("Диалог возвращён во «Входящие»: Ольга"),
      sys("Диалог передан: Ольга → Борис"),
    ]);

    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await userEvent.click(await screen.findByRole("button", { name: /История статусов: 3 события/ }));

    // След остаётся: руководителю, разбирающему, почему клиент ждал сорок
    // минут, он нужен. Но читается как отменённый, а не как решение.
    const cancelled = screen.getByText("Диалог принят: Ольга").closest("li");
    expect(cancelled).toHaveAttribute("data-cancelled", "true");
    const kept = screen.getByText("Диалог передан: Ольга → Борис").closest("li");
    expect(kept).not.toHaveAttribute("data-cancelled");
  });

  it("служебное сообщение Авито в группу не прячется", async () => {
    // Чужое действие, о котором оператор не узнает ниоткуда больше. Оно
    // обязано читаться сразу, а не после нажатия «развернуть».
    seed([
      message(),
      message({ direction: "system", sender_type: "avito", body: "Заказ отменён" }),
      message({ direction: "system", sender_type: "avito", body: "Клиент оставил отзыв" }),
    ]);

    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(await screen.findByText(/Заказ отменён/)).toBeInTheDocument();
    expect(screen.getByText(/Клиент оставил отзыв/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /История статусов/ })).not.toBeInTheDocument();
  });
});
