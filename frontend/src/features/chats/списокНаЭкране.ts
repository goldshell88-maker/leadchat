import type { InfiniteData, QueryKey } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fetchConversations } from "./api";
import { fetchInboxQueue } from "./inbox/api";

/**
 * ОДИН ОТВЕТ НА ВОПРОС «КТО СЛЕДУЮЩИЙ» — и для клавиш, и для кнопок листания.
 *
 * ⚠ ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ. Этот же расчёт уже трижды заводился заново и
 * трижды расходился с тем, что видит глаз:
 *
 *   1) стрелки и J/K читали кэш ОБЫЧНОГО списка даже при открытой очереди —
 *      во «Входящих» клавиатура уносила в диалог, которого на экране нет;
 *   2) «следующий после закрытия» перебирал ВСЕ закэшированные фильтры и брал
 *      первый попавшийся, то есть чаще всего самый старый: человек закрывал
 *      свой диалог в «Моих», а попадал к соседу по «Всем»;
 *   3) кнопка «Следующий (N)» в очереди считала число по общему счётчику и
 *      обещала переход, которого не было.
 *
 * Теперь источник один: та выдача, чей ключ сейчас нарисован слева. Кнопки
 * листания в шапке ленты и клавиши обязаны вести В ОДНО И ТО ЖЕ МЕСТО —
 * иначе человек, попробовав оба способа, перестаёт верить обоим.
 */

export interface ВидимыйСписок {
  /** Ключ кэша той выдачи, что сейчас в левой колонке. */
  readonly ключ: QueryKey;
  /** Открыта очередь «Входящие» — отдельный ресурс, а не срез списка. */
  readonly очередь: boolean;
}

export function видимыйСписок(): ВидимыйСписок {
  const { filters, inboxOpen } = useChatUiStore.getState();
  return {
    ключ: inboxOpen ? qk.inbox.list : qk.conversations.list(filters),
    очередь: inboxOpen,
  };
}

/**
 * Строки видимого списка в порядке отрисовки.
 *
 * ⚠ ДУБЛИ ОТБРАСЫВАЕМ ТУТ ЖЕ, КАК ИХ ОТБРАСЫВАЕТ САМА КОЛОНКА (находка 25.08).
 * Страницы тянутся по смещению, а список отсортирован по времени последнего
 * сообщения: клиент написал в диалог со второй страницы — тот прыгнул на
 * первую, всё ниже сдвинулось, и пограничный диалог приезжает ВТОРЫМ разом.
 * Глаз его видит один раз (`ChatListPane` дедуплицирует), и «следующий» обязан
 * считать так же — иначе шаг вниз возвращал бы в уже прочитанный диалог.
 */
export function строкиНаЭкране(): ConversationDto[] {
  const data = queryClient.getQueryData<InfiniteData<ConversationsPage>>(видимыйСписок().ключ);
  const видели = new Set<string>();
  const out: ConversationDto[] = [];
  for (const row of data?.pages.flatMap((p) => p.items) ?? []) {
    if (видели.has(row.id)) continue;
    видели.add(row.id);
    out.push(row);
  }
  return out;
}

function загружено(data: InfiniteData<ConversationsPage> | undefined): number {
  return data?.pages.reduce((n, p) => n + p.items.length, 0) ?? 0;
}

/**
 * Есть ли за последней загруженной строкой ещё диалоги на сервере.
 *
 * Формула та же, что у `getNextPageParam` в `ChatListPane` и `useInboxQueue`:
 * загружено меньше, чем сервер насчитал всего. Разойтись ей с ними нельзя —
 * это ответ на вопрос «список кончился или просто не долистан», и разные
 * ответы означали бы кнопку, которая врёт про край списка.
 */
export function естьНезагруженные(): boolean {
  const data = queryClient.getQueryData<InfiniteData<ConversationsPage>>(видимыйСписок().ключ);
  const последняя = data?.pages[data.pages.length - 1];
  if (!последняя) return false;
  return загружено(data) < последняя.page.total;
}

