import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TeamPage } from "@/features/settings/team/TeamPage";
import type { Permission } from "@/shared/auth/usePermissions";
import type { AuditLogPage } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const HEAD_PERMISSIONS: Permission[] = [
  "conversations:read",
  "conversations:manage",
  "notes:read",
  "notes:write",
  "stats:all",
  "audit:read",
];

function auditPage(offset: number, total: number): AuditLogPage {
  return {
    items: [
      {
        id: 18211 + offset,
        user: { id: "u-1", full_name: "Анна Смирнова" },
        action: "conversation.assigned",
        // Подпись события считает бэкенд (services/audit.describe).
        description: "Диалог передан коллеге",
        entity: "conversation",
        entity_id: "c4e5f6a7-1111-2222-3333-444455556666",
        details: { assignee_id: "7c1b", by: "transfer" },
        created_at: "2026-08-04T10:05:00Z",
      },
      {
        id: 18212 + offset,
        user: null,
        action: "conversation.reopened",
        entity: "conversation",
        entity_id: "c4e5f6a7-1111-2222-3333-444455556666",
        details: null,
        created_at: "2026-08-04T10:06:00Z",
      },
    ],
    page: { limit: 50, offset, total },
  };
}

/** Журнал аудита в /settings/team (11 §4.2, данные — 01 §9.7). */
describe("Журнал аудита", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const urls = () => fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));

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
      if (url.pathname.endsWith("/audit-log")) {
        return jsonResponse(200, auditPage(Number(url.searchParams.get("offset") ?? 0), 120));
      }
      if (url.pathname.endsWith("/audit-log/filters")) {
        return jsonResponse(200, {
          actions: [{ action: "conversation.reopened", label: "Диалог переоткрыт — клиент вернулся" }],
          actors: [],
        });
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("руководитель видит ТОЛЬКО вкладку журнала — управление людьми за админом", async () => {
    renderWithProviders(<TeamPage />, { route: "/settings/team" });

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(1);
    expect(tabs[0]).toHaveTextContent("Журнал аудита");
    expect(screen.queryByRole("tab", { name: "Сотрудники" })).toBeNull();
    // Таблица — единственное содержимое вкладки (подписи действий есть ещё и в
    // выпадающем фильтре, поэтому проверяем именно внутри таблицы).
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Диалог передан коллеге")).toBeInTheDocument();
  });

  it("подписывает действия по-русски, системные события — без сотрудника", async () => {
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    const table = await screen.findByRole("table");

    // description с сервера — приоритетный источник подписи…
    expect(within(table).getByText("Диалог передан коллеге")).toBeInTheDocument();
    // …а без него — название действия из реестра сервера.
    expect(await within(table).findByText("Диалог переоткрыт — клиент вернулся")).toBeInTheDocument();
    expect(within(table).getByText("Анна Смирнова")).toBeInTheDocument();
    expect(within(table).getByText("система")).toBeInTheDocument();
  });

  it("детали (JSONB) раскрываются по клику, если они есть", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    const table = await screen.findByRole("table");

    const toggles = within(table).getAllByRole("button", { name: /^Показать детали:/ });
    expect(toggles).toHaveLength(1); // у события без details кнопки нет

    await user.click(toggles[0]);
    expect(screen.getByText(/"by": "transfer"/)).toBeInTheDocument();
  });

  it("период уходит в запрос, пагинация двигает offset", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    // Дефолт вкладки — 7 дней: границы периода уходят на сервер.
    await waitFor(() => {
      expect(urls().some((u) => u.includes("/audit-log") && u.includes("date_from="))).toBe(true);
    });
    expect(urls().some((u) => u.includes("limit=50") && u.includes("offset=0"))).toBe(true);

    await user.click(screen.getByRole("button", { name: "Вперёд" }));
    await waitFor(() => {
      expect(urls().some((u) => u.includes("offset=50"))).toBe(true);
    });

    const footer = screen.getByText(/из 120/);
    expect(footer).toBeInTheDocument();
  });

  it("фильтр «Всё время» снимает границы периода", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getAllByLabelText("Период журнала")[0]);
    await user.click(await screen.findByRole("option", { name: "Всё время" }));

    await waitFor(() => {
      expect(urls().some((u) => u.includes("/audit-log") && !u.includes("date_from="))).toBe(true);
    });
  });

  it("роль без audit:read внутри страницы разделов не получает", () => {
    resetSessionStore({
      user: { ...fakeUser, role: "manager" },
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    renderWithProviders(<TeamPage />, { route: "/settings/team" });

    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.getByText("Для вашей роли здесь пока нет разделов")).toBeInTheDocument();
  });
});

describe("Строки журнала", () => {
  it("объект показывается подписью и коротким id", async () => {
    const user = userEvent.setup();
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["audit:read", "users:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/audit-log")) return jsonResponse(200, auditPage(0, 2));
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );

    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    // У админа первой стоит вкладка «Сотрудники» (11 §4.2) — журнал открываем явно.
    await user.click(screen.getByRole("tab", { name: "Журнал аудита" }));

    const table = await screen.findByRole("table");
    const cell = within(table).getAllByText("Диалог")[0].closest("td");
    expect(cell).not.toBeNull();
    expect(within(cell as HTMLElement).getByText("c4e5f6a7")).toBeInTheDocument();

    vi.unstubAllGlobals();
  });
});
