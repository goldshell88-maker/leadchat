import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { сбросКлиента } from "@/features/chats/components/card/clientApi";
import {
  inboxRows,
  insertInboxRow,
  refreshInboxCount,
  patchInboxRow,
  removeInboxRow,
} from "@/features/chats/inbox/api";
import { playInboxChime } from "@/features/chats/inbox/sound";
import { recordWsFrame } from "@/features/feed/store";
import { applyNotifyEvent } from "@/features/notifications/wsNotify";
import { принятьСвойСтатус } from "@/features/presence/usePresence";
import { closeHandoffCard, toastForHandoff, toastForInbox } from "@/platform/toast";
import { http } from "@/shared/api/http";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import type { ConversationFilters } from "@/shared/api/queryKeys";
import type {
  ConversationDetailDto,
  ConversationDto,
  ConversationsPage,
  ItemRef,
  MessageDto,
  MessagesPage,
  UserRef,
} from "@/shared/api/types";
import { compareConversationRows } from "@/shared/lib/conversationOrder";
import { подписьСотрудника } from "@/shared/lib/подписьСотрудника";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { useConnectionStore } from "./connectionStore";
import { lastKnownCursor, rememberCursor } from "./cursorRegistry";
import { scheduleCountsReconcile, scheduleCountsRefetch, scheduleListRefetch } from "./listRefetch";
import { notifyAssignedToMe, notifyNewMessage } from "./notify";
import { дельтаНеОтвечено, ожиданиеПереключилось, сдвинутьСчёт } from "./tabCounts";
import { useViewersStore } from "./viewersStore";
import type { ConversationPatch, WsServerEvent } from "./wsEvents";
import { showToast } from "@/shared/ui/toast";

/**
 * События WS → кэш TanStack Query (03 §3.3). Никаких invalidateQueries на
 * каждое сообщение — при 10 000 входящих/сутки это недопустимо: только
 * setQueryData/setQueriesData; invalidate — лишь когда честнее спросить сервер
 * (диалог не найден в кэше, смена статуса двигает диалог между вкладками).
 */

/*
 * Своего компаратора здесь больше НЕТ (разбор от 12 августа).
 *
 * Он жил ровно тут и сортировал иначе, чем сервер: «непрочитанные сверху,
 * внутри них «негатив» первыми, затем свежее сверху» — без закреплённых и без
 * полного ключа. Пока по вкладке не проехало ни одного события, список стоял
 * в серверном порядке; первое же входящее сообщение перестраивало его в
 * другой, и закреплённые уезжали вниз. Правило теперь одно на всех —
 * `shared/lib/conversationOrder`.
 */

/** Ключи patch, требующие спец-обработки (вложенное слияние / unread_delta не поле строки). */
const NESTED_PATCH_KEYS = new Set(["unread_delta", "client", "account", "item", "assignee", "last_message"]);

/** Слить patch строки (01 §11.3): вложенные объекты (client, …) — частично. */
function mergeConversationPatch<T extends ConversationDto>(row: T, patch: ConversationPatch): T {
  const flat: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(patch)) {
    if (!NESTED_PATCH_KEYS.has(key)) flat[key] = value;
  }
  const next: T = { ...row, ...(flat as Partial<T>) };
  if (patch.client) next.client = { ...row.client, ...patch.client };
  if (patch.account) next.account = { ...row.account, ...patch.account };
  if (patch.item !== undefined) {
    next.item = patch.item === null ? null : ({ ...(row.item ?? {}), ...patch.item } as ItemRef);
  }
  if (patch.assignee !== undefined) next.assignee = patch.assignee;
  if (patch.last_message !== undefined) next.last_message = patch.last_message;
  return next;
}

interface RowUpdateResult {
  data: InfiniteData<ConversationsPage>;
  found: boolean;
}

/**
 * Обновить строку во flatten-виде и пересобрать страницы с той же нарезкой.
 *
 * ⚠ ПЕРЕСОРТИРОВКА — ТОЛЬКО КОГДА ПОРЯДОК МОГ ИЗМЕНИТЬСЯ (находка 23.08 №9).
 *
 * Раньше `flat.sort(...)` звался на КАЖДЫЙ кадр, и не для одного списка:
 * `patchRowEverywhere` применяет это ко всем закэшированным вариантам фильтров,
 * включая те, на которые никто сейчас не смотрит. А кадров много и они мелкие —
 * прочитано, набирает текст, сменился ответственный, — и почти ни один из них
 * порядка не меняет: он собран из закрепления, `last_message_at` и id.
 *
 * Цена была не в самой сортировке, а в том, что пересборка отдаёт НОВЫЕ массивы
 * страниц: React считает список изменившимся целиком и перерисовывает все
 * загруженные строки. Это и складывалось в «подвисания» на общем экране.
 *
 * Теперь ключи порядка сравниваются до и после: совпали — строка заменяется на
 * месте, прежние страницы переиспользуются, и перерисовывается одна строка.
 */
function updateRowInList(
  old: InfiniteData<ConversationsPage>,
  convId: string,
  updater: (row: ConversationDto) => ConversationDto,
): RowUpdateResult {
  const flat = old.pages.flatMap((p) => p.items);
  const idx = flat.findIndex((r) => r.id === convId);
  if (idx === -1) return { data: old, found: false };
  const прежняя = flat[idx];
  const новая = updater(прежняя);
  flat[idx] = новая;
  if (compareConversationRows(прежняя, новая) !== 0) flat.sort(compareConversationRows);
  let cursor = 0;
  const pages = old.pages.map((p) => {
    const items = flat.slice(cursor, cursor + p.items.length);
    cursor += p.items.length;
    return { ...p, items };
  });
  return { data: { ...old, pages }, found: true };
}

/**
 * Обновление строки во ВСЕХ закэшированных вариантах фильтров; true — нашлась
 * хоть где-то.
 *
 * ⚠ ГДЕ СТРОКИ НЕТ — КЭШ НЕ ТРОГАЕМ ВОВСЕ, И ЭТО ГЛАВНОЕ ЗДЕСЬ (жалоба
 * владельца 31.08: «взял диалоги, а во вкладке „Мои" их нет, появятся только
 * после обновления страницы»).
 *
 * ЧТО БЫЛО. При ненайденной строке апдейтер возвращал тот же `old`. Это не
 * `undefined`, поэтому TanStack Query всё равно записывал данные и слал
 * `success` — а тот ставит `isInvalidated: false` и двигает `dataUpdatedAt` на
 * «сейчас». То есть ЛЮБОЙ кадр по ЛЮБОМУ из шестнадцати каналов «омолаживал»
 * кэш вкладки, к которой не имел отношения.
 *
 * Дальше складывалось так: приём диалога помечал список «Моих» устаревшим, но
 * запрос этой вкладки в тот момент выключен (открыта очередь), поэтому
 * перезапроса не происходило — только пометка. Через доли секунды приходили
 * собственные широковещательные кадры принятия, гасили пометку и обнуляли часы
 * свежести. Человек жал «Мои» — запрос не нужен, `staleTime` 30 секунд ещё не
 * вышел, — и видел список без только что взятых диалогов. Помогала одна
 * перезагрузка: она стирает кэш целиком.
 *
 * В живую смену кадры идут чаще раза в тридцать секунд, поэтому кэш не
 * протухал НИКОГДА. Отсюда же и «плавучесть» беды: в тишине пометка доживала,
 * и список обновлялся сам.
 *
 * ПРОВЕРЕНО РУКАМИ на установленном query-core: после `invalidate`
 * `isInvalidated=true`; после холостой записи — `false`; с возвратом
 * `undefined` пометка доживает.
 */
function patchRowEverywhere(
  convId: string,
  updater: (row: ConversationDto) => ConversationDto,
): boolean {
  let anyFound = false;
  queryClient.setQueriesData<InfiniteData<ConversationsPage>>(
    { queryKey: CONVERSATIONS_LIST_KEY },
    (old) => {
      if (!old) return old;
      const { data, found } = updateRowInList(old, convId, updater);
      if (!found) return undefined; // ничего не меняли — и записывать нечего
      anyFound = true;
      return data;
    },
  );
  // Очередь «Входящих» — такой же список строк: без этой строки её кэш не
  // получал ни одного патча, и имя/последнее сообщение/пометки замерзали
  // до перезагрузки (аудит синхронизации 16.08).
  patchInboxRow(convId, updater);
  /*
   * ⚠ ЗДЕСЬ БОЛЬШЕ НЕТ ПОХОДА К СЕРВЕРУ, И ЭТО ГЛАВНАЯ ПРАВКА 06.09.
   *
   * До 06.09 строкой ниже стоял `scheduleListRefetch()` с умолчанием «спросить
   * числа» — то есть ЛЮБОЙ патч строки заказывал у каждой вкладки список плюс
   * числа. Довод 03.09 был верен по существу («число не может быть старше
   * строк»), но цену никто не мерил. Замер 06.09: `GET /conversations/counts`
   * 55–60 тыс. и `GET /conversations` 52–56 тыс. в сутки — 54–57 % ВСЕГО
   * трафика, в пик 47 запросов на человека в минуту, 344 ответа 429 с
   * офисного адреса и «Не получилось загрузить» у людей.
   *
   * Что спросить и спрашивать ли — решает `сверитьсяПослеПатча` по паре
   * строк ДО и ПОСЛЕ: строки нет или сменились статус/хозяин — список с
   * числами; ожидание сдвинулось у моего диалога — числа двигаются дельтой в
   * кэше; всё прочее (прочитано, метка, телефон клиента) сервера не касается.
   */
  return anyFound;
}

/** Первая пара «строка до / строка после» из всех кэшей — по ней решают, что спросить. */
interface Отпечаток {
  было: ConversationDto;
  стало: ConversationDto;
}

/**
 * Обернуть апдейтер так, чтобы запомнить ПЕРВУЮ пару «до/после». Апдейтер
 * зовётся на каждый закэшированный вариант фильтров, но строка везде одна и
 * та же, и первого вызова достаточно.
 */
