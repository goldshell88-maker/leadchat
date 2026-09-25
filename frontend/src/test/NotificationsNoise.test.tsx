import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { CriticalBanners } from "@/features/notifications/CriticalBanners";
import { useNotificationStore } from "@/features/notifications/store";
import { useNotificationsBootstrap } from "@/features/notifications/useNotifications";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import type { NotificationDto } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ШУМ НЕ ДОЛЖЕН ХОРОНИТЬ НАСТОЯЩУЮ ТРЕВОГУ — самая дорогая находка разбора
 * боевой системы (6–11 августа).
 *
 * Что было. «Приём сообщений остановился» шёл критичным и дал 112 строк за
 * шесть дней. Колокольчик грузит десять последних строк, красная плашка
 * рисуется по тому, что в сторе, — и два настоящих критичных «Резервное
 * копирование не выполнилось» (за ночи 6 и 8 августа) не показались НИКОМУ:
 * они просто не попали в десятку. Копий за те ночи действительно нет.
 *
 * На сервере эта тревога понижена до «важно», но одной понижённой важности
 * мало: критичное обязано доезжать до экрана независимо от того, сколько
 * строк любой другой важности натикало сверху. Здесь проверяется именно это.
 */

const ADMIN_PERMISSIONS: Permission[] = [
  "conversations:read",
  "conversations:manage",
  "users:manage",
  "audit:read",
  "accounts:manage",
];

function asRole(role: Role, permissions: Permission[] = ADMIN_PERMISSIONS) {
  resetSessionStore({
    user: { ...fakeUser, role },
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
}

function notification(overrides: Partial<NotificationDto> = {}): NotificationDto {
  return {
    id: "n-1",
    kind: "backup.failed",
    severity: "critical",
    title: "Резервное копирование не выполнилось",
    body: "Успешных копий не было ни разу с момента запуска",
    entity: null,
    action: null,
    repeat_count: 1,
    is_read: false,
    created_at: "2026-08-06T00:30:00Z",
    last_seen_at: "2026-08-06T00:30:00Z",
    ...overrides,
  };
}

/** Свежий шум: «приём остановился», повторяющийся каждые пять минут. */
function noise(count: number): NotificationDto[] {
  return Array.from({ length: count }, (_, i) => {
    const ts = `2026-08-11T${String(10 + Math.floor(i / 6)).padStart(2, "0")}:${String(
      (i % 6) * 10,
    ).padStart(2, "0")}:00Z`;
    return notification({
      id: `noise-${i}`,
      kind: "inbound.stalled",
      severity: "warning",
      title: "Приём сообщений остановился",
      body: "Последнее сообщение от клиента пришло 48 мин назад",
      repeat_count: 4,
      created_at: ts,
      last_seen_at: ts,
    });
  });
}

const BACKUP_6 = notification({ id: "backup-6" });
const BACKUP_8 = notification({
  id: "backup-8",
  created_at: "2026-08-08T00:30:00Z",
  last_seen_at: "2026-08-08T00:30:00Z",
});

function Harness() {
  useNotificationsBootstrap();
  return <CriticalBanners />;
}

describe("Шум не хоронит критичное", () => {
  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    asRole("admin");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("красная плашка «копия не создана» доезжает из-под завала свежих строк", async () => {
    // Сервер отдаёт страницу колокольчика по времени — в первую десятку
    // попадает только шум. Критичное живёт на второй сотне.
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("unread-count")) {
        return jsonResponse(200, { unread: 114, critical: 2, warning: 112, info: 0 });
      }
      if (url.includes("severity=critical")) {
        return jsonResponse(200, {
          items: [BACKUP_8, BACKUP_6],
          page: { limit: 20, offset: 0, total: 2 },
          unread: 114,
        });
      }
      return jsonResponse(200, {
        items: noise(10),
        page: { limit: 10, offset: 0, total: 122 },
        unread: 114,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithProviders(<Harness />);

    await waitFor(() =>
      expect(screen.getAllByText("Резервное копирование не выполнилось")).toHaveLength(2),
    );
  });

  it("новая строка не вытесняет из стора неподтверждённое критичное", () => {
    const store = useNotificationStore.getState();
    store.seed([BACKUP_6], 1);

    // Полсотни свежих строк — ровно предел памяти стора.
    for (const n of noise(50)) store.push(n);

    const kept = useNotificationStore.getState().items.map((i) => i.id);
    expect(kept).toContain("backup-6");
  });
});
