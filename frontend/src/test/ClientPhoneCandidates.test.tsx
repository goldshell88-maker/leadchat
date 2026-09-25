import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, MessageDto, MessagesPage } from "@/shared/api/types";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import type { Permission } from "@/shared/auth/usePermissions";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * РАСПОЗНАННЫЙ В ПЕРЕПИСКЕ ТЕЛЕФОН — ВОПРОСОМ, А НЕ ЗАПИСЬЮ В КАРТОЧКУ
 * (требование владельца, пункт 4 от 12 августа).
 *
 * ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ ПО СУЩЕСТВУ. Номер из карточки набирают и диктуют
 * мастеру вслух, а рядом с настоящим номером в переписке лежат код домофона и
 * номер заказа. Поэтому проверяется не «блок отрисовался», а три вещи, ценой
 * которых является звонок не тому человеку: оператор ВИДИТ фразу целиком до
 * решения; ни одна кнопка не срабатывает сама; двойник по распознанному номеру
 * не склеивает карточки, а только предлагает.
 *
 * ЧТО ЛОМАЛИ РУКАМИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - убрали `<ClientPhoneCandidates>` из `ClientCardPane` — это состояние ДО
 *    правки, когда сервер кандидатов шлёт, а экран о них не знает: падают
 *    шесть тестов из десяти;
 *  - вернули прежний вывод подписи `phone_manual ? "manual" : "dialog"` —
 *    падает «подпись различает четыре происхождения» (нет «(источник
 *    неизвестен)»);
 *  - в `CandidateMessage` отключили поиск сообщения в ленте — падают «фраза
 *    показана целиком» и «за лентой чужого диалога не ходим»;
 *  - там же отключили ветку `messages.isError` — падает «не сумев достать
 *    сообщение, говорит об этом»;
 *  - и переставили ту же ветку ПЕРЕД показом найденной фразы (так было до
 *    проверки на стенде) — падает «упавшее фоновое обновление не прячет уже
 *    полученную фразу»;
 *  - в `CandidateRow` сняли условие `sameDialog` и стали монтировать
 *    `CandidateMessage` всегда — падает «за лентой чужого диалога не ходим,
 *    пока не попросили» (ушёл запрос, которого быть не должно);
 *  - в `decide` подставили `decision: "replace"` всем трём кнопкам — падает
 *    «каждая кнопка уходит своим решением»;
 *  - после решения дописали `merge.mutate(result.twins[0].id)` — падает
 *    «двойник предлагается, а не склеивается»;
 *  - заменили `onSettled` на `onSuccess` в `useResolvePhoneCandidate` и
 *    отдельно убрали ветку 409 — оба раза падает «решение, принятое коллегой
 *    раньше, не выглядит поломкой» (сначала «1 не больше 1» — карточку не
 *    перезапросили, потом — не показан спокойный тост);
 *  - в `REASON_TEXT` убрали `phone_candidate` — падает «подсказка объединения
 *    называет причину словами» (было «Совпало: .»);
 *  - (19.09, номер из голосового) в `CandidateMessage` вернули `found.body`
 *    вместо `body || voice_transcript` — падает «предложение из голосового
 *    цитирует расшифровку»; убрали строку `· из голосового (расшифровка)` —
 *    падает она же на подписи; в `ClientPhoneField.SOURCE_LABEL` подменили
 *    `voice` на «(из диалога)» — падает «основной номер из голосового подписан
 *    «(из голосового)»».
 */

// `vi.hoisted`, потому что `vi.mock` поднимается в начало файла и обычная
// переменная к моменту подмены модуля ещё не создана.
const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
  errorPersistent: vi.fn(),
  hide: vi.fn(),
}));
vi.mock("@/shared/ui/toast", () => ({
  showToast: vi.fn(),
  showUndoToast: vi.fn(),
  toast: toastSpies,
}));

const CLIENT_ID = "client-1";
const OTHER_CONV = "conv-77";
const MSG_ID = "msg-9";
/** Ровно тот боевой случай, ради которого распознавание и написано. */
const PHRASE =
  "В любое время в течении дня. Прошу сообщить о времени прихода за 1 час в СМС по номеру : +7(900)1112255.";

function candidate(overrides: Record<string, unknown> = {}) {
  return {
    id: "cand-1",
    phone: "+79001112255",
    raw: "+7(900)1112255",
    conversation_id: CONV_ID,
    message_id: MSG_ID,
    message_at: "2026-08-12T13:35:00Z",
    detected_at: "2026-08-12T13:35:01Z",
    source: "inbound",
    status: "pending",
    ...overrides,
  };
}

