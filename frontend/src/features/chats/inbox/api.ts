import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { http } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import type {
  ConversationDto,
  ConversationsPage,
  InboxClaimResponse,
  InboxCountDto,
  InboxDeclineResponse,
  InboxReleaseResponse,
} from "@/shared/api/types";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";

/**
 * Очередь «Входящие» (7.1): REST + правки кэша списка очереди.
 *
 * Очередь — отдельный ресурс, а не ещё один фильтр `/conversations`: у неё
 * своя выборка (ничей диалог, ждущий решения), свой порядок (`queued_at ASC` —
 * кто дольше ждёт, тот выше; в списке диалогов порядок обратный) и своя судьба
 * строки (приняли — она исчезает у всех тринадцати сразу). Поэтому и ключ кэша
 * свой: события очереди не должны задевать вкладки «Мои/Все/Новые/Закрытые», а
 * `invalidate` этих вкладок — трогать очередь.
 */

const PAGE_LIMIT = 50;

type InboxCache = InfiniteData<ConversationsPage>;

/**
 * `GET /inbox` — страница очереди. Как и `fetchConversations`, засеивает
 * счётчики: unread — потому что строки очереди рисуются тем же компонентом,
 * счётчик очереди — потому что сервер здесь единственная истина по `total`
 * (страниц может быть больше одной, а бейдж вкладки показывает всю очередь).
 */
export async function fetchInboxQueue(
  offset: number,
): Promise<ConversationsPage> {
  const page = await http.get<ConversationsPage>(
    `/inbox?limit=${PAGE_LIMIT}&offset=${offset}`,
  );
  useUnreadStore.getState().seedFromRows(page.items);
  // offset>0 — догрузка: id прежних страниц сохраняем, иначе состав «схлопнется».
  const known = offset === 0 ? [] : Object.keys(useInboxStore.getState().ids);
  const ids = [...known, ...page.items.map((r) => r.id)];
  /**
   * «Никто не берёт» считаем по строкам только тогда, когда вся очередь у нас
   * на руках. Иначе это была бы цифра одной страницы, выданная за итог, — а
   * `GET /inbox/count` при каждом подключении сокета отдаёт точную. Ошибиться
   * тут значит сказать «брошенных нет», когда они есть.
   */
  const whole = offset === 0 && page.items.length >= page.page.total;
  useInboxStore
    .getState()
    .seedFromServer(
      ids,
      page.page.total,
      whole
        ? page.items.filter((r) => r.escalated).map((r) => r.id)
        : undefined,
    );
  return page;
}

/**
 * `GET /inbox/count` — только счётчики, без строк.
 *
 * Отдельная ручка нужна потому, что бейдж живёт дольше открытого списка: он
 * висит на вкладке, когда открыты «Мои», показывается в заголовке браузера и
 * на иконке рейки — то есть и тогда, когда экран чатов вообще не смонтирован.
 * И он же чинит счётчик после reconnect'а: пропущенные кадры очереди
 * инкрементами уже не догнать (01 §11.7).
 */
export function fetchInboxCount(): Promise<InboxCountDto> {
  return http.get<InboxCountDto>("/inbox/count");
}

/**
 * Сверить счётчик с сервером. Зовётся при каждом подключении сокета — и на
 * первом, и после разрыва. Ошибку глотаем намеренно: сверка счётчика не тот
 * повод, чтобы показывать человеку красную плашку, а следующее подключение
 * (или открытие вкладки «Входящие») её повторит.
 *
 * Спрашиваем только у тех, кто может принимать: наблюдателю и руководителю
 * очередь не показывается вовсе (7.1 п.1), и запрос был бы ради числа, которое
 * им негде увидеть.
 */
export async function refreshInboxCount(): Promise<void> {
  if (!useSessionStore.getState().permissions.includes("messages:send")) return;
  try {
    useInboxStore.getState().setCounters(await fetchInboxCount());
  } catch {
    // сверка best-effort — см. док-строку
  }
}

/** Принятия в полёте — по одному на диалог, общие для всех вызывающих. */
const принятияВПолёте = new Map<string, Promise<InboxClaimResponse>>();