export interface Шаг {
  /** Куда вести. `null` — идти пока некуда. */
  readonly куда: string | null;
  /** Кого спросить у сервера заранее: следующий в ТУ ЖЕ сторону. */
  readonly следом: string | null;
  /** Идти некуда, но список не кончился — сначала догрузить страницу. */
  readonly догрузить: boolean;
}

const НЕКУДА: Шаг = { куда: null, следом: null, догрузить: false };

/**
 * Шаг по видимому списку от открытого диалога.
 *
 * ⚠ ДИАЛОГ, КОТОРОГО В СПИСКЕ НЕТ, — ЭТО «НЕКУДА», А НЕ «НАЧНИ СНАЧАЛА». Так
 * выглядит приход из «Разбора диалогов» и переход по прямой ссылке: там свои
 * фильтры, и открытый диалог в левую колонку часто не входит. Прыжок на первую
 * строку был бы переходом, которого человек не просил.
 */
export function шагПоВидимому(convId: string, вперёд: boolean): Шаг {
  const rows = строкиНаЭкране();
  const at = rows.findIndex((r) => r.id === convId);
  return at === -1 ? НЕКУДА : шагОт(rows, at, вперёд);
}

function шагОт(rows: ConversationDto[], at: number, вперёд: boolean): Шаг {
  const шаг = вперёд ? 1 : -1;
  const цель = rows[at + шаг];
  if (!цель) {
    return вперёд && естьНезагруженные() ? { ...НЕКУДА, догрузить: true } : НЕКУДА;
  }
  return { куда: цель.id, следом: rows[at + шаг * 2]?.id ?? null, догрузить: false };
}

/** Ключ списка, для которого страница уже запрошена этим модулем. */
let вПути: string | null = null;

/**
 * Догрузить следующую страницу видимого списка.
 *
 * ⚠ ЗАЧЕМ СВОЙ ПУТЬ, КОГДА ЕСТЬ `fetchNextPage`. Колонка тянет следующую
 * страницу, когда до строки-лоадера ДОБИРАЕТСЯ ПРОКРУТКА, — и на широком экране
 * этого хватает: переход к строке подтягивает её в видимую часть, лоадер
 * показывается, страница приезжает. Но ниже 768px колонки на экране нет вовсе
 * (`ChatsPage`: телефон — стек из двух экранов, лента ВМЕСТО списка), тянуть
 * некому, и на пятидесятой строке листание упиралось бы в стену при полутысяче
 * диалогов в выдаче.
 *
 * ⚠ ГОНКУ ЗАКРЫВАЕТ СВЕРКА ПЕРЕД ЗАПИСЬЮ, А НЕ ЗАМОК. Пока мы ходим к серверу,
 * колонка может догрузить ту же страницу сама. Две одинаковые страницы подряд
 * — это не просто дубли строк: `getNextPageParam` считает смещение по числу
 * загруженных, и следующий запрос ушёл бы мимо полусотни диалогов. Поэтому
 * записываем, только если с начала запроса ничего не приехало.
 */
export async function догрузитьСтраницу(): Promise<void> {
  const { ключ, очередь } = видимыйСписок();
  const метка = JSON.stringify(ключ);
  if (вПути === метка) return;
  const было = queryClient.getQueryData<InfiniteData<ConversationsPage>>(ключ);
  const смещение = загружено(было);
  const последняя = было?.pages[было.pages.length - 1];
  if (!последняя || смещение >= последняя.page.total) return;

  вПути = метка;
  try {
    const { filters } = useChatUiStore.getState();
    const страница = очередь
      ? await fetchInboxQueue(смещение)
      : await fetchConversations(filters, смещение);
    queryClient.setQueryData<InfiniteData<ConversationsPage>>(ключ, (old) => {
      if (!old) return old;
      if (загружено(old) !== смещение) return old; // колонка успела сама — наша страница лишняя
      return { pages: [...old.pages, страница], pageParams: [...old.pageParams, смещение] };
    });
  } finally {
    вПути = null;
  }
}
