import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { CriticalBanners } from "@/features/notifications/CriticalBanners";
import { NotificationBell } from "@/features/notifications/NotificationBell";
import { NotificationPanel } from "@/features/notifications/NotificationPanel";
import { RECENT_PAGE_SIZE } from "@/features/notifications/api";
import { criticalBanners, useNotificationStore } from "@/features/notifications/store";
import { useNotificationsBootstrap } from "@/features/notifications/useNotifications";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { showToast } from "@/shared/ui/toast";
import type { NotificationDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({
  showToast: vi.fn(),
  showUndoToast: vi.fn(),
  toast: { success: vi.fn(), info: vi.fn(), warning: vi.fn(), error: vi.fn(), hide: vi.fn() },
}));

/**
 * ТРИ СПОСОБА СОВРАТЬ ЧЕЛОВЕКУ ПРО УВЕДОМЛЕНИЯ — и защиты от каждого.
 *
 * NOTIF-01. Упавшая загрузка выглядела пустотой: «Пока ничего не происходило»
 *   и ноль на колокольчике. Администратору в этот момент могло лежать «Канал
 *   отобрали: подписка пропала».
 * NOTIF-02. Пометка прочитанным была оптимистичной и необратимой: отказ
 *   сервера гасил красную плашку навсегда и молча.
 * NOTIF-03. Уведомление обычной важности показывалось ЗЕЛЁНЫМ тостом с
 *   галочкой — цветом успеха.
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
    body: "Вебхук перебила чужая система",
    entity: null,
    action: null,
    repeat_count: 1,
    is_read: false,
    created_at: "2026-08-11T10:00:00Z",
    last_seen_at: "2026-08-11T10:00:00Z",
    ...overrides,
  };
}

function asAdmin() {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ADMIN_PERMISSIONS,
    accessToken: "t",
    bootstrapped: true,
  });
}

/** Каркас приложения: именно он ходит за содержимым центра (AppLayout.tsx). */
function Shell() {
  useNotificationsBootstrap();
  return <NotificationBell />;
}

beforeEach(() => {
  queryClient.clear();
  useNotificationStore.getState().clear();
  vi.mocked(showToast).mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  useNotificationStore.getState().clear();
});