/**
 * `POST /conversations/{id}/claim` — принять диалог. Атомарно на сервере:
 * из двух одновременных нажатий ровно одно получает 200, второе — `409
 * already_claimed` с `details.claimed_by`.
 *
 * ⚠ ОДИН ЗАПРОС НА ДИАЛОГ, ПОКА ОН В ПОЛЁТЕ (замер боя 14–15.09: 363 ответа
 * 409 за сутки, из них 357 — от того же человека через ~100 мс после его же
 * успешного принятия). У принятия три пути — клавиша, кнопка, первый набранный
 * символ в поле, — и каждый держал свой признак «уже шлю»; между ними
 * второй запрос улетал раньше, чем ответ первого доехал до кэша. Гонка с
 * самим собой безвредна, но красит консоль в красное и пугает владельца.
 * Второй вызов получает тот же promise, что и первый.
 */
export function claimConversation(convId: string): Promise<InboxClaimResponse> {
  const вПолёте = принятияВПолёте.get(convId);
  if (вПолёте) return вПолёте;
  const запрос = http
    .post<InboxClaimResponse>(
      `/conversations/${encodeURIComponent(convId)}/claim`,
    )
    .finally(() => {
      принятияВПолёте.delete(convId);
    });
  принятияВПолёте.set(convId, запрос);
  return запрос;
}

/**
 * `POST /conversations/{id}/decline` — отказаться; диалог остаётся ждать других.
 * Отвечает телом, а не 204, ровно ради счётчика: очередь после отказа стала
 * короче только у меня, и точное её значение знает только сервер.
 */
export function declineConversation(
  convId: string,
): Promise<InboxDeclineResponse> {
  return http.post<InboxDeclineResponse>(
    `/conversations/${encodeURIComponent(convId)}/decline`,
  );
}

/**
 * `POST /conversations/{id}/release` — вернуть принятый диалог в очередь.
 *
 * Не то же, что «Отклонить»: отказ говорит «пусть возьмёт кто-то другой» ДО
 * принятия, а возврат — «я взял по ошибке» ПОСЛЕ. Ручка на сервере была с
 * самого начала, а нажать её было нечем.
 */
export function releaseConversation(
  convId: string,
): Promise<InboxReleaseResponse> {
  return http.post<InboxReleaseResponse>(
    `/conversations/${encodeURIComponent(convId)}/release`,
  );
}

/** Забрать отказ обратно (UX-аудит, docs/17 §Т7) — диалог вернётся в мою очередь. */
export function undoDeclineConversation(
  convId: string,
): Promise<InboxDeclineResponse> {
  return http.post<InboxDeclineResponse>(
    `/conversations/${encodeURIComponent(convId)}/decline/undo`,
  );
}

/* --------------------------------------------------------------------- *
 *  Кэш списка очереди — точечные правки без refetch (03 §3.3)
 * --------------------------------------------------------------------- */

/** Плоский список очереди из кэша; пусто — если вкладку ещё не открывали. */
export function inboxRows(): ConversationDto[] {
  return (
    queryClient
      .getQueryData<InboxCache>(qk.inbox.list)
      ?.pages.flatMap((p) => p.items) ?? []
  );
}

