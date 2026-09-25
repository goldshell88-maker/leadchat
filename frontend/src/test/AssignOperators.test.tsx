import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import type { Permission } from "@/shared/auth/usePermissions";
import type { AvitoAccountDto, ChannelOperatorsResponse } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Назначение операторов на канал Авито (план 7.2, экран из Jivo 15 §2.2).
 *
 * Блок закрывает ровно одну беду: у заказчика девять каналов и тринадцать
 * операторов, а очередь «Входящие» (7.1) показывает всем всё — оператор видит
 * чужие обращения и берёт не свои.
 *
 * Главное, что здесь проверяется помимо галочек, — ПРАВИЛО СОВМЕСТИМОСТИ:
 * канал без назначенных операторов доступен ВСЕМ, а не никому. Если прочитать
 * это правило наоборот, первое же включение фильтрации оставит очередь пустой,
 * и обращения повиснут молча. Поэтому предупреждение о пустом наборе — часть
 * контракта экрана, а не украшение.
 */

const ADMIN_PERMISSIONS: Permission[] = ["accounts:read", "accounts:manage", "users:manage"];
const HEAD_PERMISSIONS: Permission[] = ["accounts:read", "stats:all", "audit:read"];

const ACCOUNT: AvitoAccountDto = {
  id: "acc-7",
  title: "! Парт - 7 / Ист - В43 МНЧ !",
  avito_user_id: 123456789,
  status: "active",
  token_expires_at: "2026-09-01T06:10:00Z",
  webhook: { status: "ok", url: null, last_event_at: null },
  bot_id: null,
  created_at: "2026-06-02T12:00:00Z",
  operators: {
    count: 2,
    preview: [
      { id: "u-1", full_name: "Анна Смирнова" },
      { id: "u-2", full_name: "Пётр Ковалёв" },
    ],
  },
};

/** Канал без назначенных — на карточке это «все», а не «0». */
const OPEN_ACCOUNT: AvitoAccountDto = {
  ...ACCOUNT,
  id: "acc-open",
  title: "Парт - 723 БЕЛЫЙ",
  operators: { count: 0, preview: [] },
};

const OPERATORS: ChannelOperatorsResponse = {
  assigned_ids: ["u-1", "u-2"],
  candidates: [
    {
      id: "u-1",
      full_name: "Анна Смирнова",
      role: "manager",
      is_active: true,
      is_online: true,
      can_be_operator: true,
      // Два пробела — как на бою: рядом живут «Диспетчер МНЧ» и «Диспетчер  МНЧ».
      department: "Диспетчер  МНЧ",
    },
    {
      id: "u-2",
      full_name: "Пётр Ковалёв",
      role: "manager",
      is_active: true,
      is_online: false,
      can_be_operator: true,
      department: null,
    },
    {
      id: "u-3",
      full_name: "Ольга Некрасова",
      role: "manager",
      is_active: true,
      is_online: true,
      can_be_operator: true,
      department: "ОКК",
    },
    {
      id: "u-4",
      full_name: "Олег Наблюдатель",
      role: "observer",
      is_active: true,
      is_online: false,
      can_be_operator: false,
      reason: "наблюдатель не отвечает клиентам",
      department: null,
    },
    {
      id: "u-5",
      full_name: "Ирина Руководитель",
      role: "head",
      is_active: true,
      is_online: true,
      can_be_operator: false,
      reason: "руководитель не отвечает клиентам",
      department: null,
    },
  ],
};

/**
 * Тот же канал, но один из назначенных сотрудников отключён (уволили, ушёл в
 * отпуск). Сервер отдаёт его И в `assigned_ids` (`assigned_user_ids` — «все
 * назначенные, включая отключённых»), И в кандидатах с `can_be_operator: false`.
 * Набор с ним сервер с 03.09 принимает: `set_operators` судит только добавляемых.
 */
const OPERATORS_WITH_DISABLED: ChannelOperatorsResponse = {
  ...OPERATORS,
  candidates: OPERATORS.candidates.map((c) =>
    c.id === "u-2"
      ? { ...c, is_active: false, can_be_operator: false, reason: "Сотрудник отключён" }
      : c,
  ),
};

type Handler = (url: URL, init?: RequestInit) => Response | null;

let extraHandler: Handler | null = null;
let fetchMock: ReturnType<typeof vi.fn>;
let accounts: AvitoAccountDto[];
let operators: ChannelOperatorsResponse;

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
    if (url.pathname.endsWith("/operators")) {
      if (init?.method === "PUT") return jsonResponse(200, { operators: { count: 1, preview: [] } });
      return jsonResponse(200, operators);
    }
    if (url.pathname.endsWith("/avito-accounts")) {
      return jsonResponse(200, { items: accounts, page: { limit: 50, offset: 0, total: accounts.length } });
    }
    return jsonResponse(200, {});
  });
  vi.stubGlobal("fetch", fetchMock);
}

