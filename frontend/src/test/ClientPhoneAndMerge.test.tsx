import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto } from "@/shared/api/types";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import type { Permission } from "@/shared/auth/usePermissions";
import {
  errorEnvelope,
  fakeMe,
  fakeUser,
  jsonResponse,
  resetSessionStore,
} from "./helpers";
import {
  CONV_ID,
  makeConversation,
  renderWithProviders,
  seedEmptyThread,
} from "./render";

/**
 * ТЕЛЕФОН РУКАМИ И РУЧНОЕ ОБЪЕДИНЕНИЕ КАРТОЧЕК (правка 9 от 12 августа).
 *
 * ЗАЧЕМ. Автомат вычитывает телефон из текста сообщения, и на бою это даёт 3
 * телефона из 37 обращений: люди диктуют номер голосом, пишут его в
 * объявлении, называют мастеру на пороге. Автоматическая склейка карточек
 * держится на том же телефоне и на `author_id` Авито, про который Авито нигде
 * не обещает, что он общий для разных наших аккаунтов. То есть в подавляющем
 * большинстве случаев решает человек — и до 12 августа ему нечем было решать.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - вернули `<Text>телефон не указан</Text>` вместо кнопки в
 *    `ClientPhoneField` — падает «пустой телефон предлагает вписать номер»;
 *  - в `submit` отбросили `onError` и закрывали поле всегда — падает «отказ
 *    сервера показывается словами и поле не закрывается»;
 *  - в `ClientMergePanel` заменили кнопку «Объединить» на автоматический
 *    `merge.mutate` в `useEffect` — падает «подсказка ничего не склеивает
 *    сама»;
 *  - в `identity_view` подставили `confidence: "confirmed"` всем — падает
 *    «слабая склейка названа предположительной»;
 *  - убрали `aria-expanded` у переключателя заметок — падает тест свёрнутых
 *    заметок в ClientCardPane.test.tsx.
 */

const IDENTITY_EMPTY = {
  id: "client-1",
  name: "Иван Петров",
  phone: null,
  phone_manual: false,
  // Происхождение номера карточка берёт ИЗ `phone_source`, а не выводит из
  // `phone_manual`: тот делит мир надвое, а случаев четыре (руками, из
  // переписки, принёс бот, не знаем). Фикстура повторяет ответ сервера, иначе
  // тест проверял бы поведение, которого в проде нет.
  phone_source: "none",
  phone_candidates: [],
  external_id: "923456789",
  phones: [],
  avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
  merged_from: [],
  merged_into: null,
};

