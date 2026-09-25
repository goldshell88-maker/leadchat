/**
 * ДВЕ ОСИ ВЕРДИКТА И «АДРЕС НЕВЕРНЫЙ» (пакет 6.0а, I-9, 20.09).
 *
 * Под записанным автоадресом экран показывает не только степень точки
 * (`precision`), но и причину — подпись правила словами сервера
 * (`geo.rule_label`), а также варианты источника; кнопка «Адрес неверный»
 * в два касания (подтверждение встроено в поле) зовёт
 * `POST /clients/{id}/address/wrong` — строка закрывается, карточка пустеет.
 * Строка-предложение правила (`geo.suggest`) названа предложением в той же
 * строке и получает единственную кнопку записи — «Записать в карточку»
 * (`resolve` с `replace`).
 *
 * ДИВЕРСИИ (обязаны краснеть): убрать ось `rule_label` из `GeoLine` —
 * «точка улицы, дом не найден» не находится; рисовать кнопку «Адрес неверный»
 * при любом `source` — тест «под набранным руками кнопки нет»; звать ручку
 * первым касанием, без подтверждения — обращение к `/address/wrong` появляется
 * до «Да, неверный»; не рисовать «Записать в карточку» у `suggest` — тест
 * предложения; рисовать её у обычной строки — тот же тест (вторая строка без
 * `suggest` кнопки не имеет); не показывать варианты источника —
 * «Варианты карты» под записанным адресом не находится.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ClientAddressField } from "@/features/chats/components/card/ClientAddressField";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import type { AddressGeo } from "@/features/chats/components/card/clientApi";
import { qk } from "@/shared/api/queryKeys";
import { showToast } from "@/shared/ui/toast";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

const ФОРМАТ = "Сиреневая улица, 1, посёлок Заречный, Орск";
const ПОДПИСЬ = "точка улицы, дом не найден";

/** Точка улицы правилом `street_point`: степень approx, причина — подпись. */
const ТОЧКА_ПРАВИЛОМ: AddressGeo = {
  status: "exact",
  formatted: ФОРМАТ,
  lat: 51.2031415,
  lon: 58.4926535,
  provider: "dadata~approx",
  checked_at: "2026-09-20T10:00:05Z",
  precision: "approx",
  variants: [],
  rule: "street_point",
  rule_label: ПОДПИСЬ,
  suggest: false,
};

/** Вердикт самой карты: правила нет — второй оси нет. */
const ТОЧКА_КАРТЫ: AddressGeo = {
  ...ТОЧКА_ПРАВИЛОМ,
  provider: "dadata",
  precision: "exact",
  rule: null,
  rule_label: null,
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
    geo: { ...ТОЧКА_КАРТЫ, formatted: "улица Мира, 7, Орск", lat: 51.23, lon: 58.47 },
    conversation_id: CONV_ID,
    message_id: "m-2",
    message_at: "2026-09-20T10:05:00Z",
    detected_at: "2026-09-20T10:05:01Z",
    ...over,
  };
}

function личность(over: Record<string, unknown> = {}) {
  return {
    id: "client-1",
    name: "Клиент",
    phone: "+79001112244",
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    address: `${ФОРМАТ}, кв 3`,
    address_source: "auto",
    address_geo: ТОЧКА_ПРАВИЛОМ,
    address_evidence: ЦИТАТА,
    address_candidates: [],
    addresses: [],
    external_id: "923456789",
    phones: [{ value: "+79001112244", client_id: "client-1", primary: true }],
    avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
    merged_from: [],
    merged_into: null,
    ...over,
  };
}

