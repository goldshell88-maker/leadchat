import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { CriticalBanners } from "@/features/notifications/CriticalBanners";
import { NotificationBell } from "@/features/notifications/NotificationBell";
import { NotificationPanel } from "@/features/notifications/NotificationPanel";
import { kindLabel } from "@/features/notifications/catalog";
import { useNotificationStore } from "@/features/notifications/store";
import { useNotificationsBootstrap } from "@/features/notifications/useNotifications";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import type { NotificationDto } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Центр уведомлений (14 §3): колокольчик со счётчиком, список с важностью,
 * плашки критичного и обработка WS-события `notify`.
 *
 * Право раздела — `conversations:manage` (то же, что проверяет сервер):
 * admin, head, manager. Наблюдателю не приходит ничего.
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
    kind: "backup.failed",
    severity: "critical",
    title: "Резервное копирование не выполнилось",
    body: "Последняя удачная копия — позавчера",
    entity: null,
    action: null,
    repeat_count: 1,
    is_read: false,
    created_at: "2026-08-05T10:00:00Z",
    last_seen_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function asRole(role: Role, permissions: Permission[] = ADMIN_PERMISSIONS) {
  resetSessionStore({
    user: { ...fakeUser, role },
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
}

describe("Колокольчик уведомлений", () => {
  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { ok: true })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("показывает счётчик непрочитанного администратору", () => {
    asRole("admin");
    useNotificationStore.getState().seed([notification()], 3);

    renderWithProviders(<NotificationBell />);

    expect(screen.getByLabelText("Уведомления — непрочитанных: 3")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("наблюдателю колокольчика нет — ему ничего не приходит (14 §2)", () => {
    asRole("observer", ["conversations:read"]);
    useNotificationStore.getState().seed([notification()], 3);

    renderWithProviders(<NotificationBell />);

    expect(screen.queryByLabelText(/Уведомления/)).toBeNull();
  });

  it("менеджер колокольчик видит: рабочие события адресованы и ему", () => {
    asRole("manager", ["conversations:read", "conversations:manage", "messages:send"]);
    useNotificationStore.getState().seed([], 0);

    renderWithProviders(<NotificationBell />);

    expect(screen.getByLabelText("Уведомления")).toBeInTheDocument();
  });

  it("открывает список последних уведомлений", async () => {
    const user = userEvent.setup();
    asRole("admin");
    useNotificationStore
      .getState()
      .seed([notification({ title: "На диске мало места", kind: "disk.space" })], 1);

    renderWithProviders(<NotificationBell />);
    await user.click(screen.getByLabelText("Уведомления — непрочитанных: 1"));

    expect(await screen.findByText("На диске мало места")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Показать все" })).toBeInTheDocument();
  });
});

describe("Смена сотрудника за одним компьютером", () => {
  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("центр обнуляется: уведомления адресные и чужими не бывают", () => {
    const s = useNotificationStore.getState();
    s.setOwner("u-admin:admin");
    s.seed([notification({ title: "Запрос на сброс пароля", kind: "support.password_reset" })], 2);

    s.setOwner("u-manager:manager");

    expect(useNotificationStore.getState().items).toHaveLength(0);
    expect(useNotificationStore.getState().unread).toBe(0);
  });

  it("тот же сотрудник — содержимое остаётся (повторный рендер не гасит колокольчик)", () => {
    const s = useNotificationStore.getState();
    s.setOwner("u-admin:admin");
    s.seed([notification()], 2);

    s.setOwner("u-admin:admin");

    expect(useNotificationStore.getState().items).toHaveLength(1);
    expect(useNotificationStore.getState().unread).toBe(2);
  });

  it("менеджер не видит счётчик администратора, пока сервер не ответил", async () => {
    // Ответ сервера намеренно не приходит: до него на экране не должно
    // остаться ничего от предыдущего сотрудника.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    resetSessionStore({
      user: { ...fakeUser, id: "u-admin", role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    useNotificationStore.getState().setOwner("u-admin:admin");
    useNotificationStore
      .getState()
      .seed([notification({ title: "Резервное копирование не выполнилось" })], 3);

    // За тот же компьютер сел менеджер.
    resetSessionStore({
      user: { ...fakeUser, id: "u-manager", role: "manager" },
      permissions: ["conversations:read", "conversations:manage"],
      accessToken: "t",
      bootstrapped: true,
    });

    function BellWithBootstrap() {
      useNotificationsBootstrap();
      return (
        <>
          <NotificationBell />
          <CriticalBanners />
        </>
      );
    }

    renderWithProviders(<BellWithBootstrap />);

    await waitFor(() => expect(useNotificationStore.getState().items).toHaveLength(0));
    expect(screen.getByLabelText("Уведомления")).toBeInTheDocument();
    expect(screen.queryByText("3")).toBeNull();
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });
});

describe("Список уведомлений", () => {
  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    asRole("admin");
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { ok: true })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("рисует важность цветом и словом — цвет не единственный признак", () => {
    const items = [
      notification({ id: "n-c", severity: "critical", title: "Приём сообщений остановился" }),
      notification({ id: "n-w", severity: "warning", title: "Очередь входящих не разбирается" }),
      notification({ id: "n-i", severity: "info", title: "Сертификат скоро истекает" }),
    ];
    renderWithProviders(<NotificationPanel items={items} />);

    const rows = screen.getAllByRole("button").filter((b) => b.className.includes("lc-notify-row"));
    expect(rows.map((r) => r.dataset.severity)).toEqual(["critical", "warning", "info"]);
    expect(within(rows[0]).getByText("Критичное")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Важное")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Обычное")).toBeInTheDocument();
  });

  it("показывает подавленные повторы одной строкой со счётчиком (14 §4)", () => {
    renderWithProviders(<NotificationPanel items={[notification({ repeat_count: 12 })]} />);

    expect(screen.getByText(/повторялось 12 раз/)).toBeInTheDocument();
  });

  it("«Отметить все прочитанными» гасит счётчик и уходит на сервер", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(async () =>
      jsonResponse(200, { marked: 4, unread: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    useNotificationStore.getState().seed([notification()], 4);

    renderWithProviders(<NotificationPanel items={[notification()]} />);
    await user.click(screen.getByRole("button", { name: "Отметить все прочитанными" }));

    expect(useNotificationStore.getState().unread).toBe(0);
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(urls.some((u) => u.includes("/notifications/read-all"))).toBe(true);
    });
  });

  it("непрочитанное старше видимых строк всё равно можно погасить", () => {
    // Колокольчик показывает десять последних, а в базе их больше (14 §3):
    // если непрочитанное осталось за пределами списка, бейдж показывает число,
    // и кнопка обязана работать — иначе погасить его нечем.
    useNotificationStore.getState().seed([notification({ id: "n-old", is_read: true })], 7);

    renderWithProviders(<NotificationPanel items={[notification({ id: "n-old", is_read: true })]} />);

    expect(screen.getByRole("button", { name: "Отметить все прочитанными" })).toBeEnabled();
  });

  it("когда непрочитанного нет — кнопка выключена", () => {
    useNotificationStore.getState().seed([notification({ id: "n-old", is_read: true })], 0);

    renderWithProviders(<NotificationPanel items={[notification({ id: "n-old", is_read: true })]} />);

    expect(screen.getByRole("button", { name: "Отметить все прочитанными" })).toBeDisabled();
  });
});

describe("Плашки критичного", () => {
  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    asRole("admin");
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { marked: 1, unread: 0 })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("одновременно висит не больше трёх, остальные — «и ещё N» (14 §3)", () => {
    useNotificationStore.getState().seed(
      [1, 2, 3, 4, 5].map((i) =>
        notification({
          id: `n-${i}`,
          title: `Поломка ${i}`,
          created_at: `2026-08-05T1${i}:00:00Z`,
          last_seen_at: `2026-08-05T1${i}:00:00Z`,
        }),
      ),
      5,
    );

    renderWithProviders(<CriticalBanners />);

    expect(screen.getAllByRole("alert")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "и ещё 2" })).toBeInTheDocument();
  });

  it("некритичные в плашку не попадают — им место в списке", () => {
    useNotificationStore
      .getState()
      .seed(
        [notification({ id: "n-w", severity: "warning" }), notification({ id: "n-i", severity: "info" })],
        2,
      );

    renderWithProviders(<CriticalBanners />);

    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });

  it("прочитанное уведомление плашку не поднимает", () => {
    useNotificationStore.getState().seed([notification({ id: "n-read", is_read: true })], 0);

    renderWithProviders(<CriticalBanners />);

    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });

  it("у критичного всегда есть действие одной кнопкой", () => {
    useNotificationStore.getState().seed(
      [
        notification({
          id: "n-a",
          kind: "account.needs_reauth",
          title: "Аккаунт LP-Москва требует переподключения",
          entity: { type: "account", id: "acc-1" },
          action: { code: "account.reconnect", label: "Переподключить" },
          created_at: "2026-08-05T12:00:00Z",
          last_seen_at: "2026-08-05T12:00:00Z",
        }),
        notification({ id: "n-b", kind: "backup.failed" }),
      ],
      2,
    );

    renderWithProviders(<CriticalBanners />);

    const banners = screen.getAllByRole("alert");
    // Кнопку и её надпись даёт сервер…
    expect(within(banners[0]).getByRole("button", { name: "Переподключить" })).toBeInTheDocument();
    // …а где чинить одним нажатием нечего — остаётся «Подтвердить», но кнопка есть.
    expect(within(banners[1]).getByRole("button", { name: "Подтвердить" })).toBeInTheDocument();
  });

  it("подтверждение убирает плашку и помечает уведомление прочитанным", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(async () =>
      jsonResponse(200, { marked: 1, unread: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    useNotificationStore.getState().seed([notification({ id: "n-ack" })], 1);

    renderWithProviders(<CriticalBanners />);
    await user.click(screen.getByRole("button", { name: "Подтвердить" }));

    await waitFor(() => expect(screen.queryAllByRole("alert")).toHaveLength(0));
    expect(useNotificationStore.getState().unread).toBe(0);
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(urls.some((u) => u.includes("/notifications/n-ack/read"))).toBe(true);
    });
  });
});

describe("WS-событие notify (01 §11.3 → 14 §4)", () => {
  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    asRole("admin");
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { ok: true })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("кладёт уведомление в центр и увеличивает счётчик", () => {
    useNotificationStore.getState().setUnread(2);

    applyWsEvent({
      type: "notify",
      ts: "2026-08-05T12:00:00.100Z",
      data: {
        level: "error",
        title: "Аккаунт Авито требует переподключения",
        text: "Приём сообщений по этому аккаунту остановлен",
        audience_hint: "admin",
        id: "n-ws-1",
        kind: "account.needs_reauth",
        severity: "critical",
        entity: { type: "account", id: "acc-1" },
        action: { code: "account.reconnect", label: "Переподключить" },
        repeat_count: 1,
      },
    } as never);

    const s = useNotificationStore.getState();
    expect(s.unread).toBe(3);
    expect(s.items[0].title).toBe("Аккаунт Авито требует переподключения");
    expect(s.items[0].severity).toBe("critical");
    expect(s.items[0].action?.label).toBe("Переподключить");
  });

  it("критичное сразу поднимает плашку", async () => {
    renderWithProviders(<CriticalBanners />);
    expect(screen.queryAllByRole("alert")).toHaveLength(0);

    applyWsEvent({
      type: "notify",
      ts: "2026-08-05T12:00:00.100Z",
      data: {
        level: "error",
        title: "Резервное копирование не выполнилось",
        text: "Последняя удачная копия — позавчера",
        audience_hint: "admin",
        id: "n-ws-2",
        kind: "backup.failed",
        severity: "critical",
      },
    } as never);

    expect(await screen.findByText("Резервное копирование не выполнилось")).toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("повторный кадр той же записи не плодит вторую строку и вторую плашку", async () => {
    renderWithProviders(<CriticalBanners />);

    // Подавление повторов держит сервер: повтор обновляет ТУ ЖЕ запись и
    // приезжает с тем же id и выросшим repeat_count (14 §4).
    const frame = (repeat: number, ts: string) =>
      ({
        type: "notify",
        ts,
        data: {
          level: "error",
          title: "Аккаунт Авито требует переподключения",
          text: "Приём сообщений остановлен",
          audience_hint: "admin",
          id: "n-dup",
          kind: "account.needs_reauth",
          severity: "critical",
          entity: { type: "account", id: "acc-1" },
          repeat_count: repeat,
          is_repeat: repeat > 1,
        },
      }) as never;

    applyWsEvent(frame(1, "2026-08-05T12:00:00.100Z"));
    applyWsEvent(frame(2, "2026-08-05T12:30:00.100Z"));

    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(1));
    const s = useNotificationStore.getState();
    expect(s.items).toHaveLength(1);
    expect(s.unread).toBe(1);
    expect(screen.getByText(/повторялось 2 раза/)).toBeInTheDocument();
  });

  it("служебный кадр без id остаётся тостом и в центр не попадает", () => {
    applyWsEvent({
      type: "notify",
      ts: "2026-08-05T12:00:00.100Z",
      data: { level: "info", title: "История загружена", text: "Аккаунт «LP-Москва»: 1 214 диалогов" },
    });

    expect(useNotificationStore.getState().items).toHaveLength(0);
    expect(useNotificationStore.getState().unread).toBe(0);
  });

  it("рассылка администраторам не показывается менеджеру", () => {
    asRole("manager", ["conversations:read", "conversations:manage"]);

    applyWsEvent({
      type: "notify",
      ts: "2026-08-05T12:00:00.100Z",
      data: {
        level: "error",
        title: "Планировщик не подаёт признаков жизни",
        text: "Обновление токенов остановлено",
        audience_hint: "admin",
        id: "n-ws-3",
        kind: "scheduler.down",
        severity: "critical",
      },
    } as never);

    expect(useNotificationStore.getState().items).toHaveLength(0);
  });
});

describe("Журнал /notifications", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const urls = () => fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));

  const journalPage = {
    items: [
      notification({
        id: "n-1",
        severity: "critical",
        kind: "backup.failed",
        created_at: "2026-08-05T12:00:00Z",
        last_seen_at: "2026-08-05T12:00:00Z",
      }),
      notification({
        id: "n-2",
        severity: "warning",
        kind: "support.password_reset",
        title: "Сотрудник не может войти — просит новый пароль",
        body: "Пётр Ковалёв, заявка со страницы входа",
        entity: { type: "user", id: "u-2" },
        action: { code: "user.password_reset_link", label: "Выслать новую ссылку" },
      }),
    ],
    page: { limit: 50, offset: 0, total: 2 },
    unread: 2,
  };

  beforeEach(() => {
    queryClient.clear();
    useNotificationStore.getState().clear();
    asRole("admin");
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/action") && init?.method === "POST") {
        return jsonResponse(200, {
          action: "user.password_reset_link",
          result: { invite_url: "https://chat.partner-lead-centre.ru/invite/inv_Zz9" },
          unread: 1,
        });
      }
      if (url.pathname.endsWith("/notifications")) return jsonResponse(200, journalPage);
      return jsonResponse(200, { ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("показывает журнал с важностью и типом события", async () => {
    const { NotificationsPage } = await import("@/features/notifications/NotificationsPage");
    renderWithProviders(<NotificationsPage />, { route: "/notifications" });

    const table = await screen.findByRole("table");
    expect(within(table).getByText("Критичное")).toBeInTheDocument();
    expect(within(table).getByText("Важное")).toBeInTheDocument();
    // Подпись типа из каталога и человеческий заголовок события — разные
    // колонки, но об одном и том же и одними словами (TEXT-11): подпись берём
    // из каталога, а не из памяти автора теста.
    expect(within(table).getByText(kindLabel("support.password_reset"))).toBeInTheDocument();
    expect(within(table).getByText("Сотрудник не может войти — просит новый пароль")).toBeInTheDocument();
  });

  it("фильтр важности уходит в запрос", async () => {
    const user = userEvent.setup();
    const { NotificationsPage } = await import("@/features/notifications/NotificationsPage");
    renderWithProviders(<NotificationsPage />, { route: "/notifications" });
    await screen.findByRole("table");

    await user.click(screen.getAllByLabelText("Важность")[0]);
    await user.click(await screen.findByRole("option", { name: "Критичное" }));

    await waitFor(() => {
      expect(urls().some((u) => u.includes("severity=critical"))).toBe(true);
    });
  });

  it("действие «Выслать новую ссылку» отдаёт одноразовую ссылку для копирования", async () => {
    const user = userEvent.setup();
    const { NotificationsPage } = await import("@/features/notifications/NotificationsPage");
    renderWithProviders(<NotificationsPage />, { route: "/notifications" });
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Выслать новую ссылку" }));

    expect(await screen.findByTestId("invite-link")).toHaveTextContent("inv_Zz9");
    expect(urls().some((u) => u.includes("/notifications/n-2/action"))).toBe(true);
    // Счётчик колокольчика синхронизируется ответом сервера, а не догадкой.
    await waitFor(() => expect(useNotificationStore.getState().unread).toBe(1));
  });
});

/** Бюджет бандла (03 §7): журнал уведомлений — отдельный ленивый чанк. */
describe("Ленивая загрузка журнала", () => {
  const sources = import.meta.glob(["/src/app/router.tsx", "/src/app/lazyRoutes.ts"], {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;

  it("маршрут /notifications подключён через lazy import", () => {
    /*
     * Импорт чанка живёт не в router.tsx, а в карте lazyRoutes.ts (06.09):
     * маршрут и пункт меню ждут ОДИН промис, и наведение на пункт греет ровно
     * тот файл, который потом откроет переход. Сторож стережёт обе половины:
     * маршрут спрашивает карту по своему пути, а карта знает, где страница.
     */
    const router = sources["/src/app/router.tsx"] ?? "";
    const карта = sources["/src/app/lazyRoutes.ts"] ?? "";
    expect(router).toContain('path: "/notifications"');
    expect(router).toMatch(/await loadChunk\("\/notifications"\)/);
    expect(карта).toMatch(
      /"\/notifications":\s*\(\)\s*=>\s*import\("@\/features\/notifications\/NotificationsPage"\)/,
    );
  });
});
