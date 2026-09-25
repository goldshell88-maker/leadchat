import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { MessageDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * НАЙДЕННЫЙ НОМЕР ВИДЕН В САМОМ СООБЩЕНИИ (требование владельца, п. 5).
 *
 * ЖИВОЙ ПОВОД. Диалог a1b2c3d4, входящее 12 августа 16:35: «Прошу сообщить о
 * времени прихода за 1 час в СМС по номеру : +7(900)1112240». В карточке при
 * этом пусто. Автозапись распознанного номера по умолчанию ВЫКЛЮЧЕНА, поэтому
 * путь «увидел в пузыре — нажал» не запасной, а основной: именно так номер и
 * попадёт в карточку в большинстве случаев.
 *
 * ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, КРОМЕ ОЧЕВИДНОГО:
 *  - подсветка не трогает текст (склейка знак в знак) — номер копируют мышью;
 *  - фронт НИЧЕГО не разбирает сам: нет кандидата от сервера — нет подсветки,
 *    даже если в тексте одиннадцать цифр подряд;
 *  - занятая карточка ведёт к выбору из трёх действий, а не молчит.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ КРАСНЕЮТ (каждый упал):
 *  - в `MessageBubble` вернули простой `<p>{body}</p>` для входящего — падают
 *    «подсвечен ровно тот кусок» и «одно нажатие кладёт номер в карточку»;
 *  - в `splitPhones` подставили `raw` в `RegExp` вместо `indexOf` — падает
 *    «текст сообщения не меняется ни на знак» (скобки в шаблоне рвут строку);
 *  - в `PhoneOffer` убрали ветку `cardPhone` и оставили одно «В карточку» —
 *    падает «занятая карточка ведёт к выбору из трёх действий»;
 *  - в той же ветке `known` заменили на `false` — падает «номер, который уже
 *    в карточке, не предлагают вписать снова»;
 *  - в `onError` убрали разбор 409 — падает «по решённому номеру не зовут
 *    нажать ещё раз».
 */

const CONV = "conv-1";
const MSG = "msg-1";
const CLIENT = "client-1";
const RAW = "+7(900)1112240";
const TEXT = `В любое время в течении дня. Прошу сообщить о времени прихода за 1 час в СМС по номеру : ${RAW}.`;

function incoming(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: MSG,
    conversation_id: CONV,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: TEXT,
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-08-12T13:35:00Z",
    ...overrides,
  };
}

function pendingCandidate(overrides: Record<string, unknown> = {}) {
  return {
    id: "cand-1",
    phone: "+79001112240",
    raw: RAW,
    conversation_id: CONV,
    message_id: MSG,
    message_at: "2026-08-12T13:35:00Z",
    detected_at: "2026-08-12T13:35:01Z",
    source: "inbound",
    status: "pending",
    ...overrides,
  };
}

function identityOf(overrides: Record<string, unknown> = {}) {
  return {
    id: CLIENT,
    name: "Анна Сергеевна",
    phone: null,
    phone_manual: false,
    phone_source: "none",
    external_id: "923456789",
    phones: [],
    phone_candidates: [pendingCandidate()],
    avito_ids: [{ value: "923456789", client_id: CLIENT, primary: true }],
    merged_from: [],
    merged_into: null,
    ...overrides,
  };
}

