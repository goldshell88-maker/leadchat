import { clearFeed, watchConnectionGaps } from "@/features/feed/store";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { applyWsEvent, clearPendingMessagePatches, забытьДогоны } from "./applyWsEvent";
import { useConnectionStore } from "./connectionStore";
import { clearCursors } from "./cursorRegistry";
import { forgetViewerWarnings } from "./applyWsEvent";
import { useViewersStore } from "./viewersStore";
import { WsClient } from "./WsClient";
import { startQuietResync } from "./quietResync";

/**
 * Жизненный цикл singleton-клиента (03 §3.1): connect() после логина,
 * close() при logout. Скрытая вкладка сокет НЕ отключает — менеджер должен
 * слышать звук со свёрнутой вкладкой; заморозку мобильного браузера чинит догон.
 */
let client: WsClient | null = null;
let unwatchActive: (() => void) | null = null;
let unwatchGaps: (() => void) | null = null;
let unwatchResync: (() => void) | null = null;

/**
 * Подписка на открытый диалог (SCEN-48) едет ОТСЮДА, а не с экрана чатов.
 *
 * Какой диалог открыт, уже знает `chatUiStore.activeConversationId` — то же
 * поле читают заголовок вкладки, горячие клавиши и правило «не звенеть в
 * открытом диалоге». Заводить рядом второй источник той же правды значило бы
 * однажды получить «подписан на один диалог, показываю другой»; а живёт связка
 * здесь, потому что кадр `subscribe` — часть жизненного цикла сокета: его надо
 * повторять после каждого обрыва, и знает об этом только клиент сокета.
 */
function watchActiveConversation(c: WsClient): () => void {
  c.subscribe(useChatUiStore.getState().activeConversationId);
  return useChatUiStore.subscribe((s, prev) => {
    if (s.activeConversationId === prev.activeConversationId) return;
    // Состав ПОКИНУТОГО диалога забываем сразу: сервер пришлёт новый состав
    // только тем, кто в диалоге остался, — а у нас остался бы последний
    // известный, и вернувшись в диалог человек увидел бы позавчерашних соседей.
    if (prev.activeConversationId) useViewersStore.getState().forget(prev.activeConversationId);
    c.subscribe(s.activeConversationId);
  });
}

export function startRealtime(): void {
  if (client) return;
  client = new WsClient(applyWsEvent);
  unwatchActive = watchActiveConversation(client);
  /*
   * Пропуски в живой ленте видит только тот, у кого рвётся сокет, — значит
   * следить за ними надо здесь, рядом с самим сокетом, а не на экране ленты.
   * Экран открывают раз в день, а обрывы случаются весь день: заведи слежение
   * в компоненте — и лента честно рассказывала бы ровно про те обрывы,
   * которые человек и так видел своими глазами.
   */
  unwatchGaps = watchConnectionGaps();
  /*
   * ⚠ ТИХАЯ СВЕРКА — ВТОРОЙ ЗАМОК К ДОГОНУ ПОСЛЕ ОБРЫВА (28.08).
   *
   * Догон держится на допущении «сокет жив, значит кэш верен», а кадр теряется
   * и без обрыва: публикация в Pub/Sub без подтверждения, кадр без нужного
   * поля, придержанные таймеры скрытой вкладки. Человеку в этих случаях
   * остаётся F5 — и он его жмёт, теряя черновик и место в ленте. Довод целиком
   * — в шапке `quietResync`.
   */
  unwatchResync = startQuietResync();
  void client.connect();
}

export function stopRealtime(): void {
  unwatchActive?.();
  unwatchActive = null;
  unwatchGaps?.();
  unwatchGaps = null;
  unwatchResync?.();
  unwatchResync = null;
  client?.close();
  client = null;
  clearCursors();
  clearPendingMessagePatches();
  // Память о том, за какой меткой уже догоняли: за одним компьютером люди
  // работают по очереди, и чужие догоны новому человеку не наследуются.
  забытьДогоны();
  useUnreadStore.getState().clear();
  /**
   * Очередь гаснет вместе с непрочитанными, и по той же причине: оба счётчика
   * живут на кадрах сокета и принадлежат конкретному человеку. Забыть очередь
   * здесь — значит оставить `[3] LeadChat` в заголовке вкладки на экране входа
   * после выхода (и после принудительного разлогина кадром 4403, который тоже
   * приходит сюда): ушедший сотрудник уносит с собой своё число.
   */
  useInboxStore.getState().clear();
  // Зрители — по той же причине и с той же строгостью: список живёт на кадрах
  // сокета, а на одном компьютере люди работают по очереди (11 §2.5).
  useViewersStore.getState().clear();
  // И память о том, кого видели и о ком предупреждали: за одним компьютером
  // люди работают по очереди, и новому человеку прошлые соседи не новость.
  forgetViewerWarnings();
  /*
   * Живая лента гаснет здесь же, и это не уборка «за компанию». В ней лежат
   * имена клиентов, суммы разговоров по каналам и то, кто из сотрудников
   * отошёл, — то есть содержимое смены, которая только что закончилась.
   * Оставить её на экране входа значило бы показать всё это следующему, кто
   * сядет за тот же компьютер.
   */
  clearFeed();
  useConnectionStore.setState({ status: "idle", lastEventAt: null });
}

/**
 * «Печатает…» для коллег в открытом диалоге (01 §11.4).
 *
 * ⚠ НЕ ПОДКЛЮЧЕНО. Функцию не зовёт ни один компонент, и приёмной стороны на
 * фронте тоже нет: `applyWsEvent` роняет кадр `typing` в ветку `default`.
 * Серверная половина при этом рабочая — хаб ретранслирует кадр всем, кто
 * подписан на диалог. Оставлено намеренно: это готовая половина протокола, а
 * не мусор; чтобы возможность заработала, нужны вызов отсюда из композера и
 * ветка `case "typing"` в разборе событий.
 */
export function sendTyping(conversationId: string): void {
  client?.typing(conversationId);
}
