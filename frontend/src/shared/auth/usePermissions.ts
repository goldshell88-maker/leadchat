import { useMemo } from "react";
import { useSessionStore } from "@/shared/stores/sessionStore";

export type Role = "admin" | "head" | "manager" | "observer";

// Strictly the catalog from 01-API-SPEC §12 / 03-FRONTEND §5.1.
// Names must match backend require_permission(...) — divergence is forbidden.
export type Permission =
  | "conversations:read"
  | "messages:send"
  | "conversations:manage"
  | "notes:read"
  | "notes:write"
  | "templates:own"
  | "templates:shared"
  | "stats:own"
  | "stats:all"
  | "bots:manage"
  | "accounts:read"
  | "accounts:manage"
  | "users:manage"
  | "audit:read"
  // Настройки работы команды: автораспределение и потолок диалогов.
  | "settings:manage"
  // Закрыть обращение, которое ещё никто не взял (решение владельца 22.08).
  // Только администратор: закрытие убирает диалог из «Входящих» насовсем, а
  // оператору для «я сейчас занят» есть «Отклонить» — оно возвращает диалог
  // в очередь к коллегам через три минуты.
  | "conversations:close_queued"
  // Разбор диалогов. Право отдельное от `stats:all` с 27.08: таблица открыта
  // всем ролям, а статистика по всем сотрудникам и живая лента — нет.
  | "dialogs:read"
  // ⚠ ВЕРНУТЬ ПРИНЯТЫЙ ДИАЛОГ В ОЧЕРЕДЬ — ТОЛЬКО АДМИНИСТРАТОР (решение
  // владельца 28.08). Раньше пункт висел на `messages:send`, то есть был у
  // каждого, кто умеет отвечать клиенту. Возврат снимает ответственного и
  // отдаёт тринадцати диалог, с которым человек уже поговорил: клиент получает
  // второго собеседника с нуля. Оператору для «я сейчас занят» есть
  // «Отклонить» — оно про диалог, ЕЩЁ не начатый.
  | "conversations:release";

/**
 * Permissions come exclusively from GET /auth/me (01 §2.5): the backend returns
 * the computed set, the frontend never hardcodes a role→permission matrix.
 * UI hiding is UX, not security — every permission is re-checked server-side.
 */
export function usePermissions() {
  const role = useSessionStore((s) => s.user?.role);
  const permissions = useSessionStore((s) => s.permissions);
  return useMemo(() => {
    const set = new Set<Permission>(permissions);
    return {
      role,
      can: (p: Permission) => set.has(p),
      canAny: (...ps: Permission[]) => ps.some((p) => set.has(p)),
    };
  }, [role, permissions]);
}
