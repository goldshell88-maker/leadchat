import type { InfiniteData, QueryKey } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, type ConversationFilters } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { compareConversationRows } from "@/shared/lib/conversationOrder";

/**
 * Точечные правки кэшей списка диалогов ДЛЯ СОБСТВЕННЫХ НАЖАТИЙ (замер 06.09).
 *
 * `applyWsEvent.ts` умеет править строку там, где она уже есть, и убирать её
 * из очереди — этого хватает кадрам сервера. Своё нажатие ждёт большего:
 * строка обязана УЙТИ из «Моих» в момент закрытия и ПОЯВИТЬСЯ там в момент
 * принятия, а не через круг-другой перезапроса. Замер nginx за 8 часов боя:
 * закрытие — 377 раз, p50 35 мс, и всё это время строка стояла на месте, а
 * переход к следующему ждал ответа; принятие — 552 раза, p50 43 мс, строка в
 * «Моих» — только после второго круга (список + числа), 3–5 запросов на приём.
 *
 * ⚠ ЗДЕСЬ НЕТ НИ ОДНОГО ПЕРЕЗАПРОСА, И ЭТО ГЛАВНОЕ. Все помощники правят кэш и
 * молчат: спросить сервер (`refetchListsNow`) — решение того, кто нажал, и
 * оно принимается ПОСЛЕ ответа, а не до. Перезапрос, ушедший вместе с PATCH,
 * привозил бы список с ещё живой строкой и вернул бы её на глазах.
 *
 * ⚠ НЕ ТРОГАЛИ — НЕ ЗАПИСЫВАЕМ. Возврат `undefined` из апдейтера для TanStack
 * Query значит «оставь как есть»; возврат прежнего `old` — запись с `success`,
 * которая гасит пометку «устарело» и обнуляет часы свежести (разбор 31.08 в
 * `applyWsEvent.patchRowEverywhere`). Каждый помощник ниже держит это правило.
 */

type СписокВКэше = InfiniteData<ConversationsPage>;

/** Фильтры выдачи из её ключа — `["conversations", "list", f]`. */
function фильтрыКлюча(key: QueryKey): ConversationFilters | null {
  const f = key[2];
  return f && typeof f === "object" ? (f as ConversationFilters) : null;
}

function плоско(data: СписокВКэше): ConversationDto[] {
  return data.pages.flatMap((p) => p.items);
}

/**
 * Пересобрать страницы после вставки прежней нарезкой; лишний элемент
 * достаётся последней. `total` двигается на всех страницах разом — это одно
 * число из одного ответа, и разъехавшись, оно врёт `getNextPageParam` (тот же
 * довод, что у `inbox/api.insertInboxRow`).
 */
function нарезатьСоВставкой(old: СписокВКэше, flat: ConversationDto[]): СписокВКэше {
  let cursor = 0;
  const lastIdx = old.pages.length - 1;
  const pages = old.pages.map((p, i) => {
    const size = p.items.length + (i === lastIdx ? 1 : 0);
    const items = flat.slice(cursor, cursor + size);
    cursor += size;
    return { ...p, items, page: { ...p.page, total: p.page.total + 1 } };
  });
  return { ...old, pages };
}

/** Убрать строку постранично — страницы остаются своими (как `removeInboxRow`). */
function безСтроки(old: СписокВКэше, convId: string): СписокВКэше {
  const pages = old.pages.map((p) => ({
    ...p,
    items: p.items.filter((r) => r.id !== convId),
    page: { ...p.page, total: Math.max(0, p.page.total - 1) },
  }));
  return { ...old, pages };
}

/**
 * Прячет ли выдача закрытые диалоги — зеркало `_tab_condition` сервера
 * (`services/conversations.py`): «Мои» и «Новые» — кроме закрытых, пока
 * человек не выбрал статус сам; «Все» и поиск уходят как `any` и закрытые
 * держат; «ждут ответа» — закрытый не ждёт по определению.
 *
 * Отсюда решается, откуда убирать строку при закрытии: из «Моих» — сразу, из
 * «Всех» — не убирать, а перекрасить.
 */
export function выдачаПрячетЗакрытые(f: ConversationFilters): boolean {
  if (f.status) return f.status !== "closed";
  if (f.waitingOnly) return true;
  if (f.tab === "all" || f.tab === "closed" || f.q || f.withClosed) return false;
  return true;
}

/**
 * Попадёт ли ТОЛЬКО ЧТО ВЗЯТАЯ МНОЙ строка в эту выдачу «Моих».
 *
 * Догадка нарочно осторожная: поиск и «без ответственного» не трогаем вовсе,
 * остальные срезы (аккаунт, тег, статус, ожидание) проверяем по полям строки.
 * Ошибка в любую сторону лечится фоновым перезапросом за один круг; ошибка
 * «вставили в чужой срез» стоила бы дороже — строка, которой в этой выдаче
 * быть не должно, читается как чужая.
 */
