/**
 * ВТОРОЙ НОМЕР КЛИЕНТА — С ДЕЙСТВИЕМ, А НЕ СТРОКОЙ ТЕКСТА.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «когда клиент даёт 2 номер, то исправить
 * можно только основной номер, второй изменить нельзя. Так же нельзя поменять
 * их местами».
 *
 * Здесь стережётся видимая половина правки: у каждого дополнительного номера
 * есть «Сделать основным», нажатие уходит на ручку обмена, а отказ называется
 * словами сервера. Серверная половина — что прежний основной НЕ ПРОПАДАЕТ —
 * заперта в `tests/unit/test_phone_primary_0909.py`.
 *
 * ⚠ ПРОВЕРЯЕТСЯ РЕНДЕРОМ И НАЖАТИЕМ, А НЕ ЧТЕНИЕМ ИСХОДНИКА. В этом проекте
 * дважды случалось «написано, но не подключено», и сторож по тексту файла от
 * этого не защищает.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ClientExtraPhones } from "@/features/chats/components/card/ClientExtraPhones";
import type { PhoneEntry } from "@/features/chats/components/card/clientApi";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { qk } from "@/shared/api/queryKeys";
import { CONV_ID, makeConversation, seedEmptyThread } from "./render";
import { showToast } from "@/shared/ui/toast";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/*
 * Тост читаем ВЫЗОВОМ, а не текстом на экране: он живёт в портале Mantine,
 * который в этой оснастке не смонтирован. Тот же приём, что в
 * `RetryTellsWhy0809` — сторож там про ровно тот же дефект «молчал об отказе».
 */
vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/** Строка дополнительного номера с доказательством (12.09). */
function запись(value: string, client_id = "cl-1"): PhoneEntry {
  return {
    value,
    client_id,
    primary: false,
    candidate_id: `cand-${value}`,
    source: "dialog",
    conversation_id: "conv-1",
    message_id: null,
    message_at: null,
    hint: null,
    decided_by: "auto",
    near_primary: false,
  };
}

const НОМЕРА = [запись("+79995550188"), запись("+79161112233")];

function блок(props: Partial<Parameters<typeof ClientExtraPhones>[0]> = {}) {
  return (
    <ClientExtraPhones
      clientId="cl-1"
      convId="conv-1"
      phones={НОМЕРА}
      editable
      {...props}
    />
  );
}

/**
 * ⚠ САМАЯ ВАЖНАЯ ЧАСТЬ ФАЙЛА: ЖИВАЯ КАРТОЧКА, А НЕ КОМПОНЕНТ В ИЗОЛЯЦИИ.
 *
 * Первая редакция этого набора рендерила `ClientExtraPhones` напрямую — и вся
 * правка могла уехать в бой НЕПОДКЛЮЧЁННОЙ: поставь в `ClientCardPane`
 * `editable={false}` или не передай `convId`, и кнопки на экране не будет ни у
 * кого, а все проверки останутся зелёными. Ровно тот класс дефекта, от
 * которого шапка этого файла обещает защищать («написано, но не подключено»).
 *
 * Поэтому здесь монтируется настоящая карточка со своими запросами, а кнопка
 * ищется так же, как её найдёт человек, — по доступному имени.
 */