function сОтпечатком(
  convId: string,
  updater: (row: ConversationDto) => ConversationDto,
): {
  updater: (row: ConversationDto) => ConversationDto;
  отпечаток: () => Отпечаток | null;
} {
  /*
   * ⚠ СЛЕД СНИМАЕМ С ТОЙ СТРОКИ, КОТОРУЮ ЧЕЛОВЕК ВИДИТ (правка 07.09).
   *
   * `patchRowEverywhere` обходит ВСЕ варианты кэша — по одному на каждый набор
   * фильтров, который человек открывал за сессию. Варианты грузятся с сервера
   * в разное время и между собой расходятся: строка во «Всех», снятых десять
   * минут назад, может нести другое ожидание, чем та же строка в «Моих» на
   * экране. След с первого попавшегося варианта — след с копии, на которую
   * никто не смотрит, и посчитанная по нему дельта двигала бы число мимо
   * видимого.
   *
   * Порядок обхода кэша сюда не приходит вовсе (`setQueriesData` даёт
   * обработчику только данные), поэтому активную строку берём отдельно — из
   * кэша активного набора фильтров — и след считаем по ней. Обход остаётся
   * прежним: патч ложится во все варианты.
   */
  const активная = активнаяСтрока(convId, updater);
  let первый: Отпечаток | null = null;
  return {
    updater: (row) => {
      const next = updater(row);
      if (первый === null) первый = { было: row, стало: next };
      return next;
    },
    // Активная строка главнее: она про то, что на экране. Первая попавшаяся —
    // запас на случай, когда активного варианта в кэше ещё нет (по прямой
    // ссылке список мог не грузиться вовсе).
    отпечаток: () => активная ?? первый,
  };
}

/** След по строке из кэша ТЕКУЩИХ фильтров, если она там есть. */
function активнаяСтрока(
  convId: string,
  updater: (row: ConversationDto) => ConversationDto,
): Отпечаток | null {
  const данные = queryClient.getQueryData<InfiniteData<ConversationsPage>>(
    qk.conversations.list(useChatUiStore.getState().filters),
  );
  for (const page of данные?.pages ?? []) {
    const row = page.items.find((r) => r.id === convId);
    if (row) return { было: row, стало: updater(row) };
  }
  return null;
}

/**
 * Могла ли строка переехать между вкладками (03 §2.3): сменился статус,
 * хозяин или получатель передачи. `хозяинВКадре` — `assignee_id` из
 * `message:new`: у строки в кэше может лежать устаревший ответственный, и
 * расхождение с кадром — тот же переезд, только увиденный раньше, чем приедет
 * `conversation:updated`.
 *
 * ⚠ ПОЛУЧАТЕЛЬ ПЕРЕДАЧИ — ТОЖЕ ПЕРЕЕЗД (ревью 06.09). Предложение передачи
 * ответственного НЕ меняет (двухфазность, 01 §5.5), а в «Мои» получателя
 * диалог входит с этой секунды — `mine_condition` сервера считает и
 * `transfer_to_id`, и число «на руках» вместе с ним. По одним статусу и
 * хозяину этот кадр выглядел бы как правка метки, и у получателя на вкладке
 * «Все» ни список «Моих», ни число не узнали бы о передаче до тихой сверки.
 */
function вкладкиМоглиСмениться(
  { было, стало }: Отпечаток,
  хозяинВКадре?: string | null,
): boolean {
  if (было.status !== стало.status) return true;
  const хозяинБыл = было.assignee?.id ?? null;
  const хозяинСтал = стало.assignee?.id ?? null;
  if (хозяинБыл !== хозяинСтал) return true;
  if ((было.transfer?.to.id ?? null) !== (стало.transfer?.to.id ?? null)) return true;
  return хозяинВКадре !== undefined && хозяинВКадре !== хозяинСтал;
}

/**
 * Смотрит ли кто-нибудь ПРЯМО СЕЙЧАС на срез «ждут ответа».
 *
 * Спрашиваем у кэша про наблюдателей, а не у стора фильтров: списков на экране
 * может быть несколько, а `useChatUiStore` знает лишь про свой. Наблюдатель —
 * это буквально «на это смотрят»: закрытая вкладка своего запроса не сделает.
 */
function открытСрезЖдущих(): boolean {
  return queryClient
    .getQueryCache()
    .findAll({ queryKey: CONVERSATIONS_LIST_KEY, type: "active" })
    .some((q) => (q.queryKey[2] as ConversationFilters | undefined)?.waitingOnly === true);
}

/**
 * Что спросить у сервера после патча строки — и спрашивать ли вообще.
 *
 * Три исхода, от дорогого к дешёвому: списки с числами (`scheduleListRefetch`)
 * — когда строки нет в кэше (её место знает только сервер, 03 §3.3) или она
 * могла переехать между вкладками; одни числа отложенно
 * (`scheduleCountsRefetch`) — когда по строке не понять, ждал ли клиент
 * (старый кэш без `waiting_since`); сдвиг числа в кэше без запроса — когда
 * ожидание у моего диалога погасло или зажглось. Всё остальное — ничего.
 */
function сверитьсяПослеПатча(след: Отпечаток | null, хозяинВКадре?: string | null): void {
  if (след === null || вкладкиМоглиСмениться(след, хозяинВКадре)) {
    scheduleListRefetch();
    return;
  }
  /*
   * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «снова начали долго уходить отвеченные
   * диалоги… нужно нажимать или ждать».
   *
   * Это про срез «ждут ответа» (кнопка «не отвечено» над вкладками). Он
   * считается СЕРВЕРОМ (`waiting_only`), то есть членство строки в нём — не
   * наше дело, а ответ ручки. Ответ оператора статуса не меняет (диалог уже «в
   * работе») и хозяина тоже — значит `вкладкиМоглиСмениться` выше говорит
   * «нет», и список не перезапрашивался НИКОГДА. Число «не отвечено» при этом
   * уезжало вниз дельтой сразу: под срезом «ждут 3» лежало четыре строки, и
   * отвеченная уходила только с тихой сверкой или с нажатием на вкладку.
   *
   * Ровно тот же класс, на котором этот проект попадался много раз: одно поле
   * считают два пути, и один из них про изменение не узнаёт.
   *
   * ⚠ И ТОЛЬКО ПРИ ОТКРЫТОМ СРЕЗЕ. Безусловный перезапрос вернул бы шторм,
   * разобранный 06.09: кадр приходит всем тринадцати на каждое сообщение
   * компании (1 126 в час), а срезом пользуются считанные вкладки. Ожидание
   * же переключается у каждого второго кадра — на нём и держится счёт.
   */
  if (ожиданиеПереключилось(след.было, след.стало) !== false && открытСрезЖдущих()) {
    scheduleListRefetch();
    return;
  }
  const дельта = дельтаНеОтвечено(след.было, след.стало, useSessionStore.getState().user?.id);
  if (дельта === null) scheduleCountsRefetch();
  else if (дельта !== 0) сдвинутьСчёт({ mine_waiting: дельта });
  /*
   * ⚠ И ПОТОЛОК РАСХОЖДЕНИЯ — ДАЖЕ КОГДА ДЕЛЬТА СОШЛАСЬ (07.09). Дельта читает
   * строку из кэша, а вариантов кэша столько, сколько наборов фильтров человек
   * открывал; они грузятся в разное время и расходятся между собой. Ошибись
   * дельта — число врало бы до смены статуса или тихой сверки. Раз в полминуты
   * спрашиваем сервер: мгновенная реакция за дельтой, правда за сервером.
   */
  scheduleCountsReconcile();
}

/**
 * Строка диалога из уже загруженных списков — «что мы про него УЖЕ знаем».
 *
 * ЗАЧЕМ. Открывая диалог, человек до ответа сервера видел пустую панель:
 * шапка — «Загрузка…», лента — скелет, низ панели пуст. То есть диалог,
 * который он только что видел в списке, на глазах ПРОПАДАЛ и появлялся заново
 * (жалоба владельца 22.08). А знаем мы к этому моменту почти всё: строка
 * списка несёт и клиента, и статус, и канал, и объявление — те же поля
 * сервера, только приехавшие другим запросом.
 *
 * Ищем во ВСЕХ закэшированных вариантах фильтров и в очереди: диалог могли
 * открыть из «Моих», из «Всех», из поиска или из «Входящих».
 *
 * Это не подмена детали, а её предшественник: как только приедет
 * `GET /conversations/{id}`, шапка перерисуется по нему. Придумывать здесь
 * нечего — либо строка есть, либо `undefined` и прежнее «Загрузка…».
 */
export function cachedConversationRow(convId: string): ConversationDto | undefined {
  const entries = queryClient.getQueriesData<InfiniteData<ConversationsPage>>({
    queryKey: CONVERSATIONS_LIST_KEY,
  });
  for (const [, data] of entries) {
    for (const page of data?.pages ?? []) {
      const row = page.items.find((r) => r.id === convId);
      if (row) return row;
    }
  }
  return inboxRows().find((r) => r.id === convId);
}

/** Append в последнюю страницу infinite-кэша ленты; дубли (догон+WS) отбрасываются по id. */
export function appendMessage(convId: string, msg: MessageDto): void {
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId), (old) => {
    if (!old || old.pages.length === 0) return old; // лента не открывалась — кэш не создаём
    if (old.pages.some((p) => p.items.some((m) => m.id === msg.id))) return old;
    const pages = old.pages.slice();
    const last = pages[pages.length - 1];
    pages[pages.length - 1] = { ...last, items: [...last.items, msg] };
    return { ...old, pages };
  });
}

/**
 * ЛЕНТА ЗАМЕЧАЕТ, ЧТО ОТСТАЛА, И ДОГОНЯЕТСЯ САМА.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 29.08: «приходит уведомление, а сообщение в чате не
 * появляется — приходится обновлять страницу».
 *
 * ПОЧЕМУ ТАК ВЫХОДИЛО. Низ ленты живёт ИСКЛЮЧИТЕЛЬНО на сокете: у запроса
 * стоит `getNextPageParam: () => null` («вниз ничего не догружаем — низ живой»),
 * и ни `refetchInterval`, ни `refetchOnWindowFocus` у него нет. Потерялся один
 * кадр `message:new` — и лента не узнает о сообщении никогда.
 *
 * А потеряться он может и без обрыва связи: хаб не буферизирует, рассылка
 * кадров «внутри диалога» идёт только подписанным на этот диалог, и часть
 * кадров отсекается по правам. Уведомление при этом едет своим маршрутом и
 * доезжает — отсюда и симптом «звук есть, сообщения нет».
 *
 * ЧЕМ ЛЕЧИМ. Строка списка знает авторитетное `last_message_at` и обновляется
 * НЕ ТОЛЬКО кадром ленты. Значит пропажу видно арифметически: если самое
 * свежее сообщение в открытой ленте старше, чем `last_message_at` строки, —
 * кадр потерян, и ленту надо перезапросить немедленно.
 *
 * Тихая сверка (`quietResync`) это чинила и раньше, но раз в две минуты. Никто
 * не ждёт двух минут: человек видит тишину и жмёт F5 через десять секунд,
 * теряя черновик и место в переписке.
 *
 * ⚠ ЗАЩИТА ОТ ПЕТЛИ ОБЯЗАТЕЛЬНА. Строку двигают и сообщения, которых в ленте
 * не видно: служебные записи Авито намеренно не сдвигают `last_message_at`, а
 * вот обратный случай — строка ушла вперёд, а показывать нечего — вполне
 * возможен. Без памяти о том, за какой меткой уже гнались, экран уходил бы в
 * бесконечный перезапрос, и это было бы хуже исходной жалобы.
 */
