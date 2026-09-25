import { create } from "zustand";
import type { InboxCountDto } from "@/shared/api/types";

/**
 * Счётчик очереди «Входящие» (7.1). Отдельный стор, а НЕ поле `unreadStore`:
 * непрочитанные и очередь — разные величины и живут по разным правилам.
 * Непрочитанное — «мне написали, я не прочитал»; очередь — «диалог ничей и
 * ждёт, чтобы его взяли». Диалог может быть прочитан всеми и всё равно висеть
 * в очереди; принятый диалог уходит из очереди, но непрочитанные в нём
 * остаются. Смешать их в одном числе — значит показать оператору бессмыслицу.
 *
 * **Счётчик персональный.** «Отклонить» — решение одного оператора, диалог при
 * этом остаётся свободным для остальных; единого «размера очереди» в системе
 * нет. Отсюда правило обновления, заданное сервером (app/ws/hub.py, каталог
 * событий очереди): широковещательные кадры абсолютного числа не несут — фронт
 * двигает своё на ±1, а точное значение берёт оттуда, где оно персонально:
 * из ответов ручек `claim`/`decline`, из адресного `inbox:declined` и из
 * `GET /inbox/count` при каждом подключении сокета.
 *
 * Клиент помнит id известных ему элементов очереди, чтобы повторный
 * `inbox:claimed` (ретрансляция после reconnect) не увёл счётчик в минус:
 * удаление неизвестного id счётчик не трогает, а ближайшая сверка с сервером
 * всё равно вернёт точное значение.
 */

/** «Диалог принял Иван» — плашка вместо кнопок в открытом диалоге (7.1 п.4). */
export interface ClaimedNotice {
  convId: string;
  /** Имя принявшего — показывается пользователю дословно. */
  by: string;
}

interface InboxStoreState {
  /** Элементы очереди, известные клиенту (загруженная страница + события). */
  ids: Record<string, true>;
  /** Кто из очереди помечен «не берёт никто» — поимённо (находка №20). */
  escalatedIds: Record<string, true>;
  /** Сколько всего ждёт лично меня — бейдж вкладки, title и рейка. */
  count: number;
  /** Сколько из них уже не берёт никто (01: `escalated`). */
  escalated: number;
  claimedNotice: ClaimedNotice | null;

  /** Страница `GET /inbox` — сервер перезаписывает и состав, и итог. */
  seedFromServer(ids: string[], total: number, escalatedIds?: string[]): void;
  /** Абсолютные счётчики из персонального ответа/кадра; состав не трогаются. */
  setCounters(c: InboxCountDto): void;
  /** `inbox:new` / `inbox:released` — идемпотентно: повтор id счётчик не двигает. */
  add(id: string): void;
  /** `inbox:claimed` / `inbox:declined` — идемпотентно. */
  remove(id: string): void;
  /**
   * Диалог ушёл из очереди по МОЕМУ действию: убрать id и сразу поставить
   * точные счётчики из ответа ручки. Два шага одним вызовом, потому что порядок
   * важен — `remove` после `setCounters` откатил бы серверное число на единицу.
   */
  settle(id: string, counters?: InboxCountDto): void;
  /** Диалог ВЕРНУЛСЯ в очередь моим действием: id снова известен, счётчики —
   * абсолютные из ответа. Отдельно от add: серверный count уже учёл возврат,
   * и add поверх него давал бейджу +1 (аудит синхронизации 16.08). */
  restore(id: string, counters: InboxCountDto): void;
  /** conversation:updated {escalated} — счётчик «не берёт никто» у всех. */
  bumpEscalated(id: string, on: boolean): void;
  showClaimed(convId: string, by: string): void;
  /** Без аргумента — снять плашку любую; с id — только если она про этот диалог. */
  clearClaimed(convId?: string): void;
  clear(): void;
}

/** Снять пометку «не берёт никто» с уходящего диалога — вместе со счётчиком. */
function снятьПометку(
  s: { escalatedIds: Record<string, true>; escalated: number },
  id: string,
): { escalatedIds?: Record<string, true>; escalated?: number } {
  if (!s.escalatedIds[id]) return {};
  const escalatedIds = { ...s.escalatedIds };
  delete escalatedIds[id];
  return { escalatedIds, escalated: Math.max(0, s.escalated - 1) };
}

