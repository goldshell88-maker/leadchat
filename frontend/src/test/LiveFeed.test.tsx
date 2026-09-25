import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppRail } from "@/app/AppRail";
import { queryClient } from "@/app/queryClient";
import { feedLine } from "@/features/feed/describe";
import { FeedPage } from "@/features/feed/FeedPage";
import { FEED_LIMIT, clearFeed, recordWsFrame, useFeedStore, watchConnectionGaps } from "@/features/feed/store";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessageDto } from "@/shared/api/types";
import { applyWsEvent, type WsInboxEvent } from "@/shared/realtime/applyWsEvent";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import type { WsServerEvent } from "@/shared/realtime/wsEvents";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ЖИВАЯ ЛЕНТА РАБОТЫ СИСТЕМЫ.
 *
 * Владелец просил «смотреть логи работы в реальном времени» и выбрал вариант
 * экрана внутри LeadChat. Проверяется здесь не то, что экран рисуется, а то,
 * ради чего он затевался: кадр сокета превращается в русскую строку с именем
 * и каналом, лента не растёт бесконечно, обрыв связи назван вслух, а чужого в
 * ней не появляется.
 */

const CONV = "conv-feed-1";
const ACC = "acc-7";

function row(overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id: CONV,
    status: "new",
    channel: "avito",
    account: { id: ACC, title: "Парт-7" },
    client: { id: "cli-1", name: "Наталья", phone: null, avito_rating: null },
    assignee: null,
    item: null,
    last_message: null,
    unread_count: 1,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-12T09:00:00.000Z",
    ...overrides,
  };
}

function message(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: "msg-1",
    conversation_id: CONV,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "Здравствуйте",
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-08-12T09:00:01.000Z",
    ...overrides,
  };
}

/** Кадр «диалог встал в очередь» — он один везёт диалог целиком, с именем и каналом. */
const inboxNew: WsInboxEvent = {
  type: "inbox:new",
  ts: "2026-08-12T09:00:00.000Z",
  data: { conversation_id: CONV, conversation: row(), can_claim: true },
};

/**
 * Строки ленты ТАК, КАК ИХ ВИДИТ ЧЕЛОВЕК.
 *
 * Раньше здесь стояло `e.text`, и это перестало быть правдой 12 августа: имя
 * клиента уехало из текста в отдельное поле, потому что кадр `message:new`
 * привозит дельту диалога БЕЗ имени, и у только что заведённого диалога строка
 * запекалась безымянной навсегда («Клиент: новое сообщение» над строкой очереди,
 * где тот же человек назван). Имя подставляется при отрисовке — значит и
 * проверять надо отрисованное, иначе проверки останутся зелёными ровно там, где
 * человек видит обрубок.
 *
 * `feedLine` — тот же вызов, которым рисует экран, а не его копия.
 */
function texts(): string[] {
  return useFeedStore.getState().entries.map((e) => feedLine(e).line);
}

/** Смотрящий с правом статистики — ему лента и предназначена. */
function asHead() {
  resetSessionStore({
    user: { ...fakeUser, role: "head" },
    permissions: ["conversations:read", "notes:read", "stats:own", "stats:all"],
    accessToken: "t",
    bootstrapped: true,
  });
}

beforeEach(() => {
  queryClient.clear();
  clearFeed();
  useConnectionStore.setState({ status: "open", lastEventAt: null });
  asHead();
});

afterEach(() => {
  vi.useRealTimers();
  clearFeed();
});

