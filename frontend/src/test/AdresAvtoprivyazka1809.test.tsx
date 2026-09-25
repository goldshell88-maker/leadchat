/**
 * АВТОПРИВЯЗКА АДРЕСА БЕЗ ЧЕЛОВЕКА — ЧТО ВИДИТ ОПЕРАТОР (18.09, участок B).
 *
 * Решение владельца: адрес привязывается к карточке автоматически, оператор
 * ничего не подтверждает — у него остаются «изменить» у карточки и «Не адрес»
 * у строки. Степеней три, и экран читает их ОДНИМ полем сервера
 * `geo.precision`: `exact` — точка дома, `approx` — точка улицы / массива /
 * центра пункта / места, `none` — точки нет (в карточке текст без точки).
 * Здесь стережётся, что подпись под адресом называет степень словами, что
 * степень НЕ вычитывается из хвоста провайдера и `kind`, что под заполненной
 * карточкой вторая строка — «Также назван» без кнопок подтверждения, и что
 * текст без точки не рисует ссылку «на карте».
 *
 * ДИВЕРСИИ (обязаны краснеть): вернуть в `SOURCE_LABEL` одну подпись для
 * `auto` — «точка приблизительная» и «словами клиента» не находятся; читать
 * степень из `provider.endsWith("~approx")` — записанное место (`provider:
 * "dadata"`, `precision: "approx"`) теряет «точка приблизительная»; вернуть
 * кнопку «Подтвердить» — `queryByRole` находит её; убрать «Не адрес» —
 * единственное решение с экрана пропадает; вернуть одну подпись «словами
 * клиента» на всё `precision: "none"` — строка карты без точки (есть
 * `formatted`) и слова клиента после отказа (его нет) перестают различаться.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import type { AddressGeo } from "@/features/chats/components/card/clientApi";
import { qk } from "@/shared/api/queryKeys";
import { showToast } from "@/shared/ui/toast";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

const ФОРМАТ = "Сиреневая улица, 1, посёлок Заречный, Орск";

const ТОЧКА_ДОМА: AddressGeo = {
  status: "exact",
  formatted: ФОРМАТ,
  lat: 51.2031415,
  lon: 58.4926535,
  provider: "dadata",
  checked_at: "2026-09-18T10:00:05Z",
  precision: "exact",
};

/** Точка приблизительная — по `precision`; хвоста у провайдера нарочно нет. */
const ТОЧКА_ПРИБЛИЗИТЕЛЬНАЯ: AddressGeo = { ...ТОЧКА_ДОМА, precision: "approx" };

/** Текст без точки: карта улицу знает, дом — нет; координат нет. */
const БЕЗ_ТОЧКИ: AddressGeo = {
  status: "house_missing",
  formatted: "ул Сиреневая, 1, посёлок Заречный, Орск",
  lat: null,
  lon: null,
  provider: "dadata",
  checked_at: "2026-09-18T10:00:05Z",
  precision: "none",
};

const ЦИТАТА = {
  raw: "п заречный ул сиреневая д 1, кв 3",
  parts: { office: "3" },
  conversation_id: CONV_ID,
  locality: null,
};

function строка(over: Record<string, unknown> = {}) {
  return {
    id: "addr-2",
    status: "pending",
    value: "ул Мира, 7",
    street: "ул Мира",
    house: "7",
    kind: "house",
    parts: {},
    raw: "или на Мира 7, к маме",
    level: "A",
    settlement: null,
    settlement_type: null,
    locality: null,
    district: null,
    area: null,
    geo: { ...ТОЧКА_ДОМА, formatted: "улица Мира, 7, Орск", lat: 51.23, lon: 58.47 },
    conversation_id: CONV_ID,
    message_id: "m-2",
    message_at: "2026-09-18T10:05:00Z",
    detected_at: "2026-09-18T10:05:01Z",
    ...over,
  };
}

