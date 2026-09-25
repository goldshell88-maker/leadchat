import { http } from "@/shared/api/http";
import type { WsTicketResponse } from "@/shared/api/types";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { catchUpAfterReconnect } from "./applyWsEvent";
import { useConnectionStore, отметитьВозвратСвязи } from "./connectionStore";
import type { WsServerEvent } from "./wsEvents";

/**
 * WebSocket-клиент (03 §3.1, контракт 01 §11).
 *
 * Подключение по одноразовому тикету (POST /ws/ticket): нативный WebSocket не
 * умеет Authorization-заголовок, а долгоживущий JWT в query string просочился
 * бы в логи. Heartbeat: клиент шлёт {"type":"ping"} каждые 25 с; нет pong'а за
 * 10 с → соединение мёртвое, закрываем — onclose реконнектит (ловит
 * «полумёртвые» соединения за NAT). Reconnect: экспоненциальный backoff
 * 1s → 2s → … → 30s c full jitter, перед каждой попыткой — новый тикет.
 * Серверные коды закрытия: 4401 (битый тикет) / 4408 (таймаут) / 1012
 * (рестарт) — обычный reconnect; 4403 — пользователь деактивирован → logout.
 */

const PING_INTERVAL_MS = 25_000;
const PONG_TIMEOUT_MS = 10_000;
const MAX_BACKOFF_MS = 30_000;

export class WsClient {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closedByUser = false;
  private pingTimer: ReturnType<typeof setInterval> | undefined;
  private pongWatchdog: ReturnType<typeof setTimeout> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  /**
   * Диалог, на который подписан ЭТОТ клиент, — не «последний отправленный
   * кадр», а намерение. Хранится потому, что сервер про подписку забывает при
   * каждом обрыве: сессия после реконнекта новая (см. `Session` в app/ws/hub),
   * и без повторного кадра `subscribe` человек молча перестал бы и получать
   * состав зрителей, и попадать в него сам — то есть починка SCEN-48
   * разваливалась бы от первого же моргания сети.
   */
  private subscribedTo: string | null = null;

  constructor(private readonly onEvent: (e: WsServerEvent) => void) {}