describe("Кадр сокета превращается в русскую строку, а не в JSON", () => {
  it("сообщение клиента: имя и канал берутся из того, что лента уже видела", () => {
    // Сам `message:new` имени клиента НЕ несёт — сервер кладёт в него только
    // conversation_id и сообщение. Имя приезжает раньше, кадром очереди.
    recordWsFrame(inboxNew);
    recordWsFrame({
      type: "message:new",
      ts: "2026-08-12T09:00:01.000Z",
      data: { conversation_id: CONV, message: message(), conversation_patch: { unread_delta: 1 } },
    } as WsServerEvent);

    expect(texts()[0]).toBe("Наталья: новое сообщение");
    expect(useFeedStore.getState().entries[0].accountTitle).toBe("Парт-7");
    // Никакого JSON и никаких идентификаторов в строке.
    expect(texts()[0]).not.toContain(CONV);
  });

  /**
   * ПОРЯДОК КАДРОВ ОБРАТНЫЙ: СНАЧАЛА СООБЩЕНИЕ, ПОТОМ ИМЯ.
   *
   * НАЙДЕНО НА ЖИВОМ СТЕНДЕ 12 августа, а не рассуждением. У НОВОГО клиента
   * `message:new` приходит ПЕРВЫМ (см. `app/services/inbound.py`: кадр очереди
   * публикуется вторым, «чтобы порядок совпадал с порядком в жизни»), и в нём
   * едет дельта диалога — непрочитанные, время, статус, — без имени и канала.
   * Справочник в этот момент про такой диалог не знает ничего.
   *
   * Строка запекалась навсегда: «Клиент: новое сообщение» — и через миллисекунду
   * прямо над ней вставала строка очереди, где тот же человек назван и по имени,
   * и по каналу. Соседние строки об одном событии, и одна выглядит поломкой.
   *
   * Тест идёт именно в этом порядке. Проверка, где кадр очереди первый (выше),
   * этого не ловила: она описывала удобный случай, а не тот, что бывает у
   * каждого нового клиента.
   */
  it("имя, приехавшее следующим кадром, подхватывает и уже показанная строка", () => {
    recordWsFrame({
      type: "message:new",
      ts: "2026-08-12T09:00:01.000Z",
      data: { conversation_id: CONV, message: message(), conversation_patch: { unread_delta: 1 } },
    } as WsServerEvent);

    // Пока про диалог не знает никто — честное общее слово, без выдумок.
    expect(texts()[0]).toBe("Клиент: новое сообщение");

    // Кадр очереди привозит диалог целиком — с именем и каналом.
    recordWsFrame(inboxNew);

    // И СТАРАЯ строка теперь названа: она спрашивает справочник заново.
    const строки = texts();
    expect(строки[0]).toBe("Наталья: обращение встало в очередь — ждёт, кто возьмёт");
    expect(строки[1]).toBe("Наталья: новое сообщение");
    expect(feedLine(useFeedStore.getState().entries[1]).channel).toBe("Парт-7");
  });

  /**
   * ЗАПАСНОЙ ИСТОЧНИК ИМЕНИ — КЭШ УЖЕ ЗАГРУЖЕННОГО СПИСКА.
   *
   * Кадров очереди по старому диалогу не бывает: он встал в неё когда-то давно.
   * Единственное, что о нём знает вкладка, — строка списка чатов. Без этого
   * пути каждое сообщение от постоянного клиента писалось бы обезличенным
   * «Клиент: новое сообщение», и лента годилась бы только на новых.
   */
  it("имя старого диалога берётся из уже загруженного списка чатов", () => {
    queryClient.setQueryData(qk.conversations.list({ tab: "all" }), {
      pages: [{ items: [row({ client: { id: "cli-9", name: "Борис", phone: null, avito_rating: null } })], page: { limit: 50, offset: 0, total: 1 } }],
      pageParams: [0],
    });

    recordWsFrame({
      type: "message:new",
      ts: "2026-08-12T09:00:01.000Z",
      data: { conversation_id: CONV, message: message(), conversation_patch: {} },
    } as WsServerEvent);

    expect(texts()[0]).toBe("Борис: новое сообщение");
    expect(useFeedStore.getState().entries[0].accountTitle).toBe("Парт-7");
  });

  it("имени нет нигде — строка честно говорит «Клиент», а не показывает идентификатор", () => {
    recordWsFrame({
      type: "message:new",
      ts: "2026-08-12T09:00:01.000Z",
      data: { conversation_id: CONV, message: message(), conversation_patch: {} },
    } as WsServerEvent);

    expect(texts()[0]).toBe("Клиент: новое сообщение");
  });

  it("недоставленный ответ называет причину, которую вернул Авито", () => {
    recordWsFrame(inboxNew);
    recordWsFrame({
      type: "message:status",
      ts: "2026-08-12T09:00:02.000Z",
      data: {
        conversation_id: CONV,
        message_id: "msg-2",
        delivery_status: "failed",
        error: "Авито вернул 429",
      },
    } as WsServerEvent);

    expect(texts()[0]).toBe("Наталья: ответ не доставлен — Авито вернул 429");
  });

  it("причины нет — молчание названо молчанием, а не пустотой", () => {
    recordWsFrame(inboxNew);
    recordWsFrame({
      type: "message:status",
      ts: "2026-08-12T09:00:02.000Z",
      data: { conversation_id: CONV, message_id: "msg-2", delivery_status: "failed" },
    } as WsServerEvent);

    expect(texts()[0]).toBe("Наталья: ответ не доставлен — причину Авито не назвал");
  });

  it("ответ оператора и ответ бота — разные строки", () => {
    recordWsFrame(inboxNew);
    recordWsFrame({
      type: "message:new",
      ts: "2026-08-12T09:00:03.000Z",
      data: {
        conversation_id: CONV,
        message: message({
          id: "m-out",
          direction: "out",
          sender_type: "operator",
          sender: { id: "u-2", full_name: "Иван Петров" },
        }),
        conversation_patch: {},
      },
    } as WsServerEvent);
    recordWsFrame({
      type: "message:new",
      ts: "2026-08-12T09:00:04.000Z",
      data: {
        conversation_id: CONV,
        message: message({ id: "m-bot", direction: "out", sender_type: "bot", sender: null }),
        conversation_patch: {},
      },
    } as WsServerEvent);

    expect(texts()).toContain("Наталья: ответил Иван Петров");
    expect(texts()).toContain("Наталья: ответил бот");
  });

  it("диалог взят: с именем взявшего и со временем ожидания", () => {
    recordWsFrame(inboxNew);
    recordWsFrame({
      type: "inbox:claimed",
      ts: "2026-08-12T09:02:00.000Z",
      data: {
        conversation_id: CONV,
        claimed_by: { id: "u-2", full_name: "Иван Петров" },
        waited_seconds: 120,
      },
    } as WsInboxEvent);

    expect(texts()[0]).toBe("Наталья: диалог взял Иван Петров (ждал 2 минуты)");
  });

  it("канал отвалился — строка говорит и что случилось, и чем это грозит", () => {
    recordWsFrame({
      type: "account:needs_reauth",
      ts: "2026-08-12T09:05:00.000Z",
      data: { account_id: ACC, title: "Парт-7" },
    } as WsServerEvent);

    expect(texts()[0]).toBe(
      "Канал «Парт-7» требует переподключения: ответы не уходят, обращения приходят",
    );
  });

  /**
   * НАША СОБСТВЕННАЯ СИСТЕМНАЯ ЗАПИСЬ В ЛЕНТУ НЕ ИДЁТ.
   *
   * «Статус: Новый → В работе. Иванов» приезжает дважды: сообщением и кадром
   * `conversation:updated` (см. `_publish_change` в routes/conversations.py).
   * Показать оба — значит удвоить ленту на каждом действии оператора.
   */
  it("системная запись нашего же производства не дублирует смену статуса", () => {
    recordWsFrame(inboxNew);
    const before = texts().length;
    recordWsFrame({
      type: "message:new",
      ts: "2026-08-12T09:00:05.000Z",
      data: {
        conversation_id: CONV,
        message: message({ id: "m-sys", direction: "system", sender_type: "system" }),
        conversation_patch: {},
      },
    } as WsServerEvent);

    expect(texts().length).toBe(before);
  });

  /**
   * Кадр `conversation:updated` везёт статус на КАЖДОЕ действие — заметку, тег,
   * закрепление, — даже когда статус не менялся. Без отсева лента писала бы
   * «диалог — В работе» по десять раз подряд.
   */
  it("повторный статус в ленту не попадает — только настоящая перемена", () => {
    recordWsFrame(inboxNew); // диалог со статусом «new»
    const updated = (status: string, ts: string): WsServerEvent =>
      ({
        type: "conversation:updated",
        ts,
        data: { conversation_id: CONV, patch: { status } },
      }) as unknown as WsServerEvent;

    recordWsFrame(updated("in_progress", "2026-08-12T09:01:00.000Z"));
    recordWsFrame(updated("in_progress", "2026-08-12T09:01:10.000Z"));
    recordWsFrame(updated("closed", "2026-08-12T09:02:00.000Z"));

    const statusLines = texts().filter((t) => t.includes("диалог —"));
    expect(statusLines).toEqual(["Наталья: диалог — «Закрыт»", "Наталья: диалог — «В работе»"]);
  });
});