export function строкаВходитВМои(row: ConversationDto, f: ConversationFilters): boolean {
  if (f.tab !== "mine" || f.q || f.unassigned) return false;
  if (f.assigneeId && row.assignee?.id !== f.assigneeId) return false;
  if (f.accountId && row.account.id !== f.accountId) return false;
  if (f.tag && !row.tags.includes(f.tag)) return false;
  if (f.status ? row.status !== f.status : row.status === "closed") return false;
  if (f.waitingOnly && !row.waiting_since) return false;
  return true;
}

/** Где стояла убранная строка — чтобы вернуть её ровно туда при отказе сервера. */
export interface СнимокСтроки {
  места: Array<{ ключ: QueryKey; индекс: number; row: ConversationDto }>;
}

/**
 * Убрать строку из выдач, которые `где(фильтры)` назовёт, и запомнить места.
 * `null` — нигде не было (пришли по прямой ссылке, список не грузился).
 */
export function убратьСтроку(
  convId: string,
  где: (f: ConversationFilters) => boolean,
): СнимокСтроки | null {
  const места: СнимокСтроки["места"] = [];
  for (const [ключ, data] of queryClient.getQueriesData<СписокВКэше>({
    queryKey: CONVERSATIONS_LIST_KEY,
  })) {
    const f = фильтрыКлюча(ключ);
    if (!data || !f || !где(f)) continue;
    const flat = плоско(data);
    const индекс = flat.findIndex((r) => r.id === convId);
    if (индекс === -1) continue;
    места.push({ ключ, индекс, row: flat[индекс] });
    queryClient.setQueryData<СписокВКэше>(ключ, (old) => (old ? безСтроки(old, convId) : undefined));
  }
  return места.length ? { места } : null;
}

/** Вернуть строку на прежние места (откат закрытия). Дубли не плодим. */
export function вернутьСтроку(снимок: СнимокСтроки): void {
  for (const { ключ, индекс, row } of снимок.места) {
    queryClient.setQueryData<СписокВКэше>(ключ, (old) => {
      if (!old || old.pages.length === 0) return undefined;
      const flat = плоско(old);
      if (flat.some((r) => r.id === row.id)) return undefined;
      flat.splice(Math.min(индекс, flat.length), 0, row);
      return нарезатьСоВставкой(old, flat);
    });
  }
}

/**
 * Вставить строку в выдачи, которые `где(фильтры)` назовёт, — на место по
 * порядку сервера (`compareConversationRows`), дубли по id отбрасываются.
 * `true` — вставилась хоть куда-то. Кэша нет — не создаём: выдачу, которую
 * не открывали, заведёт её собственный запрос.
 */
export function вставитьСтроку(
  row: ConversationDto,
  где: (f: ConversationFilters) => boolean,
): boolean {
  let вставлена = false;
  for (const [ключ, data] of queryClient.getQueriesData<СписокВКэше>({
    queryKey: CONVERSATIONS_LIST_KEY,
  })) {
    const f = фильтрыКлюча(ключ);
    if (!data || !f || !где(f)) continue;
    queryClient.setQueryData<СписокВКэше>(ключ, (old) => {
      if (!old || old.pages.length === 0) return undefined;
      const flat = плоско(old);
      if (flat.some((r) => r.id === row.id)) return undefined;
      const at = flat.findIndex((r) => compareConversationRows(row, r) < 0);
      if (at === -1) flat.push(row);
      else flat.splice(at, 0, row);
      вставлена = true;
      return нарезатьСоВставкой(old, flat);
    });
  }
  return вставлена;
}

/**
 * Поправить строки, которые `где(строка)` назовёт, во всех выдачах разом.
 *
 * ⚠ БЕЗ ПЕРЕСОРТИРОВКИ — намеренно и с ограничением: сюда идут поля, которых
 * в ключе порядка нет (статус, предложение передачи, пометка клиента). Кто
 * двигает `last_message_at` или закрепление, тот зовёт
 * `applyWsEvent.applyConversationPatch` — там порядок пересчитывается.
 */
export function патчСтрок(
  где: (row: ConversationDto) => boolean,
  updater: (row: ConversationDto) => ConversationDto,
): boolean {
  let тронули = false;
  queryClient.setQueriesData<СписокВКэше>({ queryKey: CONVERSATIONS_LIST_KEY }, (old) => {
    if (!old) return undefined;
    let здесь = false;
    const pages = old.pages.map((p) => {
      if (!p.items.some(где)) return p;
      здесь = true;
      return { ...p, items: p.items.map((r) => (где(r) ? updater(r) : r)) };
    });
    if (!здесь) return undefined;
    тронули = true;
    return { ...old, pages };
  });
  return тронули;
}