const догоняли = new Map<string, string>();

/** Допуск на расхождение часов и на порядок записи: секунда роли не играет. */
const ДОПУСК_МС = 1_500;

export function догнатьЕслиОтстали(convId: string): void {
  const { activeConversationId } = useChatUiStore.getState();
  // Проверяем только то, на что человек смотрит: догонять закрытые ленты
  // значит платить запросом за то, чего никто не видит.

  if (activeConversationId !== convId) return;

  const строка = cachedConversationRow(convId);
  const строкаЗнает = строка?.last_message_at;
  if (!строкаЗнает) return;
  if (догоняли.get(convId) === строкаЗнает) return;

  const лента = queryClient.getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId));
  if (!лента || лента.pages.length === 0) return; // лента не открывалась — догонять нечего

  let самоеСвежее = 0;
  for (const page of лента.pages) {
    for (const m of page.items) {
      const t = new Date(m.created_at).getTime();
      if (t > самоеСвежее) самоеСвежее = t;
    }
  }
  if (!самоеСвежее) return;

  if (new Date(строкаЗнает).getTime() - самоеСвежее > ДОПУСК_МС) {
    догоняли.set(convId, строкаЗнает);
    void queryClient.invalidateQueries({
      queryKey: qk.messages.list(convId),
      refetchType: "active",
    });
  }
}

/** Сеанс кончился — память о догонах не должна пережить смену пользователя. */
/**
 * Свернуть ленты к свежему хвосту ПЕРЕД любым их перезапросом.
 *
 * ⚠ ЭТО ПОЧИНКА ЖАЛОБЫ «ДИАЛОГ ВНЕЗАПНО ПЕРЕСКАКИВАЕТ», и никакой навигации в
 * ней нет — оттого её и не нашли три захода подряд, пока искали переходы.
 *
 * МЕХАНИЗМ. Лента — бесконечный запрос, у которого `getNextPageParam` всегда
 * `null`: вниз не догружаем, низ живёт кадрами сокета. Пока страница одна,
 * `pageParams[0] === null`, и полный перезапрос приносит свежую полусотню.
 * Но стоит оператору пролистать вверх («что мы обещали по цене» — типовое
 * движение смены), как догрузка кладёт курсор САМОГО СТАРОГО куска в
 * `pageParams[0]`. А полный перезапрос бесконечного запроса идёт только
 * ВПЕРЁД: берёт нулевую страницу по `pageParams[0]` и обрывается, потому что
 * `getNextPageParam` вернул `null`.
 *
 * Итог: тихая сверка (раз в две минуты и на каждый возврат во вкладку), догон
 * после обрыва или отставания молча заменяют ВСЮ ленту одним старым куском.
 * Адрес прежний, имя клиента прежнее — а переписка на экране из другого
 * времени. Для человека это и есть «перескочило». Хуже: состояние
 * самоподдерживающееся, `pageParams[0]` навсегда остаётся старым, а новые
 * кадры дописываются в конец старого куска — лента с дырой посередине.
 *
 * ПРОВЕРЕНО РУКАМИ на установленном query-core: после прокрутки вверх
 * `pageParams = ["стар", null]`; после перезапроса остаётся одна страница
 * `["стар"]`, свежая исчезает.
 *
 * Поэтому перед перезапросом сворачиваем каждую ленту к её ХВОСТУ (он и есть
 * свежая часть) и возвращаем `pageParams` в начало. Тогда перезапрос приносит
 * то, что человек и ожидает увидеть, — конец переписки. Пролистанная история
 * при этом теряется, но она и так терялась: раньше вместо неё пропадал хвост.
 */
export function свернутьЛентыКХвосту(): void {
  for (const q of queryClient.getQueryCache().findAll({ queryKey: qk.messages.root })) {
    const данные = q.state.data as InfiniteData<MessagesPage> | undefined;
    if (!данные || данные.pages.length <= 1) continue;
    // Догрузка старого кладёт страницы В НАЧАЛО, значит свежая — последняя.
    queryClient.setQueryData(q.queryKey, {
      pages: [данные.pages[данные.pages.length - 1]],
      pageParams: [null],
    });
  }
}

export function забытьДогоны(): void {
  догоняли.clear();
}

/**
 * Догон: строка либо добавляется, либо ОБНОВЛЯЕТСЯ серверной правдой.
 *
 * ⚠ ЧЕМ ОТЛИЧАЕТСЯ ОТ `appendMessage` И ЗАЧЕМ ЗАВЕДЕНА (28.08).
 *
 * `appendMessage` на первой же строке отбивает всё, что уже лежит в кэше — и
 * это верно для живого кадра `message:new`: то же сообщение приходит и по WS, и
 * догоном, дубль в ленте не нужен.
 *
 * Но у догона задача другая. Курсор хвоста запоминается при ЗАГРУЗКЕ ленты, то
 * есть ДО отправки, и сервер отдаёт всё, что после него, — включая наше
 * собственное сообщение, уже с настоящим `delivery_status`. Единственный, кто
 * правит статус на живой связи, — кадр `message:status`, а он и потерян: хаб
 * событий не буферизирует, и всё, что уехало в Pub/Sub при мёртвом сокете,
 * пропадает. Отбить серверную строку значило выбросить ЕДИНСТВЕННУЮ уцелевшую
 * копию этой правды.
 *
 * Как это выглядело: оператор отправляет ответ, видит часики; в эту секунду
 * перезапускается api на выкатке и все сокеты рвутся; воркер доставки
 * исчерпывает попытки и помечает сообщение `failed`; сокет возвращается через
 * несколько секунд, догон забирает хвост — и выбрасывает его. Пузырь навсегда
 * остаётся «Отправляется», кнопки «Повторить» нет (она нарисована строго по
 * `failed`), а строка слева при этом краснеет. Ничто больше ленту не освежает:
 * у неё нет ни `refetchInterval`, ни `refetchOnWindowFocus`. Клиент не получил
 * ответа, оператор уверен, что отправил.
 */
export function upsertMessage(convId: string, msg: MessageDto): void {
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId), (old) => {
    if (!old || old.pages.length === 0) return old; // лента не открывалась — кэш не создаём
    let найдено = false;
    const pages = old.pages.map((p) => {
      if (!p.items.some((m) => m.id === msg.id)) return p;
      найдено = true;
      // Поверх своего: у серверной строки может не быть полей, которые мы
      // держим локально, а спорные (статус, ошибка, текст) — за сервером.
      return { ...p, items: p.items.map((m) => (m.id === msg.id ? { ...m, ...msg } : m)) };
    });
    if (найдено) return { ...old, pages };
    const last = pages[pages.length - 1];
    const хвост = pages.slice();
    хвост[хвост.length - 1] = { ...last, items: [...last.items, msg] };
    return { ...old, pages: хвост };
  });
}

/**
 * Идёт ли эта запись в превью строки списка и двигает ли она время строки.
 *
 * ЧТО БЫЛО НА БОЮ (12 августа). В списке чатов вместо слов клиента стояло
 * «Отказ отменён: Ад…», а строка при этом прыгала наверх. Кадром `message:new`
 * приезжает не только переписка: каждая смена статуса, принятие, отказ и его
 * отмена кладут в ленту СИСТЕМНУЮ запись («Статус: Новый → В работе. Иванов»,
 * `services/inbox.py`, `api/routes/conversations.py::_publish_change`) и
 * публикуют её тем же кадром — коллеги обязаны видеть событие в ленте. А
 * обработчик писал `last_message` безусловно, и служебная строка вытесняла из
 * превью последнее сообщение клиента — единственное, ради чего в список и
 * смотрят.
 *
 * СЕРВЕР СЧИТАЕТ ТАК ЖЕ, и в этом суть правки, а не в новом правиле: превью и
 * счётчик сообщений диалога он собирает по `direction IN ('in','out')`, а
 * `last_message_at` системной записью НЕ двигает вовсе
 * (`conversations.add_system_message`: «служебная запись не должна выталкивать
 * диалог наверх»). То есть до перезагрузки страницы список показывал одно, а
 * после — другое, и правым был сервер.
 *
 * ЗАМЕТКА (`note`) — здесь же и по той же причине: она внутренняя, клиент её
 * не видел, и в строке списка ей делать нечего.
 *
 * Служебная запись САМОГО Авито (`direction: "system"`, `sender_type: "avito"`)
 * в превью попадает — с подписью «Авито:» (`ConversationListItem`). Но приходит
 * она НЕ этим кадром: `inbound.py::_apply_avito_system_event` публикует
 * `message:new` намеренно не для неё, а строку с ней отдаёт сервер. Поэтому
 * исключения для неё здесь нет — оно было бы про случай, которого не бывает.
 */
function belongsInRowPreview(msg: MessageDto): boolean {
  return msg.direction === "in" || msg.direction === "out";
}

