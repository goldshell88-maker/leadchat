import { create } from "zustand";
import { useSessionStore } from "@/shared/stores/sessionStore";
import type { WsInboxEvent } from "@/shared/realtime/applyWsEvent";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import type { WsServerEvent } from "@/shared/realtime/wsEvents";
import { describeFrame, describeGap, type FeedDraft, type FeedEntry, type FeedGroup } from "./describe";
import { clearDirectory, conversationFacts } from "./directory";

/**
 * ЖИВАЯ ЛЕНТА РАБОТЫ СИСТЕМЫ — хранилище окна «прямо сейчас».
 *
 * ЧТО ЭТО ЗА ЭКРАН. Владелец просил видеть работу системы в реальном времени:
 * пришло сообщение, диалог взят, ответ доставлен или нет, канал отвалился,
 * бот ответил. Раньше единственным ответом на такой вопрос был `docker logs`
 * на проде — то есть ответ, доступный одному человеку в проекте.
 *
 * ЛЕНТА ЖИВЁТ ТОЛЬКО В ПАМЯТИ ВКЛАДКИ, И ЭТО РЕШЕНИЕ, А НЕ ЭКОНОМИЯ. История
 * действий уже есть — журнал аудита (`/settings/team`, вкладка журнала) и
 * центр уведомлений; они переживают перезагрузку и умеют искать по периоду.
 * Лента отвечает на другой вопрос — «что происходит в эту минуту», и хранить
 * её на сервере значило бы завести третье место с теми же событиями, которое
 * разойдётся с двумя первыми.
 *
 * ПОЧЕМУ КОПИТЬ НАЧИНАЕМ ДО ОТКРЫТИЯ ЭКРАНА. Кадры приходят в приложение с
 * момента входа, а не с момента, когда человек нажал «Живая лента». Пиши мы
 * только при открытом экране, руководитель, зашедший посмотреть на всплеск,
 * увидел бы пустоту и ждал бы у пустого экрана следующего события — при том
 * что предыдущие тридцать были прямо перед этим. Поэтому запись включена
 * всегда, а сам экран — ленивый кусок.
 *
 * ПРЕДЕЛ ПАМЯТИ ОБЯЗАТЕЛЕН. Вкладку держат открытой сутками, а при десяти
 * тысячах сообщений в сутки лента без предела — это утечка с гарантией.
 * Держим последние `FEED_LIMIT` строк; в строке лежит ГОТОВЫЙ ТЕКСТ, а не
 * исходный кадр, — иначе вместе с событием жили бы вложения, тела сообщений и
 * целые строки диалогов.
 */

/**
 * Сколько строк держим. Триста — это примерно час обычной работы двух каналов
 * и заведомо больше, чем помещается на экране; при этом весь буфер — десятки
 * килобайт коротких строк.
 */
export const FEED_LIMIT = 300;

export interface FeedFilters {
  /** `null` — все каналы. */
  accountId: string | null;
  /** `null` — все виды событий. */
  group: FeedGroup | null;
}

export interface ChannelOption {
  id: string;
  title: string;
}

interface FeedState {
  entries: FeedEntry[];
  /**
   * Каналы, о которых лента уже что-то говорила, — пункты отбора «Канал ▾».
   *
   * ЖИВУТ ОТДЕЛЬНО ОТ СТРОК НАМЕРЕННО. Собери список из `entries` — и он начнёт
   * худеть вместе с ними: канал, по которому последний час было тихо,
   * вывалится из отбора ровно тогда, когда по нему захотят посмотреть, а
   * выбранный пункт исчезнет у человека из-под курсора. Каналов у нас единицы,
   * расти этому списку некуда.
   *
   * СПИСОК БЕРЁТСЯ ИЗ ПОТОКА, А НЕ С СЕРВЕРА. Сервер уже решил, какие кадры
   * этот человек получает (кадры очереди сужены `eligible_operator_ids`, см.
   * `app/ws/hub.py`). Спросив список аккаунтов у API, мы предложили бы в отборе
   * каналы, событий которых у вкладки не будет никогда, — то есть пункт с
   * гарантированно пустым экраном.
   */
  channels: ChannelOption[];
  /** Номер следующей строки. Растёт всегда — по нему считается «сколько новых». */
  nextSeq: number;
  filters: FeedFilters;
  /**
   * Прокрутка остановлена: показываем ленту такой, какой она была на момент
   * паузы. `null` — лента бежит.
   *
   * Хранится НОМЕР строки, а не снимок массива: снимок пришлось бы держать
   * рядом с основным буфером, то есть удвоить память ровно в тот момент, когда
   * человек отошёл читать и забыл снять паузу.
   */
  pausedAtSeq: number | null;

