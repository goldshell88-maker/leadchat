import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TeamPage } from "@/features/settings/team/TeamPage";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TeamUserDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Вкладка «Сотрудники» (11 §4.2, 01 §3). Блокер приёмки Б4: до этого экрана
 * завести человека, сменить роль и отключить можно было только командой в консоли.
 */

const ADMIN_PERMISSIONS: Permission[] = ["users:manage", "audit:read"];
const HEAD_PERMISSIONS: Permission[] = ["audit:read", "stats:all", "conversations:read"];

const USERS: TeamUserDto[] = [
  {
    id: "u-1",
    email: "petr@partner-lead-centre.ru",
    full_name: "Пётр Ковалёв",
    role: "manager",
    is_active: true,
    is_online: true,
    invite_pending: false,
    handles_conversations: true,
    department: null,
    created_at: "2026-06-01T08:00:00Z",
  },
  {
    id: "u-2",
    email: "olga@partner-lead-centre.ru",
    full_name: "Ольга Некрасова",
    role: "manager",
    is_active: true,
    is_online: false,
    invite_pending: true,
    handles_conversations: true,
    department: null,
    created_at: "2026-07-01T08:00:00Z",
  },
  {
    id: "u-3",
    email: "oleg@partner-lead-centre.ru",
    full_name: "Олег Иванов",
    role: "observer",
    is_active: false,
    is_online: false,
    invite_pending: false,
    handles_conversations: true,
    department: null,
    created_at: "2026-05-01T08:00:00Z",
  },
];

const INVITE_RESULT = {
  user: { ...USERS[1], id: "u-9", full_name: "Пётр Новый", email: "new@partner-lead-centre.ru" },
  invite_url: "https://chat.partner-lead-centre.ru/invite/inv_XkQ9",
  invite_expires_at: "2026-08-09T10:00:00Z",
};

type Handler = (url: URL, init?: RequestInit) => Response | null;

let extraHandler: Handler | null = null;
let fetchMock: ReturnType<typeof vi.fn>;

function calls() {
  return fetchMock.mock.calls.map((c) => ({
    url: decodeURIComponent(String(c[0])),
    init: c[1] as RequestInit | undefined,
  }));
}

function setupFetch() {
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const custom = extraHandler?.(url, init);
    if (custom) return custom;
    if (url.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
    if (url.pathname.endsWith("/audit-log")) {
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    }
    if (url.pathname.endsWith("/users") && init?.method === "POST") {
      return jsonResponse(201, INVITE_RESULT);
    }
    if (
      (url.pathname.endsWith("/invite") || url.pathname.endsWith("/reset-password")) &&
      init?.method === "POST"
    ) {
      return jsonResponse(200, INVITE_RESULT);
    }
    if (url.pathname.endsWith("/deactivate") || url.pathname.endsWith("/activate")) {
      return jsonResponse(200, USERS[0]);
    }
    if (init?.method === "PATCH") return jsonResponse(200, { ...USERS[0], role: "head" });
    if (url.pathname.endsWith("/users")) {
      return jsonResponse(200, { items: USERS, page: { limit: 50, offset: 0, total: USERS.length } });
    }
    return jsonResponse(404, errorEnvelope("not_found", "нет"));
  });
  vi.stubGlobal("fetch", fetchMock);
}