/**
 * ЛЕНТА СНИМАЕТ КАДРЫ С ОБЩЕГО ДИСПЕТЧЕРА, А НЕ ИЗ СВОЕГО СОКЕТА.
 *
 * Кадр гоним через `applyWsEvent` — ту самую точку, куда `WsClient` отдаёт
 * ВСЁ, что пришло по единственному соединению. Взят `presence:online`
 * намеренно: рабочее место на него не реагирует вовсе (ветки в `switch` у него
 * нет), то есть проверка ловит ровно одно — что съём стоит ДО разбора типов, а
 * не внутри какой-то из веток. Провались это — и «что происходит в системе»
 * молчало бы ровно про людей.
 *
 * Имя сотрудника приходит из кэша «кому можно передать диалог» — запасного
 * источника, описанного в directory.ts: сам кадр несёт только идентификатор.
 */
describe("Съём кадров стоит на общем диспетчере", () => {
  it("кадр, на который рабочее место не реагирует, всё равно попадает в ленту", () => {
    queryClient.setQueryData(qk.users.assignable, {
      items: [{ id: "u-9", full_name: "Пётр Ковалёв", role: "manager", is_online: true }],
    });

    applyWsEvent({
      type: "presence:online",
      ts: "2026-08-12T09:10:00.000Z",
      data: { user_id: "u-9", status: "away" },
    } as WsServerEvent);

    expect(texts()[0]).toBe("Пётр Ковалёв — отошёл");
  });
});