/**
 * Куда идти после решения по текущему диалогу (7.1 п.3): следующий в очереди
 * ПО КРУГУ. `null` — идти некуда. Считается ДО удаления строки, поэтому
 * «следующий» здесь означает именно соседа по списку, а не «первого попавшегося».
 *
 * ПОЧЕМУ ПО КРУГУ, А НЕ «СОСЕД СНИЗУ, ИНАЧЕ СВЕРХУ» (разбор боевой сборки от
 * 12 августа). Раньше здесь стояло `rows[idx + 1] ?? rows[idx - 1]`. На
 * последней строке очереди соседа снизу нет, срабатывал запасной вариант — и
 * «следующим» оказывался ПРЕДЫДУЩИЙ. Из предыдущего «следующим» снова
 * становился последний: два нижних диалога перекидывали оператора друг другу,
 * а до начала очереди кнопкой было не дойти вовсе. Разбор нашёл это на живом
 * проде; тест `InboxNextRing` держит именно полный обход, а не один шаг —
 * пинг-понг проходит проверку «сделал шаг и попал в другой диалог».
 *
 * ЕДИНСТВЕННЫЙ В ОЧЕРЕДИ — ЭТО `null`, А НЕ ОН САМ. Круг из одной строки
 * возвращал бы текущий диалог, то есть кнопка «Следующий» никуда бы не вела.
 * Прятать её в этом случае — работа `inboxAhead` ниже.
 *
 * ПОЧЕМУ КРУГ ВЕРЕН И ДЛЯ ПЕРЕХОДОВ ПОСЛЕ РЕШЕНИЯ. Эта же функция считает,
 * куда уйти после принятия, отказа и закрытия. Там текущий диалог из очереди
 * УХОДИТ, и «первый сверху» — ровно то, что нужно: круг замыкается на строке,
 * которой через миг не станет, а до неё стоит начало очереди.
 */
export function nextInboxId(currentId: string): string | null {
  const rows = inboxRows();
  const idx = rows.findIndex((r) => r.id === currentId);
  if (idx === -1) return rows.find((r) => r.id !== currentId)?.id ?? null;
  if (rows.length < 2) return null; // в очереди только он сам — идти некуда
  return rows[(idx + 1) % rows.length].id;
}

/**
 * Сколько диалогов очереди ЕЩЁ ждёт, кроме открытого, — число на кнопке
 * «Следующий (N)» и признак того, показывать ли её вообще.
 *
 * ПОЧЕМУ НЕ РАЗМЕР ОЧЕРЕДИ. На кнопке стоял общий счётчик из
 * `inboxStore.count`, поэтому при четырёх ждущих и одном из них открытом
 * кнопка обещала «Следующий (4)», хотя перейти можно было к трём. Обещание
 * кнопки — «столько ещё разбирать», и открытый диалог в него не входит:
 * оператор его уже разбирает.
 *
 * СЧИТАЕМ ПО СТОРУ, А НЕ ПО ЗАГРУЖЕННЫМ СТРОКАМ. Очередь постраничная, в кэше
 * лежит первая полусотня, а ждать может больше; `count` — то самое число,
 * которое сервер посчитал целиком (`GET /inbox`, `GET /inbox/count`, ответы
 * `claim`/`decline`). По строкам кэша вышло бы «осталось 49» при очереди в
 * сто двадцать.
 */
export function inboxAhead(currentId: string): number {
  const { count, ids } = useInboxStore.getState();
  return Math.max(0, count - (ids[currentId] ? 1 : 0));
}

/**
 * Порядок очереди — тот же, что у сервера (`services.inbox.list_inbox`):
 * наверху тот, кто ждёт дольше всех. Это не украшение, а суть очереди —
 * оператор жмёт «Принять» на верхней строке.
 *
 * «Ждёт» — глазами клиента: якорь `waiting_since` (тот же, от которого
 * тикает плашка в строке), а `offered_at` — запасной для строк без отметки
 * (владелец 14.09: список шёл по `offered_at`, и плашки стояли вразнобой —
 * 1 ч, 2 ч, 1 ч, 45 мин). Строки без обеих меток уходят в конец: поля нет у
 * диалогов, заведённых до 7.1, и подставлять им «ждёт с начала времён»
 * значило бы поднять их над живыми клиентами.
 */
function queueAnchor(row: ConversationDto): string | null {
  return row.waiting_since ?? row.offered_at ?? null;
}

function queuedBefore(a: ConversationDto, b: ConversationDto): boolean {
  const ta = queueAnchor(a);
  const tb = queueAnchor(b);
  if (!ta && !tb) return a.id < b.id;
  if (!ta) return false; // без ожидания — в конец
  if (!tb) return true;
  return ta === tb ? a.id < b.id : ta < tb;
}

