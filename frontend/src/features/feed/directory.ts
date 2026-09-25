import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import type {
  AssignableUsersResponse,
  ConversationDetailDto,
  ConversationDto,
  ConversationsPage,
  UserRef,
} from "@/shared/api/types";
import type { ConversationPatch } from "@/shared/realtime/wsEvents";

/**
 * СПРАВОЧНИК ИМЁН ДЛЯ ЖИВОЙ ЛЕНТЫ.
 *
 * ЗАЧЕМ ОН ВООБЩЕ НУЖЕН. Лента обязана писать «Наталья: новое сообщение», а не
 * «в диалоге 8f3c-… новое сообщение». Между тем самый частый кадр — `message:new`
 * — имени клиента НЕ НЕСЁТ вовсе: сервер кладёт в него `conversation_id`,
 * сообщение и дельту строки (`app/services/inbound.py`, `app/services/messages.py`).
 * Так и задумано — у получателя строка диалога уже есть, дублировать её в
 * каждом кадре при десяти тысячах сообщений в сутки незачем. Но у ленты своей
 * строки нет: она смотрит на поток, а не на список.
 *
 * ОТКУДА БЕРУТСЯ ИМЕНА. Из трёх мест, в порядке дешевизны:
 *  1. То, что уже проехало по этой же ленте: `inbox:new` и `inbox:released`
 *     везут диалог ЦЕЛИКОМ (у только что подключившегося его нет вовсе), а
 *     `conversation_patch` иногда несёт `client`/`account` кусочками.
 *  2. Кэш TanStack Query — деталь открытого диалога (ключ точный, поиск за
 *     константу) и уже загруженные страницы списка.
 *  3. Ничего. Тогда строка честно говорит «Клиент» — общий фолбэк интерфейса
 *     (`features/templates/vars.ts`), а не выдуманное имя и не идентификатор.
 *
 * ПОЧЕМУ ЗАПОМИНАЕМ, А НЕ ИЩЕМ КАЖДЫЙ РАЗ. Поиск по страницам списка — проход
 * по массиву; на кадр в секунду это ничто, но кадры идут часами, а результат
 * не меняется. Найденное кладём сюда, и следующий кадр того же диалога имя уже
 * не ищет.
 *
 * ПОЧЕМУ СПРАВОЧНИК ОГРАНИЧЕН ПО РАЗМЕРУ. Вкладка живёт сутками, диалоги за
 * сутки исчисляются тысячами. Несвязанный `Map` рос бы ровно столько же —
 * ради имени, которое в ленте уже не показывается (сама лента давно обрезана).
 * Поэтому предел есть и здесь, и вытесняем по порядку добавления: `Map` в JS
 * хранит ключи в порядке вставки, и первый ключ итерации — самый старый.
 */

/** Диалогов в справочнике. Заведомо больше предела самой ленты — см. FEED_LIMIT. */
const CONVERSATIONS_LIMIT = 600;

/** Сотрудников: их десятки, а не тысячи, но предел нужен и здесь — на всякий случай. */
const PEOPLE_LIMIT = 200;

export interface ConversationFacts {
  /** Имя клиента; `null` — вебхук Авито имени не принёс, и это норма. */
  clientName: string | null;
  accountId: string | null;
  accountTitle: string | null;
  /**
   * Последний известный ленте статус диалога.
   *
   * Хранится ради ОТБОРА ШУМА, а не ради показа. Кадр `conversation:updated`
   * едет почти на каждое действие и везёт статус ВСЕГДА — даже когда тот не
   * менялся (`_conversation_patch` в app/api/routes/conversations.py собирает
   * дельту из детали целиком). Без этого поля лента писала бы «статус — В
   * работе» после каждой заметки, каждого тега и каждого закрепления; строка,
   * которая повторяется без причины, перестаёт читаться первой.
   */
  status: string | null;
}

const conversations = new Map<string, ConversationFacts>();
const people = new Map<string, string>();

function put<K, V>(map: Map<K, V>, key: K, value: V, limit: number): void {
  map.delete(key); // переносим в конец: свежеупомянутый вытесняется последним
  map.set(key, value);
  if (map.size > limit) {
    const oldest = map.keys().next();
    if (!oldest.done) map.delete(oldest.value);
  }
}

/**
 * Слить новые сведения о диалоге со старыми.
 *
 * Именно СЛИТЬ, а не заменить: `conversation_patch` приходит кусочками, и
 * кадр с одним только `account` не имеет права стереть уже известное имя
 * клиента. Обратное однажды уже стоило бы ленте строк «Клиент: ответ не
 * доставлен» после того, как имя в ней было.
 */
function mergeFacts(convId: string, next: Partial<ConversationFacts>): ConversationFacts {
  const prev = conversations.get(convId);
  const merged: ConversationFacts = {
    clientName: next.clientName ?? prev?.clientName ?? null,
    accountId: next.accountId ?? prev?.accountId ?? null,
    accountTitle: next.accountTitle ?? prev?.accountTitle ?? null,
    status: next.status ?? prev?.status ?? null,
  };
  put(conversations, convId, merged, CONVERSATIONS_LIMIT);
  return merged;
}

