import { beforeEach, describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { usePermissions, type Permission, type Role } from "@/shared/auth/usePermissions";
import { fakeUser, resetSessionStore } from "./helpers";

/**
 * Права в UI (03 §5.1, каталог — 01 §12, закон — DESIGN §5.1).
 * Фронт роль→права НЕ хардкодит: набор приходит из GET /auth/me. Здесь фикстура
 * повторяет ROLE_PERMISSIONS бэкенда (app/core/rbac.py) — если матрица разъедется,
 * тест должен упасть.
 */
const SERVER_PERMISSIONS: Record<Role, Permission[]> = {
  admin: [
    "conversations:read",
    "messages:send",
    "conversations:manage",
    "notes:read",
    "notes:write",
    "templates:own",
    "templates:shared",
    "stats:own",
    "stats:all",
    "bots:manage",
    "accounts:read",
    "accounts:manage",
    "users:manage",
    "audit:read",
  ],
  head: [
    "conversations:read",
    "conversations:manage",
    "notes:read",
    "notes:write",
    "templates:own",
    "templates:shared",
    "stats:own",
    "stats:all",
    "accounts:read",
    "audit:read",
  ],
  manager: [
    "conversations:read",
    "messages:send",
    "conversations:manage",
    "notes:read",
    "notes:write",
    "templates:own",
    "stats:own",
  ],
  observer: ["conversations:read"],
};

function permissionsOf(role: Role) {
  resetSessionStore({
    user: { ...fakeUser, role },
    permissions: SERVER_PERMISSIONS[role],
    accessToken: "t",
    bootstrapped: true,
  });
  return renderHook(() => usePermissions()).result.current;
}

describe("usePermissions — матрица прав четырёх ролей", () => {
  beforeEach(() => resetSessionStore());

  it("админ может всё", () => {
    const p = permissionsOf("admin");
    expect(p.role).toBe("admin");
    for (const perm of SERVER_PERMISSIONS.admin) expect(p.can(perm)).toBe(true);
  });

  it("руководитель: читает всё, отправку сообщений НЕ может, заметки и управление — может", () => {
    const p = permissionsOf("head");

    expect(p.can("conversations:read")).toBe(true);
    expect(p.can("messages:send")).toBe(false); // 403 read_only_role на бэкенде
    expect(p.can("notes:write")).toBe(true);
    expect(p.can("conversations:manage")).toBe(true);
    expect(p.can("stats:all")).toBe(true);
    expect(p.can("audit:read")).toBe(true);
    expect(p.can("users:manage")).toBe(false);
    expect(p.can("accounts:manage")).toBe(false);
    expect(p.can("bots:manage")).toBe(false);
  });

  it("менеджер: полная работа с диалогами, статистика только своя, команда недоступна", () => {
    const p = permissionsOf("manager");

    expect(p.can("messages:send")).toBe(true);
    expect(p.can("conversations:manage")).toBe(true);
    expect(p.can("notes:write")).toBe(true);
    expect(p.can("stats:own")).toBe(true);
    expect(p.can("stats:all")).toBe(false); // ни экрана /stats, ни фильтра «по менеджеру»
    expect(p.can("templates:shared")).toBe(false);
    expect(p.can("audit:read")).toBe(false);
    expect(p.can("users:manage")).toBe(false);
  });

  it("наблюдатель: только чтение диалогов — ни отправки, ни заметок, ни статистики", () => {
    const p = permissionsOf("observer");

    expect(p.can("conversations:read")).toBe(true);
    expect(p.can("messages:send")).toBe(false);
    expect(p.can("notes:read")).toBe(false);
    expect(p.can("notes:write")).toBe(false);
    expect(p.can("conversations:manage")).toBe(false);
    expect(p.can("stats:own")).toBe(false);
    expect(p.can("stats:all")).toBe(false);
    expect(p.can("templates:own")).toBe(false);
    expect(p.can("audit:read")).toBe(false);
  });

  it("canAny истинно, если есть хотя бы одно право из списка", () => {
    const head = permissionsOf("head");
    expect(head.canAny("users:manage", "audit:read")).toBe(true); // guard /settings/team
    expect(head.canAny("users:manage", "bots:manage")).toBe(false);

    const manager = permissionsOf("manager");
    expect(manager.canAny("users:manage", "audit:read")).toBe(false);
  });

  it("источник истины — список из GET /auth/me, а не роль в UI", () => {
    // Бэкенд расширил права роли: фронт обязан подчиниться серверу, не своей таблице.
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: ["conversations:read", "notes:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    const p = renderHook(() => usePermissions()).result.current;

    expect(p.can("notes:read")).toBe(true);
    expect(p.can("messages:send")).toBe(false);
  });

  it("без сессии прав нет вовсе", () => {
    const p = renderHook(() => usePermissions()).result.current;
    expect(p.role).toBeUndefined();
    expect(p.can("conversations:read")).toBe(false);
  });
});
