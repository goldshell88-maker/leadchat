import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import type {
  ClientHistoryResponse,
  ConversationDetailDto,
} from "@/shared/api/types";
import { qk } from "@/shared/api/queryKeys";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import {
  CONV_ID,
  makeConversation,
  renderWithProviders,
  seedEmptyThread,
} from "./render";

/*
 * ОДИН ЧЕЛОВЕК НА РАЗНЫХ АККАУНТАХ АВИТО — ЧТО ВИДИТ ДИСПЕТЧЕР
 * (требование владельца 6 от 11 августа).
 *
 * Владелец сказал «не связываются клиенты, если один и тот же пишет на разные
 * аккаунты». По коду — обратное: связываются с первого дня. Карточка клиента
 * ключуется парой «канал + author_id» БЕЗ нашего аккаунта, поэтому одно и то же
 * число с двух наших аккаунтов попадает в одну строку `clients`. Не работала не
 * склейка, а её видимость: история клиента считалась сервером и не выводилась
 * ни одним компонентом (дефект аудита SCEN-14), канал прошлого обращения не
 * показывался нигде.
 *
 * Поэтому здесь проверяется не «склеилось ли», а ЧЕСТНОСТЬ РАССКАЗА О СКЛЕЙКЕ.
 * Авито нигде не обещает, что `author_id` общий для разных аккаунтов (в
 * спецификации у `Chat.users[].id` ровно «Обратите внимание на хэширование»).
 * Если он свой у каждого аккаунта, за одной карточкой стоят ДВА разных
 * человека — с чужой перепиской и чужим телефоном. Значит `assumed` не имеет
 * права выглядеть как факт, а несовпавшие телефоны обязаны быть громче самой
 * склейки.
 *
 * Отдельный файл, а не дописка в `ClientCardPane.test.tsx`: там общий
 * `HISTORY`-ответ на все проверки, а здесь ответ сервера — сам предмет
 * проверки и меняется в каждом тесте.
 */

const ACC_CURRENT = { id: "acc-1", title: "Дамир" }; // канал открытого диалога
const ACC_OTHER = { id: "acc-2", title: "Тимофей" }; // канал, с которого клиент пришёл

/** Ответ `GET /conversations/{id}/client-history`: одно прошлое обращение. */
function makeHistory(
  overrides: Partial<ClientHistoryResponse> = {},
): ClientHistoryResponse {
  return {
    client: { id: "client-1", name: "Иван Петров" },
    items: [
      {
        id: "conv-old",
        status: "closed",
        item: { title: "Ремонт MacBook Air" },
        account: ACC_OTHER,
        // Ответственный приезжает общей ссылкой на человека (04.09): тем же
        // `user_ref`, что и в шапке ленты, вместе с отделом.
        assignee: { id: "u-old", full_name: "Пётр Ковалёв", department: "ОКК" },
        last_message_at: "2026-05-11T14:00:00Z",
        messages_count: 18,
      },
    ],
    summary: {
      // Порядок — по обращениям клиента: пришёл на «Тимофея», потом написал
      // «Дамиру». Текущий канал отсюда не выкидывается сервером, его убирает
      // сама карточка — иначе строку «Писал ещё на…» пришлось бы читать вместе
      // с шапкой ленты, чтобы понять, какой из каналов открыт сейчас.
      channels: [ACC_OTHER, ACC_CURRENT],
      link_confidence: "assumed",
      link_phone_conflict: false,
      cross_account_since: "2026-08-11T10:40:00Z",
    },
    ...overrides,
  };
}

/** Деталь диалога с межканальными признаками в карточке клиента. */
function makeDetail(
  client: Partial<ConversationDetailDto["client"]> = {},
): ConversationDetailDto {
  const base = makeConversation();
  return makeConversation({
    account: ACC_CURRENT,
    client: { ...base.client, phone: "+79261234567", ...client },
    client_conversations_count: 2,
  });
}