/** Запомнить всё, что известно из целой строки диалога. */
export function rememberConversation(row: ConversationDto): void {
  mergeFacts(row.id, {
    clientName: row.client?.name ?? null,
    accountId: row.account?.id ?? null,
    accountTitle: row.account?.title ?? null,
    status: row.status ?? null,
  });
  if (row.assignee) rememberPerson(row.assignee);
}

/** Запомнить то немногое, что бывает в дельте строки. */
export function rememberPatch(convId: string, patch: ConversationPatch | undefined): void {
  if (!patch) return;
  mergeFacts(convId, {
    clientName: patch.client?.name ?? null,
    accountId: patch.account?.id ?? null,
    accountTitle: patch.account?.title ?? null,
    status: patch.status ?? null,
  });
  if (patch.assignee) rememberPerson(patch.assignee);
}

export function rememberPerson(user: UserRef | null | undefined): void {
  if (!user?.id || !user.full_name) return;
  put(people, user.id, user.full_name, PEOPLE_LIMIT);
}

/**
 * Ищем диалог в кэше запросов: сперва деталь (точный ключ), потом страницы
 * списка.
 *
 * ЦИКЛАМИ, А НЕ `flatMap().find()`. Разница не в красоте: у безымянного клиента
 * (вебхук Авито имени не несёт — это норма, а не редкость) поиск повторяется на
 * КАЖДОМ его кадре, а `flatMap` при этом собирал бы два новых массива со всеми
 * строками всех закэшированных фильтров. Цикл с досрочным выходом не выделяет
 * ничего и обычно останавливается на первых строках — свежий диалог лежит
 * вверху списка.
 *
 * ОТРИЦАТЕЛЬНЫЙ ОТВЕТ НЕ ЗАПОМИНАЕМ НАМЕРЕННО. Запомнить «не нашли» было бы
 * дешевле, но это запечатало бы диалог безымянным навсегда: руководитель,
 * открывший ленту ДО списка чатов (обычный порядок — он и идёт сюда, а не в
 * переписку), получил бы промах на первом же кадре, а подъехавший через минуту
 * список уже никого бы не спас.
 */
function findInQueryCache(convId: string): ConversationFacts | null {
  let row: ConversationDto | undefined = queryClient.getQueryData<ConversationDetailDto>(
    qk.conversations.detail(convId),
  );
  if (!row) {
    const lists = queryClient.getQueriesData<InfiniteData<ConversationsPage>>({
      queryKey: CONVERSATIONS_LIST_KEY,
    });
    outer: for (const [, data] of lists) {
      for (const page of data?.pages ?? []) {
        for (const item of page.items) {
          if (item.id === convId) {
            row = item;
            break outer;
          }
        }
      }
    }
  }
  if (!row) return null;
  return {
    clientName: row.client?.name ?? null,
    accountId: row.account?.id ?? null,
    accountTitle: row.account?.title ?? null,
    // Статус из кэша НЕ берём: он попал бы в справочник как «уже известный», и
    // первое же настоящее изменение статуса лента бы проглотила, если кэш
    // успел обновиться раньше кадра. Пусто здесь честнее — см. `status`.
    status: null,
  };
}

/** Что лента знает об этом диалоге прямо сейчас. Никогда не `null` — только пустые поля. */
export function conversationFacts(convId: string): ConversationFacts {
  const known = conversations.get(convId);
  // Не «нашли что-то», а «знаем имя»: строка, попавшая в справочник кадром с
  // одним лишь каналом, обязана дать поиску в кэше второй шанс — иначе диалог,
  // открытый оператором минуту спустя, так и остался бы безымянным до вечера.
  if (known?.clientName) return known;
  const found = findInQueryCache(convId);
  if (!found) {
    return known ?? { clientName: null, accountId: null, accountTitle: null, status: null };
  }
  return mergeFacts(convId, found);
}

/**
 * Имя сотрудника по идентификатору. `null` — не знаем.
 *
 * Нужно ровно одному кадру — `presence:online`: в нём едут `user_id` и статус,
 * имени нет (`app/ws/presence.py`). Запасной источник — кэш «кому можно
 * передать диалог»: его загружает экран чатов, и там есть полные имена всех,
 * кто вообще может оказаться в этом кадре.
 */
export function personName(userId: string): string | null {
  const known = people.get(userId);
  if (known) return known;
  const assignable = queryClient.getQueryData<AssignableUsersResponse>(qk.users.assignable);
  const found = assignable?.items.find((u) => u.id === userId);
  if (!found) return null;
  put(people, userId, found.full_name, PEOPLE_LIMIT);
  return found.full_name;
}

/**
 * Забыть всё. Зовётся при выходе вместе с очередью, непрочитанным и зрителями
 * (`shared/realtime/realtime.ts`) и по той же причине: на одном компьютере
 * люди работают по очереди (11 §2.5), а здесь лежат имена клиентов и коллег.
 */
export function clearDirectory(): void {
  conversations.clear();
  people.clear();
}