function renderAccounts(permissions: Permission[] = ADMIN_PERMISSIONS) {
  resetSessionStore({
    user: { ...fakeUser, role: permissions.includes("accounts:manage") ? "admin" : "head" },
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
  return renderWithProviders(<AccountsPage />, { route: "/settings/accounts" });
}

/**
 * Раскрыть первую строку списка каналов.
 *
 * ⚠ 27.08 каналы стали списком: состав операторов — отчётный блок, он живёт под
 * раскрытием. Сигнальное (статус, токен, подписка, действия) осталось в строке.
 */
async function раскрыть(user: ReturnType<typeof userEvent.setup>) {
  await user.click((await screen.findAllByRole("button", { name: /^Развернуть / }))[0]);
}

/**
 * Открыть экран назначения и дождаться ДАННЫХ, а не только появления окна:
 * чанк ленивый, список приезжает запросом, и `findByRole("dialog")` резолвится
 * ещё на спиннере.
 */
async function openAssign(user: ReturnType<typeof userEvent.setup>) {
  await раскрыть(user);
  await user.click((await screen.findAllByRole("button", { name: /^Назначить операторов на аккаунт/ }))[0]);
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByLabelText("Поиск по имени или отделу");
  return dialog;
}

function checkbox(dialog: HTMLElement, name: string | RegExp) {
  return within(dialog).getByRole("checkbox", { name });
}

describe("Назначение операторов на канал (7.2, Jivo 15 §2.2)", () => {
  beforeEach(() => {
    accounts = [ACCOUNT];
    operators = OPERATORS;
    extraHandler = null;
    queryClient.clear();
    setupFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    queryClient.clear();
  });

  it("на карточке видно «Операторы: N» и кнопку «Назначить»", async () => {
    const user = userEvent.setup();
    renderAccounts();
    await раскрыть(user);
    expect(await screen.findByText(/Операторы:/)).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `Назначить операторов на аккаунт ${ACCOUNT.title}` }),
    ).toBeInTheDocument();
  });

  it("канал без назначенных подписан «все», а не «0» — правило совместимости", async () => {
    const user = userEvent.setup();
    accounts = [OPEN_ACCOUNT];
    renderAccounts();
    await раскрыть(user);
    expect(await screen.findByText("все")).toBeInTheDocument();
    expect(
      screen.getByText("Никто не назначен — обращения канала видят все операторы"),
    ).toBeInTheDocument();
  });

  it("руководителю показывают состав, но не дают кнопку «Назначить» (11 §4.1)", async () => {
    const user = userEvent.setup();
    renderAccounts(HEAD_PERMISSIONS);
    await раскрыть(user);
    expect(await screen.findByText(/Операторы:/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Назначить операторов/ })).not.toBeInTheDocument();
  });

  it("экран показывает сотрудников с галочками и текущий набор канала", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    expect(
      within(dialog).getByText(
        "Только сотрудники, назначенные операторами, могут общаться с клиентами этого аккаунта.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/Назначить операторов на аккаунт/)).toBeInTheDocument();

    expect(checkbox(dialog, /Анна Смирнова/)).toBeChecked();
    expect(checkbox(dialog, /Пётр Ковалёв/)).toBeChecked();
    expect(checkbox(dialog, /Ольга Некрасова/)).not.toBeChecked();
    expect(within(dialog).getByText("Выбрано: 2 из 3")).toBeInTheDocument();
  });

  it("поиск по имени оставляет в списке только совпавших", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    await user.type(within(dialog).getByLabelText("Поиск по имени или отделу"), "ольга");

    expect(checkbox(dialog, /Ольга Некрасова/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox", { name: /Анна Смирнова/ })).not.toBeInTheDocument();
  });

  /*
   * ОТДЕЛ В СКОБКАХ У ИМЕНИ (просьба владельца 04.09).
   *
   * ⚠ ЗАЧЕМ ЭТО НУЖНО ИМЕННО ЗДЕСЬ. На канал назначают отделами — «все чатеры
   * на этот канал», — а список показывал одни имена: чтобы понять, кто из них
   * чатер, приходилось держать открытой вкладку «Команда» в соседнем окне.
   *
   * ⚠ ОТДЕЛ ЗАПОЛНЕН НЕ У ВСЕХ (замер боя), и это не мелочь: «Пётр Ковалёв ()»
   * читался бы как сломанный экран. Пустой отдел скобок не даёт вовсе.
   */
  it("отдел стоит в скобках у имени, а у кого его нет — скобок нет", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    expect(checkbox(dialog, /^Ольга Некрасова \(ОКК\)/)).toBeInTheDocument();
    // Два пробела в поле схлопнуты: на бою рядом лежат «Диспетчер МНЧ» и
    // «Диспетчер  МНЧ», и человек читает их как один отдел.
    expect(checkbox(dialog, /^Анна Смирнова \(Диспетчер МНЧ\)/)).toBeInTheDocument();
    // У Петра отдел пуст — имя остаётся голым, без «()».
    const пётр = checkbox(dialog, /Пётр Ковалёв/);
    expect(пётр.getAttribute("aria-label") ?? пётр.closest("label")?.textContent ?? "").not.toContain(
      "()",
    );
  });

  /*
   * ПОИСК ЗНАЕТ ТО, ЧТО ПОКАЗЫВАЕТ. Увидев «Ольга Некрасова (ОКК)», человек
   * печатает «ОКК» — и список, который на это ничего не находит, читается как
   * сломанный поиск.
   */
  it("поиск находит по отделу, а не только по имени", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    await user.type(within(dialog).getByLabelText("Поиск по имени или отделу"), "окк");

    expect(checkbox(dialog, /Ольга Некрасова/)).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("checkbox", { name: /Анна Смирнова/ }),
    ).not.toBeInTheDocument();
  });

  it("наблюдателя и руководителя назначить нельзя, и видно почему", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    const observer = checkbox(dialog, /Олег Наблюдатель/);
    expect(observer).toBeDisabled();
    expect(checkbox(dialog, /Ирина Руководитель/)).toBeDisabled();
    expect(within(dialog).getByText(/наблюдатель не отвечает клиентам/)).toBeInTheDocument();
    expect(within(dialog).getByText(/руководитель не отвечает клиентам/)).toBeInTheDocument();

    // Клик по неактивной строке ничего не меняет — набор остаётся прежним.
    await user.click(observer);
    expect(observer).not.toBeChecked();
    expect(within(dialog).getByText("Выбрано: 2 из 3")).toBeInTheDocument();
  });

  it("снятые все галочки дают предупреждение «канал будет доступен всем»", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    expect(within(dialog).queryByText("Никто не выбран")).not.toBeInTheDocument();

    await user.click(checkbox(dialog, /Анна Смирнова/));
    await user.click(checkbox(dialog, /Пётр Ковалёв/));

    expect(await within(dialog).findByText("Никто не выбран")).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Аккаунт будет доступен/).textContent,
    ).toMatch(/доступен\s+всем\s+операторам/);
  });

  it("сохранение — один PUT с ПОЛНЫМ набором, а не добавить/удалить", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    await user.click(checkbox(dialog, /Ольга Некрасова/)); // + u-3
    await user.click(checkbox(dialog, /Пётр Ковалёв/)); // − u-2
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => {
      const put = calls().find((c) => c.init?.method === "PUT");
      expect(put).toBeTruthy();
      expect(put!.url).toContain("/avito-accounts/acc-7/operators");
      expect(JSON.parse(String(put!.init?.body))).toEqual({ operator_ids: ["u-1", "u-3"] });
    });

    // Ровно один запрос на сохранение — замена целиком, без «дельт».
    expect(calls().filter((c) => c.init?.method === "PUT")).toHaveLength(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("пустой набор сохраняется как есть — это осознанное «открыть всем»", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    await user.click(checkbox(dialog, /Анна Смирнова/));
    await user.click(checkbox(dialog, /Пётр Ковалёв/));
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => {
      const put = calls().find((c) => c.init?.method === "PUT");
      expect(put).toBeTruthy();
      expect(JSON.parse(String(put!.init?.body))).toEqual({ operator_ids: [] });
    });
  });

  it("«Сохранить» неактивно, пока ничего не меняли", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    expect(within(dialog).getByRole("button", { name: "Сохранить" })).toBeDisabled();
    await user.click(checkbox(dialog, /Ольга Некрасова/));
    expect(within(dialog).getByRole("button", { name: "Сохранить" })).toBeEnabled();
  });

  it("закрытие с несохранёнными изменениями сначала спрашивает", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    await user.click(checkbox(dialog, /Ольга Некрасова/));
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));

    // Экран на месте, показан вопрос — ввод не потерян.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(dialog).getByText("Изменения не сохранены")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Остаться" }));
    expect(checkbox(dialog, /Ольга Некрасова/)).toBeChecked();

    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await user.click(within(dialog).getByRole("button", { name: "Закрыть без сохранения" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(calls().some((c) => c.init?.method === "PUT")).toBe(false);
  });

  it("без изменений «Отмена» закрывает сразу, без лишнего вопроса", async () => {
    const user = userEvent.setup();
    renderAccounts();
    const dialog = await openAssign(user);

    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("экран назначения подключён ЛЕНИВО и в стартовый бандл не попадает (03 §7)", () => {
    // Сторож бюджета: `import(...)` внутри AccountsPage Rollup выносит в свой
    // чанк, статический `import ... from "./AssignOperatorsModal"` — нет, и
    // экран с формой уедет в бандл рабочего места молча. Проверяем по
    // исходникам, как webBundlePurity.test.ts, — без сборки.
    const sources = import.meta.glob("/src/features/settings/accounts/*.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const page = sources["/src/features/settings/accounts/AccountsPage.tsx"] ?? "";

    expect(page).toMatch(/import\(\s*["']\.\/AssignOperatorsModal["']\s*\)/);
    expect(page).not.toMatch(/^[ \t]*import\s[^;]*["']\.\/AssignOperatorsModal["']/m);
  });

  /**
   * Отключённый сотрудник, оставшийся назначенным на канал, — не редкий случай,
   * а обычный: людей увольняют и отправляют в отпуск, а связь с каналом сервер
   * намеренно НЕ рвёт (`assigned_user_ids`: «правда о галочках», иначе форма
   * молча снимала бы отпускника).
   *
   * Такая галочка стоит (человек назначен), и снять её можно, а вернуть — нет.
   * С 03.09 сервер набор с ним принимает (судит только добавляемых), и экран
   * сохранения не запирает (проверка 24.09): иначе ради новичка приходилось
   * снимать отпускника, и после включения тот молча оставался без канала.
   */
  describe("на канале остался назначенный, но отключённый сотрудник", () => {
    beforeEach(() => {
      operators = OPERATORS_WITH_DISABLED;
    });

    it("его галочку МОЖНО снять — иначе канал не сохранить вовсе", async () => {
      const user = userEvent.setup();
      renderAccounts();
      const dialog = await openAssign(user);

      const disabledRow = checkbox(dialog, /Пётр Ковалёв/);
      expect(disabledRow).toBeChecked();
      expect(disabledRow).toBeEnabled();

      await user.click(disabledRow);
      expect(disabledRow).not.toBeChecked();
    });

    it("снятого назад не вернуть: назначать отключённого нельзя", async () => {
      const user = userEvent.setup();
      renderAccounts();
      const dialog = await openAssign(user);

      await user.click(checkbox(dialog, /Пётр Ковалёв/));
      expect(checkbox(dialog, /Пётр Ковалёв/)).toBeDisabled();
    });

    it("его присутствие не запирает сохранение: он остаётся в наборе, и сказано почему", async () => {
      const user = userEvent.setup();
      renderAccounts();
      const dialog = await openAssign(user);

      // Кто сейчас не ведёт — названо именем: строка может быть скрыта поиском.
      const note = within(dialog).getByRole("alert");
      expect(note).toHaveTextContent("Сейчас не ведут аккаунт");
      expect(note).toHaveTextContent("Пётр Ковалёв");

      // Добавили новичка — сохраняется вместе с отпускником.
      await user.click(checkbox(dialog, /Ольга Некрасова/));
      expect(within(dialog).getByRole("button", { name: "Сохранить" })).toBeEnabled();
      await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
      await waitFor(() => {
        const put = calls().find((c) => c.init?.method === "PUT");
        expect(put).toBeTruthy();
        expect(JSON.parse(String(put!.init?.body))).toEqual({
          operator_ids: ["u-1", "u-2", "u-3"],
        });
      });
    });

    it("счётчик не показывает «выбрано 3 из 2» — знаменатель считает и запертых", async () => {
      const user = userEvent.setup();
      renderAccounts();
      const dialog = await openAssign(user);

      // Назначено двое (u-1, u-2), но отвечать клиентам могут u-1 и u-3.
      expect(within(dialog).getByText("Выбрано: 2 из 3")).toBeInTheDocument();
      await user.click(checkbox(dialog, /Ольга Некрасова/));
      expect(within(dialog).getByText("Выбрано: 3 из 3")).toBeInTheDocument();
    });
  });

  it("отказ сервера оставляет экран открытым и объясняет, что случилось", async () => {
    const user = userEvent.setup();
    extraHandler = (url, init) =>
      url.pathname.endsWith("/operators") && init?.method === "PUT"
        ? jsonResponse(500, { error: { code: "internal_error", message: "Не получилось сохранить" } })
        : null;
    renderAccounts();
    const dialog = await openAssign(user);

    await user.click(checkbox(dialog, /Ольга Некрасова/));
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/Не получилось сохранить/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