describe("адрес: подпись правила, варианты источника и «Адрес неверный»", () => {
  let ответ: Record<string, unknown>;
  let ответНеверный: () => Response;
  let обращения: Array<{ url: string; method: string; body: unknown }>;

  beforeEach(() => {
    ответ = личность();
    ответНеверный = () => jsonResponse(200, { address: null, changed: true });
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
        const method = init?.method ?? "GET";
        обращения.push({
          url,
          method,
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
        if (url.endsWith("/address/wrong") && method === "POST") {
          const r = ответНеверный();
          // После удачного стирания сервер отдаёт пустую карточку.
          if (r.ok) {
            ответ = личность({
              address: null,
              address_source: "none",
              address_geo: null,
              address_evidence: null,
            });
          }
          return r;
        }
        if (url.includes("/address-candidates/") && url.endsWith("/resolve")) {
          return jsonResponse(200, {
            address: "улица Мира, 7, Орск",
            candidate: { ...строка(), status: "accepted" },
          });
        }
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

  const запросыНеверный = () =>
    обращения.filter((о) => о.method === "POST" && о.url.endsWith("/address/wrong"));

  it("под автоадресом — обе оси: степень и подпись правила словами сервера", async () => {
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText(`${ФОРМАТ}, кв 3`)).toBeTruthy();
    // Первая ось — степень.
    expect(container.querySelector('[data-approx="1"]')?.textContent).toContain(
      "точка приблизительная",
    );
    // Вторая ось — причина, ровно словами сервера и после степени.
    const причина = container.querySelector('[data-rule="street_point"]');
    expect(причина?.textContent).toContain(ПОДПИСЬ);
    expect(причина?.textContent).not.toContain("предложение");
    const строкаКарты = container.querySelector(".card-address__geo")?.textContent ?? "";
    expect(строкаКарты.indexOf("точка приблизительная")).toBeLessThan(строкаКарты.indexOf(ПОДПИСЬ));
    // Кнопка есть, но без нажатия на сервер ничего не уходит.
    expect(блок.getByRole("button", { name: "Адрес неверный" })).toBeTruthy();
    expect(обращения.some((о) => о.method !== "GET")).toBe(false);
  });

  it("без правила второй оси нет: решала сама карта", async () => {
    ответ = личность({ address_geo: ТОЧКА_КАРТЫ });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("(автоматически, по карте)")).toBeTruthy();
    expect(container.querySelector("[data-rule]")).toBeNull();
    expect(блок.queryByText(new RegExp(ПОДПИСЬ))).toBeNull();
  });

  it("варианты источника показываются под записанным адресом", async () => {
    ответ = личность({
      address_geo: {
        ...ТОЧКА_ПРАВИЛОМ,
        rule: "only_in_radius",
        rule_label: "один из нескольких тёзок в 40 км от города",
        variants: [
          { formatted: "Сиреневая улица, 1, Орск", lat: 51.23, lon: 58.47, city: "Орск" },
          { formatted: ФОРМАТ, lat: 51.2031415, lon: 58.4926535, city: "Орск" },
        ],
      },
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    const список = await блок.findByRole("list", { name: "Варианты карты" });
    expect(within(список).getAllByRole("listitem").map((э) => э.textContent)).toEqual([
      "Сиреневая улица, 1, Орск",
      ФОРМАТ,
    ]);
    expect(container.querySelector('[data-rule="only_in_radius"]')?.textContent).toContain(
      "один из нескольких тёзок",
    );
  });

  it("«Адрес неверный»: первое касание — вопрос, второе — POST /address/wrong, карточка пустеет", async () => {
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    await userEvent.click(await блок.findByRole("button", { name: "Адрес неверный" }));
    // Подтверждение встроено в поле; на сервер ещё ничего не ушло.
    const вопрос = блок.getByRole("group", { name: "Подтверждение: адрес неверный" });
    expect(within(вопрос).getByText(/Убрать адрес из карточки/)).toBeTruthy();
    expect(запросыНеверный()).toHaveLength(0);
    // «Отмена» закрывает вопрос без запроса.
    await userEvent.click(within(вопрос).getByRole("button", { name: "Отмена" }));
    expect(блок.queryByRole("group", { name: "Подтверждение: адрес неверный" })).toBeNull();
    expect(запросыНеверный()).toHaveLength(0);
    // Второй заход — подтверждаем.
    await userEvent.click(блок.getByRole("button", { name: "Адрес неверный" }));
    await userEvent.click(блок.getByRole("button", { name: "Да, неверный" }));
    await waitFor(() => expect(запросыНеверный()).toHaveLength(1));
    expect(запросыНеверный()[0].body).toEqual({ conversation_id: CONV_ID });
    // Карточка перечитана: адреса нет, кнопки нет, есть «указать адрес».
    expect(await блок.findByRole("button", { name: /указать адрес/ })).toBeTruthy();
    expect(блок.queryByRole("button", { name: "Адрес неверный" })).toBeNull();
    expect(vi.mocked(showToast)).toHaveBeenCalledWith(
      expect.objectContaining({ message: "Адрес убран: отмечен как неверный" }),
    );
  });

  it("409 от сервера: слова сервера в тосте, карточка перечитывается", async () => {
    ответНеверный = () =>
      jsonResponse(
        409,
        errorEnvelope("address_not_auto", "Адрес записан не автоматикой — исправьте его через «изменить»"),
      );
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    await userEvent.click(await блок.findByRole("button", { name: "Адрес неверный" }));
    const запросовДо = обращения.filter((о) => о.url.includes("/identity")).length;
    await userEvent.click(блок.getByRole("button", { name: "Да, неверный" }));
    await waitFor(() =>
      expect(vi.mocked(showToast)).toHaveBeenCalledWith(
        expect.objectContaining({
          color: "red",
          message: "Адрес записан не автоматикой — исправьте его через «изменить»",
        }),
      ),
    );
    // Устаревший экран перечитывает личность и после отказа.
    await waitFor(() =>
      expect(обращения.filter((о) => о.url.includes("/identity")).length).toBeGreaterThan(запросовДо),
    );
  });

  it("под набранным руками адресом кнопки «Адрес неверный» нет", async () => {
    ответ = личность({
      address: "Ленина 5",
      address_source: "manual",
      address_geo: null,
      address_evidence: null,
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("(со слов)")).toBeTruthy();
    expect(блок.getByRole("button", { name: "изменить" })).toBeTruthy();
    expect(блок.queryByRole("button", { name: "Адрес неверный" })).toBeNull();
  });

  it("под принятым человеком адресом (dialog) кнопки нет — спор двух людей решает «изменить»", async () => {
    ответ = личность({ address_source: "dialog" });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    expect(await блок.findByText("(из диалога)")).toBeTruthy();
    expect(блок.queryByRole("button", { name: "Адрес неверный" })).toBeNull();
  });

  it("без права править (`editable: false`) кнопки нет, оси и варианты остаются", () => {
    // Карточка без `conversations:manage` личность не запрашивает вовсе, так
    // что заслон проверяется на самом поле: показ — всем, действия — по праву.
    const { container } = renderWithProviders(
      <ClientAddressField
        clientId="client-1"
        convId={CONV_ID}
        address={`${ФОРМАТ}, кв 3`}
        source="auto"
        geo={{
          ...ТОЧКА_ПРАВИЛОМ,
          variants: [{ formatted: ФОРМАТ, lat: 51.22, lon: 58.47, city: "Орск" }],
        }}
        evidence={ЦИТАТА}
        candidates={[]}
        editable={false}
      />,
    );
    const блок = within(container.querySelector(".card-address") as HTMLElement);
    expect(блок.getByText(`${ФОРМАТ}, кв 3`)).toBeTruthy();
    expect(container.querySelector('[data-rule="street_point"]')?.textContent).toContain(ПОДПИСЬ);
    expect(блок.getByRole("list", { name: "Варианты карты" })).toBeTruthy();
    expect(блок.queryByRole("button", { name: "Адрес неверный" })).toBeNull();
    expect(блок.queryByRole("button", { name: "изменить" })).toBeNull();
  });

  it("строка-предложение правила: названа предложением, «Записать в карточку» одним нажатием, у обычной строки кнопки нет", async () => {
    ответ = личность({
      address: null,
      address_source: "none",
      address_geo: null,
      address_evidence: null,
      address_candidates: [
        строка({
          geo: {
            ...ТОЧКА_ПРАВИЛОМ,
            formatted: "улица Мира, Орск",
            provider: "dadata~approx~suggest",
            suggest: true,
            variants: [
              { formatted: "улица Мира, 7, Орск", lat: 51.23, lon: 58.47, city: "Орск" },
            ],
          },
        }),
        строка({ id: "addr-3", value: "ул Пушкина, 9", street: "ул Пушкина", house: "9", raw: "Пушкина 9" }),
      ],
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);
    await waitFor(() => expect(container.querySelector('[data-suggest="1"]')).toBeTruthy());
    expect(container.querySelector('[data-suggest="1"]')?.textContent).toContain(
      `${ПОДПИСЬ} — предложение`,
    );
    // Точка предложения настоящая: степень названа, ссылка есть.
    expect(container.querySelector('[data-approx="1"]')).toBeTruthy();
    // Варианты предложения сохранены и показаны.
    expect(блок.getByRole("list", { name: "Варианты карты" })).toBeTruthy();
    // Кнопка записи — ровно одна, у предложения; у обычной строки её нет.
    // Ищем по видимой подписи: доступное имя обязано с неё начинаться (WCAG 2.5.3),
    // иначе голосовая команда «Записать в карточку» кнопку не найдёт.
    const кнопки = блок.getAllByRole("button", { name: /Записать в карточку/ });
    expect(кнопки).toHaveLength(1);
    expect(кнопки[0].getAttribute("aria-label")).toBe("Записать в карточку: улица Мира, Орск");
    expect(блок.getAllByRole("button", { name: /Это не адрес/ })).toHaveLength(2);
    await userEvent.click(кнопки[0]);
    await waitFor(() =>
      expect(
        обращения.find((о) => о.method === "POST" && о.url.endsWith("/address-candidates/addr-2/resolve")),
      ).toBeTruthy(),
    );
    expect(
      обращения.find((о) => о.method === "POST" && о.url.endsWith("/address-candidates/addr-2/resolve"))
        ?.body,
    ).toEqual({ decision: "replace" });
    expect(vi.mocked(showToast)).toHaveBeenCalledWith(
      expect.objectContaining({ message: "Адрес записан в карточку" }),
    );
  });
  it("автоматика сама не пишет — у строки со степенью есть «Записать в карточку» (проверка 24.09)", async () => {
    // Выключенная автозапись обещала «подтвердить или не адрес», а кнопка
    // была только «Не адрес»: принять адрес можно было лишь перепечаткой.
    ответ = личность({
      address: null,
      address_source: "none",
      address_geo: null,
      address_evidence: null,
      address_candidates: [
        строка({ geo: { ...ТОЧКА_КАРТЫ, formatted: "улица Мира, 7, Орск" }, writable: true }),
        строка({ id: "addr-3", value: "ул Пушкина, 9", street: "ул Пушкина", house: "9", raw: "Пушкина 9" }),
      ],
    });
    const { container } = renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const блок = await блокАдреса(container);

    const кнопки = await блок.findAllByRole("button", { name: /Записать в карточку/ });
    expect(кнопки).toHaveLength(1);
    expect(кнопки[0].getAttribute("aria-label")).toBe("Записать в карточку: улица Мира, 7, Орск");
    await userEvent.click(кнопки[0]);
    await waitFor(() =>
      expect(
        обращения.find((о) => о.method === "POST" && о.url.endsWith("/address-candidates/addr-2/resolve"))
          ?.body,
      ).toEqual({ decision: "replace" }),
    );
  });
});