/** message:new (03 §3.3): лента + строка списков + непрочитанные + звук. */
export function applyNewMessage(convId: string, msg: MessageDto, patch?: ConversationPatch): void {
  const { activeConversationId } = useChatUiStore.getState();
  const isActiveAndVisible = activeConversationId === convId && document.visibilityState === "visible";
  const unreadDelta = msg.direction === "in" && !isActiveAndVisible ? (patch?.unread_delta ?? 1) : 0;

  // 1) Лента открытого/закэшированного диалога.
  appendMessage(convId, msg);

  // 2) Строка в списках: последнее сообщение, patch, пересортировка.
  let seenAssigneeId: string | null | undefined; // из строки кэша — для unreadStore
  const след = сОтпечатком(convId, (row) => {
    if (seenAssigneeId === undefined) seenAssigneeId = row.assignee?.id ?? null;
    const merged = patch ? mergeConversationPatch(row, patch) : { ...row };
    if (belongsInRowPreview(msg)) {
      // `sender_type` — как в строке с сервера (`conversations.conversation_out`):
      // без него вопрос системы об адресе до перезагрузки подписан «Вы:».
      merged.last_message = {
        body: msg.body,
        direction: msg.direction,
        created_at: msg.created_at,
        sender_type: msg.sender_type,
      } as ConversationDto["last_message"];
      merged.last_message_at = msg.created_at;
    }
    merged.unread_count = Math.max(0, row.unread_count + unreadDelta);
    return merged;
  });
  patchRowEverywhere(convId, след.updater);
  /*
   * Совсем новый диалог: одна строка честнее собирается сервером (03 §3.3).
   * Схлопнуто: на выгрузке истории и на утреннем наплыве такие кадры идут
   * пачками, и перезапрос на каждый складывался в тот самый поток запросов.
   *
   * ⚠ А ЗНАКОМАЯ СТРОКА СЕРВЕРА НЕ КАСАЕТСЯ (06.09). Этот кадр приходит всем
   * тринадцати на КАЖДОЕ сообщение компании — 1 126 в час, — и до 06.09
   * каждый из них через `patchRowEverywhere` заказывал список с числами у
   * каждой вкладки. Строка уже поправлена выше; число «не отвечено» двигается
   * дельтой по `waiting_since` из кадра; список спрашивают только если
   * сменились статус или хозяин.
   *
   * Без `patch` сюда приходит и СВОЁ отправляемое сообщение
   * (`useSendMessage`): строка знакома, ожидание не менялось — сервера оно не
   * касается. Кадр сокета без патча разбирает `applyWsEvent` ниже.
   */
  сверитьсяПослеПатча(след.отпечаток(), patch?.assignee_id);

  // 3) Деталь, если открыта.
  if (patch) {
    queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (old) =>
      old ? mergeConversationPatch(old, patch) : old,
    );
  }

  // 4) Непрочитанные + звук — только чужие входящие не в открытом видимом диалоге.
  if (unreadDelta > 0) {
    // assigneeId обязателен: без него запись рождалась с null, и бейджи (N)
    // в title/трее не считали непрочитанное моих диалогов (аудит 16.08)
    useUnreadStore.getState().increment(convId, unreadDelta, {
      ...(patch?.status ? { status: patch.status } : null),
      ...(seenAssigneeId !== undefined ? { assigneeId: seenAssigneeId } : null),
    });
    /*
     * ⚠ ЗВУК — ТОЛЬКО ПО СВОИМ ДИАЛОГАМ (обратная связь диспетчера 02.09).
     *
     * Жалоба дословно: «звук выключил, потому что он режет слух и 90% просто
     * так оповещает, когда даже сообщений нет… крч не работопригодно». Без
     * звука человек не реагирует на сообщения, со звуком не может сидеть.
     *
     * ЗАМЕР ПОДТВЕРДИЛ ЖАЛОБУ ПОЧТИ ДОСЛОВНО. За день: 521 входящее сообщение и
     * 81 постановка в очередь — около шестисот сигналов на каждого. Звенело на
     * ЛЮБОЕ входящее в ЛЮБОМ диалоге: тринадцать человек слышали каждое
     * сообщение всей компании по шестнадцати каналам. Своих среди них — меньше
     * десятой части, отсюда и «90% просто так».
     *
     * Правило теперь одно: звук значит «нужен ТЫ». Свой диалог — звенит. Чужой
     * — молчит, но счётчик непрочитанного растёт как раньше: видно, а не слышно.
     * Новый клиент в очереди — свой отдельный двойной сигнал, он не отсюда.
     *
     * ⚠ НЕИЗВЕСТНЫЙ ХОЗЯИН — МОЛЧИМ. Кадр несёт `assignee_id` с 02.09, так что
     * неизвестность означает старый сервер или строку не из этого кадра. Звенеть
     * на всякий случай значило бы вернуть ровно тот шум, от которого человек
     * выключил звук; пропущенный сигнал по своему диалогу при этом виден в
     * счётчике и в заголовке вкладки.
     */
    const я = useSessionStore.getState().user?.id;
    const хозяин = patch?.assignee_id ?? seenAssigneeId;
    if (я !== undefined && хозяин === я) notifyNewMessage(msg);
  } else if (patch?.status) {
    useUnreadStore.getState().patchMeta(convId, { status: patch.status });
  }

  // Кадр обработан — но если вставка не удалась (дубль по id, чужая страница
  // кэша), лента могла остаться позади. Проверка дешёвая, а молчание дорогое.
  догнатьЕслиОтстали(convId);
}

/** conversation:updated: слить patch в деталь и строки; смена статуса — честный refetch активных списков. */
/** Висит ли у этого диалога предложение передачи мне — по детали или строке в кэше. */
function предложеноМне(convId: string, деталь: ConversationDetailDto | undefined): boolean {
  const я = useSessionStore.getState().user?.id;
  if (!я) return false;
  if (деталь?.transfer?.to.id === я) return true;
  return inboxRows().some((row) => row.id === convId && row.transfer?.to.id === я);
}

export function applyConversationPatch(convId: string, patch: ConversationPatch): void {
  /*
   * ДИАЛОГ ПЕРЕЕХАЛ В ДРУГУЮ КАРТОЧКУ (объединение — руками или автоматикой по
   * телефону, 12.09). Заплатка несёт новый `client.id`; человек, у которого
   * этот диалог открыт, увидит другое имя в шапке и другую карточку справа —
   * без слова об этом он решил бы, что открыл чужой диалог. Тост говорит,
   * что случилось. Наблюдателю тоже: личность ему не нужна, имя — да.
   */
  const прежняя = queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail(convId));
  const былоМнеПредложено = предложеноМне(convId, прежняя);
  const новыйКлиент = patch.client?.id;
  // Только у ОТКРЫТОГО диалога: деталь в кэше живёт полчаса, и одна склейка
  // с сотней диалогов дала бы сотню тостов о диалогах, которых на экране нет.
  // Слово «объединили» не произносится: тот же кадр приходит и при разъединении.
  if (
    прежняя &&
    новыйКлиент &&
    прежняя.client.id !== новыйКлиент &&
    window.location.pathname.includes(`/chats/${convId}`)
  ) {
    showToast({
      title: "Диалог теперь в другой карточке",
      message: `Карточка клиента — «${patch.client?.name?.trim() || "без имени"}»; история и номера читаются оттуда`,
      color: "lp",
    });
  }
  queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (old) =>
    old ? mergeConversationPatch(old, patch) : old,
  );

  const след = сОтпечатком(convId, (row) => mergeConversationPatch(row, patch));
  patchRowEverywhere(convId, след.updater);

  /*
   * ⚠ ЭТО ГЛАВНОЕ МЕСТО ПРОВЕРКИ, А НЕ ЗАПАСНОЕ.
   *
   * Сюда строка приходит вперёд БЕЗ кадра ленты: `conversation:updated` едет
   * своим маршрутом и своими фильтрами. Ровно так и выглядит жалоба «звук
   * есть, сообщения нет» — строка знает о новом сообщении, лента о нём не
   * знает, и узнать ей неоткуда.
   */
  догнатьЕслиОтстали(convId);

  const unread = useUnreadStore.getState();
  if (typeof patch.unread_count === "number") unread.setCount(convId, patch.unread_count);
  unread.patchMeta(convId, {
    ...(patch.status ? { status: patch.status } : null),
    ...(patch.assignee !== undefined ? { assigneeId: patch.assignee?.id ?? null } : null),
  });

  /*
   * Смена статуса/ответственного может переместить диалог между вкладками
   * (03 §2.3) — сверяемся с сервером.
   *
   * ⚠ НО ОДНИМ ПЕРЕЗАПРОСОМ НА ПАЧКУ, А НЕ НА КАЖДОЕ ИЗМЕНЕНИЕ (жалобы
   * владельца 22.08). Строка к этому моменту УЖЕ поправлена локально
   * (`patchRowEverywhere` выше), и человек видит изменение мгновенно. А
   * безусловный перезапрос давал на пачке закрытий по полному перезапросу
   * тяжёлого списка НА КАЖДОЕ: отсюда разом «зависания страницы», «диалог
   * визуально пропадает» и тосты об ошибке — поток запросов упирался в лимит
   * nginx (30 в секунду на адрес) и часть возвращалась 429.
   *
   * Деталь диалога поправлена выше `setQueryData`, поэтому спрашиваем сервер
   * только про списки — подробности в `listRefetch.ts`.
   *
   * ⚠ «СМЕНИЛИСЬ», А НЕ «ПРИСУТСТВУЮТ В ПАТЧЕ» (06.09). Здесь стояло
   * `patch.status !== undefined || patch.assignee !== undefined`, а сервер
   * кладёт `status` почти в каждый патч (`_publish_change`), включая те, где
   * он не менялся, — и кадр про метку или телефон клиента перезапрашивал
   * список у всех тринадцати. Сравниваем со строкой в кэше: она и есть то,
   * что человек видит; не изменилось на экране — не переехало и на сервере.
   */
  сверитьсяПослеПатча(след.отпечаток());

  /*
   * УХОД ИЗ «ВХОДЯЩИХ» — ЗДЕСЬ ЖЕ, А НЕ ТОЛЬКО ПО КАДРАМ `inbox:*` (16.08).
   *
   * Закрытие диалога и вход бота кадра очереди не шлют — только этот
   * `conversation:updated`. Очередь же чистили одни `inbox:claimed/declined`,
   * и строка закрытого (или уведённого ботом) диалога висела во «Входящих»
   * до ручного обновления: оператор открывал её и «закрывал второй раз» —
   * ровно жалоба владельца. Убираем строку тем же движением, что и
   * `inbox:claimed`; вернуть её может только настоящий кадр `inbox:new`.
   */
  if (patch.status === "closed" || patch.bot_active === true) {
    useInboxStore.getState().remove(convId);
    removeInboxRow(convId);
  }

  /*
   * Предложение передачи мне появилось или развязалось. Его строка живёт во
   * «Входящих» получателя, а кадров очереди передача не шлёт: без этого строка
   * появлялась и уходила только с тихой сверкой, раз в две минуты. Передачи
   * редки, поэтому просто спрашиваем сервер.
   */
  if (patch.transfer !== undefined) {
    const я = useSessionStore.getState().user?.id;
    const теперьМне = patch.transfer?.to.id === я;
    if (былоМнеПредложено || теперьМне) {
      void queryClient.invalidateQueries({ queryKey: qk.inbox.list, refetchType: "active" });
      void refreshInboxCount();
    }
    // Отказ, отмена, истечение: ⚑ «передан вам» сервер уже снял. Принятие
    // оставляет его до открытия диалога — там он и значит «новое для вас».
    const принялЯ = patch.assignee?.id === я || patch.assignee_id === я;
    if (былоМнеПредложено && !теперьМне && !принялЯ) {
      patchRowEverywhere(convId, (row) => ({ ...row, transferred_to_me: false }));
    }
    // Карточка с «Принять / Отклонить» висит до нажатия; решённому предложению
    // она не нужна, чем бы оно ни кончилось.
    if (былоМнеПредложено && !теперьМне) closeHandoffCard(convId);
  }

  // «Отказались все» приходит остальным именно этим кадром — счётчик и
  // подсветка обязаны сдвинуться у всех, не только у отказавшегося (16.08).
  if (patch.escalated !== undefined) {
    useInboxStore.getState().bumpEscalated(convId, patch.escalated === true);
  }
}

