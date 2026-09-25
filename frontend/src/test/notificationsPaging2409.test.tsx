import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { NotificationsPage } from "@/features/notifications/NotificationsPage";
import { useNotificationStore } from "@/features/notifications/store";
import type { Permission } from "@/shared/auth/usePermissions";
import type { NotificationDto } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ОПУСТЕВШАЯ СТРАНИЦА ЖУРНАЛА УВЕДОМЛЕНИЙ (проверка 24.09).
 *
 * На второй странице «Только непрочитанные» человек отмечает строки
 * прочитанными. Когда непрочитанных остаётся не больше пятидесяти, вторая
 * страница пустеет, и экран писал «Непрочитанных нет — всё, что приходило, вы
 * уже разобрали», хотя на первой их ещё десятки. Кнопки «Назад» не было:
 * подвал жил внутри ветки непустого списка. У всех семи администраторов на бою
 * непрочитанных больше пятидесяти (медиана 240).
 */

const PERMISSIONS: Permission[] = ["conversations:read", "conversations:manage", "users:manage"];

const UNREAD: NotificationDto = {
  id: "n-1",
  kind: "backup.failed",
  severity: "critical",
  title: "Резервное копирование не выполнилось",
  body: "Последняя удачная копия — позавчера",
  entity: null,
  action: null,
  repeat_count: 1,
  is_read: false,
  created_at: "2026-09-24T10:00:00Z",
  last_seen_at: "2026-09-24T10:00:00Z",
};

describe("Опустевшая страница журнала уведомлений", () => {
  let offsets: string[];

  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    resetSessionStore({ user: { ...fakeUser, role: "admin" }, permissions: PERMISSIONS, accessToken: "t", bootstrapped: true });
    offsets = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (!url.pathname.endsWith("/notifications")) return jsonResponse(200, { ok: true });
        const offset = Number(url.searchParams.get("offset") ?? 0);
        offsets.push(String(offset));
        // Непрочитанных осталось 45 — все на первой странице.
        return jsonResponse(200, {
          items: offset === 0 ? [UNREAD] : [],
          page: { limit: 50, offset, total: 45 },
          unread: 45,
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("не говорит «непрочитанных нет» и ведёт назад, где строки есть", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NotificationsPage />, { route: "/notifications?unread=1&offset=50" });

    expect(await screen.findByText("На этой странице пусто")).toBeInTheDocument();
    expect(screen.queryByText("Непрочитанных нет")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Назад" }));

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(offsets.at(-1)).toBe("0");
  });
});