function личность(over: Record<string, unknown> = {}) {
  return {
    id: "client-1",
    name: "Иван Петров",
    phone: "+79005550177",
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    address: `${ФОРМАТ}, кв 3`,
    address_source: "auto",
    address_geo: ТОЧКА_ДОМА,
    address_evidence: ЦИТАТА,
    address_candidates: [],
    addresses: [],
    external_id: "923456789",
    phones: [{ value: "+79005550177", client_id: "client-1", primary: true }],
    avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
    merged_from: [],
    merged_into: null,
    ...over,
  };
}

describe("автопривязка адреса: подписи по степени и экран без подтверждения", () => {
  let ответ: Record<string, unknown>;
  let обращения: Array<{ url: string; method: string; body: unknown }>;

  beforeEach(() => {
    ответ = личность();
    обращения = [];
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
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
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

  async function блокАдреса(container: HTMLElement) {
    await waitFor(() => expect(container.querySelector(".card-address")).toBeTruthy());
    return within(container.querySelector(".card-address") as HTMLElement);
  }

  it("точка дома: подпись «по карте», «точка дома» по data-precision, ссылка есть", async () => {
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText(`${ФОРМАТ}, кв 3`)).toBeTruthy();
    expect(блок.getByText("(автоматически, по карте)")).toBeTruthy();
    expect(container.querySelector('[data-precision="exact"]')?.textContent).toContain("точка дома");
    expect(container.querySelector('[data-approx="1"]')).toBeNull();
    expect(блок.getByRole("link", { name: "на карте" })).toBeTruthy();
    // Цитата под записанным автоматикой адресом — доказательство.
    expect(container.querySelector(".card-address__quote")?.textContent).toContain(ЦИТАТА.raw);
  });

  it("приблизительная точка: подпись и слова — по `precision`, не по хвосту провайдера", async () => {
    ответ = личность({ address_geo: ТОЧКА_ПРИБЛИЗИТЕЛЬНАЯ });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("(автоматически, точка приблизительная)")).toBeTruthy();
    expect(container.querySelector('[data-approx="1"]')?.textContent).toContain(
      "точка приблизительная",
    );
    expect(container.querySelector('[data-precision="exact"]')).toBeNull();
    expect(блок.queryByText("(автоматически, по карте)")).toBeNull();
  });

  it("записанное место — «точка приблизительная» без чтения kind на экране", async () => {
    // Сервер отдаёт для места `precision: "approx"` сам; экран не знает,
    // что это место, и всё равно называет степень верно.
    ответ = личность({
      address: "деревня Ивановка, Гатчинский район",
      address_geo: {
        ...ТОЧКА_ПРИБЛИЗИТЕЛЬНАЯ,
        formatted: "деревня Ивановка, Гатчинский район",
        lat: 59.5,
        lon: 30.1,
      },
      address_evidence: { ...ЦИТАТА, raw: "деревня Ивановка, Гатчинский район", parts: {} },
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("(автоматически, точка приблизительная)")).toBeTruthy();
    const подписи = Array.from(container.querySelectorAll(".card-address__attribution")).map(
      (э) => э.textContent ?? "",
    );
    expect(подписи.some((т) => т.includes("точка приблизительная"))).toBe(true);
    expect(подписи.some((т) => т.includes("точка дома"))).toBe(false);
  });

  it("текст без точки: подпись «улица по карте, дом со слов клиента», слова отказа, ссылки «на карте» нет", async () => {
    // В поле лежит строка КАРТЫ без точки (карта знает улицу клиента, номер
    // дома — его слова), а не слова клиента: подпись обязана это различать
    // (ревью 19.09) — и различает по `formatted`, не по хвостам провайдера.
    ответ = личность({
      address: "ул Сиреневая, 1, посёлок Заречный, Орск, кв 3",
      address_geo: БЕЗ_ТОЧКИ,
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(
      await блок.findByText("(автоматически: улица по карте, дом со слов клиента)"),
    ).toBeTruthy();
    expect(блок.queryByText("(автоматически, словами клиента)")).toBeNull();
    expect(блок.getByText(/карта нашла улицу, но не дом/)).toBeTruthy();
    expect(блок.queryByRole("link", { name: "на карте" })).toBeNull();
    // Под текстом без точки степень словами не дублируется: слова статуса
    // уже сказали, почему точки нет.
    expect(container.querySelector('[data-precision="exact"]')).toBeNull();
    expect(container.querySelector('[data-approx="1"]')).toBeNull();
  });

  it("под адресом без вердикта карты автозапись подписана «словами клиента»", async () => {
    // Карта выключена: сервер `address_geo` не отдаёт, но адрес поставила
    // автоматика — подпись честная, а не «по карте».
    ответ = личность({ address: "ул Сиреневая, 1, кв 3", address_geo: null });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("(автоматически, словами клиента)")).toBeTruthy();
  });

  it("отказ карты без строки улицы: в поле слова клиента — подпись «словами клиента»", async () => {
    // Карта окончательно отказала и улицы не знает (`formatted` нет):
    // пересборка положила в поле слова клиента с его пунктом — подпись
    // «улица по карте» здесь соврала бы.
    ответ = личность({
      address: "ул Полевая, 1, посёлок Луговой, кв 35",
      address_geo: { ...БЕЗ_ТОЧКИ, status: "not_found", formatted: null },
      address_evidence: { ...ЦИТАТА, raw: "Луговой; Полевая 1 кв 35", parts: { office: "35" } },
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("(автоматически, словами клиента)")).toBeTruthy();
    expect(блок.queryByText(/улица по карте/)).toBeNull();
    expect(блок.queryByRole("link", { name: "на карте" })).toBeNull();
  });

  it("заполненная карточка + вторая строка: «Также назван», без подтверждения, с «Не адрес» и «изменить»", async () => {
    ответ = личность({ address_candidates: [строка()] });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    const заголовок = await блок.findByText("Также назван");
    const карточкаСтроки = within(заголовок.closest(".card-address__suggest") as HTMLElement);
    expect(карточкаСтроки.getByText(/улица Мира, 7, Орск/)).toBeTruthy();
    expect(карточкаСтроки.getByText("или на Мира 7, к маме")).toBeTruthy();
    // Между местами автоматика не переезжает — и экран не предлагает нажать за неё.
    expect(screen.queryByRole("button", { name: /Подтвердить|Записать адрес выезда/ })).toBeNull();
    expect(карточкаСтроки.getByRole("button", { name: /Это не адрес/ })).toBeTruthy();
    expect(блок.getByRole("button", { name: "изменить" })).toBeTruthy();
    // Без нажатия карточка на сервер ничего не пишет.
    expect(обращения.some((о) => о.method !== "GET")).toBe(false);
  });

  it("заполненная карточка + место: «Также названо место»", async () => {
    ответ = личность({
      address_candidates: [
        строка({
          id: "addr-3",
          kind: "place",
          value: "деревня Ивановка",
          street: "",
          house: "",
          raw: "мы в деревне Ивановка",
          geo: { ...ТОЧКА_ПРИБЛИЗИТЕЛЬНАЯ, formatted: "деревня Ивановка, Гатчинский район" },
        }),
      ],
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("Также названо место")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Подтвердить|Записать адрес выезда/ })).toBeNull();
  });

  it("пустая карточка: строка — «Из переписки», кнопок подтверждения нет, «Не адрес» есть", async () => {
    ответ = личность({
      address: null,
      address_source: "none",
      address_geo: null,
      address_evidence: null,
      address_candidates: [строка({ geo: { ...ТОЧКА_ДОМА, status: "pending", formatted: null, lat: null, lon: null, precision: "none" } })],
    });
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    expect(await screen.findByText("Из переписки")).toBeTruthy();
    expect(screen.getByText(/проверяем по карте/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Подтвердить|Записать адрес выезда/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Это не адрес/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /указать адрес/ })).toBeTruthy();
  });
});