/**
 * Закрепление у СЕБЯ — правка одной строки в кэше, без перезапроса списка.
 *
 * Закрепление никого, кроме нажавшего, не касается: это личная отметка, и
 * меняет она ровно два обстоятельства — флажок в строке и её место в порядке
 * (закреплённые идут первым ключом). Раньше на успех ручки уходил
 * `invalidateQueries` по всему корню списка: до пятидесяти строк заново с
 * сервера ради собственной галочки, и по разу на каждое нажатие. На девяти
 * каналах и живом потоке это ещё и мигало — пришедший тем временем диалог
 * появлялся именно в этот момент, и выглядело так, будто список дёрнулся сам.
 *
 * Пересортировка внутри `patchRowEverywhere` та же, что у сервера
 * (`shared/lib/conversationOrder`), поэтому строка встаёт ровно туда, куда её
 * поставил бы перезапрос.
 */
export function applyPinnedInLists(convId: string, pinned: boolean): void {
  // Закрепление не двигает ни вкладку, ни числа — только порядок строк, и он
  // считается на клиенте. Спрашивать сервер незачем (обещание докстринга выше).
  patchRowEverywhere(convId, (row) => ({ ...row, pinned }));
}

/** Полная строка из догона updated_since: заменить, где есть; true — нашлась. */
export function applyConversationRow(row: ConversationDto): boolean {
  // Строка могла ПОЯВИТЬСЯ, а не измениться — патч строк такого не видит.
  scheduleListRefetch();
  queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(row.id), (old) =>
    old ? { ...old, ...row } : old,
  );
  return patchRowEverywhere(row.id, () => row);
}

/* --------------------------------------------------------------------- *
 *  Очередь «Входящие» (7.1)
 * --------------------------------------------------------------------- */

/**
 * Кадры очереди. Нормативный каталог событий — 01 §11.3 и `wsEvents.ts`;
 * до его правки (файл вне зоны задачи) типы объявлены здесь, а разбор принимает
 * объединение. Формат конверта тот же: `{ type, ts, data }`.
 *
 * Несимметричность состава задана сервером (app/ws/hub.py) и намеренна:
 * `inbox:new` и `inbox:released` несут диалог ЦЕЛИКОМ — строку надо вставить, а
 * у только что подключившегося её нет вовсе; `inbox:claimed` несёт только
 * патч — строку надо убрать из очереди и пометить занятой.
 *
 * Персональные поля подставляет хаб каждому получателю отдельно: `can_claim` —
 * может ли ЭТОТ человек принимать (у руководителя и наблюдателя очередь не
 * звенит и не считается), `is_mine` — не я ли сам принял этот диалог в другой
 * вкладке. Абсолютного счётчика в широковещательных кадрах нет: очередь у
 * каждого своя, и одно число на всех было бы враньём для отказавшегося.
 */
export type WsInboxEvent =
  | {
      type: "inbox:new";
      ts: string;
      data: {
        conversation_id: string;
        conversation: ConversationDto;
        can_claim?: boolean;
        /** Диалог ВЕРНУЛСЯ после истёкшего отказа, а не пришёл впервые — не звенеть. */
        returned?: boolean;
      };
    }
  | {
      type: "inbox:claimed";
      ts: string;
      data: {
        conversation_id: string;
        /**
         * ⚠ МОЖЕТ БЫТЬ `null` (аудит 30.08). Кадр шлёт не только «человек
         * нажал Принять»: `inbound` публикует его же, когда диалог ушёл из
         * очереди БЕЗ принявшего — коллега ответил клиенту прямо из приложения
         * Авито. По замерам прода это ОСНОВНОЙ путь ответов, то есть ветка не
         * редкая. Тип обещал `UserRef`, обработчик читал `claimedBy.full_name`
         * — и падал с TypeError ровно у того, у кого этот диалог открыт.
         */
        claimed_by: UserRef | null;
        claimed_at?: string | null;
        waited_seconds?: number | null;
        conversation_patch?: ConversationPatch;
        is_mine?: boolean;
      };
    }
  | {
      type: "inbox:released";
      ts: string;
      data: {
        conversation_id: string;
        conversation: ConversationDto;
        released_by: UserRef;
        offered_at?: string | null;
        conversation_patch?: ConversationPatch;
        can_claim?: boolean;
      };
    }
  | {
      type: "inbox:declined";
      ts: string;
      data: {
        conversation_id: string;
        declined_by: UserRef;
        reason?: string | null;
        /** Абсолютный размер МОЕЙ очереди: кадр адресный, врать некому. */
        count?: number;
        declined_count?: number;
        escalated?: boolean;
      };
    };

/**
 * Может ли этот человек принимать диалоги. Первое слово за сервером: хаб
 * подставляет `can_claim` каждому получателю по матрице прав, и новая
 * роль-оператор получит очередь, не дожидаясь правки фронта. Флага нет
 * (кадр не из хаба, старый сервер) — спрашиваем свои же права.
 */
function canClaimNow(flag?: boolean): boolean {
  if (typeof flag === "boolean") return flag;
  return useSessionStore.getState().permissions.includes("messages:send");
}

/**
 * Диалог встал в очередь: счётчик, строка в списке очереди, двойной сигнал.
 * Руководителю и наблюдателю — ничего: очередь им видна на экране, но они по
 * ней не работают, и звенеть у них ей незачем.
 */
export function applyInboxNew(
  row: ConversationDto,
  canClaim?: boolean,
  returned?: boolean,
): void {
  if (!canClaimNow(canClaim)) return;
  useInboxStore.getState().add(row.id);
  insertInboxRow(row);
  if (queryClient.isFetching({ queryKey: qk.inbox.list })) {
    // снапшот уже летит и перетрёт вставленную строку — перезапросим следом,
    // иначе «звонок прозвенел, а клиента нигде нет» (аудит 16.08)
    void queryClient.invalidateQueries({ queryKey: qk.inbox.list });
  }
  // ⚠ ВОЗВРАТ ПОСЛЕ ОТКАЗА — БЕЗ ЗВУКА. Строка появляется тем же кадром, что и новый
  // клиент, но событие другое: этот диалог оператор уже видел и сам от него отказался
  // три минуты назад (см. `DECLINE_TTL`). Звонок здесь — ложная тревога, причём у
  // каждого, кто пользуется кнопкой «Отклонить», и каждые три минуты. Звук значит
  // «пришёл новый клиент», и значить что-то ещё он не должен.
  if (!returned) {
    playInboxChime();
    /*
     * ⚠ ЗВУКА ОДНОГО МАЛО (жалоба владельца 07.09). У диспетчера поверх
     * LeadChat открыта CRM, и до 07.09 приход диалога не давал ему НИЧЕГО,
     * кроме сигнала: карточку показывала только передача, а очередь — нет.
     *
     * Условие то же самое, что у звука, и это не совпадение, а замысел:
     * `canClaimNow(canClaim)` выше наследует серверный отбор по правам и по
     * каналам (кадр просто не приедет тому, кому этот канал не открыт), а
     * `!returned` не будит человека диалогом, от которого он сам отказался три
     * минуты назад. Второго условия для карточки заводить нельзя — разойдётся
     * со звуком (правило в шапке `platform/toast.ts`).
     *
     * Размер очереди берём из стора: `add` выше уже посчитал этот диалог, и
     * число совпадает с бейджем вкладки — карточка и экран говорят одно и то же.
     */
    toastForInbox(row.id, useInboxStore.getState().count);
  }
}

/**
 * Диалог принят. Событие приходит ВСЕМ — в этом весь смысл очереди: у
 * остальных двенадцати строка исчезает раньше, чем они успеют нажать «Принять».
 * Если принятый диалог сейчас открыт у меня — вместо кнопок появится плашка
 * «Диалог принял Иван» (7.1 п.4): пользователь узнаёт причину до нажатия, а не
 * из ошибки после.
 */
export function applyInboxClaimed(
  convId: string,
  claimedBy: UserRef | null,
  patch?: ConversationPatch,
  isMine?: boolean,
): void {
  useInboxStore.getState().remove(convId);
  removeInboxRow(convId);

  // Строка и деталь: диалог теперь ведёт принявший — без похода на сервер.
  // Патч сервера кладём поверх своего минимума: у него есть `claimed_at`,
  // которого у нас нет, а `in_inbox: false` там же — и это главное поле кадра.
  // ⚠ БЕЗ ПРИНЯВШЕГО ОТВЕТСТВЕННОГО НЕ ПРИДУМЫВАЕМ. `claimed_by: null` значит
  // «строка ушла из очереди, но человека за ней нет» — так `inbound` сообщает,
  // что коллега ответил клиенту из приложения Авито. Записать сюда `null`
  // ответственным значило бы стереть уже известного: патч сервера ниже кладёт
  // правду, а до него строка обязана остаться как есть.
  const merged: ConversationPatch = claimedBy
    ? { assignee: claimedBy, in_inbox: false, ...patch }
    : { in_inbox: false, ...patch };
  patchRowEverywhere(convId, (row) => mergeConversationPatch(row, merged));
  queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (old) =>
    old ? mergeConversationPatch(old, merged) : old,
  );

  const me = useSessionStore.getState().user;
  const mine = typeof isMine === "boolean" ? isMine : claimedBy?.id === me?.id;
  const { activeConversationId } = useChatUiStore.getState();
  // Плашку «Диалог принял …» показываем, только когда есть КОГО назвать.
  // Ответ из приложения Авито принявшего не имеет: строка из очереди уходит, а
  // сказать «принял никто» нельзя — молчим, список сверится ниже.
  if (activeConversationId === convId && !mine && claimedBy) {
    useInboxStore.getState().showClaimed(convId, подписьСотрудника(claimedBy));
  }

  /*
   * Принятый диалог переехал в «Мои» принявшего — вкладки могли измениться.
   *
   * ⚠ КАДР ШИРОКОВЕЩАТЕЛЬНЫЙ: его получают ВСЕ тринадцать диспетчеров на
   * каждое чужое «Принять». Безусловный перезапрос здесь означал, что при
   * живом разборе очереди список перезапрашивается у всех и почти непрерывно —
   * это и есть «страница подвисает». Строка и деталь поправлены выше, поэтому
   * сверка с сервером собирается в одну на пачку.
   */
  scheduleListRefetch();
}

/**
 * Диалог вернули в очередь («я не тот, кто нужен», 01 — release). Для очереди
 * это то же событие, что и новый: строка снова ждёт хозяина, и молчать о ней
 * нельзя — иначе диалог, который уже один раз никто не довёл, тихо ляжет вниз
 * списка. Поэтому и звук тот же; свой собственный возврат, разумеется, не звенит.
 */
export function applyInboxReleased(
  row: ConversationDto,
  releasedBy: UserRef,
  canClaim?: boolean,
  patch?: ConversationPatch,
): void {
  const merged: ConversationPatch = { assignee: null, in_inbox: true, ...patch };
  patchRowEverywhere(row.id, (r) => mergeConversationPatch(r, merged));
  queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(row.id), (old) =>
    old ? mergeConversationPatch(old, merged) : old,
  );
  // Диалог ушёл из «Моих» вернувшего и появился в очереди у всех — вкладки
  // поехали. Кадр так же широковещательный, как `inbox:claimed`, — схлопываем.
  scheduleListRefetch();

  if (!canClaimNow(canClaim)) return;
  useInboxStore.getState().add(row.id);
  insertInboxRow(row);
  if (releasedBy.id !== useSessionStore.getState().user?.id) playInboxChime();
}