  setFilters(patch: Partial<FeedFilters>): void;
  pause(): void;
  resume(): void;
  clear(): void;
}

export const useFeedStore = create<FeedState>()((set) => ({
  entries: [],
  channels: [],
  nextSeq: 1,
  filters: { accountId: null, group: null },
  pausedAtSeq: null,

  setFilters: (patch) => set((s) => ({ filters: { ...s.filters, ...patch } })),
  pause: () => set((s) => (s.pausedAtSeq === null ? { pausedAtSeq: s.nextSeq - 1 } : {})),
  resume: () => set({ pausedAtSeq: null }),
  /*
   * Отборы гасятся вместе со строками, и это не «заодно». `clear` зовётся при
   * выходе — за компьютер садится другой человек (11 §2.5). Оставленный отбор
   * «Канал: Парт-7» встретил бы его пустым экраном, на котором лента выглядит
   * сломанной: событий нет, а почему — написано мелким шрифтом в поле, куда он
   * не смотрел. К тому же сам список каналов к этому моменту уже обнулён, и
   * отбор указывал бы на пункт, которого в нём нет.
   */
  clear: () =>
    set({
      entries: [],
      channels: [],
      nextSeq: 1,
      pausedAtSeq: null,
      filters: { accountId: null, group: null },
    }),
}));

/** Пополнить список каналов, не трогая ссылку на массив, когда пополнять нечем. */
function withChannel(known: ChannelOption[], draft: FeedDraft): ChannelOption[] {
  if (!draft.accountId || !draft.accountTitle) return known;
  if (known.some((c) => c.id === draft.accountId)) return known;
  return [...known, { id: draft.accountId, title: draft.accountTitle }].sort((a, b) =>
    a.title.localeCompare(b.title, "ru"),
  );
}

/** Положить готовую строку в буфер (свежие — в начале). */
function push(draft: FeedDraft): void {
  useFeedStore.setState((s) => {
    const entry: FeedEntry = { ...draft, seq: s.nextSeq };
    const entries = [entry, ...s.entries];
    // Обрезаем на каждой вставке, а не «когда-нибудь»: одно присваивание длины
    // на событие дешевле любого сторожа и не даёт буферу вырасти даже на
    // секунду. Обрезаем ХВОСТ: свежие строки лежат в начале.
    if (entries.length > FEED_LIMIT) entries.length = FEED_LIMIT;
    return { entries, channels: withChannel(s.channels, draft), nextSeq: s.nextSeq + 1 };
  });
}

/**
 * Ведём ли мы ленту для ЭТОГО человека.
 *
 * Экран показывается тем же, кому показывается статистика (`stats:all` —
 * администратор и руководитель), и держать буфер оператору не за чем: открыть
 * он его не сможет, а память вкладки у него та же самая, и работает он в ней
 * всю смену. Право спрашиваем на каждом кадре, а не один раз: роль человека
 * может смениться в этой же вкладке (`/auth/me` после смены — 01 §3.4).
 */
function feedIsWatched(): boolean {
  return useSessionStore.getState().permissions.includes("stats:all");
}

/**
 * Кадр WS → строка ленты. Зовётся из `applyWsEvent` — единственного места, куда
 * приходят все кадры сокета.
 *
 * ВТОРОГО СОЕДИНЕНИЯ НЕТ И НЕ БУДЕТ: лента слушает тот же сокет, что и всё
 * остальное. Своё соединение означало бы второй тикет, второй heartbeat, вторую
 * очередь переподключения — и, что хуже всего, второй набор кадров, который
 * пришлось бы отдельно фильтровать по правам.
 */