export const useInboxStore = create<InboxStoreState>()((set) => ({
  ids: {},
  /*
   * ⚠ ПОЧЕМУ ПОИМЁННО, А НЕ ОДНИМ ЧИСЛОМ (находка 23.08 №20).
   *
   * `escalated` был счётчиком без памяти: `bumpEscalated` его двигал, а `remove`
   * и `settle` — нет, потому что не знали, был ли уходящий диалог помечен. Чужое
   * «Принять» уносило строку из очереди, число оставалось прежним — и подпись
   * «Никто не берёт: 3» висела под ПУСТОЙ очередью до перезагрузки страницы.
   * Хуже безобидного: она зовёт разбирать то, что уже разобрали.
   *
   * Множество имён стоит рядом с `ids`, а не вместо значения в нём: `ids`
   * отвечает на вопрос «диалог в очереди», и подмешивать туда второй смысл
   * значило бы сломать все проверки вида `if (!s.ids[id])`.
   */
  escalatedIds: {},
  count: 0,
  escalated: 0,
  claimedNotice: null,

  seedFromServer: (ids, total, escalatedIds) =>
    set((s) => {
      const next: Record<string, true> = {};
      for (const id of ids) next[id] = true;
      const помечены: Record<string, true> = {};
      for (const id of escalatedIds ?? []) помечены[id] = true;
      return {
        ids: next,
        escalatedIds: escalatedIds ? помечены : s.escalatedIds,
        count: Math.max(0, total),
        escalated: Math.max(0, escalatedIds ? escalatedIds.length : s.escalated),
      };
    }),

  setCounters: ({ count, escalated }) =>
    set({ count: Math.max(0, count), escalated: Math.max(0, escalated) }),

  add: (id) => set((s) => (s.ids[id] ? s : { ids: { ...s.ids, [id]: true }, count: s.count + 1 })),

  remove: (id) =>
    set((s) => {
      if (!s.ids[id]) return s; // неизвестный id: счётчик не трогаем — см. заголовок файла
      const ids = { ...s.ids };
      delete ids[id];
      return { ids, count: Math.max(0, s.count - 1), ...снятьПометку(s, id) };
    }),

  restore: (id, counters) =>
    set((s) => ({
      ids: { ...s.ids, [id]: true },
      // Вернулся в очередь — пометки «не берёт никто» на нём нет: счётчики ниже
      // абсолютные и пришли с сервера, а имя не должно остаться в множестве.
      escalatedIds: снятьПометку(s, id).escalatedIds ?? s.escalatedIds,
      count: Math.max(0, counters.count),
      escalated: Math.max(0, counters.escalated),
    })),

  bumpEscalated: (id, on) =>
    set((s) => {
      if (!s.ids[id]) return s;
      // Кадр может повториться (переподключение, две вкладки): двигаем счётчик
      // только когда пометка ДЕЙСТВИТЕЛЬНО меняется, иначе он уползёт.
      if (Boolean(s.escalatedIds[id]) === on) return s;
      const escalatedIds = { ...s.escalatedIds };
      if (on) escalatedIds[id] = true;
      else delete escalatedIds[id];
      return { escalatedIds, escalated: Math.max(0, s.escalated + (on ? 1 : -1)) };
    }),

  settle: (id, counters) =>
    set((s) => {
      const ids = { ...s.ids };
      const known = Boolean(ids[id]);
      delete ids[id];
      const escalatedIds = { ...s.escalatedIds };
      const был_помечен = Boolean(escalatedIds[id]);
      delete escalatedIds[id];
      if (counters) {
        // Счётчики абсолютные — им и верим; множество имён просто чистим.
        return {
          ids,
          escalatedIds,
          count: Math.max(0, counters.count),
          escalated: Math.max(0, counters.escalated),
        };
      }
      return {
        ids,
        escalatedIds,
        count: known ? Math.max(0, s.count - 1) : s.count,
        escalated: был_помечен ? Math.max(0, s.escalated - 1) : s.escalated,
      };
    }),

  showClaimed: (convId, by) => set({ claimedNotice: { convId, by } }),

  clearClaimed: (convId) =>
    set((s) => {
      if (!s.claimedNotice) return s;
      if (convId && s.claimedNotice.convId !== convId) return s;
      return { claimedNotice: null };
    }),

  clear: () => set({ ids: {}, escalatedIds: {}, count: 0, escalated: 0, claimedNotice: null }),
}));

/** Число диалогов в очереди — бейдж вкладки «Входящие», title и рейка. */
export function selectInboxCount(s: { count: number }): number {
  return s.count;
}

/** Сколько из очереди не берёт никто — подпись под списком (7.1). */
export function selectInboxEscalated(s: { escalated: number }): number {
  return s.escalated;
}