/**
 * Отказ. Кадр адресный (`only_user`): чужой отказ мою очередь не меняет —
 * диалог как ждал, так и ждёт. Раз получатель один, сервер везёт в кадре
 * абсолютный счётчик — он честен, и брать его точнее, чем вычитать единицу.
 */
export function applyInboxDeclined(
  convId: string,
  declinedBy: UserRef,
  counters?: { count?: number; escalated?: boolean },
  undone?: boolean,
): void {
  if (declinedBy.id !== useSessionStore.getState().user?.id) return;
  const inbox = useInboxStore.getState();
  if (undone) {
    // отмена отказа: диалог ВОЗВРАЩАЕТСЯ — кадр без строки, поэтому честный
    // перезапрос списка; раньше ветка удаляла его же строку (аудит 16.08)
    if (typeof counters?.count === "number") {
      useInboxStore.setState({ count: Math.max(0, counters.count) });
    }
    void queryClient.invalidateQueries({ queryKey: qk.inbox.list });
    return;
  }
  inbox.settle(
    convId,
    typeof counters?.count === "number"
      ? { count: counters.count, escalated: inbox.escalated }
      : undefined,
  );
  removeInboxRow(convId);
  if (counters?.escalated) {
    // «Отказались все» — диалог остаётся в очереди и обязан быть заметным.
    patchRowEverywhere(convId, (row) => ({ ...row, escalated: true }));
  }
}

/**
 * Буфер «обгоняющих» патчей (03 §3.4): воркер бывает быстрее сети клиента, и
 * message:status приходит РАНЬШЕ ответа POST — серверного id в кэше ещё нет.
 * Патч по неизвестному id складывается сюда, replaceMessageInCache применяет
 * его сразу после подстановки серверного сообщения.
 */
let lastBackfillSyncAt = 0; // дроссель кадров загрузки истории (17.08)

const pendingPatches = new Map<string, Partial<MessageDto>>();
const PENDING_PATCHES_LIMIT = 200;

const patchKey = (convId: string, messageId: string) => `${convId}:${messageId}`;

/** Удаление сообщения из кэша ленты (удалённая заметка, 17.08). */
export function removeMessageFromCache(convId: string, messageId: string): void {
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId), (old) => {
    if (!old) return old;
    const pages = old.pages.map((p) => ({
      ...p,
      items: p.items.filter((m) => m.id !== messageId),
    }));
    return { ...old, pages };
  });
}

/** Точечное обновление сообщения в ленте; false — сообщения в кэше нет. */
function patchInCache(convId: string, messageId: string, patch: Partial<MessageDto>): boolean {
  let found = false;
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId), (old) => {
    if (!old) return old;
    const pages = old.pages.map((p) => {
      if (!p.items.some((m) => m.id === messageId)) return p;
      found = true;
      return { ...p, items: p.items.map((m) => (m.id === messageId ? { ...m, ...patch } : m)) };
    });
    return found ? { ...old, pages } : old;
  });
  return found;
}

/**
 * Патч сообщения по id. Если сообщения ещё нет (HTTP-ответ не вернулся) —
 * патч уходит в буфер и применится при replaceMessageInCache.
 */
export function patchMessageInCache(convId: string, messageId: string, patch: Partial<MessageDto>): void {
  if (patchInCache(convId, messageId, patch)) return;
  // Буферизуем только для открытой (закэшированной) ленты — иначе память течёт.
  if (!queryClient.getQueryData(qk.messages.list(convId))) return;
  const key = patchKey(convId, messageId);
  const merged = { ...(pendingPatches.get(key) ?? {}), ...patch };
  pendingPatches.delete(key);
  if (pendingPatches.size >= PENDING_PATCHES_LIMIT) {
    const oldest = pendingPatches.keys().next().value;
    if (oldest !== undefined) pendingPatches.delete(oldest);
  }
  pendingPatches.set(key, merged);
}

/**
 * Насколько далеко продвинулась доставка. Ответ POST несёт `pending`, а кадр
 * «доставлено» мог прийти раньше него и лечь на серверного близнеца: итог
 * обязан остаться «доставлено», а не откатиться к часам.
 */
const DELIVERY_PROGRESS: Record<MessageDto["delivery_status"], number> = {
  pending: 0,
  delivered: 1,
  failed: 1,
  dismissed: 2,
};

/**
 * temp → серверное сообщение (03 §3.4): подставляем ответ POST на место
 * оптимистичного пузыря и сразу применяем накопленный «обгоняющий» патч.
 */
export function replaceMessageInCache(convId: string, tempId: string, serverMsg: MessageDto): void {
  const key = patchKey(convId, serverMsg.id);
  const buffered = pendingPatches.get(key);
  pendingPatches.delete(key);
  let merged: MessageDto = buffered ? { ...serverMsg, ...buffered } : serverMsg;
  const twin = queryClient
    .getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId))
    ?.pages.flatMap((p) => p.items)
    .find((m) => m.id === merged.id && m.id !== tempId);
  if (
    twin &&
    DELIVERY_PROGRESS[twin.delivery_status] > DELIVERY_PROGRESS[merged.delivery_status]
  ) {
    merged = {
      ...merged,
      delivery_status: twin.delivery_status,
      delivery_error: twin.delivery_error,
    };
  }

  let replaced = false;
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(convId), (old) => {
    if (!old) return old;
    const pages = old.pages.map((p) => {
      if (!p.items.some((m) => m.id === tempId)) return p;
      replaced = true;
      // Дубль возможен, если WS message:new с тем же сообщением уже долетел.
      const items = p.items
        .filter((m) => m.id !== merged.id || m.id === tempId)
        .map((m) => (m.id === tempId ? merged : m));
      return { ...p, items };
    });
    return replaced ? { ...old, pages } : old;
  });

  // temp-пузыря не нашлось (лента перезагрузилась) — сообщение всё равно должно быть в ленте.
  if (!replaced) appendMessage(convId, merged);
}

/** Сброс буфера патчей (logout / смена пользователя). */
export function clearPendingMessagePatches(): void {
  pendingPatches.clear();
}

/** message:status: найти сообщение по id, заменить delivery_status (03 §2.3). */
export function patchMessageStatus(
  convId: string,
  messageId: string,
  deliveryStatus: MessageDto["delivery_status"],
  error?: string,
): void {
  patchMessageInCache(convId, messageId, {
    delivery_status: deliveryStatus,
    delivery_error: error ?? null,
  });
}

/**
 * message:transcript: расшифровка голосового досчиталась — текст ложится в
 * пузырь без перечитывания ленты (06.09).
 *
 * Тем же путём, что и статус доставки: точечный патч сообщения по id и буфер
 * на случай, если кадр обогнал саму строку. Обгон здесь редок (расшифровка
 * идёт секунды после входящего, а строка приезжает кадром `message:new` сразу),
 * но копировать механизм ради «здесь не бывает» — заводить второй, который
 * однажды разойдётся с первым.
 */
export function patchMessageTranscript(
  convId: string,
  messageId: string,
  transcript: string | null,
  status: MessageDto["voice_transcript_status"],
): void {
  patchMessageInCache(convId, messageId, {
    voice_transcript: transcript,
    voice_transcript_status: status,
  });
}

/** Локальный сброс бейджа при открытии диалога (03 §3.5) + гашение ⚑. */
export function applyLocalRead(convId: string): void {
  useUnreadStore.getState().reset(convId);
  // Числа над вкладкой считаются по владению и по ожиданию ответа, а не по
  // непрочитанным: открытие диалога их не двигает. Спрашивать сервер незачем —
  // это ровно то, что обещает докстринг и чего строка выше не делала.
  patchRowEverywhere(convId, (row) => ({ ...row, unread_count: 0, transferred_to_me: false }));
}

/**
 * Перезапросить ровно то, что сейчас на экране: открытый список и открытую
 * ленту. `refetchType: "active"` — не жадность, а условие бесшовности: react-query
 * держит прежние данные, пока идёт перезапрос, поэтому список не мигает
 * пустотой, а лента не теряет прокрутку (скелеты в `ChatListPane` и
 * `ChatThreadPane` висят на `isPending`, а он на перезапросе не поднимается).
 * Неактивные ключи просто помечаются протухшими и оживут при открытии.
 */
function refetchOpenScreens(): void {
  /*
   * ⚠ СВОРАЧИВАЕМ ЛЕНТЫ ПЕРВЫМИ, ПОТОМ СВЕРЯЕМ ВСЁ, ЧТО НА ЭКРАНЕ (31.08).
   *
   * Раньше здесь перечислялись два ключа — диалоги и сообщения. После обрыва
   * связи устареть могло что угодно: карточка клиента, шаблоны, счётчики,
   * настройки, статистика. Ни одно из этого не сверялось, и единственным
   * способом узнать правду оставалась перезагрузка страницы — ровно то, от
   * чего владелец просил избавить.
   *
   * `refetchType: "active"` перезапрашивает только то, у чего есть живой
   * наблюдатель, то есть ровно видимое. Остальное помечается устаревшим и
   * обновится при открытии экрана.
   */
  свернутьЛентыКХвосту();
  void queryClient.invalidateQueries({ refetchType: "active" });
}

/**
 * Догон после reconnect (03 §3.2, 01 §11.7): хаб события не буферизирует —
 * дифф забирается через REST.
 *
 * `resumed` — «связь ВОЗВРАЩАЛАСЬ, а не поднялась впервые». Различать
 * обязательно: при первом коннекте догонять нечего (кэш только что собран
 * REST-запросами), а при возврате дыра есть всегда.
 */