/** Голосовое, из расшифровки которого вычитан номер (все номера вымышленные). */
const VOICE_MSG_ID = "msg-voice-1";
const VOICE_PHRASE = "Здравствуйте, вот мой номер 900-111-22-44. Если хотите, позвоните.";

function voiceMessage(): MessageDto {
  return message({
    id: VOICE_MSG_ID,
    body: null,
    attachments: [
      {
        media_id: "avito_voice_2229d5a7",
        kind: "file",
        name: "Голосовое сообщение",
        size: null,
        avito_type: "voice",
      },
    ],
    voice_transcript: VOICE_PHRASE,
    voice_transcript_status: "done",
  });
}

function identityWith(overrides: Record<string, unknown> = {}) {
  return {
    id: CLIENT_ID,
    name: "Анна Сергеевна",
    phone: null,
    phone_manual: false,
    phone_source: "none",
    phone_candidates: [],
    external_id: "923456789",
    phones: [],
    avito_ids: [{ value: "923456789", client_id: CLIENT_ID, primary: true }],
    merged_from: [],
    merged_into: null,
    ...overrides,
  };
}

function message(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: MSG_ID,
    conversation_id: CONV_ID,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: PHRASE,
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-08-12T13:35:00Z",
    ...overrides,
  };
}

/** Лента в кэше — ровно та, что лежала бы там от открытого диалога. */
function seedThread(convId: string, items: MessageDto[]): void {
  queryClient.setQueryData(qk.messages.list(convId), {
    pages: [
      {
        items,
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

describe("Карточка клиента — распознанный телефон", () => {
  let calls: Array<{ url: string; method: string; body: Record<string, unknown> }>;
  let identity: unknown;
  let mergeCandidates: unknown[];
  let detail: ConversationDetailDto;
  /** Ответ на POST …/resolve — тесты его подменяют. */
  let resolveResponse: () => Response;
  /** Ответ ленты чужого диалога — тесты его подменяют. */
  let messagesResponse: () => Response;

  beforeEach(() => {
    calls = [];
    identity = identityWith();
    mergeCandidates = [];
    detail = makeConversation();
    resolveResponse = () =>
      jsonResponse(200, {
        phone: "+79001112255",
        candidate: candidate({ status: "accepted" }),
        twins: [],
      });
    messagesResponse = () =>
      jsonResponse(200, {
        items: [message({ conversation_id: OTHER_CONV })],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      });
    queryClient.clear();
    Object.values(toastSpies).forEach((s) => s.mockClear());
    seedThread(CONV_ID, [message()]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({
          url,
          method: init?.method ?? "GET",
          body: init?.body ? JSON.parse(String(init.body)) : {},
        });
        // Порядок проверок значим: «/phone-candidates/…/resolve» содержит и
        // «/phone», и «/candidates».
        if (url.includes("/phone-candidates/")) return resolveResponse();
        if (url.includes("/merge-candidates")) return jsonResponse(200, { items: mergeCandidates });
        if (url.includes("/identity")) return jsonResponse(200, identity);
        if (url.includes("/merge")) return jsonResponse(200, { moved_conversations: 1 });
        // Лента чужого диалога — то самое сообщение с номером в ней.
        if (url.includes("/messages")) return messagesResponse();
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
    resetSessionStore({ user: fakeUser, permissions, accessToken: "t", bootstrapped: true });
    detail = conv;
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  const resolveCalls = () => calls.filter((c) => c.url.includes("/phone-candidates/"));

  it("пустой список кандидатов не рисует блока вовсе", async () => {
    render();

    // Ждём ответа личности, иначе «блока нет» означало бы «ещё не пришёл».
    await waitFor(() => expect(calls.some((c) => c.url.includes("/identity"))).toBe(true));
    expect(screen.queryByText(/Распознан телефон/)).not.toBeInTheDocument();
  });

  it("блок называет номер, исходную запись и время сообщения", async () => {
    identity = identityWith({ phone_candidates: [candidate()] });
    render();

    expect(await screen.findByText("Распознан телефон в переписке")).toBeInTheDocument();
    // Номер — по группам, как везде в карточке: его сверяют глазами.
    expect(screen.getByText("+7 900 111-22-55")).toBeInTheDocument();
    // Исходная запись обязательна: без неё нашу же догадку нечем проверить.
    expect(screen.getByText(/В сообщении: «\+7\(900\)1112255»/)).toBeInTheDocument();
  });

  it("фраза из сообщения показана целиком, до решения", async () => {
    identity = identityWith({ phone_candidates: [candidate()] });
    render();

    // ГЛАВНОЕ В БЛОКЕ. «Распознан телефон +7 900 111-22-55» без фразы — это
    // предложение, которое нечем проверить: рядом с настоящим номером в
    // переписке лежат код домофона и номер заказа.
    expect(await screen.findByText(PHRASE)).toBeInTheDocument();
  });

  it("за лентой чужого диалога не ходим, пока не попросили", async () => {
    identity = identityWith({
      phone_candidates: [candidate({ conversation_id: OTHER_CONV, source: "rescan" })],
    });
    render();

    await screen.findByText("Распознан телефон в переписке");
    const otherThread = () => calls.some((c) => c.url.includes(`/conversations/${OTHER_CONV}/messages`));
    // Пятьдесят сообщений чужого диалога ради предложения, в которое оператор
    // может и не посмотреть, — плата ни за что.
    expect(otherThread()).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /Показать сообщение/ }));
    await waitFor(() => expect(otherThread()).toBe(true));
    expect(await screen.findByText(PHRASE)).toBeInTheDocument();
  });

  it("упавшее фоновое обновление ленты не прячет уже полученную фразу", async () => {
    identity = identityWith({ phone_candidates: [candidate()] });
    messagesResponse = () => jsonResponse(500, errorEnvelope("internal_error", "Сервер прилёг"));
    // Повторы для ЭТОГО ключа выключены: под проверкой порядок веток, а не
    // политика переспрашивания (её проверяет соседний тест, и там ожидание
    // длинное именно поэтому).
    queryClient.setQueryDefaults(qk.messages.list(CONV_ID), { retry: false });
    render();

    await screen.findByText(PHRASE);
    // Так лента и обновляется в жизни: пришло новое сообщение — кэш помечен
    // устаревшим, запрос ушёл заново. Здесь он падает.
    await queryClient.invalidateQueries({ queryKey: qk.messages.list(CONV_ID) });
    // Лента в кэше есть, фоновое обновление провалилось. Показать вместо
    // готовой фразы «не получилось» значило бы отнять её ровно в тот момент,
    // когда оператор решает, чей это телефон.
    await waitFor(() =>
      expect(queryClient.getQueryState(qk.messages.list(CONV_ID))?.status).toBe("error"),
    );
    expect(screen.getByText(PHRASE)).toBeInTheDocument();
    expect(screen.queryByText(/Не получилось загрузить сообщение/)).not.toBeInTheDocument();
  });

  it("не сумев достать сообщение, говорит об этом, а не гадает", async () => {
    identity = identityWith({
      phone_candidates: [candidate({ conversation_id: OTHER_CONV })],
    });
    messagesResponse = () => jsonResponse(500, errorEnvelope("internal_error", "Сервер прилёг"));
    render();

    await screen.findByText("Распознан телефон в переписке");
    fireEvent.click(screen.getByRole("button", { name: /Показать сообщение/ }));

    // «Сообщение в другом диалоге» после неудачной загрузки было бы догадкой,
    // выданной за знание: ленту мы не видели вовсе.
    //
    // Ожидание длинное намеренно: общий queryClient переспрашивает дважды
    // (03 §2.4), то есть отказ доезжает до экрана через три секунды. Сокращать
    // повторы ради скорости теста значило бы проверять политику, которой в
    // приложении нет.
    expect(
      await screen.findByText(/Не получилось загрузить сообщение/, {}, { timeout: 6000 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  }, 10_000);

  it("каждая кнопка уходит на сервер своим решением", async () => {
    for (const [label, decision] of [
      [/Заменить телефон карточки/, "replace"],
      [/Добавить .* вторым номером/, "add"],
      [/Отклонить/, "reject"],
    ] as const) {
      calls = [];
      identity = identityWith({ phone_candidates: [candidate()] });
      const view = render();

      await screen.findByText("Распознан телефон в переписке");
      // ДО нажатия ни одного запроса: молчаливая запись чужого номера в
      // карточку — это звонок постороннему человеку.
      expect(resolveCalls()).toHaveLength(0);

      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() => expect(resolveCalls()).toHaveLength(1));
      const post = resolveCalls()[0];
      expect(post.method).toBe("POST");
      expect(post.url).toContain("/clients/client-1/phone-candidates/cand-1/resolve");
      expect(post.body).toEqual({ decision });
      view.unmount();
    }
  });

  it("двойник по распознанному номеру предлагается, а не склеивается", async () => {
    identity = identityWith({ phone_candidates: [candidate()] });
    resolveResponse = () =>
      jsonResponse(200, {
        phone: "+79001112255",
        candidate: candidate({ status: "accepted" }),
        twins: [{ id: "client-2", name: "Оля", external_id: "777042", phone: "+79001112255" }],
      });
    render();

    await screen.findByText("Распознан телефон в переписке");
    fireEvent.click(screen.getByRole("button", { name: /Добавить .* вторым номером/ }));

    await waitFor(() => expect(resolveCalls()).toHaveLength(1));
    // ПРЯМОЕ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА: по распознанному номеру автообъединения нет.
    // 11 августа молчаливая склейка собрала под одним именем восемь человек.
    expect(calls.some((c) => c.method === "POST" && /\/merge$/.test(c.url))).toBe(false);
    // Двойник назван словами — иначе оператор не поймёт, откуда взялось
    // предложение в блоке ниже.
    await waitFor(() => expect(toastSpies.success).toHaveBeenCalled());
    expect(String(toastSpies.success.mock.calls[0][1])).toMatch(/Оля/);
  });

  it("решение, принятое коллегой раньше, не выглядит поломкой", async () => {
    identity = identityWith({ phone_candidates: [candidate()] });
    resolveResponse = () =>
      jsonResponse(409, errorEnvelope("already_resolved", "По этому номеру решение уже принято"));
    render();

    await screen.findByText("Распознан телефон в переписке");
    fireEvent.click(screen.getByRole("button", { name: /Отклонить/ }));

    // 409 здесь — обычный ход дел (тот же вопрос висел у коллеги), и красный
    // тост про ошибку научил бы бояться нормального.
    await waitFor(() => expect(toastSpies.info).toHaveBeenCalled());
    expect(toastSpies.error).not.toHaveBeenCalled();
    // Карточку перезапрашиваем: правда о ней сейчас только на сервере.
    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes("/identity")).length).toBeGreaterThan(1),
    );
  });

  it("подпись у номера различает четыре происхождения", async () => {
    identity = identityWith({ phone: "+79001112255", phone_source: "other" });
    render(
      makeConversation({
        client: { id: CLIENT_ID, name: "Анна", phone: "+79001112255", avito_rating: null },
      }),
    );

    // «other» — номер принёс бот или он лежит в базе с тех пор, когда
    // происхождение не записывали. «(из диалога)» здесь ручалось бы за него
    // чужим авторитетом, а промолчать значило бы выдать его за проверенный.
    expect(await screen.findByText("(источник неизвестен)")).toBeInTheDocument();
    expect(screen.queryByText("(из диалога)")).not.toBeInTheDocument();
  });

  it("подсказка объединения называет причину «распознан в переписке» словами", async () => {
    mergeCandidates = [
      {
        id: "client-2",
        name: "Оля",
        external_id: "777042",
        phone: "+79001112255",
        reason: "phone_candidate",
        confidence: "assumed",
      },
    ];
    render();

    // Пока `phone_candidate` не было в типе, словарь причин отдавал undefined,
    // и подсказка печатала «Совпало: .» — то есть предлагала склеить карточки,
    // не сказав почему.
    expect(await screen.findByText(/тот же номер, распознанный в переписке/)).toBeInTheDocument();
    expect(screen.queryByText(/Совпало: \./)).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------- 19.09: голос

  it("предложение из голосового подписано и цитирует расшифровку, а не пустое тело", async () => {
    seedThread(CONV_ID, [message(), voiceMessage()]);
    identity = identityWith({
      phone_candidates: [
        candidate({
          id: "cand-voice",
          phone: "+79001112244",
          raw: "900-111-22-44",
          message_id: VOICE_MSG_ID,
          source: "voice",
        }),
      ],
    });
    render();

    await screen.findByText("Распознан телефон в переписке");
    // Доказательство машинное: ослышка Whisper в одной цифре даёт
    // правдоподобный номер, и оператор обязан знать, что сверять надо со
    // звуком, а не с написанным.
    expect(screen.getByText(/из голосового \(расшифровка\)/)).toBeInTheDocument();
    // У голосового тело пусто; без цитаты из расшифровки предложение нечем
    // проверить — ровно та слепота, от которой блок и заводился.
    expect(await screen.findByText(VOICE_PHRASE)).toBeInTheDocument();
    expect(screen.queryByText(/Сообщение выше по переписке/)).not.toBeInTheDocument();
  });

  it("основной номер из голосового подписан «(из голосового)», а не «(из диалога)»", async () => {
    identity = identityWith({ phone: "+79001112244", phone_source: "voice" });
    render(
      makeConversation({
        client: { id: CLIENT_ID, name: "Анна", phone: "+79001112244", avito_rating: null },
      }),
    );

    // Подпись стоит там, где риск: при включённой автозаписи блок предложений
    // пуст, а автозаполненный из ослышки основной под «(из диалога)» выглядел
    // бы написанным рукой клиента.
    expect(await screen.findByText("(из голосового)")).toBeInTheDocument();
    expect(screen.queryByText("(из диалога)")).not.toBeInTheDocument();
  });
});