describe("Лента идёт часами — памяти вкладки это стоить не должно", () => {
  it("держится ровно предел, лишнее выбрасывается с хвоста", () => {
    for (let i = 0; i < FEED_LIMIT + 25; i += 1) {
      recordWsFrame({
        type: "account:needs_reauth",
        ts: new Date(Date.UTC(2026, 7, 12, 9, 0, i)).toISOString(),
        data: { account_id: ACC, title: `Парт-${i}` },
      } as WsServerEvent);
    }

    const { entries } = useFeedStore.getState();
    expect(entries.length).toBe(FEED_LIMIT);
    // Осталась свежая часть, а не первая: у хвоста ленты нет ценности.
    expect(entries[0].text).toContain(`Парт-${FEED_LIMIT + 24}`);
  });
});

describe("Кому лента ведётся", () => {
  it("без права на статистику события не копятся вовсе", () => {
    resetSessionStore({
      user: { ...fakeUser, role: "manager" },
      permissions: ["conversations:read", "messages:send", "stats:own"],
      accessToken: "t",
      bootstrapped: true,
    });

    recordWsFrame(inboxNew);

    expect(useFeedStore.getState().entries).toEqual([]);
  });

  /**
   * Пункт в левой панели — по тому же праву, что «Статистика» и «Разбор
   * диалогов»: лента отвечает на управленческий вопрос «как идут дела», только
   * за последние десять минут. Оператору отдельный экран не нужен — всё, что
   * лента ему показала бы, у него и так перед глазами.
   */
  it("пункт «Живая лента» есть у руководителя и нет у оператора", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(404, { error: { code: "x", message: "нет" } })));
    try {
      asHead();
      const head = renderWithProviders(<AppRail />, { route: "/chats" });
      expect(await screen.findByRole("link", { name: "Живая лента" })).toHaveAttribute(
        "href",
        "/feed",
      );
      head.unmount();

      resetSessionStore({
        user: { ...fakeUser, role: "manager" },
        permissions: ["conversations:read", "messages:send", "stats:own"],
        accessToken: "t",
        bootstrapped: true,
      });
      renderWithProviders(<AppRail />, { route: "/chats" });
      await screen.findByRole("link", { name: /Чаты/ });
      expect(screen.queryByRole("link", { name: "Живая лента" })).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("Обрыв связи назван вслух", () => {
  it("после возврата связи в ленте стоит строка о пропуске", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-12T09:00:00.000Z"));
    const stop = watchConnectionGaps();

    useConnectionStore.setState({ status: "open" });
    useConnectionStore.setState({ status: "reconnecting" });
    vi.setSystemTime(new Date("2026-08-12T09:02:00.000Z"));
    useConnectionStore.setState({ status: "open" });
    stop();

    const first = useFeedStore.getState().entries[0];
    expect(first.gap).toBe(true);
    expect(first.text).toBe(
      "Связь с сервером пропадала на 2 минуты — что было в это время, лента не знает",
    );
  });

  it("моргание в доли секунды тревогой не объявляется", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-12T09:00:00.000Z"));
    const stop = watchConnectionGaps();

    useConnectionStore.setState({ status: "open" });
    useConnectionStore.setState({ status: "reconnecting" });
    vi.setSystemTime(new Date("2026-08-12T09:00:00.400Z"));
    useConnectionStore.setState({ status: "open" });
    stop();

    expect(useFeedStore.getState().entries).toEqual([]);
  });
});

