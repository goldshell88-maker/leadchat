import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TeamPage } from "@/features/settings/team/TeamPage";
import type { Permission } from "@/shared/auth/usePermissions";
import type { AuditFilterOptions, AuditLogPage } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ФИЛЬТРЫ ЖУРНАЛА АУДИТА — С СЕРВЕРА (проверка 24.09).
 *
 * «Действие» собиралось из словаря экрана на 23 пункта: из 72 действий,
 * лежащих в журнале, 50 выбрать было нельзя, все `settings.*` в том числе —
 * «кто выключил распределение» не находилось. «Сотрудник» брался из
 * назначаемых операторов: руководителя, отключённых и удалённых в нём не было,
 * хотя их действия журнал хранит.
 */

const HEAD_PERMISSIONS: Permission[] = ["conversations:read", "stats:all", "audit:read"];

const OPTIONS: AuditFilterOptions = {
  actions: [
    { action: "conversation.assigned", label: "Назначение диалога" },
    { action: "settings.distribution_changed", label: "Изменены настройки распределения диалогов" },
  ],
  actors: [
    { id: "u-anna", full_name: "Анна Смирнова", state: "active" },
    { id: "u-ivan", full_name: "Иван Петров", state: "inactive" },
    { id: "u-olga", full_name: "Ольга Сидорова", state: "deleted" },
  ],
};

function page(items: AuditLogPage["items"]): AuditLogPage {
  return { items, page: { limit: 50, offset: 0, total: items.length } };
}

const ROWS: AuditLogPage["items"] = [
  {
    id: 1,
    user: { id: "u-anna", full_name: "Анна Смирнова" },
    action: "conversation.assigned",
    description: "Диалог передан коллеге",
    entity: "conversation",
    entity_id: "c4e5f6a7-1111-2222-3333-444455556666",
    details: null,
    created_at: "2026-09-24T10:05:00Z",
  },
  {
    id: 2,
    user: null,
    // Действие вне реестра: переименовали, а старые строки остались.
    action: "conversation.legacy_moved",
    entity: "conversation",
    entity_id: "c4e5f6a7-1111-2222-3333-444455556666",
    details: null,
    created_at: "2026-09-24T10:06:00Z",
  },
];

describe("Фильтры журнала аудита", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const logUrls = () =>
    fetchMock.mock.calls
      .map((c) => new URL(String(c[0]), "http://localhost"))
      .filter((u) => u.pathname.endsWith("/audit-log"));

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: HEAD_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/audit-log/filters")) return jsonResponse(200, OPTIONS);
      if (url.pathname.endsWith("/audit-log")) {
        // Отфильтрованная выдача пуста: выбранный пункт не должен от этого пропасть.
        const filtered = url.searchParams.has("action") || url.searchParams.has("user_id");
        return jsonResponse(200, page(filtered ? [] : ROWS));
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function openSelect(label: string) {
    const user = userEvent.setup();
    await screen.findByRole("table");
    await user.click(screen.getAllByLabelText(label)[0]);
    return user;
  }

  it("«Действие» предлагает весь реестр сервера, включая настройки", async () => {
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    const user = await openSelect("Действие");

    await user.click(
      await screen.findByRole("option", { name: "Изменены настройки распределения диалогов" }),
    );

    await waitFor(() => {
      expect(logUrls().some((u) => u.searchParams.get("action") === "settings.distribution_changed")).toBe(
        true,
      );
    });
  });

  it("«Сотрудник» знает отключённых и удалённых — с пометкой", async () => {
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    const user = await openSelect("Сотрудник");

    expect(await screen.findByRole("option", { name: "Иван Петров (отключён)" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Ольга Сидорова (удалён)" }));

    await waitFor(() => {
      expect(logUrls().some((u) => u.searchParams.get("user_id") === "u-olga")).toBe(true);
    });
  });

  it("выбранное действие вне реестра не пропадает из поля на пустой выдаче", async () => {
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    const user = await openSelect("Действие");

    await user.click(await screen.findByRole("option", { name: "conversation.legacy_moved" }));

    await screen.findByText("За выбранный период записей нет");
    expect(screen.getAllByLabelText("Действие")[0]).toHaveValue("conversation.legacy_moved");
  });

  it("строка без описания подписана названием из реестра, а не ключом", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/audit-log/filters")) return jsonResponse(200, OPTIONS);
      return jsonResponse(
        200,
        page([{ ...ROWS[0], action: "settings.distribution_changed", entity: "settings", description: null }]),
      );
    });
    renderWithProviders(<TeamPage />, { route: "/settings/team" });

    const table = await screen.findByRole("table");
    expect(
      await within(table).findByText("Изменены настройки распределения диалогов"),
    ).toBeInTheDocument();
    expect(within(table).getByText("Настройки")).toBeInTheDocument();
  });
});
