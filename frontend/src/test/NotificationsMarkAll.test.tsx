import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { CriticalBanners } from "@/features/notifications/CriticalBanners";
import { NotificationPanel } from "@/features/notifications/NotificationPanel";
import { criticalBanners, useNotificationStore } from "@/features/notifications/store";
import type { NotificationDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * «Отметить все прочитанными» не гасит критичное (дефект M2 из аудита).
 *
 * ЧТО БЫЛО. Один клик клал в `acknowledged` идентификаторы ВСЕХ строк
 * хранилища, а красные плашки фильтруются именно по нему. Администратор,
 * разгребающий колокольчик, одним нажатием убирал с экрана «Канал отобрали:
 * подписка пропала» и «Приём сообщений остановился» — без подтверждения, без
 * отмены и без «кроме критичных». Беда при этом оставалась: канал отобран,
 * приём стоит, напоминать некому.
 *
 * Тонкость, ради которой половина этих проверок и написана: локально не гасить
 * недостаточно. Ручка `read-all` помечает прочитанным всё, включая критичное,
 * и следом прилетает ответ сервера со своим `is_read` — плашка обязана пережить
 * и его тоже.
 */

const ADMIN_PERMISSIONS: Permission[] = [
  "conversations:read",
  "conversations:manage",
  "users:manage",
  "audit:read",
  "accounts:manage",
];

function notification(overrides: Partial<NotificationDto> = {}): NotificationDto {
  return {
    id: "n-1",
    kind: "webhook.lost",
    severity: "critical",
    title: "Канал отобрали: подписка пропала",
    body: "LP-Москва: вебхук переписан чужой системой",
    entity: null,
    action: null,
    repeat_count: 1,
    is_read: false,
    created_at: "2026-08-11T10:00:00Z",
    last_seen_at: "2026-08-11T10:00:00Z",
    ...overrides,
  };
}

const CRITICAL = notification();
const STALLED = notification({
  id: "n-2",
  kind: "inbound.stalled",
  severity: "critical",
  title: "Приём сообщений остановился",
  created_at: "2026-08-11T09:00:00Z",
  last_seen_at: "2026-08-11T09:00:00Z",
});
const ROUTINE = notification({
  id: "n-3",
  kind: "support.password_reset",
  severity: "warning",
  title: "Запрос на сброс пароля",
  created_at: "2026-08-11T08:00:00Z",
  last_seen_at: "2026-08-11T08:00:00Z",
});

describe("Массовая пометка прочитанным и критичное (M2)", () => {
  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { marked: 3, unread: 0 })),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("не считает массовую пометку подтверждением критичного", () => {
    const s = useNotificationStore.getState();
    s.seed([CRITICAL, STALLED, ROUTINE], 3);

    s.markAllRead();

    const after = useNotificationStore.getState();
    expect(after.acknowledged).toEqual([]);
    expect(criticalBanners(after.items, after.acknowledged, after.pinnedCritical).map((n) => n.title)).toEqual(
      ["Канал отобрали: подписка пропала", "Приём сообщений остановился"],
    );
  });

  it("плашка переживает ответ сервера, который пометил прочитанным всё", () => {
    const s = useNotificationStore.getState();
    s.seed([CRITICAL, ROUTINE], 2);
    s.markAllRead();

    // Ровно то, что вернёт `GET /notifications` после `read-all`:
    // ручка помечает прочитанным всё видимое, в том числе критичное.
    s.seed(
      [
        { ...CRITICAL, is_read: true },
        { ...ROUTINE, is_read: true },
      ],
      0,
    );

    const after = useNotificationStore.getState();
    expect(criticalBanners(after.items, after.acknowledged, after.pinnedCritical)).toHaveLength(1);
  });

  it("массовая пометка гасит некритичное, а критичное оставляет непрочитанным", () => {
    // Так же поступает сервер (24.09): критичное гасится только поимённо, и
    // после перезагрузки плашку снова поднимает его непрочитанность.
    const s = useNotificationStore.getState();
    s.seed([CRITICAL, ROUTINE], 2);

    s.markAllRead();

    const after = useNotificationStore.getState();
    expect(after.items.find((i) => i.id === ROUTINE.id)?.is_read).toBe(true);
    expect(after.items.find((i) => i.id === CRITICAL.id)?.is_read).toBe(false);
    expect(after.unread).toBe(1);
  });

  it("на экране: нажатие не убирает красные плашки, а подтверждение убирает", async () => {
    const user = userEvent.setup();
    useNotificationStore.getState().seed([CRITICAL, ROUTINE], 2);

    renderWithProviders(
      <>
        <CriticalBanners />
        <NotificationPanel items={[CRITICAL, ROUTINE]} />
      </>,
    );
    expect(screen.getAllByRole("alert")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Отметить все прочитанными" }));

    // Главное утверждение всего файла.
    const banners = screen.getAllByRole("alert");
    expect(banners).toHaveLength(1);
    expect(within(banners[0]).getByText("Канал отобрали: подписка пропала")).toBeInTheDocument();

    // …а поимённое подтверждение работает как работало.
    await user.click(screen.getByRole("button", { name: "Подтвердить" }));
    await waitFor(() => expect(screen.queryAllByRole("alert")).toHaveLength(0));
  });

  it("кнопка предупреждает, что критичные останутся", () => {
    useNotificationStore.getState().seed([CRITICAL], 1);

    renderWithProviders(<NotificationPanel items={[CRITICAL]} />);

    expect(screen.getByText("Критичные останутся на экране, пока не подтвердите каждое")).toBeInTheDocument();
  });

  it("без неподтверждённого критичного лишней подписи нет", () => {
    useNotificationStore.getState().seed([ROUTINE], 1);

    renderWithProviders(<NotificationPanel items={[ROUTINE]} />);

    expect(screen.queryByText(/Критичные останутся на экране/)).toBeNull();
  });

  it("за компьютером сменился человек — чужие плашки не всплывают", () => {
    const s = useNotificationStore.getState();
    s.setOwner("u-admin:admin");
    s.seed([CRITICAL], 1);
    s.markAllRead();

    s.setOwner("u-manager:manager");

    const after = useNotificationStore.getState();
    expect(after.pinnedCritical).toEqual([]);
    expect(criticalBanners(after.items, after.acknowledged, after.pinnedCritical)).toHaveLength(0);
  });
});