describe("Экран живой ленты", () => {
  it("пусто — это норма, а не поломка", async () => {
    renderWithProviders(<FeedPage />, { route: "/feed" });

    expect(await screen.findByText("Пока тихо")).toBeInTheDocument();
    expect(screen.getByText(/Событий нет — это нормально/)).toBeInTheDocument();
  });

  it("нет связи — молчание объяснено, а не выдано за тишину", async () => {
    useConnectionStore.setState({ status: "reconnecting" });
    renderWithProviders(<FeedPage />, { route: "/feed" });

    expect(await screen.findByText("Нет связи с сервером")).toBeInTheDocument();
    expect(screen.queryByText("Пока тихо")).toBeNull();
  });

  it("строка диалога ведёт в этот диалог", async () => {
    recordWsFrame(inboxNew);
    renderWithProviders(<FeedPage />, { route: "/feed" });

    const link = await screen.findByRole("link", {
      name: /обращение встало в очередь/,
    });
    expect(link).toHaveAttribute("href", `/chats/${CONV}`);
  });

  it("отбор по виду события прячет чужое, но не прячет пропуск в записи", async () => {
    recordWsFrame(inboxNew);
    recordWsFrame({
      type: "account:needs_reauth",
      ts: "2026-08-12T09:05:00.000Z",
      data: { account_id: "acc-9", title: "Парт-9" },
    } as WsServerEvent);
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-12T09:06:00.000Z"));
    const stop = watchConnectionGaps();
    useConnectionStore.setState({ status: "open" });
    useConnectionStore.setState({ status: "reconnecting" });
    vi.setSystemTime(new Date("2026-08-12T09:07:00.000Z"));
    useConnectionStore.setState({ status: "open" });
    stop();
    vi.useRealTimers();

    renderWithProviders(<FeedPage />, { route: "/feed" });
    useFeedStore.getState().setFilters({ group: "queue" });

    await waitFor(() => expect(screen.queryByText(/Канал «Парт-9» требует переподключения/)).toBeNull());
    expect(screen.getByText(/обращение встало в очередь/)).toBeInTheDocument();
    /*
     * СТРОКА О ПРОПУСКЕ ОСТАЁТСЯ ПРИ ЛЮБОМ ОТБОРЕ. Спрятать её значит выдать
     * дырявую запись за гладкую: человек отобрал очередь, увидел непрерывный
     * список и решил, что за эти две минуты в очереди ничего не было.
     */
    expect(screen.getByText(/Связь с сервером пропадала/)).toBeInTheDocument();
  });

  it("отбор по каналу собран из того, что лента видела, и сужает её", async () => {
    recordWsFrame(inboxNew); // канал «Парт-7»
    recordWsFrame({
      type: "account:needs_reauth",
      ts: "2026-08-12T09:05:00.000Z",
      data: { account_id: "acc-9", title: "Парт-9" },
    } as WsServerEvent);

    renderWithProviders(<FeedPage />, { route: "/feed" });

    await userEvent.click(screen.getByRole("textbox", { name: "Фильтр по каналу" }));
    await userEvent.click(await screen.findByRole("option", { name: "Парт-9" }));

    expect(await screen.findByText(/Канал «Парт-9» требует переподключения/)).toBeInTheDocument();
    expect(screen.queryByText(/обращение встало в очередь/)).toBeNull();
  });

  it("пауза держит экран на месте и честно считает пропущенное", async () => {
    recordWsFrame(inboxNew);
    renderWithProviders(<FeedPage />, { route: "/feed" });
    await screen.findByText(/обращение встало в очередь/);

    await userEvent.click(screen.getByRole("button", { name: "Пауза" }));

    recordWsFrame({
      type: "account:needs_reauth",
      ts: "2026-08-12T09:05:00.000Z",
      data: { account_id: "acc-9", title: "Парт-9" },
    } as WsServerEvent);

    // Строка появилась в хранилище, но не на экране: человек читает.
    expect(useFeedStore.getState().entries.length).toBe(2);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Продолжить · 1 событие" })).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Канал «Парт-9» требует переподключения/)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Продолжить · 1 событие" }));
    expect(await screen.findByText(/Канал «Парт-9» требует переподключения/)).toBeInTheDocument();
  });
});
