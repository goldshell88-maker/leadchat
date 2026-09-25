/**
 * АДРЕС ВЫЕЗДА В КАРТОЧКЕ КЛИЕНТА.
 *
 * ⚠ ВОПРОС ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «И куда привязывается адрес, не могу
 * найти». Ответ на тот момент был честный и плохой: НИКУДА — выкачена была
 * только запись в таблицу, а экрана у неё не было. Этот набор стережёт, чтобы
 * такое не повторилось: карточка монтируется НАСТОЯЩАЯ, со своими запросами.
 *
 * ⚠ ГЛАВНОЕ ПРАВИЛО, КОТОРОЕ ЗДЕСЬ ЗАПЕРТО (редакция 18.09): адрес пишет
 * АВТОМАТИКА на сервере по степени строки (`candidate_grade`); сама
 * карточка на экране ничего не записывает и ничего не спрашивает — строка
 * под полем показ, не вопрос. Человек только меняет адрес («изменить», PUT)
 * или отказывает строке («Не адрес»), и его правку автоматика не трогает.
 * Кнопки «Подтвердить» с экрана нет: её стерёг тест, который удалён вместе
 * с ней (проект автопривязки, участок B).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { qk } from "@/shared/api/queryKeys";
import { showToast } from "@/shared/ui/toast";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

const ПРЕДЛОЖЕНИЕ = {
  id: "addr-1",
  status: "pending",
  value: "ул. Ленина, 5",
  street: "ул. Ленина",
  house: "5",
  parts: { office: "3", entrance: "2" },
  raw: "приезжайте на ул. Ленина 5 кв 3, 2 подъезд",
  level: "A",
  conversation_id: CONV_ID,
  message_id: "m-1",
  message_at: "2026-09-09T10:00:00Z",
  detected_at: "2026-09-09T10:00:01Z",
};

function личность(over: Record<string, unknown> = {}) {
  return {
    id: "client-1",
    name: "Иван Петров",
    phone: "+79125550177",
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    address: null,
    address_source: "none",
    address_candidates: [ПРЕДЛОЖЕНИЕ],
    addresses: [],
    external_id: "923456789",
    phones: [{ value: "+79125550177", client_id: "client-1", primary: true }],
    avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
    merged_from: [],
    merged_into: null,
    ...over,
  };
}

describe("Адрес выезда в карточке клиента", () => {
  let обращения: Array<{ url: string; method: string; body: Record<string, unknown> }>;
  let ответ: Record<string, unknown>;

  beforeEach(() => {
    обращения = [];
    ответ = личность();
    queryClient.clear();
    seedEmptyThread();
    vi.mocked(showToast).mockClear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    const conv = makeConversation();
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        обращения.push({
          url,
          method: init?.method ?? "GET",
          body: init?.body ? JSON.parse(String(init.body)) : {},
        });
        if (url.includes("/address-candidates/")) {
          return jsonResponse(200, { address: "ул. Ленина, 5", candidate: ПРЕДЛОЖЕНИЕ });
        }
        if (url.includes("/address")) return jsonResponse(200, { address: "Мира 7", changed: true });
        if (url.includes("/identity")) return jsonResponse(200, ответ);
        if (url.includes("/merge-candidates")) return jsonResponse(200, { items: [] });
        if (/\/conversations\//.test(url)) return jsonResponse(200, conv);
        return jsonResponse(200, { items: [] });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("распознанный адрес виден в карточке вместе с цитатой клиента", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    expect(await screen.findByText("Из переписки")).toBeTruthy();
    expect(screen.getByText(/ул\. Ленина, 5/)).toBeTruthy();
    // Части — рядом: мастеру нужен вход в квартиру, а не только дом.
    expect(screen.getByText(/кв 3 · 2 подъезд/)).toBeTruthy();
    // ⚠ ЦИТАТА ОБЯЗАТЕЛЬНА: по адресу поедет мастер, и предложение без фразы
    // клиента проверить нечем.
    expect(screen.getByText(ПРЕДЛОЖЕНИЕ.raw)).toBeTruthy();
  });

  it("карточка сама ничего не пишет: строка — показ, не вопрос", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    await screen.findByText("Из переписки");
    // Поле предлагает вписать адрес: строку под ним автоматика сервера не
    // записала (проверяется, удержана сторожем), и экран за неё не решает.
    expect(screen.getByRole("button", { name: /указать адрес/ })).toBeTruthy();
    expect(
      обращения.some((о) => о.method !== "GET" && о.url.includes("/address")),
      "карточка сама записала адрес, ничего не спросив",
    ).toBe(false);
    // Подтверждения с экрана нет (18.09): пишет автоматика.
    expect(screen.queryByRole("button", { name: /Записать адрес выезда/ })).toBeNull();
  });

  it("«Не адрес» отправляет отказ, а не подтверждение", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    await userEvent.click(await screen.findByRole("button", { name: /Это не адрес/ }));

    await waitFor(() =>
      expect(обращения.some((о) => о.url.includes("/address-candidates/"))).toBe(true),
    );
    expect(обращения.find((о) => о.url.includes("/address-candidates/"))!.body).toMatchObject({
      decision: "reject",
    });
  });

  it("подтверждённый адрес показан с подписью происхождения", async () => {
    /*
     * ⚠ ИЩЕМ ВНУТРИ БЛОКА АДРЕСА, А НЕ ПО ВСЕЙ КАРТОЧКЕ. Подпись «(из диалога)»
     * стоит и у телефона — она одна на оба поля по замыслу, и поиск по всему
     * экрану нашёл бы две. Такая проверка зеленела бы от чужой подписи.
     */
    ответ = личность({
      address: "ул. Ленина, 5",
      address_source: "dialog",
      address_candidates: [],
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    await waitFor(() => expect(container.querySelector(".card-address")).toBeTruthy());
    const блок = within(container.querySelector(".card-address") as HTMLElement);
    expect(await блок.findByText("ул. Ленина, 5")).toBeTruthy();
    expect(блок.getByText("(из диалога)")).toBeTruthy();
    // Предложения больше нет — вопрос снят.
    expect(screen.queryByText("Из переписки")).toBeNull();
  });

  it("наблюдатель видит адрес выезда, но не правит его", async () => {
    /*
     * Чтение личности открыто по `conversations:read`: наблюдатель отвечает на
     * вопрос «куда ехал мастер», а адрес лежит только здесь. Правка остаётся
     * за `conversations:manage`. Строка из переписки в данных оставлена
     * нарочно: у менеджера под ней стояла бы кнопка «Не адрес».
     */
    ответ = личность({ address: "ул. Мира, 12", address_source: "dialog" });
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    await waitFor(() => expect(container.querySelector(".card-address")).toBeTruthy());
    const блок = within(container.querySelector(".card-address") as HTMLElement);
    expect(await блок.findByText("ул. Мира, 12")).toBeTruthy();
    expect(блок.getByText("Также назван")).toBeTruthy();
    for (const имя of [/изменить/, /указать адрес/, /Не адрес/, /Записать в карточку/]) {
      expect(блок.queryByRole("button", { name: имя })).toBeNull();
    }
  });
});
