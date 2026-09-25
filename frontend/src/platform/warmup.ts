import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk, type ConversationFilters } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage, TabCounts } from "@/shared/api/types";
import { defaultTabForRole, useChatUiStore } from "@/shared/stores/chatUiStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { getBridgeOrNull } from "./bridge";

/**
 * Мгновенный старт из локального кэша (04 §5.4 шаг 1) и фоновая запись
 * серверных ответов обратно в кэш (шаг 2, `cache_apply_sync`).
 *
 * Кэш — проекция сервера только для чтения: снапшот кладётся в кэш TanStack
 * СРАЗУ ПРОТУХШИМ, поэтому первый же ответ API его перезаписывает (сервер прав).
 */

/** CACHE_DIALOGS из 04 §5.1. */
export const CACHE_DIALOGS = 200;

/*
 * СЕМАНТИКА ВКЛАДОК ЗДЕСЬ ПОВТОРЕНА, И ЭТО НЕИЗБЕЖНО: прогрев считает срез по
 * кэшу, когда сети нет вовсе, а на сервере тот же срез считает
 * `_tab_condition`. Дубль ПРАВИЛА остаётся; дубля СЛОВАРЯ не заводим —
 * «открыт» выражено отрицанием от `closed`, поэтому два новых значения
 * (`waiting_client`) попали во «Все» и в «Мои» сами и правильно.
 * Перечисли мы здесь открытые статусы списком — их пришлось бы дописывать, и
 * забытое значение молча исчезло бы из офлайн-списка.
 */
// Экспортируется РАДИ ТЕСТА, и это оправдано: правило вкладок здесь — копия
// серверного, и расхождение копий видно только проверкой. Через `warmupFromCache`
// его не достать, не подсунув весь снимок и localStorage.
export function matchesTab(
  row: ConversationDto,
  f: ConversationFilters,
  userId: string | undefined,
): boolean {
  const open = row.status !== "closed";
  // ⚠ «Показывать закрытые» СНИМАЕТ отсечение целиком — и здесь тоже. Правило вкладок
  // продублировано в этом файле намеренно (снимок рисуется ДО первого ответа сервера),
  // и забудь мы про переключатель — список моргнул бы и подменился: сперва без архива
  // из кэша, потом с архивом от сервера. Со стороны это «интерфейс сам себя переписал».
  if (f.withClosed) return true;
  switch (f.tab) {
    case "mine":
      return row.assignee?.id === userId && open;
    case "new":
      return row.status === "new";
    case "closed":
      return row.status === "closed";
    default:
      return open;
  }
}

/**
 * Фильтры, которые считает сервер: воспроизводить их по кэшу нельзя — снапшот пропускаем.
 *
 * ⚠ СОСТОЯНИЕ, «ЖДУТ ОТВЕТА» И «БЕЗ ОТВЕТСТВЕННОГО» ДОБАВЛЕНЫ 05.09 ВМЕСТЕ С
 * ИХ ПОЛЯМИ В ПАНЕЛИ. Их здесь не было, и до сегодня это почти не всплывало:
 * поставить срез по состоянию можно было только хоткеем Alt+3/Alt+4, а
 * `waiting_only` — пилюлей долга. Теперь оба выбираются мышью на вкладке
 * «Все», и без этой строки снимок нарисовал бы ВЕСЬ кэш под чипом
 * «Состояние: закрытые»: `matchesTab` про статус не знает вовсе, а предикат
 * ожидания живёт только на сервере (`_waiting_condition`). Пропустить снимок
 * значит на долю секунды остаться без офлайн-списка; нарисовать чужой —
 * соврать, а потом переписать себя ответом сервера.
 */
function hasServerSideFilters(f: ConversationFilters): boolean {
  return Boolean(
    f.q || f.accountId || f.assigneeId || f.tag || f.status || f.waitingOnly || f.unassigned,
  );
}

function toPage(items: ConversationDto[]): InfiniteData<ConversationsPage> {
  return {
    pages: [{ items, page: { limit: items.length, offset: 0, total: items.length } }],
    pageParams: [0],
  };
}

/**
 * Прочитать снапшот и отрисовать список до первого ответа API.
 * Зовётся из `sessionStore` при восстановлении сессии. Источник снапшота
 * платформенный: SQLite в десктопе, IndexedDB в вебе (`снимокСписка.ts`);
 * нет ни того ни другого — `warmup()` даёт null и функция сразу выходит.
 */