describe("Стартовая загрузка центра не удалась (NOTIF-01)", () => {
  it("панель говорит о неудаче и предлагает повторить — а не «ничего не происходило»", async () => {
    const user = userEvent.setup();
    asAdmin();
    // 502 при перезапуске api — самый частый случай из тех, что ловил аудит.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(502, errorEnvelope("internal_error", "Запрос завершился с кодом 502"))),
    );

    renderWithProviders(<Shell />);

    const bell = await screen.findByLabelText("Уведомления — не загрузились", undefined, {
      timeout: 10_000,
    });
    await user.click(bell);

    expect(await screen.findByText("Не удалось загрузить уведомления")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    // Главное: ложного «всё спокойно» на экране нет.
    expect(screen.queryByText(/Пока тихо/)).toBeNull();
  }, 20_000);

  it("удачная загрузка признак снимает — пустой центр остаётся пустым центром", () => {
    useNotificationStore.getState().setLoadFailed(true);
    useNotificationStore.getState().seed([], 0);

    expect(useNotificationStore.getState().loadFailed).toBe(false);
  });

  it("пустой центр объясняет, что здесь появится, а не молчит серой строкой", () => {
    asAdmin();
    renderWithProviders(<NotificationPanel items={[]} />);

    expect(screen.getByText("Пока тихо — уведомлений нет")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
  });
});

describe("Отказ сервера при пометке прочитанным (NOTIF-02)", () => {
  it("красная плашка возвращается, и человеку об этом говорят", async () => {
    const user = userEvent.setup();
    asAdmin();
    useNotificationStore.getState().seed([notification()], 1);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(500, errorEnvelope("internal_error", "Не сохранилось"))),
    );

    renderWithProviders(<CriticalBanners />);
    await user.click(screen.getByRole("button", { name: "Подтвердить" }));

    // Плашка на месте: беда никуда не делась, гасить её было не за что.
    await waitFor(() => {
      const s = useNotificationStore.getState();
      expect(criticalBanners(s.items, s.acknowledged, s.pinnedCritical)).toHaveLength(1);
    });
    expect(await screen.findByText("Канал отобрали: подписка пропала")).toBeInTheDocument();

    // Счётчик вернулся, подтверждение снято, тост сказал правду.
    await waitFor(() => expect(useNotificationStore.getState().unread).toBe(1));
    expect(useNotificationStore.getState().acknowledged).not.toContain("n-1");
    expect(vi.mocked(showToast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Не отметилось прочитанным", tone: "danger" }),
    );
  });

  it("удачная пометка плашку гасит — откат не мешает обычной работе", async () => {
    const user = userEvent.setup();
    asAdmin();
    useNotificationStore.getState().seed([notification()], 1);
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { unread: 0 })));

    renderWithProviders(<CriticalBanners />);
    await user.click(screen.getByRole("button", { name: "Подтвердить" }));

    await waitFor(() => {
      const s = useNotificationStore.getState();
      expect(criticalBanners(s.items, s.acknowledged, s.pinnedCritical)).toHaveLength(0);
    });
  });

  it("откат массовой пометки не теряет то, что пришло за время запроса", () => {
    const s = useNotificationStore.getState();
    s.seed([notification({ id: "a" }), notification({ id: "b", severity: "info" })], 2);
    s.markAllRead();
    // Пока запрос летел, пришло новое уведомление.
    s.push(notification({ id: "c", severity: "info", title: "Новое" }));

    // Пометка погасила только некритичное «b», критичное «a» осталось.
    useNotificationStore.getState().revertAllRead(["b"], 2, 1);

    expect(useNotificationStore.getState().unread).toBe(3);
    expect(useNotificationStore.getState().items.filter((i) => !i.is_read)).toHaveLength(3);
  });
});

describe("Тон тоста по важности (NOTIF-03)", () => {
  beforeEach(() => {
    resetSessionStore({ permissions: ["conversations:manage", "users:manage", "audit:read"] });
  });

  it("обычная важность — синий «вот что произошло», а не зелёная галочка успеха", () => {
    applyWsEvent({
      type: "notify",
      ts: new Date().toISOString(),
      data: {
        id: "n-info",
        level: "info",
        severity: "info",
        kind: "cert.expiring",
        title: "Сертификат скоро истекает",
        text: "Осталось 7 дней",
        audience_hint: "admin",
      },
    });

    expect(vi.mocked(showToast)).toHaveBeenCalledWith(expect.objectContaining({ tone: "info" }));
    const arg = vi.mocked(showToast).mock.calls[0][0];
    expect(arg).not.toHaveProperty("color");
    expect((arg as { tone?: string }).tone).not.toBe("success");
  });

  it("важное — предупреждение, критичное — опасность и без автозакрытия", () => {
    applyWsEvent({
      type: "notify",
      ts: new Date().toISOString(),
      data: {
        id: "n-warn",
        level: "warning",
        severity: "warning",
        kind: "conversation.no_reply",
        title: "Диалог без ответа",
        text: "40 минут",
      },
    });
    expect(vi.mocked(showToast)).toHaveBeenLastCalledWith(expect.objectContaining({ tone: "warning" }));

    // Служебный кадр без id (история загружена) — тоже по важности, не «успех».
    applyWsEvent({
      type: "notify",
      ts: new Date().toISOString(),
      data: { level: "error", title: "Приём сообщений остановился", text: "" },
    });
    expect(vi.mocked(showToast)).toHaveBeenLastCalledWith(
      expect.objectContaining({ tone: "danger", autoClose: false }),
    );
  });
});