describe("Дополнительные номера — в живой карточке клиента", () => {
  const ЛИЧНОСТЬ = {
    id: "client-1",
    name: "Иван Петров",
    phone: "+79125550177",
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    external_id: "923456789",
    phones: [
      { value: "+79125550177", client_id: "client-1", primary: true },
      { value: "+79995550188", client_id: "client-1", primary: false },
    ],
    avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
    merged_from: [],
    merged_into: null,
  };

  let обращения: Array<{ url: string; method: string; body: Record<string, unknown> }>;

  beforeEach(() => {
    обращения = [];
    queryClient.clear();
    seedEmptyThread();
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
        if (url.includes("/phone/primary")) {
          return jsonResponse(200, { phone: "+79995550188", changed: true });
        }
        if (url.includes("/identity")) return jsonResponse(200, ЛИЧНОСТЬ);
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

  it("кнопка «Сделать основным» доезжает до экрана и шлёт запрос обмена", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    const кнопка = await screen.findByRole("button", {
      name: /Сделать \+7 999 555-01-88 основным номером/,
    });
    await userEvent.click(кнопка);

    await waitFor(() =>
      expect(обращения.some((о) => о.url.includes("/phone/primary"))).toBe(true),
    );
    const свой = обращения.find((о) => о.url.includes("/phone/primary"))!;
    expect(свой.method).toBe("POST");
    expect(свой.body).toMatchObject({ phone: "+79995550188" });
  });
});

/**
 * ⚠ УСТАРЕВШИЙ СНИМОК СТРОКИ СПИСКА НЕ ИМЕЕТ ПРАВА РЕШАТЬ, ЕСТЬ ЛИ ТЕЛЕФОН.
 *
 * ЖАЛОБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «не привязываются сразу номера, нужно
 * нажимать или ждать». Замер боя по присланному снимку: у карточки в базе
 * телефон ЕСТЬ, а на экране стояло «указать телефон» — и тот же номер
 * показывался ниже «ещё одним».
 *
 * Причина: у телефона карточки было ДВА источника. Основной брался из
 * `head.client`, то есть из детали диалога, а при её отсутствии — вовсе из
 * СТРОКИ СПИСКА в кэше выдачи, которая обновляется реже всего. Список
 * дополнительных при этом брался из личности и отбирался сравнением
 * `p.value !== client.phone` — то есть сверялся с устаревшим снимком и
 * ошибался ровно тогда, когда ошибался он.
 *
 * Здесь оснастка воспроизводит это буквально: строка списка говорит «телефона
 * нет», личность говорит «телефон такой-то».
 */
describe("Карточка верит личности, а не устаревшей строке списка", () => {
  const НОМЕР = "+79125550177";
  const ЛИЧНОСТЬ_С_ТЕЛЕФОНОМ = {
    id: "client-1",
    name: "Иван Петров",
    phone: НОМЕР,
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    external_id: "923456789",
    phones: [
      { value: НОМЕР, client_id: "client-1", primary: true },
      { value: "+79995550188", client_id: "client-1", primary: false },
    ],
    avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
    merged_from: [],
    merged_into: null,
  };

  beforeEach(() => {
    queryClient.clear();
    seedEmptyThread();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    // ⚠ СТРОКА СПИСКА УСТАРЕЛА: телефона в ней нет. Деталь диалога намеренно
    // НЕ кладём в кэш — так карточка и падает на строку списка, ровно как в бою.
    const conv = makeConversation({
      client: {
        id: "client-1",
        name: "Иван Петров",
        phone: null,
        avito_rating: null,
        external_id: "923456789",
      },
    });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/identity")) return jsonResponse(200, ЛИЧНОСТЬ_С_ТЕЛЕФОНОМ);
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

  it("телефон из личности показан, а «указать телефон» не предлагается", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    expect(await screen.findByText("+7 912 555-01-77")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /указать телефон/ }),
      "карточка предлагает вписать номер, который у неё уже есть",
    ).toBeNull();
  });

  it("основной номер не попадает в «ещё номера»", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);

    // Дождались личности: второй номер на месте.
    expect(await screen.findByText("+7 999 555-01-88")).toBeTruthy();
    // ⚠ И ОСНОВНОГО СРЕДИ НИХ НЕТ. Предложение «Сделать основным» на номере,
    // который уже основной, — это то, что владелец увидел на снимке.
    expect(
      screen.queryByRole("button", { name: /Сделать \+7 912 555-01-77 основным/ }),
      "основной номер предложен «сделать основным»",
    ).toBeNull();
  });
});