describe("Экран команды — права ролей", () => {
  beforeEach(() => {
    queryClient.clear();
    extraHandler = null;
    setupFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("руководителю управление людьми недоступно — только журнал", async () => {
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: HEAD_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });

    renderWithProviders(<TeamPage />, { route: "/settings/team" });

    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(screen.queryByRole("tab", { name: "Сотрудники" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Пригласить" })).toBeNull();
  });

  it("админ открывает вкладку «Сотрудники» первой и видит таблицу", async () => {
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });

    renderWithProviders(<TeamPage />, { route: "/settings/team" });

    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Сотрудники", "Журнал аудита"]);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("Пётр Ковалёв")).toBeInTheDocument();
    expect(within(table).getByText("petr@partner-lead-centre.ru")).toBeInTheDocument();
    // Колонка «Статус» говорит только о нештатном: «активен» — состояние по
    // умолчанию, и печатать его в каждой строке значит забить столбец
    // одинаковыми словами (docs/35, часть 1, пункт 5).
    expect(within(table).queryByText("активен")).toBeNull();
    expect(within(table).getByText("ждёт пароля")).toBeInTheDocument();
    expect(within(table).getByText("отключён")).toBeInTheDocument();
    expect(within(table).getByText("в сети")).toBeInTheDocument();
  });
});

describe("Управление сотрудниками", () => {
  beforeEach(() => {
    queryClient.clear();
    extraHandler = null;
    setupFetch();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("приглашение показывает одноразовую ссылку для копирования", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    // Кнопка панели ОТКРЫВАЕТ окно, кнопка окна — отправляет форму. Раньше обе
    // назывались «Пригласить» (панельная — с «плюсом», который в доступное имя
    // не попадал), и различить их было нельзя ни тестом, ни голосом.
    await user.click(screen.getByRole("button", { name: "Пригласить" }));
    await user.type(await screen.findByLabelText("Имя"), "Пётр Новый");
    await user.type(screen.getByLabelText("Email"), "new@partner-lead-centre.ru");
    await user.click(screen.getByRole("radio", { name: /Руководитель/ }));
    await user.click(screen.getByRole("button", { name: "Создать приглашение" }));

    expect(await screen.findByTestId("invite-link")).toHaveTextContent(INVITE_RESULT.invite_url);
    expect(screen.getByText(/Ссылка показывается один раз/)).toBeInTheDocument();
    // ⚠ ЭМОДЗИ БОЛЬШЕ НЕТ В ДОСТУПНОМ ИМЕНИ (04.09). «📋 Скопировать» читалось
    // скринридером как «планшет с зажимом Скопировать»: символ рисуется шрифтом
    // и попадает в имя кнопки. Теперь значок стоит рядом с подписью и помечен
    // aria-hidden, а имя кнопки — одно слово.
    expect(screen.getByRole("button", { name: "Скопировать" })).toBeInTheDocument();

    const post = calls().find((c) => c.init?.method === "POST" && c.url.endsWith("/users"));
    expect(post).toBeDefined();
    expect(JSON.parse(String(post?.init?.body))).toEqual({
      email: "new@partner-lead-centre.ru",
      full_name: "Пётр Новый",
      role: "head",
    });
  });

  it("дубль email подсвечивает поле, а не роняет форму", async () => {
    const user = userEvent.setup();
    extraHandler = (url, init) =>
      url.pathname.endsWith("/users") && init?.method === "POST"
        ? jsonResponse(409, errorEnvelope("conflict", "Занято", { reason: "email_taken" }))
        : null;

    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    // Кнопка панели ОТКРЫВАЕТ окно, кнопка окна — отправляет форму. Раньше обе
    // назывались «Пригласить» (панельная — с «плюсом», который в доступное имя
    // не попадал), и различить их было нельзя ни тестом, ни голосом.
    await user.click(screen.getByRole("button", { name: "Пригласить" }));
    await user.type(await screen.findByLabelText("Имя"), "Пётр Новый");
    await user.type(screen.getByLabelText("Email"), "petr@partner-lead-centre.ru");
    await user.click(screen.getByRole("button", { name: "Создать приглашение" }));

    expect(await screen.findByText("Сотрудник с таким email уже существует")).toBeInTheDocument();
  });

  it("смена роли спрашивает подтверждение и уходит PATCH-ом", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getAllByLabelText("Роль: Пётр Ковалёв")[0]);
    await user.click(await screen.findByRole("option", { name: "Руководитель" }));

    expect(await screen.findByText(/Права применятся немедленно/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сменить роль" }));

    await waitFor(() => {
      const patch = calls().find((c) => c.init?.method === "PATCH");
      expect(patch?.url).toContain("/users/u-1");
      expect(JSON.parse(String(patch?.init?.body))).toEqual({ role: "head" });
    });
  });

  it("участие в диалогах сохраняется сразу, без подтверждения", async () => {
    // Смена роли спрашивает подтверждение, а это — нет, и разница не в
    // небрежности: роль меняет ПРАВА (человек теряет доступ к разделам), а
    // здесь меняется только состав раздачи, и обратный щелчок возвращает всё.
    // Диалог на каждый щелчок превратил бы перенастройку отдела из тринадцати
    // человек в тринадцать модальных окон.
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Ведёт диалоги: Пётр Ковалёв"));

    await waitFor(() => {
      const patch = calls().find((c) => c.init?.method === "PATCH");
      expect(patch?.url).toContain("/users/u-1");
      expect(JSON.parse(String(patch?.init?.body))).toEqual({ handles_conversations: false });
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("отдел сохраняется по уходу фокуса, а не на каждой букве", async () => {
    // Отдел печатают целиком; запрос на каждый символ засыпал бы и сервер, и
    // журнал аудита.
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    const field = screen.getByLabelText("Отдел: Пётр Ковалёв");
    await user.type(field, "ОКК");
    expect(calls().some((c) => c.init?.method === "PATCH")).toBe(false);

    await user.tab();
    await waitFor(() => {
      const patch = calls().find((c) => c.init?.method === "PATCH");
      expect(JSON.parse(String(patch?.init?.body))).toEqual({ department: "ОКК" });
    });
  });

  it("у роли без права отвечать тумблер неактивен", async () => {
    // Наблюдатель диалоги не ведёт по определению роли — переключать нечего.
    // Живой тумблер здесь обещал бы несуществующее.
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    expect(screen.getByLabelText("Ведёт диалоги: Олег Иванов")).toBeDisabled();
    expect(screen.getByLabelText("Ведёт диалоги: Пётр Ковалёв")).toBeEnabled();
  });

  it("отключение предупреждает о мгновенной потере доступа и о диалогах", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Отключить" }));

    expect(await screen.findByText(/потеряет доступ прямо сейчас/)).toBeInTheDocument();
    expect(screen.getByText(/Открытые диалоги останутся назначенными на него/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Отключить" }));

    await waitFor(() => {
      expect(calls().some((c) => c.url.includes("/users/u-1/deactivate"))).toBe(true);
    });
  });

  /*
   * ЭТОТ ТЕСТ САМ БЫЛ ЧАСТЬЮ БЕДЫ. Он подсовывал экрану выдуманный ответ —
   * `422 unprocessable` с текстом «Нельзя» — и был зелёным, потому что экран
   * ловил ЛЮБОЙ 422 и печатал свою фразу про администратора (TEAM-02).
   * Настоящий сервер отвечает здесь `409 last_admin` с полным объяснением
   * (`app/services/users.py:151-158`), то есть охраняемый случай в
   * проверявшуюся ветку не попадал ни разу. Ответ приведён к настоящему.
   */
  it("последнего админа отключить нельзя — экран печатает объяснение сервера", async () => {
    const user = userEvent.setup();
    const serverSays =
      "Нельзя отключить сотрудника: это последний администратор системы. " +
      "Сначала назначьте администратором кого-то ещё.";
    extraHandler = (url) =>
      url.pathname.endsWith("/deactivate")
        ? jsonResponse(409, errorEnvelope("last_admin", serverSays, { reason: "last_admin" }))
        : null;

    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Отключить" }));
    await screen.findByText(/потеряет доступ прямо сейчас/);
    await user.click(screen.getByRole("button", { name: "Отключить" }));

    expect(await screen.findByText(serverSays)).toBeInTheDocument();
  });

  it("отключённого можно включить обратно", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Олег Иванов"));
    await user.click(await screen.findByRole("menuitem", { name: "Включить" }));
    await screen.findByText(/снова сможет войти с прежним паролем/);
    await user.click(screen.getByRole("button", { name: "Включить" }));

    await waitFor(() => {
      expect(calls().some((c) => c.url.includes("/users/u-3/activate"))).toBe(true);
    });
  });

  it("работающему сотруднику предлагается сброс пароля, а не перевыпуск приглашения", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    // У Петра пароль установлен: `/invite` ответил бы 422 (01 §3.3).
    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Сбросить пароль" }));

    /*
     * ⚠ ЧЕРЕЗ ПОДТВЕРЖДЕНИЕ (обход экранов 31.08). Пункт стоит ПЕРВЫМ в меню, а
     * срабатывал сразу: промах мышью обнулял пароль работающему диспетчеру и
     * выкидывал его из смены посреди разговора с клиентом. Соседние опасные
     * действия (роль, отключение, удаление) давно спрашивают — это не
     * спрашивало. Тест обновлён вместе со смыслом, а не подогнан под код.
     */
    expect(
      await screen.findByText(/выйдет из системы посреди работы/),
      "сброс пароля не объясняет последствий",
    ).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Сбросить пароль" }));

    expect(await screen.findByTestId("invite-link")).toHaveTextContent(INVITE_RESULT.invite_url);
    expect(calls().some((c) => c.url.includes("/users/u-1/reset-password"))).toBe(true);
  });

  it("после выданной ссылки окно подтверждения закрыто — второй сброс не погасит её", async () => {
    // Проверка 24.09: окно «Сбросить пароль» оставалось под ссылкой с живой
    // красной кнопкой. Админ, решив, что сброс не прошёл, жал её снова — и
    // отправленная сотруднику ссылка переставала работать.
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");
    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Сбросить пароль" }));
    await user.click(await screen.findByRole("button", { name: "Сбросить пароль" }));
    await screen.findByTestId("invite-link");

    await waitFor(() =>
      expect(screen.queryByText(/выйдет из системы посреди работы/)).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Сбросить пароль" })).toBeNull();
    expect(calls().filter((c) => c.url.includes("/reset-password"))).toHaveLength(1);
  });

  // Заказчик просил дважды: «сделай так, чтобы можно было удалять сотрудников,
  // а не просто отключать» и «администратор меняет имя аккаунта и пароли сам».
  // Обе ручки на сервере были готовы давно — кнопок не было ни одной.

  it("удаление объясняет, что будет с диалогами и с журналом", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Удалить" }));

    // Три вещи, которые человек обязан узнать ДО нажатия: куда денутся диалоги,
    // что человек останется в журнале и что вернуть нельзя.
    expect(await screen.findByText(/вернутся в «Входящие»/)).toBeInTheDocument();
    expect(screen.getByText(/В журнале аудита\s+человек останется/)).toBeInTheDocument();
    expect(screen.getByText(/Отменить удаление нельзя/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Удалить" }));

    await waitFor(() => {
      expect(
        calls().some((c) => c.url.includes("/users/u-1") && c.init?.method === "DELETE"),
      ).toBe(true);
    });
  });

  it("себя удалить нельзя — пункт неактивен и говорит почему", async () => {
    const user = userEvent.setup();
    // Список подменяем только здесь: добавлять себя в общий набор значило бы
    // переписать ожидания полутора десятков чужих проверок ради одной.
    const me = {
      ...USERS[0],
      id: fakeUser.id,
      email: fakeUser.email,
      full_name: fakeUser.full_name,
      role: "admin" as const,
    };
    extraHandler = (url, init) =>
      url.pathname.endsWith("/users") && init?.method !== "POST"
        ? jsonResponse(200, { items: [me], page: { limit: 25, offset: 0, total: 1 } })
        : null;

    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText(`Действия: ${fakeUser.full_name}`));

    const item = await screen.findByRole("menuitem", { name: "Удалить (нельзя себя)" });
    expect(item).toHaveAttribute("data-disabled", "true");
  });

  it("администратор задаёт пароль голосом, без одноразовой ссылки", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Задать пароль" }));

    const field = await screen.findByLabelText("Новый пароль");
    const submit = screen.getByRole("button", { name: "Задать пароль" });

    // ⚠ 27.08 ПЛАНКА ДЛИНЫ СНЯТА (решение владельца: «сделай так, чтобы можно
    // было указывать любой пароль»). Здесь администратор заводит вход руками,
    // чаще всего временный; там, где сотрудник задаёт пароль СЕБЕ — приём
    // приглашения и смена своего пароля, — десять знаков остались.
    await user.type(field, "1");
    expect(submit).toBeEnabled();

    // Пустое поле по-прежнему не отправляется: ноль знаков это не простой
    // пароль, а вход без пароля.
    await user.clear(field);
    expect(submit).toBeDisabled();

    await user.type(field, "довольно длинный пароль");
    expect(submit).toBeEnabled();
    await user.click(submit);

    await waitFor(() => {
      expect(calls().some((c) => c.url.includes("/users/u-1/set-password"))).toBe(true);
    });
  });

  /*
   * Пустота от фильтра называет причину поимённо и даёт выход. «Никого не
   * нашлось» одинаково верно и для промаха в поиске, и для роли, которой в
   * команде нет, — а делать в этих случаях надо разное. Снять фильтр с пустого
   * экрана было нечем: поиск и селект роли остаются выше и теряются из виду.
   */
  it("отфильтрованная пустота названа причиной и снимается кнопкой", async () => {
    const user = userEvent.setup();
    extraHandler = (url) => {
      if (url.pathname.endsWith("/users") && url.searchParams.get("q")) {
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }
      return null;
    };
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.type(screen.getByLabelText("Поиск сотрудника"), "Синицын");

    expect(await screen.findByText("Никого не нашлось")).toBeInTheDocument();
    expect(screen.getByText("По запросу «Синицын» никого нет")).toBeInTheDocument();
    // «Пригласите первого сотрудника» здесь было бы враньём: сотрудники есть.
    expect(screen.queryByText("Пригласите первого сотрудника")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Сбросить фильтры" }));

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.getByLabelText("Поиск сотрудника")).toHaveValue("");
  });

  it("«Выслать новую ссылку» перевыпускает приглашение", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamPage />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Ольга Некрасова"));
    await user.click(await screen.findByRole("menuitem", { name: "Выслать новую ссылку" }));
    // Перевыпуск тоже спрашивает: прежняя ссылка перестаёт работать.
    await user.click(await screen.findByRole("button", { name: "Выслать" }));

    expect(await screen.findByTestId("invite-link")).toHaveTextContent(INVITE_RESULT.invite_url);
    expect(calls().some((c) => c.url.includes("/users/u-2/invite"))).toBe(true);
  });
});