export async function catchUpAfterReconnect(resumed = false): Promise<void> {
  /**
   * Счётчик очереди сверяем ВСЕГДА и первым делом — до `since`-выхода ниже.
   * Причин две. Первая: бейдж живёт дольше открытого списка (он в заголовке
   * вкладки и на иконке рейки), и на первом подключении его иначе нечем
   * засеять — экран чатов может быть вообще не открыт. Вторая: пропущенные
   * кадры очереди инкрементами не догнать, а очередь — единственный счётчик,
   * ошибка в котором означает «оператор не знает, что клиент ждёт».
   */
  void refreshInboxCount();

  const since = useConnectionStore.getState().lastEventAt;
  if (!since) {
    /*
     * ТИХОЕ УТРО СЪЕДАЛО СООБЩЕНИЯ. `lastEventAt` ставится только на пришедший
     * кадр с `ts`. Пока по вкладке не проехало ни одного события, он null — и
     * это НЕ значит «кэш свежий»: значит «отсчитывать дифф не от чего».
     *
     * Как это выглядело. Оператор открыл чаты в 9:00, поток тихий. В 9:20
     * связь моргнула на полминуты, и ровно в эти полминуты написал клиент.
     * Сокет вернулся, догон дошёл до этой строки и вышел: строка в списке
     * старая, лента без нового сообщения, счётчик не дрогнул. Узнать о
     * клиенте можно было только перезагрузкой вкладки — то есть никак,
     * потому что повода перезагружаться у человека нет.
     *
     * Точки отсчёта для диффа по-прежнему нет, поэтому берём честный
     * перезапрос открытых экранов. Он дороже диффа ровно на один запрос
     * списка, случается только на возврате связи и данных с экрана не снимает.
     *
     * ⚠ И САМИ СТРОКИ ОЧЕРЕДИ — ТОЖЕ (28.08). `refetchOpenScreens` обновляет
     * `conversations` и `messages`, а очередь живёт на СВОЁМ ключе
     * (`["inbox","list"]`), и в этот корень не попадает. Получалось
     * наполовину: бейдж сверялся выше и показывал правду, а список под ним
     * оставался вчерашним — со строками, которые коллеги давно забрали.
     * Оператор жал «Принять» и получал отказ, а число над вкладкой при этом
     * с содержимым не сходилось. Ниже, на обычной ветке догона, этот же
     * перезапрос стоит и обоснован тем же: пропущенный `inbox:claimed`
     * инкрементами не догнать.
     */
    if (resumed) {
      refetchOpenScreens();
      void queryClient.invalidateQueries({ queryKey: qk.inbox.list });
    }
    return;
  }

  /**
   * Сами строки очереди догоняются не диффом, а перезапросом: хаб события не
   * буферизирует, а пропущенный `inbox:claimed` оставил бы на экране строку,
   * которую давно забрал коллега — оператор нажал бы «Принять» и получил
   * отказ. Список короткий, один запрос дешевле любой сверки.
   */
  void queryClient.invalidateQueries({ queryKey: qk.inbox.list });

  if (Date.now() - Date.parse(since) > 30 * 60_000) {
    // Офлайн дольше 30 минут (01 §11.7 п.4): полный refetch дешевле диффа.
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: qk.conversations.root }),
      (свернутьЛентыКХвосту(), queryClient.invalidateQueries({ queryKey: qk.messages.root })),
    ]);
    return;
  }

  try {
    // Перекрытие −30 с безопасно: appendMessage дедуплицирует по message.id.
    const overlap = new Date(Date.parse(since) - 30_000).toISOString();
    /*
     * ⚠ `tab=any` — ИНАЧЕ ЗАКРЫТЫЕ В ДИФФ НЕ ПОПАДАЮТ (28.08).
     *
     * Без вкладки сервер берёт умолчание `all`, а оно исключает закрытые
     * (`services/conversations.py`: «`all` исключает закрытые»). Диалог,
     * который коллега закрыл, пока у меня рвалась связь, в дифф не приезжал
     * вовсе — и оставался на экране «в работе». Оператор открывал его,
     * дописывал клиенту и узнавал о закрытии по 422 «Диалог закрыт», уже
     * потратив время на ответ.
     *
     * `any` заведён ровно как «везде, включая закрытые» и покрыт тестами
     * сервера. Строк добавляется немного: закрытие — редкое событие, а предел
     * в 200 остаётся прежним.
     */
    const diff = await http.get<ConversationsPage>(
      `/conversations?tab=any&updated_since=${encodeURIComponent(overlap)}&limit=200`,
    );
    useUnreadStore.getState().seedFromRows(diff.items);
    let missing = false;
    for (const row of diff.items) {
      const found = applyConversationRow(row);
      missing = missing || !found;
    }
    if (missing) {
      // Диффом приехали строки, которых в кэше списка нет, — их место знает
      // только сервер. Деталь при этом уже обновлена `applyConversationRow`.
      void queryClient.invalidateQueries({
        queryKey: CONVERSATIONS_LIST_KEY,
        refetchType: "active",
      });
    }

    // Хвост ленты открытого диалога — по сохранённому next_cursor (01 §11.7 п.3).
    const { activeConversationId } = useChatUiStore.getState();
    const cursor = activeConversationId ? lastKnownCursor(activeConversationId) : null;
    if (activeConversationId && cursor) {
      /*
       * ХВОСТ ДОБИРАЕТСЯ ДО КОНЦА, А НЕ ОДНОЙ СТРАНИЦЕЙ.
       *
       * Было: ровно один запрос на 50 сообщений, и `has_more_after` из ответа
       * никто не смотрел. Всё, что не поместилось, оставалось в базе и в
       * ленту не попадало — молча, без единого признака на экране. Курсор при
       * этом сдвигался на конец полученной страницы, то есть пропуск
       * закреплялся: следующий догон начинал уже ПОСЛЕ дыры.
       *
       * Пятьдесят сообщений за один обрыв — не выдумка: девять каналов, а
       * обрыв бывает и на десять минут (рестарт api, потеря wifi на кухне,
       * спящий ноутбук). Плюс в диалог пишет не только клиент — туда же
       * ложатся ответы бота и системные строки о передаче.
       *
       * Потолок в 10 страниц — против бесконечного цикла, если сервер вдруг
       * вернёт `has_more_after` с тем же курсором. Упёрлись в потолок —
       * честный перезапрос ленты: пятьсот сообщений догонять по одному глупее,
       * чем взять первую страницу заново.
       */
      let next: string | null = cursor;
      let more = true;
      let guard = 10;
      while (more && next && guard > 0) {
        guard -= 1;
        const tail: MessagesPage = await http.get<MessagesPage>(
          `/conversations/${activeConversationId}/messages?after=${encodeURIComponent(next)}&limit=50`,
        );
        for (const m of tail.items) upsertMessage(m.conversation_id, m);
        rememberCursor(activeConversationId, tail.page.next_cursor);
        more = tail.page.has_more_after;
        // Курсор не сдвинулся — идти дальше некуда: иначе тот же запрос по кругу.
        next = tail.page.next_cursor !== next ? tail.page.next_cursor : null;
      }
      if (more) {
        void queryClient.invalidateQueries({
          queryKey: qk.messages.list(activeConversationId),
          refetchType: "active",
        });
      }
    } else if (activeConversationId) {
      /*
       * Диалог открыт, а курсора нет. Так бывает: реестр курсоров живёт в
       * памяти модуля и пуст после перезагрузки вкладки и после смены
       * пользователя (`clearCursors` в `stopRealtime`), а заполняется он
       * только ответом ленты. Обрыв в эту щель — и хвост докачивать нечем.
       *
       * Молчать здесь нельзя: строка в списке из диффа обновится (превью
       * нового сообщения появится), а сама лента останется без него. Хуже
       * пустого экрана: человек видит в списке «клиент написал», открытая
       * лента показывает старое, и он решает, что сообщение пропало.
       */
      void queryClient.invalidateQueries({
        queryKey: qk.messages.list(activeConversationId),
        refetchType: "active",
      });
    }
  } catch {
    /*
     * Догон best-effort: не получился — честный перезапрос открытых экранов.
     *
     * ЛЕНТА ЗДЕСЬ ОБЯЗАТЕЛЬНА, и раньше её не было — перезапрашивался только
     * список. Цена пропуска несимметрична: список подтянется сам при любом
     * следующем событии, а лента открытого диалога — нет, и сообщения,
     * пришедшие за время обрыва, не появятся в ней до перехода в другой
     * диалог и обратно. То есть оператор смотрит в диалог, в котором клиент
     * уже написал, и не отвечает.
     *
     * Сюда попадает и обрыв хвоста на середине (запросов теперь может быть
     * несколько): часть сообщений уже легла в ленту, часть нет — перезапрос
     * закрывает дыру целиком.
     */
    refetchOpenScreens();
  }
}

/**
 * Состав зрителей диалога (SCEN-48) и предупреждение о столкновении.
 *
 * ПОЧЕМУ ТУТ ЕЩЁ И ТОСТ. Постоянный признак «диалог открыт коллегой» рисует
 * экран диалога (`useOtherViewers`), но признак работает, только если на него
 * смотрят. Опасный момент — другой: человек уже читает переписку и собирается
 * отвечать, а коллега открывает тот же диалог у себя. Именно тогда, а не
 * «когда-нибудь потом», и нужно сказать вслух; иначе оба дописывают свои
 * ответы и клиент получает два.
 *
 * Говорим ТОЛЬКО про новых соседей: состав приходит целиком и на каждое
 * изменение, и без сравнения с предыдущим тост повторялся бы при каждом чужом
 * приходе-уходе, включая свой собственный реконнект.
 */
/**
 * Когда мы в последний раз видели человека в этом диалоге и когда предупреждали.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «идёт спам „Диалог открыт не только у вас“, хотя
 * человек просто в сети». Так и было. Состав зрителей приходит целиком и на
 * КАЖДОЕ изменение, а любой обрыв связи у коллеги — это `leave` и следом
 * `touch`: он исчезал из состава и через секунду возвращался. Для нашего
 * сравнения «кто новый» он оказывался новым КАЖДЫЙ РАЗ, и человеку, спокойно
 * сидящему в диалоге, прилетало предупреждение за предупреждением.
 *
 * Две памяти, и обе нужны. Первая гасит мигание связи: вернувшийся через
 * секунду — не новичок, а тот же человек. Вторая гасит повтор по существу:
 * если про этого соседа в этом диалоге уже сказали, второй раз за десять минут
 * говорить незачем — он там же, где был.
 */
const ВИДЕЛИ = new Map<string, number>();
const ПРЕДУПРЕЖДАЛИ = new Map<string, number>();

/** Забыть, кого видели и о ком предупреждали (смена человека за компьютером, тесты). */
export function forgetViewerWarnings(): void {
  ВИДЕЛИ.clear();
  ПРЕДУПРЕЖДАЛИ.clear();
}
/** Пропажа короче этого — обрыв связи, а не уход из диалога. */
const МИГАНИЕ_МС = 90_000;
/** Про одного и того же соседа в одном диалоге — не чаще этого. */
const ПОВТОР_МС = 10 * 60_000;

