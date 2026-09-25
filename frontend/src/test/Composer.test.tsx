import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { qk } from "@/shared/api/queryKeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import type { MessageDto } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import {
  CONV_ID,
  makeConversation,
  renderWithProviders,
  seedEmptyThread,
  threadMessages,
  wrap,
} from "./render";

const SERVER_MESSAGE: MessageDto = {
  id: "srv-1",
  conversation_id: CONV_ID,
  direction: "out",
  sender_type: "operator",
  sender: { id: fakeUser.id, full_name: fakeUser.full_name },
  body: "Добрый день!",
  attachments: [],
  delivery_status: "pending",
  client_message_id: "will-be-overwritten",
  created_at: "2026-08-05T10:12:00Z",
};

describe("Composer — оптимистичная отправка (03 §3.4)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const bodies: Array<Record<string, unknown>> = [];
  const urls: string[] = [];

  beforeEach(() => {
    bodies.length = 0;
    urls.length = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubFetch(handler: (url: string, body: Record<string, unknown>) => Response | Promise<Response>) {
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      // Загрузка файла шлёт FormData, а не JSON: без защиты разбор падал бы
      // прямо в стабе, и тест про вложения не доходил бы до проверки.
      let body: Record<string, unknown> = {};
      if (init?.body && typeof init.body === "string") {
        body = JSON.parse(init.body) as Record<string, unknown>;
      }
      urls.push(url);
      bodies.push(body);
      return handler(url, body);
    });
    vi.stubGlobal("fetch", fetchMock);
  }

  it("temp-пузырь появляется мгновенно с ⏳, затем заменяется серверным и становится delivered", async () => {
    const user = userEvent.setup();
    // Ответ сервера держим «в воздухе»: проверяем, что пузырь есть ДО него.
    let release!: () => void;
    const held = new Promise<void>((r) => {
      release = r;
    });
    stubFetch(async (_url, body) => {
      await held;
      return jsonResponse(201, { ...SERVER_MESSAGE, client_message_id: body.client_message_id });
    });

    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Добрый день!");
    await user.keyboard("{Enter}");

    // Пузырь в ленте — сразу, ещё до ответа сервера.
    const optimistic = threadMessages();
    expect(optimistic).toHaveLength(1);
    expect(optimistic[0].delivery_status).toBe("pending");
    expect(optimistic[0].id).toBe(optimistic[0].client_message_id); // локальный temp

    const tempId = optimistic[0].id;

    release();

    // Ответ сервера подменяет temp на серверный id, статус всё ещё pending.
    await waitFor(() => {
      expect(threadMessages()[0].id).toBe("srv-1");
    });
    expect(threadMessages()[0].delivery_status).toBe("pending");
    /*
     * ⚠ ИЩЕМ ОТПРАВКУ, А НЕ ПЕРВЫЙ ЗАПРОС (28.08). С появлением полосы быстрых
     * ответов композер первым делом грузит заготовки — и `urls[0]` стал
     * указывать на них. Тест про отправку обязан находить отправку, а не
     * полагаться на порядок вызовов: иначе любой новый запрос при открытии
     * панели ломает его, ничего не сломав в продукте.
     */
    const отправка = urls.findIndex((u) => u.includes(`/conversations/${CONV_ID}/messages`));
    expect(отправка, "запроса отправки не было вовсе").toBeGreaterThan(-1);
    expect(bodies[отправка].client_message_id).toBe(tempId);

    // Итог доставки прилетает WS-событием message:status (01 §11.3).
    applyWsEvent({
      type: "message:status",
      ts: "2026-08-05T10:12:03Z",
      data: { conversation_id: CONV_ID, message_id: "srv-1", delivery_status: "delivered" },
    });
    expect(threadMessages()[0].delivery_status).toBe("delivered");
  });

  it("ошибка отправки: текст ВЕРНУЛСЯ В ПОЛЕ и пузыря не осталось, «взял в работу» откатан", async () => {
    /*
     * Упавшее сообщение живёт в ОДНОМ месте. Раньше их было два сразу: пузырь
     * «Не отправилось · Повторить» в ленте и тот же текст в поле ввода. Два
     * повтора рядом — это два разных сообщения клиенту: у отправки из поля
     * новый `client_message_id`, и идемпотентность сервера её не остановит.
     * Выбрано поле: оно переживает F5, а оптимистичный пузырь исчезает
     * бесследно — сервер сообщения не создавал.
     */
    const user = userEvent.setup();
    stubFetch(() => jsonResponse(500, { error: { code: "internal_error", message: "сбой" } }));

    // Диалог «Новый» без ответственного: успешная отправка забрала бы его себе (01 §6.2).
    const conv = makeConversation({ status: "new", assignee: null, bot_active: true });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);

    renderWithProviders(<Composer convId={CONV_ID} conversation={conv} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Замена экрана — 8900");
    await user.keyboard("{Enter}");

    // Черновик возвращён — текст не потеряется ни при уходе со страницы, ни при
    // перезагрузке (03 §3.4).
    await waitFor(() => {
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe("Замена экрана — 8900");
    });
    expect((screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement).value).toBe(
      "Замена экрана — 8900",
    );
    // Второго места нет: пузырь с «Повторить» из ленты убран.
    expect(threadMessages()).toHaveLength(0);
    // Сообщения не случилось — значит и «взял в работу»/«бот замолчал» не случилось:
    // деталь помечена устаревшей и перезапрошена у сервера.
    await waitFor(() => {
      expect(queryClient.getQueryState(qk.conversations.detail(CONV_ID))?.isInvalidated).not.toBe(false);
    });
  });

  it("режим заметки шлёт POST в /notes и красит панель жёлтым", async () => {
    const user = userEvent.setup();
    stubFetch((_url, body) =>
      jsonResponse(201, {
        ...SERVER_MESSAGE,
        id: "srv-note",
        direction: "note",
        delivery_status: "delivered",
        client_message_id: body.client_message_id,
      }),
    );

    const { container } = renderWithProviders(
      <Composer convId={CONV_ID} conversation={makeConversation()} />,
    );

    await user.click(screen.getByRole("button", { name: "Заметка" }));
    expect(container.querySelector('.composer[data-note="true"]')).not.toBeNull();

    await user.type(screen.getByLabelText("Текст заметки"), "торгуется, скидка до 10%");
    await user.click(screen.getByRole("button", { name: "Отправить заметку" }));

    await waitFor(() => {
      expect(
        urls.some((u) => u.includes(`/conversations/${CONV_ID}/notes`)),
        "заметка ушла не в /notes",
      ).toBe(true);
    });
    expect(threadMessages()[0].direction).toBe("note");
  });

  it("Enter в пустом поле ничего не отправляет", async () => {
    const user = userEvent.setup();
    stubFetch(() => jsonResponse(201, SERVER_MESSAGE));

    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.click(screen.getByLabelText("Текст сообщения"));
    await user.keyboard("{Enter}");

    // Отправки не было. Запрос заготовок для полосы быстрых ответов при этом
    // законен: он про показ подсказок, а не про отправку.
    expect(urls.filter((u) => u.includes(`/conversations/${CONV_ID}/messages`))).toHaveLength(0);
    expect(threadMessages()).toHaveLength(0);
  });

  it("счётчик символов появляется после 800 (лимит Авито 1000)", async () => {
    stubFetch(() => jsonResponse(201, SERVER_MESSAGE));
    useChatUiStore.setState({ drafts: { [CONV_ID]: { text: "я".repeat(801), isNote: false } } });

    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    expect(screen.getByText("801 / 1000")).toBeInTheDocument();
  });

  it("черновик живёт по диалогу: переключение диалога текст не теряет", async () => {
    const user = userEvent.setup();
    stubFetch(() => jsonResponse(201, SERVER_MESSAGE));

    const { rerender, container } = renderWithProviders(
      <Composer convId={CONV_ID} conversation={makeConversation()} />,
    );

    await user.type(screen.getByLabelText("Текст сообщения"), "не потеряй меня");

    // Ушли в другой диалог — поле чистое…
    rerender(wrap(<Composer convId="conv-2" conversation={makeConversation({ id: "conv-2" })} />));
    expect((container.querySelector(".composer__input") as HTMLTextAreaElement).value).toBe("");

    // …вернулись — текст на месте.
    rerender(wrap(<Composer convId={CONV_ID} conversation={makeConversation()} />));
    expect((container.querySelector(".composer__input") as HTMLTextAreaElement).value).toBe(
      "не потеряй меня",
    );
  });

  describe("Документ клиенту отправить нельзя, и это видно заранее", () => {
    /*
     * ⚠ ПРАВИЛО СУЗИЛОСЬ 07.09, И ЭТИ ПРОВЕРКИ ПЕРЕВЕДЕНЫ НА PDF.
     *
     * Раньше здесь стоял `деталь.png` и утверждение «файл клиенту не уходит».
     * Утверждение было неверным: картинки Авито принимает двумя ручками
     * (docs/26 строки 49/59/63), и заслон отнимал у оператора самую частую
     * просьбу переписки — «покажите фото». Метода на произвольный файл в
     * каталоге по-прежнему нет, поэтому правило живо ровно для документов, и
     * проверяется оно тем, что документом и является.
     *
     * Что картинка отправку НЕ гасит — сторож в `ComposerImagePaste0709`.
     *
     * Проверяется не «есть ли предупреждение», а то, что человек НЕ МОЖЕТ
     * отправить такое сообщение: узнать о потере из красного «не доставлено»
     * почти так же плохо, как не узнать вовсе.
     */
    /*
     * Ждём именно `data-state="ready"`, а не появления имени файла: имя видно
     * и во время загрузки, а `canSubmit` до её конца ложен по другой причине
     * («uploading»). Без этого ожидания тест «файл не отправляется» проходил
     * бы, даже если бы вложения ничего не блокировали.
     */
    const attachDoc = async (user: ReturnType<typeof userEvent.setup>) => {
      const input = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(input, new File(["%PDF-1.4"], "смета.pdf", { type: "application/pdf" }));
      await waitFor(() =>
        expect(document.querySelector('.composer__chip[data-state="ready"]')).not.toBeNull(),
      );
    };

    // `kind: "file"` — так разбирает pdf сервер (app/services/media.py).
    const withUpload = () =>
      stubFetch((url) =>
        url.includes("/media")
          ? jsonResponse(201, {
              media_id: "m-1",
              kind: "file",
              url: "/api/v1/media/m-1",
              name: "смета.pdf",
              size: 8,
            })
          : jsonResponse(201, SERVER_MESSAGE),
      );

    it("с документом сообщение клиенту не отправляется", async () => {
      const user = userEvent.setup();
      withUpload();
      renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

      await user.type(screen.getByLabelText("Текст сообщения"), "смета по ремонту");
      await attachDoc(user);

      // Фокус возвращаем явно: после выбора файла он уходит на input[type=file],
      // и Enter «в никуда» не отправил бы ничего сам по себе — тест проходил бы,
      // даже если бы вложения ничего не блокировали.
      await user.click(screen.getByLabelText("Текст сообщения"));
      await user.keyboard("{Enter}");

      // Ни одного POST в ленту — ушла только загрузка файла на /media.
      expect(urls.filter((u) => u.includes("/messages"))).toHaveLength(0);
      expect(threadMessages()).toHaveLength(0);
    });

    it("и объясняет почему — до отправки, а не после", async () => {
      const user = userEvent.setup();
      withUpload();
      renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

      await attachDoc(user);

      expect(await screen.findByText(/PDF и документы Авито от нас не принимает/)).toBeInTheDocument();
      expect(screen.getByText(/приложите к заметке или дайте ссылку/)).toBeInTheDocument();
    });

    it("а к заметке документ приложить можно — она не уходит клиенту", async () => {
      const user = userEvent.setup();
      withUpload();
      useChatUiStore.setState({ drafts: { [CONV_ID]: { text: "", isNote: true } }, activeConversationId: CONV_ID });
      renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

      await user.type(screen.getByLabelText("Текст заметки"), "смета от подрядчика");
      await attachDoc(user);

      expect(screen.queryByText(/PDF и документы/)).toBeNull();

      await user.click(screen.getByLabelText("Текст заметки"));
      await user.keyboard("{Enter}");
      await waitFor(() => expect(urls.some((u) => u.includes("/notes"))).toBe(true));
    });
  });

  it("закрытый диалог: вместо поля — «Диалог закрыт» и «Вернуть в работу» (01 §6.2)", () => {
    stubFetch(() => jsonResponse(200, makeConversation({ status: "in_progress" })));

    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation({ status: "closed" })} />);

    expect(screen.getByText("Диалог закрыт.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Вернуть в работу" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Текст сообщения")).not.toBeInTheDocument();
  });
});
