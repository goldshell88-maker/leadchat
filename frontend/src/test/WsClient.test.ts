import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { catchUpAfterReconnect } from "@/shared/realtime/applyWsEvent";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import { WsClient } from "@/shared/realtime/WsClient";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * Догон подменён НАМЕРЕННО: здесь проверяется не он сам (это делает
 * `reconnectCatchUp.test.ts`), а то, ЧЕМ его зовёт сокет. Признак «связь
 * возвращалась, а не поднялась впервые» вычисляется только здесь, и если
 * вычислять его неправильно, догон останется зелёным в своих тестах, а на
 * проде не сделает ничего: тесты догона зовут функцию напрямую и о сокете
 * ничего не знают.
 */
vi.mock("@/shared/realtime/applyWsEvent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/realtime/applyWsEvent")>();
  return { ...actual, catchUpAfterReconnect: vi.fn(async () => {}) };
});

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
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

describe("WsClient", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let ticketCounter: number;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(catchUpAfterReconnect).mockClear();
    FakeWebSocket.instances = [];
    ticketCounter = 0;
    useConnectionStore.setState({ status: "idle", lastEventAt: null, reconnects: [] });
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });

    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/ws/ticket")) {
        ticketCounter += 1;
        return jsonResponse(200, { ticket: `wst_${ticketCounter}`, expires_in: 60 });
      }
      return jsonResponse(204, {});
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const ticketCalls = () => fetchMock.mock.calls.filter((c) => String(c[0]).includes("/ws/ticket"));

  it("connects via a one-time ticket and reconnects with a FRESH ticket after 4408", async () => {
    const client = new WsClient(() => {});
    await client.connect();

    expect(ticketCalls()).toHaveLength(1);
    expect(FakeWebSocket.instances).toHaveLength(1);
    const first = FakeWebSocket.instances[0];
    expect(first.url).toContain("/api/v1/ws?ticket=wst_1");

    first.readyState = FakeWebSocket.OPEN;
    first.onopen?.();
    expect(useConnectionStore.getState().status).toBe("open");

    // Сервер закрыл по heartbeat-таймауту (01 §11.7) → backoff → НОВЫЙ тикет.
    first.onclose?.({ code: 4408 });
    expect(useConnectionStore.getState().status).toBe("reconnecting");

    await vi.advanceTimersByTimeAsync(2000); // backoff первой попытки ≤ 1000 мс
    expect(ticketCalls()).toHaveLength(2);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(FakeWebSocket.instances[1].url).toContain("ticket=wst_2"); // тикет одноразовый — не переиспользован

    client.close();
  });

  it("close code 4403 (user deactivated) logs out and does NOT reconnect", async () => {
    const client = new WsClient(() => {});
    await client.connect();
    const ws = FakeWebSocket.instances[0];
    ws.readyState = FakeWebSocket.OPEN;
    ws.onopen?.();

    ws.onclose?.({ code: 4403 });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(FakeWebSocket.instances).toHaveLength(1); // реконнекта не было
    expect(ticketCalls()).toHaveLength(1);
    expect(useSessionStore.getState().user).toBeNull(); // сессия сброшена → /login
  });

  it("догон различает первое подключение и ВОЗВРАТ связи", async () => {
    /*
     * ЗАЧЕМ ЭТО ОТДЕЛЬНО ПРОВЕРЯТЬ. Догон после возврата связи умеет забрать
     * то, что клиент написал в минуту обрыва, только если ему сказали, что
     * связь именно ВОЗВРАЩАЕТСЯ. Первый коннект догонять нечего — кэш только
     * что собран REST-запросами, и лишний перезапрос на каждом входе это
     * тринадцать вкладок, дружно дёргающих api по утрам.
     *
     * Признак считается из `attempt`, а его обнуляет тот же `onopen`. Порядок
     * двух строк — вся разница между «работает» и «молча ничего не делает»,
     * и снаружи она не видна ничем, кроме этой проверки.
     */
    const client = new WsClient(() => {});
    await client.connect();

    const first = FakeWebSocket.instances[0];
    first.readyState = FakeWebSocket.OPEN;
    first.onopen?.();

    expect(vi.mocked(catchUpAfterReconnect).mock.calls).toEqual([[false]]);

    // Обрыв (рестарт api на выкатке — 1012) и возврат по backoff.
    first.onclose?.({ code: 1012 });
    await vi.advanceTimersByTimeAsync(2000);

    const second = FakeWebSocket.instances[1];
    second.readyState = FakeWebSocket.OPEN;
    second.onopen?.();

    expect(vi.mocked(catchUpAfterReconnect).mock.calls).toEqual([[false], [true]]);

    /*
     * ⚠ И ВОЗВРАТ ЗАСЧИТАН (разбор 03.09). Точка связи в рейке показывает
     * «Связь нестабильна», когда канал возвращался трижды за десять минут, —
     * иначе человек читает свои обрывы как «сайт стал в пять раз медленнее» и
     * жалуется на службу. Считает возвраты ровно этот `onopen`; без строки
     * счётчик всегда ноль, а экран об этом молчит — диверсия показала, что
     * без этой проверки пропажу проводки не ловит ничто.
     */
    expect(useConnectionStore.getState().reconnects).toHaveLength(1);

    client.close();
  });

  it("failed ticket request goes into backoff and retries", async () => {
    fetchMock.mockImplementationOnce(async () => jsonResponse(500, { error: { code: "internal_error", message: "boom" } }));

    const client = new WsClient(() => {});
    await client.connect();

    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(useConnectionStore.getState().status).toBe("reconnecting");

    await vi.advanceTimersByTimeAsync(2000);
    expect(FakeWebSocket.instances).toHaveLength(1); // вторая попытка удалась

    client.close();
  });
});
