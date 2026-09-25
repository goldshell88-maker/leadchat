import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { applyWsEvent, forgetViewerWarnings } from "@/shared/realtime/applyWsEvent";
import { ConversationViewers } from "@/shared/realtime/ConversationViewers";
import { otherViewers, useViewersStore, viewersLabel } from "@/shared/realtime/viewersStore";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { WsClient } from "@/shared/realtime/WsClient";
import { startRealtime, stopRealtime } from "@/shared/realtime/realtime";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * Двое в одном диалоге (SCEN-48).
 *
 * До правки признака не было ни на одном конце: сервер кадр `subscribe`
 * принимал, а браузер его не слал вовсе. Здесь проверяется весь клиентский
 * конец: кадр уходит, переживает обрыв, состав доезжает до экрана и
 * столкновение показывается вслух.
 */

const CONV = "conv-1";
const ME = { id: "u-me", full_name: "Я Сам" };
const PETR = { id: "u-petr", full_name: "Пётр Петров" };
const ANNA = { id: "u-anna", full_name: "Анна Иванова" };

const toasts: { title?: string; message?: string }[] = [];
vi.mock("@/shared/ui/toast", () => ({
  showToast: (o: { title?: string; message?: string }) => {
    toasts.push(o);
    return "id";
  },
}));

beforeEach(() => {
  toasts.length = 0;
  useViewersStore.getState().clear();
  forgetViewerWarnings();
  useChatUiStore.setState({ activeConversationId: CONV });
  useSessionStore.setState({ user: { ...ME, role: "manager" } as never });
});

describe("состав зрителей", () => {
  it("показывает коллегу и не показывает меня самого", () => {
    applyWsEvent({
      type: "conversation:viewers",
      ts: "2026-08-12T10:00:00.000Z",
      data: { conversation_id: CONV, viewers: [ME, PETR] },
    });

    render(<ConversationViewers conversationId={CONV} />);
    expect(screen.getByTestId("conversation-viewers")).toHaveTextContent("Пётр Петров");
    expect(screen.getByTestId("conversation-viewers")).not.toHaveTextContent("Я Сам");
  });

  it("в одиночестве не рисует ничего", () => {
    applyWsEvent({
      type: "conversation:viewers",
      ts: "2026-08-12T10:00:00.000Z",
      data: { conversation_id: CONV, viewers: [ME] },
    });

    const { container } = render(<ConversationViewers conversationId={CONV} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("состав заменяется целиком, а не дополняется", () => {
    const set = useViewersStore.getState().setViewers;
    set(CONV, [ME, PETR, ANNA]);
    set(CONV, [ME, ANNA]); // Пётр ушёл

    expect(otherViewers(useViewersStore.getState().byConversation, CONV, ME.id)).toEqual([ANNA]);
  });

  it("подпись перечисляет людей, а длинный список сворачивает", () => {
    expect(viewersLabel([PETR])).toBe("Диалог открыт: Пётр Петров");
    expect(viewersLabel([PETR, ANNA])).toBe("Диалог открыт: Пётр Петров и Анна Иванова");
    expect(viewersLabel([PETR, ANNA, { id: "x", full_name: "Третий" }])).toBe(
      "Диалог открыт: Пётр Петров, Анна Иванова и ещё 1",
    );
  });
});

describe("предупреждение о столкновении", () => {
  it("говорит вслух, когда коллега открыл тот же диалог", () => {
    applyWsEvent({
      type: "conversation:viewers",
      ts: "2026-08-12T10:00:00.000Z",
      data: { conversation_id: CONV, viewers: [ME] },
    });
    expect(toasts).toHaveLength(0); // сам себе не сосед

    applyWsEvent({
      type: "conversation:viewers",
      ts: "2026-08-12T10:00:01.000Z",
      data: { conversation_id: CONV, viewers: [ME, PETR] },
    });

    expect(toasts).toHaveLength(1);
    expect(toasts[0].message).toContain("Пётр Петров");
  });

  it("не повторяется, пока состав не изменился", () => {
    const frame = (viewers: typeof ME[]) =>
      applyWsEvent({
        type: "conversation:viewers",
        ts: "2026-08-12T10:00:00.000Z",
        data: { conversation_id: CONV, viewers },
      });

    frame([ME, PETR]);
    frame([ME, PETR]); // повтор после реконнекта — тот же состав
    expect(toasts).toHaveLength(1);
  });

  it("молчит про диалог, который человек уже закрыл", () => {
    useChatUiStore.setState({ activeConversationId: "conv-other" });
    applyWsEvent({
      type: "conversation:viewers",
      ts: "2026-08-12T10:00:00.000Z",
      data: { conversation_id: CONV, viewers: [ME, PETR] },
    });
    expect(toasts).toHaveLength(0);
  });
});

/**
 * Кадр `subscribe` гоняется через НАСТОЯЩИЙ жизненный цикл сокета, а не через
 * подставленное поле: половина смысла этой правки — в том, что подписка
 * восстанавливается после обрыва, а обрыв виден только отсюда.
 */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;

  url: string;
  readyState = FakeWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code: 1000 });
  }
}