export async function warmupFromCache(): Promise<boolean> {
  const bridge = getBridgeOrNull();
  if (!bridge) return false;

  const snapshot = await bridge.convCache.warmup().catch(() => null);
  if (!snapshot || snapshot.dialogs.length === 0) return false;

  const filters = useChatUiStore.getState().filters;
  if (hasServerSideFilters(filters)) return false;

  const user = useSessionStore.getState().user;
  const userId = user?.id;

  /*
   * ⚠ ЧУЖОЙ СНИМОК НЕ ПОКАЗЫВАЕМ. Компьютер сменный: за одним столом
   * работает смена, и список диалогов ушедшего — это его переписка, а не общее
   * имущество. Выход стирает снимок (`convCache.clear`), но выходят не всегда:
   * закрыли вкладку, ушли домой, следующий вошёл. Здесь второй замок.
   *
   * К моменту вызова личность уже известна: `bootstrap` зовёт нас ПОСЛЕ
   * успешного `POST /auth/refresh`, а тот кладёт `user` в стор (`http.ts`,
   * `doRefresh`). Не известна — значит показывать нечего и некому.
   *
   * Условие написано «есть поле — проверяем»: десктопный снимок приходит из
   * Rust без `ownerId`, и его поведение остаётся прежним.
   */
  if (snapshot.ownerId && snapshot.ownerId !== userId) return false;

  /*
   * ⚠ ЗАСЕИВАЕМ ДВЕ ВКЛАДКИ, А НЕ ОДНУ, И ЭТО НЕ ПЕРЕСТРАХОВКА.
   *
   * Фильтры перезагрузку не переживают (`chatUiStore.partialize`), поэтому в
   * момент вызова на них всегда умолчание «Все». Роль приезжает следом, и
   * `useRoleUiSync` переводит менеджера на «Мои» — другой ключ кэша, где
   * снимка нет. То есть ровно те, кому список нужнее всех, выигрыша не
   * увидели бы вовсе: у менеджера рабочая вкладка «Мои».
   *
   * Лишних запросов это не добавляет: ключ, который никто не наблюдает,
   * ничего не грузит, а вкладку «Мои» экран после смены роли запросит и так.
   */
  const вкладки = new Set([filters.tab, defaultTabForRole(user?.role)]);

  let засеяно = false;
  for (const tab of вкладки) {
    const срез = { ...filters, tab };
    const key = qk.conversations.list(срез);
    if (queryClient.getQueryData(key)) continue; // сервер уже ответил — не мешаем
    const rows = snapshot.dialogs.filter((r) => matchesTab(r, срез, userId));
    if (rows.length === 0) continue;
    // updatedAt: 0 — данные считаются протухшими, экран сразу уходит за свежими.
    queryClient.setQueryData(key, toPage(rows), { updatedAt: 0 });
    useUnreadStore.getState().seedFromRows(rows);
    засеяно = true;
  }

  /*
   * Числа над «Моими» — та же логика: свой ключ, свой запрос, и без снимка
   * они появляются только с ответом сервера. Кладём протухшими, чтобы экран
   * тут же пошёл за настоящими.
   */
  if (snapshot.counts && !queryClient.getQueryData(qk.conversations.counts)) {
    queryClient.setQueryData<TabCounts>(qk.conversations.counts, snapshot.counts, { updatedAt: 0 });
  }

  return засеяно;
}

/** Все закэшированные строки списков, дедуп по id, топ-200 по last_message_at (04 §5.1 п.3). */
export function collectDialogsForCache(): ConversationDto[] {
  const byId = new Map<string, ConversationDto>();
  const entries = queryClient.getQueriesData<InfiniteData<ConversationsPage>>({
    queryKey: CONVERSATIONS_LIST_KEY,
  });
  for (const [, data] of entries) {
    for (const page of data?.pages ?? []) {
      for (const row of page.items) byId.set(row.id, row);
    }
  }
  return Array.from(byId.values())
    .sort((a, b) => (b.last_message_at ?? "").localeCompare(a.last_message_at ?? ""))
    .slice(0, CACHE_DIALOGS);
}

/**
 * Ответ сервера → `cache_apply_sync` (04 §5.4 шаг 2). Подписка на кэш
 * TanStack, а не на каждый вызов API: любой источник строк (первая загрузка,
 * догон `updated_since`, WS-патчи) попадёт в локальный кэш одинаково.
 *
 * ⚠ ОДНА ПРОВОДКА НА ОБЕ ПЛАТФОРМЫ. Веб-снимок повесить второй подпиской было
 * бы проще, и это ровно тот класс дефекта, который мы уже ловили: два пути к
 * одному полю расходятся молча, и расхождение видно только на бою. Куда лягут
 * строки — в SQLite или в IndexedDB, — решает мост, а не эта функция.
 */
export function installCachePersist(debounceMs = 2000): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;

  const flush = () => {
    timer = null;
    const dialogs = collectDialogsForCache();
    if (dialogs.length === 0) return;
    // Снимок принадлежит человеку: без личности его некому будет проверить при
    // чтении, поэтому не пишем вовсе (веб-ветка отбросила бы такую запись).
    const user = useSessionStore.getState().user;
    void getBridgeOrNull()
      ?.convCache.persist({
        dialogs,
        savedAt: new Date().toISOString(),
        ownerId: user?.id ?? null,
        role: user?.role ?? null,
        tab: useChatUiStore.getState().filters.tab,
        counts: queryClient.getQueryData<TabCounts>(qk.conversations.counts) ?? null,
      })
      .catch(() => {});
  };

  const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
    if (event.type !== "updated") return;
    const key = event.query.queryKey;
    if (!Array.isArray(key) || key[0] !== "conversations" || key[1] !== "list") return;
    if (timer) return;
    timer = setTimeout(flush, debounceMs);
  });

  return () => {
    if (timer) clearTimeout(timer);
    unsubscribe();
  };
}