export function recordWsFrame(e: WsServerEvent | WsInboxEvent): void {
  if (!feedIsWatched()) return;
  const draft = describeFrame(e);
  if (draft) push(draft);
}

/**
 * СЛЕЖЕНИЕ ЗА ОБРЫВАМИ СВЯЗИ.
 *
 * Пока сокет переподключается, события не пропадают у системы — они пропадают
 * у ЭТОЙ ленты: догон после реконнекта (`catchUpAfterReconnect`) восстанавливает
 * списки и счётчики, но не кадры, а лента живёт именно кадрами. Разница видна
 * на экране только если о ней сказать: без строки о пропуске тихие две минуты
 * обрыва выглядят точно так же, как тихие две минуты ночи.
 *
 * Возвращает отписку — её держит `startRealtime`/`stopRealtime`, симметрично
 * подписке на открытый диалог.
 */
export function watchConnectionGaps(): () => void {
  let lostAt: number | null = null;
  return useConnectionStore.subscribe((s, prev) => {
    if (prev.status === "open" && s.status !== "open" && s.status !== "idle") {
      lostAt = Date.now();
      return;
    }
    if (s.status === "open" && lostAt !== null) {
      const from = lostAt;
      lostAt = null;
      // Про моргание в доли секунды не сообщаем: за него ничего не успевает
      // произойти, а строка о пропуске — тревожная и обязана что-то значить.
      if (Date.now() - from < 1500) return;
      if (!feedIsWatched()) return;
      push(describeGap(from, Date.now()));
    }
  });
}

/** Забыть ленту вместе со справочником имён — при выходе (11 §2.5). */
export function clearFeed(): void {
  useFeedStore.getState().clear();
  clearDirectory();
}

/**
 * Проходит ли строка через отбор.
 *
 * СТРОКИ О ПРОПУСКЕ ОТБОРУ НЕ ПОДЧИНЯЮТСЯ, и это не недосмотр. Отбор сужает
 * ленту, а строка о пропуске говорит, что лента НЕПОЛНАЯ; спрятать её значит
 * превратить дырявую запись в гладкую на вид — ровно та ложь, ради устранения
 * которой строка и заведена.
 *
 * КАНАЛ СТРОКИ ДОБИРАЕТСЯ ТАК ЖЕ ЛЕНИВО, КАК ЕГО ПОДПИСЬ (проверка 24.09).
 * `message:new` приходит раньше `inbox:new` и канала не несёт: строка «Ольга:
 * новое сообщение» стояла на экране с подписью «Парт-7» (её `feedLine` берёт
 * из справочника при отрисовке), а отбор «Парт-7» её прятал — он читал только
 * то, что было известно в миг кадра.
 */
export function matchesFilters(entry: FeedEntry, filters: FeedFilters): boolean {
  if (entry.gap) return true;
  if (filters.group && entry.group !== filters.group) return false;
  if (filters.accountId) {
    const accountId =
      entry.accountId ?? (entry.conversationId ? conversationFacts(entry.conversationId).accountId : null);
    if (accountId !== filters.accountId) return false;
  }
  return true;
}

/** Что показать на экране с учётом паузы и отбора. */
export function visibleEntries(
  entries: FeedEntry[],
  filters: FeedFilters,
  pausedAtSeq: number | null,
): FeedEntry[] {
  return entries.filter(
    (e) => (pausedAtSeq === null || e.seq <= pausedAtSeq) && matchesFilters(e, filters),
  );
}

/**
 * Сколько событий накопилось за паузой — с учётом отбора.
 *
 * Именно с учётом: человек, отобравший один канал, не должен видеть «12 новых»
 * и получить после снятия паузы две строки. Обещанное число обязано совпасть с
 * тем, что появится.
 */
export function pendingCount(
  entries: FeedEntry[],
  filters: FeedFilters,
  pausedAtSeq: number | null,
): number {
  if (pausedAtSeq === null) return 0;
  return entries.filter((e) => e.seq > pausedAtSeq && matchesFilters(e, filters)).length;
}