describe("Карточка клиента — телефон руками и объединение", () => {
  let calls: Array<{
    url: string;
    method: string;
    body: Record<string, unknown>;
  }>;
  let identity: unknown;
  let candidates: unknown[];
  /** Что отдаёт сервер на GET /conversations/{id} при рефетче после правки. */
  let detail: ConversationDetailDto;
  /** Ответ на PUT /clients/{id}/phone — тесты его подменяют. */
  let phoneResponse: () => Response;

  beforeEach(() => {
    calls = [];
    identity = IDENTITY_EMPTY;
    candidates = [];
    detail = makeConversation();
    phoneResponse = () =>
      jsonResponse(200, { phone: "+79125550177", changed: true, twins: [] });
    queryClient.clear();
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({
          url,
          method: init?.method ?? "GET",
          body: init?.body ? JSON.parse(String(init.body)) : {},
        });
        if (url.includes("/phone")) return phoneResponse();
        if (url.includes("/merge-candidates"))
          return jsonResponse(200, { items: candidates });
        if (url.includes("/identity")) return jsonResponse(200, identity);
        if (url.includes("/merge"))
          return jsonResponse(200, { moved_conversations: 2 });
        if (url.includes("/unmerge"))
          return jsonResponse(200, { moved_conversations: 2 });
        // Деталь диалога отдаётся НАСТОЯЩАЯ: правки личности сбрасывают её
        // кэш, и заглушка `{items: []}` на рефетче превратила бы карточку в
        // объект без клиента — падало бы не то, что проверяем.
        if (/\/conversations\//.test(url)) return jsonResponse(200, detail);
        return jsonResponse(200, { items: [] });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function render(
    conv: ConversationDetailDto = makeConversation(),
    permissions: Permission[] = fakeMe.permissions as Permission[],
  ) {
    resetSessionStore({
      user: fakeUser,
      permissions,
      accessToken: "t",
      bootstrapped: true,
    });
    detail = conv;
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  // --- телефон ---------------------------------------------------------------

  it("пустой телефон предлагает вписать номер, а не сообщает о его отсутствии", async () => {
    render();

    fireEvent.click(screen.getByRole("button", { name: /указать телефон/ }));
    const input = screen.getByLabelText("Телефон клиента");
    fireEvent.change(input, { target: { value: "8 912 555-01-77" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => {
      expect(
        calls.some((c) => c.method === "PUT" && c.url.includes("/phone")),
      ).toBe(true);
    });
    const put = calls.find((c) => c.url.includes("/phone"));
    // В теле уезжает то, что НАБРАЛ человек. Приведение к `+7…` делает сервер:
    // правило одно на всю систему и обязано совпадать с разбором переписки,
    // иначе один и тот же номер ляжет в базу двумя разными строками и
    // перестанет совпадать при поиске двойников.
    expect(put?.body).toMatchObject({
      phone: "8 912 555-01-77",
      conversation_id: CONV_ID,
    });
  });

  it("отказ сервера показывается его словами, и поле не закрывается", async () => {
    phoneResponse = () =>
      jsonResponse(
        422,
        errorEnvelope(
          "invalid_phone",
          "Не похоже на телефон. Введите российский номер целиком: +7 912 555-01-77.",
        ),
      );
    render();

    fireEvent.click(screen.getByRole("button", { name: /указать телефон/ }));
    fireEvent.change(screen.getByLabelText("Телефон клиента"), {
      target: { value: "не знаю" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    // Текст сервера — как есть (01 §1.3): он говорит, ЧТО ДЕЛАТЬ. «Ошибка
    // сохранения» не говорит ничего, а набранное при этом пропадает.
    expect(
      await screen.findByText(/Введите российский номер целиком/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Телефон клиента")).toHaveValue("не знаю");
  });

  it("подпись под номером не выдаёт введённый руками за вычитанный из переписки", async () => {
    identity = {
      ...IDENTITY_EMPTY,
      phone: "+79125550177",
      phone_manual: true,
      phone_source: "manual",
    };
    render(
      makeConversation({
        client: {
          id: "client-1",
          name: "Иван Петров",
          phone: "+79125550177",
          avito_rating: null,
        },
      }),
    );

    // «(из диалога)» на номере, записанном со слов клиента, ручалось бы за
    // него чужим авторитетом: написанный клиентом номер он написал сам, а
    // услышанный по телефону можно и не расслышать.
    expect(await screen.findByText("(со слов)")).toBeInTheDocument();
    expect(screen.queryByText("(из диалога)")).not.toBeInTheDocument();
  });

  it("пока происхождение номера неизвестно, подписи нет вовсе", () => {
    // Наблюдателю ручка личности недоступна (`conversations:manage`), и
    // «(из диалога)» было бы утверждением, за которое нечем отвечать.
    render(
      makeConversation({
        client: {
          id: "client-1",
          name: "Иван Петров",
          phone: "+79125550177",
          avito_rating: null,
        },
      }),
      ["conversations:read"],
    );

    expect(screen.getByText("+7 912 555-01-77")).toBeInTheDocument();
    expect(screen.queryByText("(из диалога)")).not.toBeInTheDocument();
    expect(screen.queryByText("(со слов)")).not.toBeInTheDocument();
  });

  // --- объединение -----------------------------------------------------------

  it("подсказка ничего не склеивает сама — только предлагает кнопку", async () => {
    candidates = [
      {
        id: "client-2",
        name: "И. Петров",
        external_id: "777042",
        phone: "+79125550177",
        reason: "phone",
        confidence: "confirmed",
      },
    ];
    render();

    expect(
      await screen.findByText(/Возможно, это тот же человек/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Совпало: тот же телефон/)).toBeInTheDocument();
    // ДО нажатия ни одного запроса на объединение быть не должно. Ровно из-за
    // молчаливой склейки 11 августа под одним именем собрались восемь человек
    // из разных городов.
    expect(
      calls.some((c) => c.method === "POST" && /\/merge$/.test(c.url)),
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Объединить" }));
    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.url.includes("/merge"),
      );
      expect(post?.body).toMatchObject({ source_id: "client-2" });
    });
  });

  it("слабая склейка названа предположительной, сильная — подтверждённой", async () => {
    identity = {
      ...IDENTITY_EMPTY,
      merged_from: [
        {
          id: "client-2",
          name: "Оля",
          external_id: "777042",
          phone: null,
          merged_at: "2026-08-12T09:00:00Z",
          confidence: "assumed",
        },
      ],
    };
    render();

    expect(
      await screen.findByText(/Объединена вручную с карточкой «Оля»/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Предположительно/)).toBeInTheDocument();
    expect(screen.queryByText(/Подтверждено/)).not.toBeInTheDocument();
  });

  it("объединённое разъединяется обратно из той же карточки", async () => {
    identity = {
      ...IDENTITY_EMPTY,
      phones: [
        { value: "+79125550177", client_id: "client-1", primary: true },
        { value: "+79165550188", client_id: "client-2", primary: false },
      ],
      avito_ids: [
        { value: "923456789", client_id: "client-1", primary: true },
        { value: "777042", client_id: "client-2", primary: false },
      ],
      merged_from: [
        {
          id: "client-2",
          name: "Оля",
          external_id: "777042",
          phone: "+79165550188",
          merged_at: "2026-08-12T09:00:00Z",
          confidence: "confirmed",
        },
      ],
    };
    render(
      makeConversation({
        client: {
          id: "client-1",
          name: "Иван Петров",
          phone: "+79125550177",
          avito_rating: null,
          external_id: "923456789",
        },
      }),
    );

    // ОБЪЕДИНЕНИЕ НЕ РАЗРУШАЕТ ДАННЫЕ: второй телефон и второй идентификатор
    // остаются видны. У одного человека их законно несколько — по одному на
    // каждый наш аккаунт.
    //
    // ⚠ ПОДПИСЬ СТАЛА ЧИСЛОЗАВИСИМОЙ (правка 09.09): один номер — «Ещё номер»,
    // несколько — «Ещё номера». Блок заодно перестал быть строкой текста и
    // получил действие «Сделать основным» (разбор — в шапке
    // `ClientExtraPhones`), поэтому проверяем не подпись, а САМ НОМЕР: он и
    // есть то, что обязано не пропасть при объединении.
    expect(
      await screen.findByText(/Ещё номер этого человека/),
    ).toBeInTheDocument();
    expect(screen.getByText("+7 916 555-01-88")).toBeInTheDocument();
    expect(screen.getByText(/\+7 916 555-01-88/)).toBeInTheDocument();
    // Идентификаторы объединённых карточек — по кнопке «ещё N», с копированием
    // каждого (аудит 15.09): строка «Ещё идентификаторы: …» тело карточки
    // больше не занимает.
    expect(screen.queryByText(/777042/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "ещё 1" }));
    expect(screen.getByText("ID 777042")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Скопировать идентификатор 777042" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Разъединить" }));
    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.url.includes("/unmerge"),
      );
      expect(post?.body).toMatchObject({ source_id: "client-2" });
    });
  });

  it("подсказка в карточку с группой присоединяет открытую к ней, а не наоборот", async () => {
    // В карточку, в которую уже объединяли, другую влить нельзя: сервер отвечал
    // 422 на каждое нажатие, и кнопка не работала никогда.
    candidates = [
      {
        id: "client-2",
        name: "И. Петров",
        external_id: "777042",
        phone: "+79125550177",
        reason: "phone",
        confidence: "confirmed",
        has_group: true,
        mine_has_group: false,
      },
    ];
    render();

    fireEvent.click(await screen.findByRole("button", { name: "Объединить" }));
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/merge"));
      expect(post?.url).toContain("/clients/client-2/merge");
      expect(post?.body).toMatchObject({ source_id: "client-1" });
    });
  });

  it("когда группы есть у обеих карточек, кнопки нет — есть объяснение", async () => {
    candidates = [
      {
        id: "client-2",
        name: "И. Петров",
        external_id: "777042",
        phone: "+79125550177",
        reason: "phone",
        confidence: "confirmed",
        has_group: true,
        mine_has_group: true,
      },
    ];
    render();

    expect(await screen.findByText(/В обе карточки уже объединяли/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Объединить" })).toBeNull();
  });

  it("наблюдатель читает личность клиента, но решать по ней ему нечем", async () => {
    // Чтение личности открыто по `conversations:read` (наблюдателю нужен адрес
    // выезда), правки остаются за `conversations:manage`. Данные подобраны
    // так, что у менеджера здесь были бы и вопрос по номеру, и «Разъединить».
    identity = {
      ...IDENTITY_EMPTY,
      phone_candidates: [
        {
          id: "cand-1",
          phone: "+79001112255",
          raw: "+7(900)1112255",
          conversation_id: CONV_ID,
          message_id: "msg-1",
          message_at: "2026-08-12T13:35:00Z",
          detected_at: "2026-08-12T13:35:01Z",
          source: "inbound",
          status: "pending",
        },
      ],
      merged_from: [
        {
          id: "client-2",
          name: "Оля",
          external_id: "777042",
          phone: null,
          merged_at: "2026-08-12T09:00:00Z",
          confidence: "assumed",
        },
      ],
    };
    render(makeConversation(), ["conversations:read"]);

    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("/identity"))).toBe(true),
    );
    expect(await screen.findByText(/Оля/)).toBeInTheDocument();
    expect(screen.queryByText(/Распознан телефон/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Разъединить" })).toBeNull();
    expect(screen.queryByRole("button", { name: /указать телефон/ })).toBeNull();
    expect(calls.some((c) => c.url.includes("/merge-candidates"))).toBe(false);
  });
});