/**
 * Новая строка очереди (`inbox:new`, `inbox:released`) на своё место по
 * ожиданию; дубли по id отбрасываются.
 *
 * Место считаем, а не приписываем в конец, из-за возврата в очередь: при
 * release сервер НЕ переставляет `offered_at` — клиент ждёт с того момента,
 * как впервые обратился, и возвращённый диалог обязан оказаться НАВЕРХУ.
 * Дописать его в хвост значило бы спрятать под низ списка ровно того клиента,
 * которого уже один раз не довели, — то самое «третьего никто не берёт»,
 * ради которого вся очередь и делалась.
 */
export function insertInboxRow(row: ConversationDto): void {
  queryClient.setQueryData<InboxCache>(qk.inbox.list, (old) => {
    // Кэша нет — создавать его кадром нельзя; записывать тоже нечего.
    if (!old || old.pages.length === 0) return undefined;
    const flat = old.pages.flatMap((p) => p.items);
    if (flat.some((r) => r.id === row.id)) return undefined; // дубль — кэш не трогаем

    // Первый, кто ждёт МЕНЬШЕ новичка, — перед ним и встаём.
    const at = flat.findIndex((r) => queuedBefore(row, r));
    if (at === -1) flat.push(row);
    else flat.splice(at, 0, row);

    // Пересобираем страницы прежней нарезкой; лишний элемент достаётся последней.
    let cursor = 0;
    const lastIdx = old.pages.length - 1;
    const pages = old.pages.map((p, i) => {
      const size = p.items.length + (i === lastIdx ? 1 : 0);
      const items = flat.slice(cursor, cursor + size);
      cursor += size;
      return { ...p, items, page: { ...p.page, total: p.page.total + 1 } };
    });
    return { ...old, pages };
  });
}

/**
 * Строка ушла из очереди (принята, отклонена мной, закрыта) — убрать отовсюду.
 * `total` правим на всех страницах: это одно и то же число из одного ответа, и
 * разъехавшись, оно врёт `getNextPageParam` — очередь либо не догрузится, либо
 * запросит несуществующую страницу.
 */
/** Точечное обновление строки очереди (см. patchRowEverywhere: раньше кэш
 * очереди не патчился вовсе — имя/последнее сообщение/пометки замерзали). */
export function patchInboxRow(
  convId: string,
  updater: (row: ConversationDto) => ConversationDto,
): void {
  queryClient.setQueryData<InboxCache>(qk.inbox.list, (old) => {
    if (!old) return old;
    let touched = false;
    const pages = old.pages.map((p) => {
      if (!p.items.some((r) => r.id === convId)) return p;
      touched = true;
      return {
        ...p,
        items: p.items.map((r) => (r.id === convId ? updater(r) : r)),
      };
    });
    // ⚠ НЕ ТРОГАЛИ — НЕ ЗАПИСЫВАЕМ. Возврат прежнего `old` для TanStack Query
    // не «ничего не делать»: он записывает данные и шлёт `success`, а тот
    // гасит пометку «устарело» и обнуляет часы свежести. Кадры по чужим
    // диалогам так «омолаживали» кэш очереди, и запланированная сверка не
    // случалась (разбор — в `applyWsEvent.patchRowEverywhere`).
    return touched ? { ...old, pages } : undefined;
  });
}

export function removeInboxRow(convId: string): void {
  queryClient.setQueryData<InboxCache>(qk.inbox.list, (old) => {
    if (!old) return old;
    if (!old.pages.some((p) => p.items.some((r) => r.id === convId)))
      return undefined;
    const pages = old.pages.map((p) => ({
      ...p,
      items: p.items.filter((r) => r.id !== convId),
      page: { ...p.page, total: Math.max(0, p.page.total - 1) },
    }));
    return { ...old, pages };
  });
}

/*
 * Запросов разгрузки очереди здесь больше нет (требование владельца от 11
 * августа, №8): `GET /inbox/stale` и `POST /inbox/close-stale` удалены и на
 * сервере. Обычное закрытие диалога с ними ничего не делило — оно идёт через
 * `PATCH /conversations/{id}/status` в features/chats/api.ts.
 */