describe("кадр subscribe", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/ws/ticket")
          ? jsonResponse(200, { ticket: "wst_1", expires_in: 60 })
          : jsonResponse(204, {}),
      ),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const frames = (ws: FakeWebSocket) => ws.sent.map((raw) => JSON.parse(raw));

  async function open(index = 0) {
    const ws = FakeWebSocket.instances[index];
    ws.readyState = FakeWebSocket.OPEN;
    ws.onopen?.();
    return ws;
  }

  it("уходит на сервер при открытом диалоге — раньше не слался ни один", async () => {
    const client = new WsClient(() => {});
    client.subscribe(CONV); // сокета ещё нет: кадру некуда идти
    await client.connect();

    const ws = await open();
    expect(frames(ws)).toContainEqual({ type: "subscribe", data: { conversation_id: CONV } });
  });

  it("повторяется после обрыва: сессия на сервере новая и про диалог не знает", async () => {
    const client = new WsClient(() => {});
    client.subscribe(CONV);
    await client.connect();
    const first = await open();
    expect(frames(first)).toHaveLength(1);

    first.onclose?.({ code: 4408 }); // сервер закрыл по таймауту кадров
    await vi.advanceTimersByTimeAsync(2000); // backoff первой попытки

    const second = await open(1);
    expect(frames(second)).toContainEqual({
      type: "subscribe",
      data: { conversation_id: CONV },
    });
  });

  it("без открытого диалога молчит — лишних кадров на каждый коннект не шлём", async () => {
    const client = new WsClient(() => {});
    client.subscribe(null);
    await client.connect();

    const ws = await open();
    expect(frames(ws).filter((f) => f.type === "subscribe")).toHaveLength(0);
  });

  it("следует за открытым диалогом и забывает состав покинутого", async () => {
    useChatUiStore.setState({ activeConversationId: CONV });
    startRealtime();
    await vi.advanceTimersByTimeAsync(0); // connect() ходит за тикетом
    const ws = await open();
    useViewersStore.getState().setViewers(CONV, [ME, PETR]);

    useChatUiStore.setState({ activeConversationId: "conv-2" });

    expect(frames(ws)).toContainEqual({
      type: "subscribe",
      data: { conversation_id: "conv-2" },
    });
    // Прежний состав забыт: сервер пришлёт новый только оставшимся, и вернись
    // человек обратно — он увидел бы позавчерашних соседей.
    expect(useViewersStore.getState().byConversation[CONV]).toBeUndefined();
    stopRealtime();
  });

  it("в закрытый сокет не пишет и не падает", () => {
    const client = new WsClient(() => {});
    expect(() => client.subscribe(CONV)).not.toThrow();
    expect(() => client.typing(CONV)).not.toThrow();
  });
});

describe("спам предупреждений (жалоба владельца 19.08)", () => {
  const кадр = (viewers: (typeof ME)[], ts: string) =>
    applyWsEvent({
      type: "conversation:viewers",
      ts,
      data: { conversation_id: CONV, viewers },
    });

  it("обрыв связи у коллеги не считается новым приходом", () => {
    /**
     * ЧТО ВИДЕЛ ВЛАДЕЛЕЦ: «идёт спам „Диалог открыт не только у вас“, хотя
     * человек просто в сети». Состав зрителей приходит целиком и на КАЖДОЕ
     * изменение, а любой обрыв связи у коллеги — это уход и следом приход:
     * он исчезал из состава и через секунду возвращался. Для сравнения «кто
     * новый» он оказывался новым каждый раз, и человеку, спокойно сидящему в
     * диалоге, летело предупреждение за предупреждением.
     */
    кадр([ME, PETR], "2026-08-12T10:00:00.000Z");
    expect(toasts).toHaveLength(1); // первый приход — законный повод

    кадр([ME], "2026-08-12T10:00:05.000Z"); // связь оборвалась
    кадр([ME, PETR], "2026-08-12T10:00:07.000Z"); // и вернулась через две секунды

    expect(toasts).toHaveLength(1); // мигание связи молчит
  });

  it("настоящий приход другого человека сказать всё равно надо", () => {
    кадр([ME, PETR], "2026-08-12T10:00:00.000Z");
    expect(toasts).toHaveLength(1);

    кадр([ME, PETR, ANNA], "2026-08-12T10:00:20.000Z");

    expect(toasts).toHaveLength(2);
    expect(toasts[1].message).toContain("Анна");
  });
});
