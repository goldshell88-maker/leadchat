import { useQuery } from "@tanstack/react-query";
import { http } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import type { AssignableUsersResponse, AvitoAccountsPage } from "@/shared/api/types";

/**
 * Справочники, общие для нескольких экранов (передача диалога, фильтр «по
 * менеджеру» в списке чатов и на /stats, фильтр аккаунта). Один endpoint —
 * одно место объявления и один ключ кэша.
 */

/** GET /users/assignable (01 §3.1): активные admin/manager; право `conversations:manage`. */
export function fetchAssignableUsers(): Promise<AssignableUsersResponse> {
  return http.get<AssignableUsersResponse>("/users/assignable");
}

/** Страница списка каналов — предел сервера (`le=100`). */
const ACCOUNTS_PAGE = 100;

/**
 * GET /avito-accounts (01 §4.1) — ВСЕ каналы; право `accounts:read` — admin и head.
 *
 * ⚠ СПИСОК ЛИСТАЕТСЯ ДО КОНЦА (проверка 24.09). Здесь стоял один запрос без
 * параметров, и сервер отдавал первые 50: 51-й канал (сортировка по созданию —
 * то есть самый новый) не появлялся ни на экране каналов, ни в фильтрах чатов,
 * статистики, таблицы, ботов и заявок, и предупреждения не было. В бою каналов
 * 36 при росте с 9 за шесть недель. Один ключ кэша (`qk.accounts`) — один
 * загрузчик: его зовут и справочники, и экран «Аккаунты Авито».
 */
export async function fetchAvitoAccounts(): Promise<AvitoAccountsPage> {
  const first = await http.get<AvitoAccountsPage>(
    `/avito-accounts?limit=${ACCOUNTS_PAGE}&offset=0`,
  );
  const items = [...first.items];
  while (items.length < first.page.total) {
    const next = await http.get<AvitoAccountsPage>(
      `/avito-accounts?limit=${ACCOUNTS_PAGE}&offset=${items.length}`,
    );
    // Канал удалили между запросами — список короче, чем обещал `total`.
    if (next.items.length === 0) break;
    items.push(...next.items);
  }
  return { items, page: { limit: items.length, offset: 0, total: first.page.total } };
}

/** Справочники живут долго — 5 минут без рефетча (03 §2.4). */
export function useAssignableUsersQuery(enabled = true) {
  return useQuery({
    queryKey: qk.users.assignable,
    queryFn: fetchAssignableUsers,
    enabled,
    staleTime: 5 * 60_000,
  });
}

export function useAvitoAccountsQuery(enabled = true) {
  return useQuery({
    queryKey: qk.accounts,
    queryFn: fetchAvitoAccounts,
    enabled,
    staleTime: 5 * 60_000,
  });
}