export function applyViewers(convId: string, viewers: UserRef[]): void {
  const meId = useSessionStore.getState().user?.id ?? null;
  const store = useViewersStore.getState();
  const known = new Set((store.byConversation[convId] ?? []).map((v) => v.id));
  store.setViewers(convId, viewers);

  const сейчас = Date.now();
  const свежий = (id: string, память: Map<string, number>, окно: number): boolean => {
    const было = память.get(`${convId}:${id}`);
    return было !== undefined && сейчас - было < окно;
  };

  // Свой собственный приход в список поводом для тревоги не является.
  const newcomers = viewers.filter(
    (v) =>
      v.id !== meId &&
      !known.has(v.id) &&
      !свежий(v.id, ВИДЕЛИ, МИГАНИЕ_МС) &&
      !свежий(v.id, ПРЕДУПРЕЖДАЛИ, ПОВТОР_МС),
  );
  for (const v of viewers) {
    if (v.id !== meId) ВИДЕЛИ.set(`${convId}:${v.id}`, сейчас);
  }
  if (newcomers.length === 0) return;
  for (const v of newcomers) ПРЕДУПРЕЖДАЛИ.set(`${convId}:${v.id}`, сейчас);
  // Предупреждаем только про ОТКРЫТЫЙ диалог: сервер и так шлёт состав лишь
  // подписанным, но подписка меняется кадром, а стор — мгновенно, и в зазоре
  // между ними тост мог бы прилететь про уже покинутый диалог.
  if (useChatUiStore.getState().activeConversationId !== convId) return;

  showToast({
    title: "Диалог открыт не только у вас",
    message: `${newcomers.map((v) => v.full_name).join(", ")} — договоритесь, кто отвечает, иначе клиент получит два ответа`,
    tone: "warning",
    autoClose: 8000,
  });
}

/** Диспетчер входящих кадров WS (pong обрабатывает WsClient, сюда не доходит). */
export function applyWsEvent(e: WsServerEvent | WsInboxEvent): void {
  /*
   * ЖИВАЯ ЛЕНТА СЛУШАЕТ ОТСЮДА, А НЕ ИЗ СВОЕГО СОКЕТА.
   *
   * Второе соединение стоило бы второго тикета, второго heartbeat'а и второй
   * очереди переподключения — но главное не в этом. Хаб раздаёт кадры по
   * правам и по каналам (очередь сужена `eligible_operator_ids`, заметки не
   * уходят без `notes:read`, `account:needs_reauth` — только администратору),
   * и лента, собранная из УЖЕ ПРИНЯТЫХ кадров, наследует весь этот отбор
   * бесплатно и навсегда. Свой источник пришлось бы фильтровать заново —
   * второй копией матрицы прав, которая однажды разойдётся с первой.
   *
   * Стоит перед `switch`, а не в его ветках: лента обязана видеть и те кадры,
   * на которые рабочее место не реагирует (чужой `presence:online`), — иначе
   * «что происходит в системе» молчало бы ровно о людях.
   */
  recordWsFrame(e);

  switch (e.type) {
    case "inbox:new":
      applyInboxNew(e.data.conversation, e.data.can_claim, e.data.returned);
      break;

    case "inbox:claimed":
      applyInboxClaimed(
        e.data.conversation_id,
        e.data.claimed_by,
        e.data.conversation_patch,
        e.data.is_mine,
      );
      break;

    case "inbox:released":
      applyInboxReleased(
        e.data.conversation,
        e.data.released_by,
        e.data.can_claim,
        e.data.conversation_patch,
      );
      break;

    case "inbox:declined":
      applyInboxDeclined(
        e.data.conversation_id,
        e.data.declined_by,
        { count: e.data.count, escalated: e.data.escalated },
        (e.data as { undone?: boolean }).undone,
      );
      break;

    case "message:new":
      applyNewMessage(e.data.conversation_id, e.data.message, e.data.conversation_patch);
      /*
       * ⚠ КАДР БЕЗ ПАТЧА — ЭТО УЧАСТНИКИ, И ОН СПРАШИВАЕТ СПИСОК (ревью 06.09).
       * Без `conversation_patch` сервер шлёт `message:new` ровно в двух местах:
       * «позвал(а) в диалог» и «вышел(а) из диалога» (`routes/conversations.py`).
       * Оба меняют состав «Моих» у позванного — `mine_condition` считает и
       * участников, — а по строке этого не видно: ни статус, ни хозяин не
       * менялись, и `applyNewMessage` по ней промолчит. Кадры редкие, один
       * схлопнутый перезапрос на них — та же цена, что до 06.09; молчание же
       * оставило бы позванного на вкладке «Все» без диалога в «Моих» и с
       * прежним числом «на руках» до тихой сверки. Здесь, а не в
       * `applyNewMessage`: туда же без патча приходит и своё отправляемое
       * сообщение, а ему перезапрос не нужен.
       */
      if (e.data.conversation_patch === undefined) scheduleListRefetch();
      break;

    /*
     * Карточка клиента изменилась на сервере (телефон из текста входящего —
     * жалоба владельца 31.08 «чтобы номер привязался, приходится обновлять
     * страницу»). Сам телефон в кадре НЕ едет: у карточки своя ручка с
     * правами, и рассылать персональные данные всем подписчикам ради
     * экономии одного запроса нельзя. Кадр говорит только «перезапроси».
     */
    case "client:updated":
      // Оба ключа: телефон живёт в детали диалога, «ещё номера» — в семействе
      // клиентов. Сбрось один — и карточка обновится наполовину, ровно как
      // было на записи владельца 01.09.
      сбросКлиента(e.data.conversation_id ?? undefined);
      break;

    case "message:status":
      patchMessageStatus(e.data.conversation_id, e.data.message_id, e.data.delivery_status, e.data.error);
      // Строка слева обязана покраснеть в тот же миг, что и пузырь (#26):
      // ровно между ними человек и решает, идти ли к следующему клиенту.
      if (e.data.conversation_patch) {
        applyConversationPatch(e.data.conversation_id, e.data.conversation_patch);
      }
      break;

    // Строку списка расшифровка не трогает: превью и время строки считаются
    // по самому сообщению, а оно не изменилось — изменился текст под ним.
    case "message:transcript":
      patchMessageTranscript(
        e.data.conversation_id,
        e.data.message_id,
        e.data.voice_transcript,
        e.data.voice_transcript_status,
      );
      break;

    case "conversation:updated":
      applyConversationPatch(e.data.conversation_id, e.data.patch);
      break;

    case "conversation:assigned": {
      // Строку пришлёт conversation:updated; назначение меняет вкладки — спросим сервер.
      // Массовая передача (руководитель раскидывает смену) даёт пачку таких кадров.
      scheduleListRefetch();
      if (!e.data.is_for_you) break;
      // Старый сервер поле не шлёт: тогда это предложение, как и было до него.
      const предложение = e.data.offer !== false;
      // ⚑ в строке ставим сразу — не ждём отдельного conversation:updated (11 §2.1).
      // Ответственного здесь не трогаем: у предложения он не меняется, а при
      // прямом назначении его несёт патч того же события.
      patchRowEverywhere(e.data.conversation_id, (row) => ({ ...row, transferred_to_me: true }));
      // Полоса «Принять» рисуется по детали, а предложение стоит во «Входящих»
      // получателя: без перезапроса обоих открытый диалог показывал «Войти в
      // диалог», а во «Входящих» ничего не появлялось до тихой сверки.
      void queryClient.invalidateQueries({
        queryKey: qk.conversations.detail(e.data.conversation_id),
        refetchType: "active",
      });
      void queryClient.invalidateQueries({ queryKey: qk.inbox.list, refetchType: "active" });
      void refreshInboxCount();
      const кто = подписьСотрудника(e.data.assigned_by);
      showToast({
        title: предложение ? "Диалог передан вам" : "Вам назначили диалог",
        message: `${предложение ? "Передал(а)" : "Назначил(а)"}: ${кто}${e.data.comment ? ` — «${e.data.comment}»` : ""}`,
        color: "lp",
        news: true,
      });
      notifyAssignedToMe(); // звук у получателя (11 §2.4)
      // Приоритетный нативный тост: показывается даже в фокусе (04 §4.1).
      // Кнопки «Принять / Отклонить» — только у предложения: у прямого
      // назначения принимать нечего, и нажатие молча ничего не делало.
      toastForHandoff(e.data.conversation_id, кто, { offer: предложение });
      break;
    }

    case "message:deleted":
      // заметка удалена автором (17.08) — убрать из ленты у всех, кто её видит
      removeMessageFromCache(e.data.conversation_id, e.data.message_id);
      break;

    case "account:backfill": {
      // Загрузка истории (17.08): прогресс на карточке и свежие диалоги в
      // списках — без F5. Дроссель обязателен: на каждый кадр перезапрашивать
      // списки значит «всё скачет» (жалоба владельца) — раз в 4 секунды
      // достаточно живо и не дёргает скролл.
      const nowTs = Date.now();
      // финальные кадры (idle/failed/stopped) мимо дросселя: их не повторят,
      // и без них карточка застревала на «Загружаем…» до F5
      const finalPhase = ["idle", "failed", "stopped"].includes(e.data.phase ?? "");
      if (!finalPhase && nowTs - lastBackfillSyncAt < 4000) break;
      lastBackfillSyncAt = nowTs;
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      // Выгрузка истории приносит НОВЫЕ диалоги — это про списки; открытую
      // деталь она не меняет, и дёргать её вместе с ними незачем.
      void queryClient.invalidateQueries({
        queryKey: CONVERSATIONS_LIST_KEY,
        refetchType: "active",
      });
      break;
    }

    case "account:needs_reauth":
      void queryClient.invalidateQueries({ queryKey: qk.accounts });
      showToast({
        title: "Аккаунт Авито требует переподключения",
        // Приём НЕ останавливается (проверка 24.09): вебхукам токен не нужен,
        // не уходят только ответы — как пишет карточка канала. Прежний текст
        // «приём остановлен» толкал отключить или удалить канал, а это стирает
        // переписку.
        message: `«${e.data.title}»: ответы клиентам не уходят, обращения продолжают приходить`,
        color: "red",
        autoClose: false,
      });
      break;

    case "notify":
      // Центр уведомлений (14 §4): строка с `id` уходит в колокольчик и в
      // плашку критичного, служебный кадр без `id` остаётся тостом, как был.
      applyNotifyEvent(e.data, e.ts);
      break;

    case "conversation:viewers":
      applyViewers(e.data.conversation_id, e.data.viewers);
      break;

    case "presence:online":
      // Своё состояние, выставленное с другого устройства. Чужие статусы
      // экран спрашивает сам, когда они ему нужны.
      if (
        e.data.user_id === useSessionStore.getState().user?.id &&
        (e.data.status === "online" || e.data.status === "away")
      ) {
        принятьСвойСтатус(e.data.status);
      }
      break;

    default:
      // ⚠ `typing` СЮДА ПОПАДАЕТ И ЗДЕСЬ ЖЕ УМИРАЕТ. Сервер его честно
      // ретранслирует (`ws/hub.py`), стили индикатора нарисованы
      // (`chat-thread.css`, витрина UiKitPage) — не хватает ровно этой ветки и
      // вызова `sendTyping` из композера. Пока их нет, «Печатает…» в продукте
      // не существует, что бы ни говорили соседние комментарии.
      // Неизвестные типы игнорируются (01 §11.2).
      break;
  }
}
