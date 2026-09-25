/**
 * АДРЕС, ПРОВЕРЕННЫЙ КАРТОЙ, — В КАРТОЧКЕ И В НАСТРОЙКАХ (11.09).
 *
 * Просьба владельца: «поле должно работать полностью автоматически… адрес в
 * таком формате, как в Яндекс-картах». Здесь стережётся то, что оператор
 * ВИДИТ: строку в формате карт, слова вердикта, цитату под записанным адресом,
 * ссылку «на карте» БЕЗ адреса в строке запроса — и выключатель автоматики.
 *
 * ДИВЕРСИИ (обязаны краснеть): подменить строку карты на `value` при exact —
 * «в формате карт» не находится; положить адрес в `?text=` ссылки —
 * проверка «без адреса в запросе» краснеет; убрать подпись `auto` —
 * «(автоматически, по карте)» не находится; вернуть чтение хвоста «~approx»
 * вместо `precision` — «точка приблизительная» при `precision: "exact"`
 * появляется, а при `precision: "approx"` без хвоста пропадает.
 *
 * С 18.09 подтверждения с экрана нет (владелец: адрес пишет автоматика):
 * варианты карты — текстом, кнопок «Записать адрес выезда» нет, остаётся
 * «Не адрес». Подписи источника по степени — в `AdresAvtoprivyazka1809`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { geoWords } from "@/features/chats/components/card/geoWords";
import type { AddressGeo } from "@/features/chats/components/card/clientApi";
import { AddressDetectBlock } from "@/features/settings/accounts/AddressDetectBlock";
import { qk } from "@/shared/api/queryKeys";
import { showToast } from "@/shared/ui/toast";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

const ФОРМАТ = "Сиреневая улица, 1, посёлок Заречный, Орск";

const ГЕО_EXACT: AddressGeo = {
  status: "exact",
  formatted: ФОРМАТ,
  lat: 51.2031415,
  lon: 58.4926535,
  provider: "nominatim",
  checked_at: "2026-09-11T10:00:05Z",
  precision: "exact",
};

function предложение(over: Record<string, unknown> = {}) {
  return {
    id: "addr-1",
    status: "pending",
    value: "ул сиреневая, 1",
    street: "ул сиреневая",
    house: "1",
    parts: { office: "3" },
    raw: "п заречный ул сиреневая д 1, кв 3",
    level: "A",
    settlement: "заречный",
    settlement_type: "посёлок",
    locality: null,
    geo: ГЕО_EXACT,
    conversation_id: CONV_ID,
    message_id: "m-1",
    message_at: "2026-09-11T10:00:00Z",
    detected_at: "2026-09-11T10:00:01Z",
    ...over,
  };
}

function личность(over: Record<string, unknown> = {}) {
  return {
    id: "client-1",
    name: "Иван Петров",
    phone: "+79001112250",
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    address: null,
    address_source: "none",
    address_geo: null,
    address_evidence: null,
    address_candidates: [],
    addresses: [],
    external_id: "923456789",
    phones: [{ value: "+79001112250", client_id: "client-1", primary: true }],
    avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
    merged_from: [],
    merged_into: null,
    ...over,
  };
}

describe("адрес, проверенный картой, в карточке", () => {
  let ответ: Record<string, unknown>;

  beforeEach(() => {
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
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
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

  it("подтверждённое предложение показано в формате карт со словами и ссылкой координатами", async () => {
    ответ = личность({ address_candidates: [предложение()] });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const заголовок = await screen.findByText("Из переписки");
    const блок = within(заголовок.closest(".card-address__suggest") as HTMLElement);

    expect(блок.getByText(new RegExp(ФОРМАТ))).toBeTruthy();
    expect(блок.getByText(/карта подтвердила/)).toBeTruthy();
    // Цитата клиента — как написана, а не как переписала карта.
    expect(блок.getByText("п заречный ул сиреневая д 1, кв 3")).toBeTruthy();
    const ссылка = блок.getByRole("link", { name: "на карте" }) as HTMLAnchorElement;
    expect(ссылка.href).toContain("ll=58.4926535,51.2031415");
    // ⚠ БЕЗ АДРЕСА В СТРОКЕ ЗАПРОСА: это персональные данные клиента в чужом сервисе.
    expect(ссылка.href).not.toMatch(/text=|сиренев/i);
    expect(ссылка.rel).toContain("noopener");
    expect(container.querySelector(".card-address__attribution")?.textContent).toContain("OpenStreetMap");
  });

  it("точка приблизительная — по `precision`, точка от Яндекса подписана (18.09)", async () => {
    // DaData знает дом, но точку даёт только посёлка (qc_geo 3): мастер обязан
    // видеть, что на карте не сам дом. Степень экран читает ТОЛЬКО из
    // `precision` — хвост «~approx» у провайдера сам по себе ничего не значит.
    // Уточнил Яндекс — подпись по условиям карт, «точка дома» — по степени.
    ответ = личность({
      address_candidates: [
        предложение({ geo: { ...ГЕО_EXACT, provider: "dadata~approx", precision: "approx" } }),
        предложение({
          id: "addr-2",
          value: "ул сиреневая, 2",
          house: "2",
          geo: { ...ГЕО_EXACT, provider: "dadata+yandex" },
        }),
      ],
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    await screen.findAllByText("Из переписки");
    const подписи = Array.from(container.querySelectorAll(".card-address__attribution")).map(
      (э) => э.textContent,
    );
    expect(подписи.some((т) => т?.includes("точка приблизительная"))).toBe(true);
    expect(подписи.some((т) => т?.includes("© Яндекс"))).toBe(true);
    expect(подписи.some((т) => т?.includes("OpenStreetMap"))).toBe(false);
    expect(container.querySelectorAll('[data-precision="exact"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-approx="1"]')).toHaveLength(1);
  });

  it("точка OSM с хвостом «~approx» подписана © OpenStreetMap (подстрокой, не концом)", async () => {
    // Хвост «~approx» — степень точки, а не другая карта: условия
    // использования OSM он не отменяет (ревью 19.09: `endsWith("nominatim")`
    // терял подпись у приблизительной точки OSM).
    ответ = личность({
      address_candidates: [
        предложение({ geo: { ...ГЕО_EXACT, provider: "nominatim~approx", precision: "approx" } }),
      ],
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    await screen.findByText("Из переписки");
    const подписи = Array.from(container.querySelectorAll(".card-address__attribution")).map(
      (э) => э.textContent,
    );
    expect(подписи.some((т) => т?.includes("OpenStreetMap"))).toBe(true);
    expect(подписи.some((т) => т?.includes("точка приблизительная"))).toBe(true);
    expect(container.querySelector('[data-precision="exact"]')).toBeNull();
  });

  it("хвост «~approx» без `precision: approx` точку приблизительной не делает", async () => {
    // Диверсия наоборот: если экран снова начнёт разбирать провайдера,
    // здесь появится лишняя подпись — два пути к одному признаку.
    ответ = личность({
      address_candidates: [предложение({ geo: { ...ГЕО_EXACT, provider: "dadata~approx~picked" } })],
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    await screen.findByText("Из переписки");
    expect(container.querySelector('[data-approx="1"]')).toBeNull();
    expect(container.querySelector('[data-precision="exact"]')?.textContent).toContain("точка дома");
  });

  it("отказ карты назван словами, строка остаётся словами клиента", async () => {
    ответ = личность({
      address_candidates: [
        предложение({
          locality: "Гай",
          geo: { ...ГЕО_EXACT, status: "other_city_in_text", formatted: null, lat: null, lon: null },
        }),
      ],
    });
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    expect(await screen.findByText(/клиент назвал другой город: Гай/)).toBeTruthy();
    expect(screen.getByText(/ул сиреневая, 1/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "на карте" })).toBeNull();
  });

  it("пока карта думает — так и написано", async () => {
    ответ = личность({
      address_candidates: [предложение({ geo: { ...ГЕО_EXACT, status: "pending", formatted: null, lat: null, lon: null } })],
    });
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    expect(await screen.findByText(/проверяем по карте/)).toBeTruthy();
  });

  it("адрес, поставленный автоматикой, подписан и держит цитату клиента", async () => {
    ответ = личность({
      address: `${ФОРМАТ}, кв 3`,
      address_source: "auto",
      address_geo: ГЕО_EXACT,
      address_evidence: {
        raw: "п заречный ул сиреневая д 1, кв 3",
        // Подъезд и домофон названы ПОСЛЕ автозаписи: в тексте поля их нет,
        // значит они обязаны быть видны под цитатой (ревью 11.09).
        parts: { office: "3", entrance: "2", intercom: "1234" },
        conversation_id: CONV_ID,
        locality: null,
      },
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    expect(await screen.findByText(`${ФОРМАТ}, кв 3`)).toBeTruthy();
    expect(screen.getByText("(автоматически, по карте)")).toBeTruthy();
    const цитата = container.querySelector(".card-address__quote")!;
    expect(цитата.textContent).toContain("п заречный ул сиреневая д 1, кв 3");
    expect(цитата.textContent).toContain("2 подъезд");
    expect(цитата.textContent).toContain("домофон 1234");
    // «кв 3» уже в поле — под цитатой не повторяется.
    expect(цитата.textContent).not.toMatch(/·\s*кв 3/);
    expect(screen.getByRole("link", { name: "на карте" })).toBeTruthy();
  });

  it("варианты карты — текстом; единственная кнопка «Не адрес» шлёт reject (18.09)", async () => {
    // Карта не выбрала один дом: автоматика такое не пишет, а найденные
    // адреса — подсказка тому, кто нажмёт «изменить». Кнопок записи нет,
    // слова вердикта констатируют, а не просят выбрать.
    const обращения: Array<{ url: string; body: unknown }> = [];
    ответ = личность({
      address_candidates: [
        предложение({
          geo: {
            ...ГЕО_EXACT,
            status: "elsewhere",
            formatted: null,
            lat: null,
            lon: null,
            precision: "none",
            variants: [
              { formatted: "улица Школьная, 14/3, Комсомольск-на-Амуре", lat: 50.5, lon: 137, city: "Комсомольск-на-Амуре" },
              { formatted: "улица Школьная, 14/3, Амурск", lat: 50.2, lon: 136.9, city: "Амурск" },
            ],
          },
        }),
      ],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/resolve")) {
          обращения.push({ url, body: JSON.parse(String(init?.body)) });
          return jsonResponse(200, { address: null, candidate: {} });
        }
        if (url.includes("/identity")) return jsonResponse(200, ответ);
        if (url.includes("/merge-candidates")) return jsonResponse(200, { items: [] });
        if (/\/conversations\//.test(url)) return jsonResponse(200, makeConversation());
        return jsonResponse(200, { items: [] });
      }),
    );
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const слова = await screen.findByText(/такого дома нет, но он есть в области/);
    expect(слова.textContent).not.toMatch(/проверьте вариант/);
    expect(screen.getByText("улица Школьная, 14/3, Амурск")).toBeTruthy();
    expect(screen.getByText("улица Школьная, 14/3, Комсомольск-на-Амуре")).toBeTruthy();
    expect(screen.queryAllByRole("button", { name: /Записать адрес выезда/ })).toHaveLength(0);
    expect(screen.queryByText(/Подтвердить/)).toBeNull();
    // Без нажатия к `/resolve` не ходим: варианты — информация.
    expect(обращения).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: /Это не адрес/ }));
    await waitFor(() => expect(обращения).toHaveLength(1));
    expect(обращения[0].body).toEqual({ decision: "reject" });
  });

  it("каждый статус карты имеет слова", () => {
    const статусы = [
      "pending", "exact", "ambiguous", "not_found", "house_missing", "house_mismatch",
      "street_mismatch", "settlement_mismatch", "city_mismatch", "region_mismatch",
      "other_city_in_text", "no_city", "blocked", "error", "elsewhere",
    ] as const;
    for (const status of статусы) {
      expect(geoWords({ ...ГЕО_EXACT, status }, "Гай"), status).toBeTruthy();
    }
    expect(geoWords(null, null)).toBeNull();
  });
});

describe("настройки адреса", () => {
  let обращения: Array<{ url: string; method: string; body: Record<string, unknown> }>;
  let настройки: Record<string, unknown>;

  beforeEach(() => {
    обращения = [];
    настройки = {
      enabled: true,
      autofill: true,
      levels: "A",
      geo_enabled: true,
      provider: "nominatim",
      yandex_key_present: false,
      yandex_daily_limit: 900,
      yandex_used_today: 0,
      suggest_enabled: true,
      suggest_key_present: true,
      suggest_daily_limit: 900,
      suggest_used_today: 4,
      dadata_enabled: true,
      dadata_key_present: true,
      dadata_daily_limit: 9000,
      dadata_used_today: 12,
      llm_enabled: true,
      llm_key_present: true,
      llm_daily_limit: 50,
      llm_used_today: 3,
    };
    queryClient.clear();
    vi.mocked(showToast).mockClear();
    resetSessionStore({ user: fakeUser, permissions: fakeMe.permissions as never, accessToken: "t", bootstrapped: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        обращения.push({ url, method: init?.method ?? "GET", body });
        if (init?.method === "PATCH") {
          настройки = { ...настройки, ...body };
        }
        return jsonResponse(200, настройки);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("тумблер автозаписи шлёт ровно своё поле", async () => {
    renderWithProviders(<AddressDetectBlock />);
    const тумблер = await screen.findByRole("switch", {
      name: /Писать подтверждённый картой адрес прямо в карточку/,
    });
    expect(тумблер).toBeChecked();
    await userEvent.click(тумблер);
    await waitFor(() => expect(обращения.some((o) => o.method === "PATCH")).toBe(true));
    const patch = обращения.find((o) => o.method === "PATCH")!;
    expect(patch.url).toContain("/settings/address-detect");
    expect(patch.body).toEqual({ autofill: false });
    await waitFor(() => expect(тумблер).not.toBeChecked());
  });

  it("уровни показа переключаются одним полем", async () => {
    renderWithProviders(<AddressDetectBlock />);
    const все = await screen.findByRole("radio", { name: "Все" });
    await userEvent.click(все);
    await waitFor(() => expect(обращения.some((o) => o.method === "PATCH")).toBe(true));
    expect(обращения.find((o) => o.method === "PATCH")!.body).toEqual({ levels: "ABC" });
  });

  it("режим «OSM, потом Яндекс» показывает расход за сегодня", async () => {
    настройки = { ...настройки, provider: "osm_then_yandex", yandex_key_present: true, yandex_used_today: 37 };
    renderWithProviders(<AddressDetectBlock />);
    const расход = await screen.findByTestId("yandex-usage");
    expect(расход.textContent).toContain("37 из 900");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("подсказка показывает расход и предупреждает без ключа", async () => {
    renderWithProviders(<AddressDetectBlock />);
    expect((await screen.findByTestId("suggest-usage")).textContent).toContain("4 из 900");
    настройки = { ...настройки, suggest_key_present: false };
    queryClient.clear();
    renderWithProviders(<AddressDetectBlock />);
    expect((await screen.findByRole("alert")).textContent).toContain(
      "У шлюза внешних API нет ключа Геосаджеста",
    );
  });

  it("DaData: тумблер шлёт своё поле, расход виден, без ключа — предупреждение", async () => {
    renderWithProviders(<AddressDetectBlock />);
    expect((await screen.findByTestId("dadata-usage")).textContent).toContain("12 из 9000");
    const тумблер = screen.getByRole("switch", { name: /Сначала спрашивать DaData/ });
    await userEvent.click(тумблер);
    await waitFor(() => expect(обращения.some((o) => o.method === "PATCH")).toBe(true));
    expect(обращения.find((o) => o.method === "PATCH")!.body).toEqual({ dadata_enabled: false });
    await waitFor(() => expect(screen.queryByTestId("dadata-usage")).toBeNull());
    настройки = { ...настройки, dadata_enabled: true, dadata_key_present: false };
    queryClient.clear();
    renderWithProviders(<AddressDetectBlock />);
    const тревоги = await screen.findAllByRole("alert");
    expect(
      тревоги.some((t) => t.textContent?.includes("У шлюза внешних API нет ключа DaData")),
    ).toBe(true);
  });

  it("шлюз не настроен — подсказка называет шлюз, а не ключ (проверка 24.09)", async () => {
    настройки = {
      ...настройки,
      dadata_enabled: true,
      dadata_key_present: false,
      gateway_configured: false,
    };
    renderWithProviders(<AddressDetectBlock />);
    const тревоги = await screen.findAllByRole("alert");
    expect(
      тревоги.some((t) => t.textContent?.includes("Шлюз внешних API не настроен (GATEWAY_URL")),
    ).toBe(true);
    expect(тревоги.some((t) => t.textContent?.includes("нет ключа"))).toBe(false);
  });

  it("Яндекс без ключа предупреждает, а не молчит", async () => {
    настройки = { ...настройки, provider: "yandex" };
    renderWithProviders(<AddressDetectBlock />);
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      expect.stringContaining("У шлюза внешних API нет ключа Яндекс Геокодера"),
    );
  });
});