describe("телефон, найденный в тексте входящего", () => {
  let calls: Array<{ url: string; method: string; body: Record<string, unknown> }>;
  let identity: unknown;
  /** Ответ на POST …/resolve — тесты его подменяют. */
  let resolveResponse: () => Response;

  beforeEach(() => {
    calls = [];
    identity = identityOf();
    resolveResponse = () =>
      jsonResponse(200, {
        phone: "+79001112240",
        candidate: pendingCandidate({ status: "accepted" }),
        twins: [],
      });
    queryClient.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({
          url,
          method: init?.method ?? "GET",
          body: init?.body ? JSON.parse(String(init.body)) : {},
        });
        if (url.includes("/phone-candidates/")) return resolveResponse();
        if (url.includes("/identity")) return jsonResponse(200, identity);
        return jsonResponse(200, { items: [] });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function render(
    msg: MessageDto = incoming(),
    permissions: Permission[] = fakeMe.permissions as Permission[],
  ) {
    resetSessionStore({ user: fakeUser, permissions, accessToken: "t", bootstrapped: true });
    return renderWithProviders(
      <MessageBubble msg={msg} prev={null} clientId={CLIENT} clientName="Анна Сергеевна" />,
    );
  }

  it("подсвечен ровно тот кусок, который сервер назвал номером", async () => {
    const { container } = render();

    const mark = await waitFor(() => {
      const found = container.querySelector("mark.msg__phone");
      expect(found).not.toBeNull();
      return found!;
    });
    // Подсвечена ИСХОДНАЯ запись, а не приведённая к `+7…`: оператор сверяет
    // нашу догадку с тем, что человек написал на самом деле.
    expect(mark.textContent).toBe(RAW);
  });

  it("текст сообщения не меняется ни на знак — номер копируют мышью", async () => {
    const { container } = render();

    await screen.findByRole("button", { name: /В карточку/ });
    const paragraph = container.querySelector("p.msg__text");
    // Подсветка — краска поверх тех же символов. Появись в абзаце хоть один
    // свой знак (пробел вокруг подложки, значок, подпись) — в буфер обмена
    // уехало бы не то, что написал клиент, и номер набрали бы с ошибкой.
    expect(paragraph?.textContent).toBe(TEXT);
  });

  it("одно нажатие кладёт номер в карточку и отвечает словами", async () => {
    render();

    fireEvent.click(await screen.findByRole("button", { name: /В карточку/ }));

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/resolve"));
      expect(post?.url).toContain(`/clients/${CLIENT}/phone-candidates/cand-1/resolve`);
      expect(post?.body).toMatchObject({ decision: "replace" });
    });
    // Ответ на успех виден на месте кнопки: сервер отдаёт только ОЖИДАЮЩИХ
    // кандидатов, и без этой памяти строка исчезла бы вместе с подсветкой —
    // «нажал, и куда-то делось».
    expect(await screen.findByText(/в карточке/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /В карточку/ })).not.toBeInTheDocument();
  });

  it("занятая ДРУГИМ номером карточка ведёт к выбору из трёх действий", async () => {
    identity = identityOf({ phone: "+79001112251", phone_source: "manual" });
    render();

    // Молча затирать чужой номер нельзя, и молчать в ответ на нажатие — тоже.
    expect(await screen.findByRole("button", { name: /Заменить телефон/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Добавить .* вторым/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Отклонить/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /В карточку/ })).not.toBeInTheDocument();
    expect(screen.getByText(/в карточке другой/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Добавить .* вторым/ }));
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/resolve"));
      expect(post?.body).toMatchObject({ decision: "add" });
    });
  });

  it("номер, который уже в карточке, не предлагают вписать снова", async () => {
    identity = identityOf({
      phone: "+79001112240",
      phones: [{ value: "+79001112240", client_id: CLIENT, primary: true }],
    });
    const { container } = render();

    expect(await screen.findByText(/уже в карточке/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /В карточку/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Заменить/ })).not.toBeInTheDocument();
    // Подсветка остаётся: она отвечает на другой вопрос — где человек это
    // написал, — и ответ верен по-прежнему.
    expect(container.querySelector("mark.msg__phone")).not.toBeNull();
  });

  it("по решённому номеру не зовут нажать ещё раз", async () => {
    resolveResponse = () =>
      jsonResponse(409, errorEnvelope("already_resolved", "По этому номеру решение уже принято"));
    render();

    fireEvent.click(await screen.findByRole("button", { name: /В карточку/ }));

    // Пока оператор читал переписку, по номеру решили в карточке или в
    // соседней вкладке. Это новость, а не сбой: строка меняет вид, а не
    // предлагает повторить то, чего больше нет.
    expect(await screen.findByText(/решение уже принято/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /В карточку/ })).not.toBeInTheDocument();
  });

  it("отказ связи говорит словами и оставляет кнопку на месте", async () => {
    resolveResponse = () => {
      throw new TypeError("Failed to fetch");
    };
    render();

    fireEvent.click(await screen.findByRole("button", { name: /В карточку/ }));

    expect(await screen.findByText(/Сервер недоступен/)).toBeInTheDocument();
    // Повторить есть чем: кандидат на сервере остался ожидающим.
    expect(screen.getByRole("button", { name: /В карточку/ })).toBeInTheDocument();
  });

  it("подсвечивает находку сервера, а не всё, что похоже на номер", async () => {
    /*
     * ДВА ПУЗЫРЯ РЯДОМ, И ВТОРОЙ ЗДЕСЬ НЕ ДЛЯ ПОЛНОТЫ: он доказывает, что
     * ответ сервера доехал и отрисовался. Без такого «контрольного» пузыря
     * проверка «подсветки нет» зеленела бы и на пустом экране — то есть не
     * проверяла бы ничего.
     *
     * В первом сообщении лежат обе ловушки сразу: «заказ№89001112240» —
     * одиннадцать цифр с восьмёрки, по всем прочим признакам телефон (спасает
     * только знак номера), и НАСТОЯЩИЙ номер, который сервер нашёл В ДРУГОМ
     * сообщении. Кандидата у первого сообщения нет — значит и подсветки нет
     * ни у номера заказа, ни у чужой находки. Повтори мы разбор на фронте,
     * две копии правил разошлись бы молча, и оператор набрал бы номер заказа.
     */
    identity = identityOf({ phone_candidates: [pendingCandidate({ message_id: "msg-2" })] });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as Permission[],
      accessToken: "t",
      bootstrapped: true,
    });
    renderWithProviders(
      <>
        <div data-testid="без-находки">
          <MessageBubble
            msg={incoming({ body: `оплатил заказ№89001112240, звоните ${RAW}` })}
            prev={null}
            clientId={CLIENT}
            clientName="Анна Сергеевна"
          />
        </div>
        <div data-testid="с-находкой">
          <MessageBubble
            msg={incoming({ id: "msg-2", body: `${RAW} — записал?` })}
            prev={null}
            clientId={CLIENT}
            clientName="Анна Сергеевна"
          />
        </div>
      </>,
    );

    const found = screen.getByTestId("с-находкой");
    await waitFor(() => expect(found.querySelector("mark.msg__phone")).not.toBeNull());
    expect(found.querySelector("mark.msg__phone")?.textContent).toBe(RAW);

    const plain = screen.getByTestId("без-находки");
    expect(plain.querySelector("mark.msg__phone")).toBeNull();
    expect(plain.querySelector(".msg__phone-offer")).toBeNull();
  });

  it("без права вести диалоги личность не запрашивается вовсе", async () => {
    // Ручка личности требует `conversations:manage`, и решение по кандидату —
    // тем более. Подсветка без права нажать рассказала бы о находке и не дала
    // бы её забрать, а запрос без права ловил бы 403 на каждом входящем.
    const { container } = render(incoming(), ["conversations:read"]);

    await waitFor(() => {
      expect(screen.getByText(new RegExp(RAW.replace(/[+()]/g, "\\$&")))).toBeInTheDocument();
    });
    expect(calls.some((c) => c.url.includes("/identity"))).toBe(false);
    expect(container.querySelector("mark.msg__phone")).toBeNull();
  });
});
