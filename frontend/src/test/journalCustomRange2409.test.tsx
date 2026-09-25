import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AuditLogTab } from "@/features/settings/team/AuditLogTab";
import { NotificationsPage } from "@/features/notifications/NotificationsPage";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const auditRow = {
  id: 1,
  user: { id: "u-1", full_name: "Анна" },
  action: "auth.login",
  description: "Вход в систему",
  entity: "user",
  entity_id: "u-1",
  details: null,
  created_at: "2026-09-20T10:05:00Z",
};

/**
 * «ПРОИЗВОЛЬНЫЙ» ПЕРИОД ЖУРНАЛОВ СОБИРАЕТСЯ ДВУМЯ ЩЕЛЧКАМИ (проверка 24.09).
 *
 * Календарь управляемый: первый щелчок давал `[день, null]`, журналы его не
 * хранили, и второй щелчок снова считался первым — период не собирался
 * никогда, а «Произвольный» повторял «7 дней». Проба проверяющего, ставшая
 * тестом; поле теперь общее — `shared/ui/DateRangeInput`.
 */
describe("Произвольный период в журналах", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const urls = () => fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-24T09:00:00Z"));
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["conversations:read", "conversations:manage", "audit:read", "users:manage", "stats:all"],
      accessToken: "t",
      bootstrapped: true,
    });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/audit-log")) {
        return jsonResponse(200, { items: [auditRow], page: { limit: 50, offset: 0, total: 1 } });
      }
      if (url.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      if (url.pathname.endsWith("/notifications")) {
        return jsonResponse(200, {
          items: [
            {
              id: "n-1", kind: "backup.failed", severity: "critical", title: "t", body: null,
              entity: null, action: null, repeat_count: 1, is_read: true, audience: "admin",
              created_at: "2026-09-20T10:00:00Z", last_seen_at: "2026-09-20T10:00:00Z", expires_at: "2026-12-20T10:00:00Z",
            },
          ],
          page: { limit: 50, offset: 0, total: 1 },
          unread: 0,
        });
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("журнал аудита: два щелчка по дням меняют период запроса", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AuditLogTab />, { route: "/settings/team" });
    await screen.findByRole("table");
    await user.click(screen.getAllByLabelText("Период журнала")[0]);
    await user.click(await screen.findByRole("option", { name: "Произвольный" }));
    await waitFor(() => expect(urls().some((u) => u.includes("date_from=2026-09-18") && u.includes("date_to=2026-09-24"))).toBe(true));

    await user.click(screen.getByLabelText("Произвольный период журнала"));
    await screen.findByRole("button", { name: "1 сентября 2026" });
    await user.click(screen.getByRole("button", { name: "1 сентября 2026" }));
    await user.click(screen.getByRole("button", { name: "3 сентября 2026" }));
    await new Promise((r) => setTimeout(r, 300));
    expect(urls().some((u) => u.includes("date_from=2026-09-01") && u.includes("date_to=2026-09-03"))).toBe(true);
  });

  it("журнал уведомлений: два щелчка по дням меняют период запроса", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NotificationsPage />, { route: "/notifications?period=custom&from=2026-09-18&to=2026-09-24" });
    await screen.findByRole("table");
    await user.click(screen.getByLabelText("Произвольный период"));
    await screen.findByRole("button", { name: "1 сентября 2026" });
    await user.click(screen.getByRole("button", { name: "1 сентября 2026" }));
    await user.click(screen.getByRole("button", { name: "3 сентября 2026" }));
    await new Promise((r) => setTimeout(r, 300));
    expect(urls().some((u) => u.includes("date_from=2026-09-01") && u.includes("date_to=2026-09-03"))).toBe(true);
  });
});