  /**
   * Отправить кадр, если сокет открыт (01 §11.4).
   *
   * Молча выходим, когда закрыт: все три кадра клиента (ping/subscribe/typing)
   * — про «сейчас», и очередь на отправку после реконнекта доставила бы
   * протухшее. Подписку восстанавливает `onopen` — она единственная, у кого
   * есть смысл вне момента.
   */
  send(frame: { type: string; data?: unknown }): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify(frame));
  }

  /** Открыт диалог (или закрыт — тогда `null`): сообщаем серверу. */
  subscribe(conversationId: string | null): void {
    this.subscribedTo = conversationId;
    this.send({ type: "subscribe", data: { conversation_id: conversationId } });
  }

  /** «Печатает…» для коллег в этом же диалоге (01 §11.4). */
  typing(conversationId: string): void {
    this.send({ type: "typing", data: { conversation_id: conversationId } });
  }

  async connect(): Promise<void> {
    this.closedByUser = false;
    useConnectionStore.setState({ status: this.attempt ? "reconnecting" : "connecting" });
    try {
      const { ticket } = await http.post<WsTicketResponse>("/ws/ticket");
      // В Tauri origin = tauri://localhost — база берётся из VITE_API_BASE (04 §1.1).
      const base = (import.meta.env.VITE_API_BASE as string | undefined) ?? location.origin;
      // Upgrade-эндпоинт бэкенда — /api/v1/ws (01 §11 упоминает /api/ws, но
      // реализация и nginx-location закреплены на /api/v1/ws — заметка бэкенда).
      const url = `${base.replace(/^http/, "ws")}/api/v1/ws?ticket=${encodeURIComponent(ticket)}`;
      const ws = new WebSocket(url);
      this.ws = ws;

      ws.onopen = () => {
        // Считать ДО обнуления: `attempt` растит только scheduleReconnect, то
        // есть ненулевой он ровно тогда, когда связь ВОЗВРАЩАЕТСЯ. Догон без
        // этого признака не мог отличить первый коннект от возврата и на
        // возврате после тихого периода не делал ничего (см. catchUpAfterReconnect).
        const resumed = this.attempt > 0;
        this.attempt = 0;
        useConnectionStore.setState({ status: "open", lastFrameAt: Date.now() });
        // Возврат, а не первое открытие: считаем, чтобы показать человеку,
        // что рвётся ЕГО канал (см. `отметитьВозвратСвязи`).
        if (resumed) отметитьВозвратСвязи();
        this.startHeartbeat();
        // Подписка живёт в сессии СЕРВЕРА, а сессия умирает вместе с сокетом.
        // Поэтому её восстанавливаем на каждом открытии, а не только на
        // возврате: первый коннект приходит уже с открытым диалогом, если
        // человек зашёл по прямой ссылке /chats/:id.
        if (this.subscribedTo !== null) {
          this.send({ type: "subscribe", data: { conversation_id: this.subscribedTo } });
        }
        void catchUpAfterReconnect(resumed); // ВСЕГДА, даже при первом коннекте (03 §3.1)
      };

      ws.onmessage = (ev: MessageEvent) => {
        let e: WsServerEvent;
        try {
          e = JSON.parse(ev.data as string) as WsServerEvent;
        } catch {
          return; // битый кадр — игнорируем, сокет живёт (01 §11.4)
        }
        if (e.type === "pong") {
          clearTimeout(this.pongWatchdog);
          return;
        }
        // Метка по ЛОКАЛЬНЫМ часам: по ней сторож фона понимает, что сокет
        // жив, но молчит (см. `lastFrameAt`).
        useConnectionStore.setState({ lastFrameAt: Date.now() });
        if (e.ts) useConnectionStore.setState({ lastEventAt: e.ts });
        this.onEvent(e);
      };

      ws.onclose = (ev: CloseEvent) => {
        if (ev.code === 4403) {
          // Пользователь деактивирован/разлогинен (01 §11.7) — уйти на /login.
          this.stopTimers();
          void useSessionStore.getState().logout();
          return;
        }
        this.scheduleReconnect(); // 4401/4408/1012 и любой обрыв — обычный reconnect
      };

      ws.onerror = () => this.ws?.close();
    } catch {
      this.scheduleReconnect(); // не выдали тикет (нет сети/401) — тоже backoff
    }
  }

  /**
   * Вкладка вернулась на глаза — подтверждаем соединение немедленно.
   *
   * ⚠ ЗАЧЕМ (замер боя 27.08). Браузер тормозит таймеры фоновой вкладки: ping
   * раз в 25 секунд превращается в раз в минуту и дольше. Сервер до 27.08 рвал
   * соединение после 60 секунд тишины — отсюда 357 переподключений за сутки на
   * 24 человек и медиана жизни соединения в 298 секунд. Серверный запас поднят
   * до трёх минут, но вернувшаяся вкладка не должна ЖДАТЬ следующего тика: и
   * присутствие («на месте» у коллег), и живость сокета подтверждаются одним
   * кадром, который дешевле любого догона.
   *
   * Слушатель снимается в `stopTimers`, как и сами таймеры: без этого каждое
   * переподключение вешало бы ещё один.
   */
  private вернуласьВкладка = (): void => {
    if (typeof document === "undefined" || document.visibilityState !== "visible") return;
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send('{"type":"ping","data":{"n":0}}');
    clearTimeout(this.pongWatchdog);
    this.pongWatchdog = setTimeout(() => this.ws?.close(), PONG_TIMEOUT_MS);
  };

  private startHeartbeat(): void {
    clearInterval(this.pingTimer);
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.вернуласьВкладка);
      document.addEventListener("visibilitychange", this.вернуласьВкладка);
    }
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState !== WebSocket.OPEN) return;
      this.ws.send('{"type":"ping","data":{"n":0}}');
      // Нет pong'а за 10 с → соединение мёртвое (01 §11.5); onclose реконнектит.
      clearTimeout(this.pongWatchdog);
      this.pongWatchdog = setTimeout(() => this.ws?.close(), PONG_TIMEOUT_MS);
    }, PING_INTERVAL_MS);
  }

  private stopTimers(): void {
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.вернуласьВкладка);
    }
    clearInterval(this.pingTimer);
    clearTimeout(this.pongWatchdog);
    clearTimeout(this.reconnectTimer);
  }

  private scheduleReconnect(): void {
    if (this.closedByUser) return;
    this.stopTimers();
    useConnectionStore.setState({ status: "reconnecting" });
    // Full jitter: 50 клиентов не ломятся одновременно после рестарта api (03 §3.1).
    const base = Math.min(1000 * 2 ** this.attempt, MAX_BACKOFF_MS);
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => void this.connect(), Math.random() * base);
  }

  close(): void {
    this.closedByUser = true;
    this.stopTimers();
    this.ws?.close();
    this.ws = null;
    useConnectionStore.setState({ status: "idle" });
  }
}