describe("Один клиент на разных аккаунтах Авито (требование владельца 6)", () => {
  let history: ClientHistoryResponse;
  let detail: ConversationDetailDto;

  beforeEach(() => {
    history = makeHistory();
    detail = makeDetail({
      link_confidence: "assumed",
      link_phone_conflict: false,
    });
    queryClient.clear();
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("client-history")) return jsonResponse(200, history);
        if (url.includes("/users/assignable"))
          return jsonResponse(200, { items: [] });
        return jsonResponse(200, detail);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function renderCard() {
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as Permission[],
      accessToken: "t",
      bootstrapped: true,
    });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), detail);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  it("два аккаунта — одна карточка, и оба канала видно в истории", async () => {
    renderCard();

    // Число в заголовке равно числу строк списка (аудит 15.09): список
    // содержит только ПРОШЛЫЕ диалоги — их и считаем, «включая это» не нужно.
    expect(await screen.findByText("Другие обращения · 1")).toBeInTheDocument();
    // Чужой канал назван словами, без текущего: «ещё» отвечает на вопрос
    // «кроме какого» само.
    expect(
      await screen.findByText("Писал ещё на канал: Тимофей"),
    ).toBeInTheDocument();
    // Канал у САМОГО обращения — иначе список из двух каналов читается как
    // история одного.
    const row = (await screen.findByText("Ремонт MacBook Air")).closest(
      "button",
    );
    expect(row?.textContent).toContain("Тимофей");
    // Чужой канал выделен, и выделен по идентификатору, а не по названию.
    expect(row?.querySelector("strong")?.textContent).toBe("Тимофей");
  });

  it("склейка по одному author_id подаётся предположением, а не фактом", async () => {
    renderCard();

    expect(
      await screen.findByText(
        "Возможно, этот же человек писал и на другой наш канал",
      ),
    ).toBeInTheDocument();
    // Формулировка факта не должна встретиться нигде: ровно она и была бы
    // «выдать предположение за факт».
    expect(
      screen.queryByText("Этот же человек писал и на другой наш канал"),
    ).toBeNull();
    expect(
      screen.getByText(/Совпал только идентификатор Авито/),
    ).toBeInTheDocument();
  });

  it("совпавший телефон с другого канала — это уже факт, и сказано так", async () => {
    detail = makeDetail({
      link_confidence: "confirmed",
      link_phone_conflict: false,
    });
    history = makeHistory({
      summary: { ...makeHistory().summary!, link_confidence: "confirmed" },
    });
    renderCard();

    expect(
      await screen.findByText("Этот же человек писал и на другой наш канал"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Подтверждено: с обоих каналов пришёл один и тот же телефон/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Совпал только идентификатор Авито/)).toBeNull();
  });

  it("разные телефоны при одном author_id — предупреждение перед звонком", async () => {
    // Тот самый случай, ради которого всё затевалось: если `author_id` не
    // сквозной, в одну карточку попали два разных человека, и номер в ней —
    // чужой. Единственный сигнал об этом, который у нас есть, обязан звучать.
    detail = makeDetail({
      link_confidence: "assumed",
      link_phone_conflict: true,
    });
    renderCard();

    expect(
      await screen.findByText(
        "Телефоны с разных каналов не совпали — проверьте, тот ли это человек, прежде чем звонить.",
      ),
    ).toBeInTheDocument();
    // И склейка при этом ОСТАЁТСЯ предположением.
    expect(
      screen.getByText("Возможно, этот же человек писал и на другой наш канал"),
    ).toBeInTheDocument();
  });

  it("клиент писал только на один канал — ни строки о склейке, ни предупреждения", async () => {
    // «Каналы: Дамир» у каждого второго клиента приучает пролистывать это
    // место, и тогда строка не сработает там, где она нужна.
    detail = makeDetail({ link_confidence: null, link_phone_conflict: false });
    history = makeHistory({
      items: [{ ...makeHistory().items[0], account: ACC_CURRENT }],
      summary: {
        channels: [ACC_CURRENT],
        link_confidence: null,
        link_phone_conflict: false,
        cross_account_since: null,
      },
    });
    renderCard();

    const row = (await screen.findByText("Ремонт MacBook Air")).closest(
      "button",
    );
    // Канал показан всё равно: девять аккаунтов, и «на какой из них он писал»
    // — вопрос и без всякой склейки.
    expect(row?.textContent).toContain("Дамир");
    // Но своим он не помечен, и о склейке не сказано ни слова.
    expect(row?.querySelector("strong")).toBeNull();
    expect(screen.queryByText(/Писал ещё на канал/)).toBeNull();
    expect(
      screen.queryByText(/этот же человек писал и на другой наш канал/i),
    ).toBeNull();
  });

  it("ответ сервера без сводки (выкатка ещё не доехала) не превращается в ложь", async () => {
    // Между выкатками фронт новый, а бэкенд старый: сводки нет, у канала
    // прошлого обращения нет идентификатора. Молчать в этом случае обязательно
    // — иначе карточка пометит чужим каналом КАЖДУЮ строку и соврёт ровно в тот
    // день, когда предупреждение впервые увидят.
    history = {
      client: { id: "client-1", name: "Иван Петров" },
      items: [
        {
          id: "conv-old",
          status: "closed",
          item: { title: "Ремонт MacBook Air" },
          account: {
            title: "Тимофей",
          } as ClientHistoryResponse["items"][0]["account"],
          assignee: null,
          last_message_at: "2026-05-11T14:00:00Z",
          messages_count: 18,
        },
      ],
    };
    detail = makeDetail({ link_confidence: null, link_phone_conflict: false });
    renderCard();

    const row = (await screen.findByText("Ремонт MacBook Air")).closest(
      "button",
    );
    expect(row?.textContent).toContain("Тимофей");
    expect(row?.querySelector("strong")).toBeNull();
    expect(screen.queryByText(/Писал ещё на канал/)).toBeNull();
  });
});