describe("Колокольчик показывает последние десять (NOTIF-06)", () => {
  it("строк в панели не больше, чем просили с сервера", () => {
    asAdmin();
    const many = Array.from({ length: 25 }, (_, i) =>
      notification({
        id: `n-${i}`,
        severity: "info",
        title: `Событие ${i}`,
        created_at: `2026-08-11T10:${String(i).padStart(2, "0")}:00Z`,
        last_seen_at: `2026-08-11T10:${String(i).padStart(2, "0")}:00Z`,
      }),
    );

    renderWithProviders(<NotificationPanel items={many} />);

    const rows = document.querySelectorAll(".lc-notify-row");
    expect(rows).toHaveLength(RECENT_PAGE_SIZE);
    expect(screen.getByText(`Показаны последние ${RECENT_PAGE_SIZE}`)).toBeInTheDocument();
  });

  it("когда строк меньше десяти — никакой приписки внизу нет", () => {
    asAdmin();
    renderWithProviders(<NotificationPanel items={[notification()]} />);

    expect(screen.queryByText(/Показаны последние/)).toBeNull();
  });
});

/**
 * ФИЛЬТРЫ ЖУРНАЛА — В АДРЕСЕ СТРАНИЦЫ (NOTIF-04).
 *
 * Администратор отбирал «Критичное · 30 дней», уходил по ссылке уведомления в
 * настройки, жал «Назад» — и возвращался к пустому фильтру на первой странице.
 * Разбор начинался заново, а послать коллеге ссылку на отобранное было нельзя.
 */
describe("Журнал помнит фильтры (NOTIF-04)", () => {
  async function openJournal(route: string) {
    const { NotificationsPage } = await import("@/features/notifications/NotificationsPage");
    return renderWithProviders(<NotificationsPage />, { route });
  }

  it("адрес с фильтрами открывает ровно ту выборку", async () => {
    asAdmin();
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 }, unread: 0 });
      }),
    );

    await openJournal("/notifications?severity=critical&unread=1&kind=webhook.lost");

    await waitFor(() => expect(calls.some((u) => u.includes("severity=critical"))).toBe(true));
    expect(calls.some((u) => u.includes("unread_only=true"))).toBe(true);
    expect(calls.some((u) => u.includes("kind=webhook.lost"))).toBe(true);
  });

  it("выбор фильтра попадает в адрес — ссылку можно послать коллеге", async () => {
    const user = userEvent.setup();
    asAdmin();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 }, unread: 0 }),
      ),
    );

    await openJournal("/notifications");
    await user.click(screen.getAllByLabelText("Важность")[0]);
    await user.click(await screen.findByRole("option", { name: "Критичное" }));

    // MemoryRouter адрес наружу не отдаёт — смотрим на кнопку сброса: она
    // появляется ровно тогда, когда в строке запроса что-то есть.
    expect(await screen.findByRole("button", { name: "Сбросить фильтры" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Сбросить фильтры" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Сбросить фильтры" })).toBeNull(),
    );
  });
});

describe("Откат возвращает ровно то, что сам изменил (NOTIF-02)", () => {
  it("уже прочитанную строку откат непрочитанной не делает — но плашку поднимает", () => {
    const s = useNotificationStore.getState();
    s.seed([notification()], 1);
    s.markAllRead(); // строка прочитана, но не подтверждена — плашка держится
    s.markRead("n-1"); // «Подтвердить»: сюда уходит подтверждение

    useNotificationStore.getState().revertRead("n-1", {
      wasUnread: false,
      wasAcknowledged: false,
    });

    const after = useNotificationStore.getState();
    expect(after.items[0].is_read).toBe(true); // журнал не переписываем
    expect(after.unread).toBe(0); // счётчик тоже
    expect(criticalBanners(after.items, after.acknowledged, after.pinnedCritical)).toHaveLength(1);
  });
});