describe("Дополнительные номера клиента", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.mocked(showToast).mockClear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("у каждого номера есть «Сделать основным»", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithProviders(блок());

    const кнопки = screen.getAllByRole("button", { name: /Сделать .* основным номером/ });
    expect(кнопки, "действия нет ни у одного номера").toHaveLength(НОМЕРА.length);
  });

  it("нажатие уходит на ручку обмена, а не на правку телефона", async () => {
    /*
     * ⚠ АДРЕС РУЧКИ ПРОВЕРЯЕТСЯ ИМЕННО ЗДЕСЬ. Соседняя `PUT /phone` делает
     * почти то же и ЗАТИРАЕТ прежний номер — то есть промах в адресе выглядел
     * бы как работающая кнопка, которая молча теряет второй номер человека.
     */
    const запросы: Array<{ url: string; method: string; body: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init: RequestInit) => {
        запросы.push({
          url: String(url),
          method: String(init?.method ?? "GET"),
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
        return Promise.resolve(jsonResponse(200, { phone: НОМЕРА[0].value, changed: true }));
      }),
    );
    renderWithProviders(блок());

    await userEvent.click(
      screen.getAllByRole("button", { name: /Сделать .* основным номером/ })[0],
    );

    await waitFor(() => expect(запросы.length).toBeGreaterThan(0));
    const свой = запросы.find((з) => з.url.includes("/phone/primary"));
    expect(свой, "запрос ушёл не на ручку обмена").toBeTruthy();
    expect(свой!.method).toBe("POST");
    expect(свой!.body).toMatchObject({ phone: НОМЕРА[0].value, conversation_id: "conv-1" });
  });

  it("отказ сервера называется его же словами", async () => {
    /*
     * Дефект «повтор молчал об отказе» в этом проекте уже был: человек жмёт
     * снова и снова, потому что причины ему не сказали.
     */
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            422,
            errorEnvelope("unknown_phone", "Этот номер за клиентом не числится."),
          ),
        ),
      ),
    );
    renderWithProviders(блок());

    await userEvent.click(
      screen.getAllByRole("button", { name: /Сделать .* основным номером/ })[0],
    );

    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const аргумент = vi.mocked(showToast).mock.calls[0][0];
    expect(аргумент.message).toMatch(/за клиентом не числится/);
    expect(аргумент.color).toBe("red");
  });

  it("без диалога кнопки нет, но номера видны", () => {
    /*
     * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА С ЖИВЫМ ОСТАТКОМ. Прежний основной удерживается
     * строкой кандидата, а у той обязателен диалог — без него сервер ответит
     * 422, и кнопка заведомо отвечала бы отказом. Но САМИ НОМЕРА обязаны
     * остаться на экране: иначе проверка зеленела бы и от того, что блока нет
     * вовсе.
     */
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithProviders(блок({ convId: null }));

    expect(screen.queryAllByRole("button", { name: /основным номером/ })).toHaveLength(0);
    expect(screen.getByText("+7 999 555-01-88")).toBeTruthy();
  });

  it("у номера присоединённой карточки действия нет, но он виден", () => {
    /*
     * ⚠ ГРАНИЦА КАРТОЧЕК. Сделать основным номер, доказанный ПРИСОЕДИНЁННОЙ
     * карточкой, значило бы перенести его через границу, а «Разъединить» его
     * назад не забирает — у победителя остался бы телефон чужого человека.
     *
     * Проверка отрицательная, поэтому рядом стоит СВОЙ номер с живой кнопкой:
     * иначе она зеленела бы и от того, что действий нет ни у кого.
     */
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithProviders(
      блок({
        phones: [запись("+79995550188"), запись("+79161112233", "cl-other")],
      }),
    );

    expect(screen.getAllByRole("button", { name: /основным номером/ })).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: /Сделать \+7 999 555-01-88 основным номером/ }),
    ).toBeTruthy();
    expect(screen.getByText("+7 916 111-22-33")).toBeTruthy();
  });

  it("наблюдателю действий не даём, номера показываем", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithProviders(блок({ editable: false }));

    expect(screen.queryAllByRole("button", { name: /основным номером/ })).toHaveLength(0);
    expect(screen.getByText("+7 999 555-01-88")).toBeTruthy();
  });

  it("номер из голосового подписан «из голосового» — сам или подтверждён", () => {
    /*
     * Ревью 19.09, C7. Ветка `source === "voice"` в подписи заведена, чтобы
     * номер из расшифровки не подписывался «присоединённая карточка»
     * (ветка-умолчание): оператор обязан знать, что сверять надо со звуком,
     * а не с написанным. Без сторожа она ломалась бы молча при первой правке
     * `подпись()`. Рядом — номер из переписки, чтобы подпись голосового
     * отличалась от соседней, а не совпадала с ней по случайности.
     */
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithProviders(
      блок({
        phones: [
          { ...запись("+79001112244"), source: "voice", decided_by: "auto" },
          { ...запись("+79001112255"), source: "voice", decided_by: "operator" },
          запись("+79001112266"),
        ],
      }),
    );

    // Подпись начинается с происхождения, дальше через « · » идут детали
    // («в этом диалоге»); `\b` в JS кириллицы не знает — граница словом « · ».
    expect(screen.getByText(/^из голосового, сам · /)).toBeTruthy();
    expect(screen.getByText(/^из голосового, подтверждён · /)).toBeTruthy();
    expect(screen.getByText(/^из переписки, сам · /)).toBeTruthy();
    expect(screen.queryByText(/присоединённая карточка/)).toBeNull();
  });
});
