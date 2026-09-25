import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import type { SandboxStartBody } from "@/shared/api/types";
import { ADMIN_PERMISSIONS, BOT_ID, avitoAccountsPage, botDetail, resetBotDraft } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

/**
 * Песочница (02 §5.3, 11 §5.4): работает с ЧЕРНОВИКОМ (сохранять не нужно),
 * рисует ответы бота, серую трассировку шагов, панель состояния и умеет
 * промотать таймаут `ask` без ожидания суток.
 */

const START = {
  session_id: "sess-1",
  events: [],
  state: null,
};

const AFTER_MESSAGE = {
  events: [
    { kind: "bot_message", text: "Здравствуйте, Иван! Это сервис Lead Partner" },
    { kind: "step", id: "ask_problem", type: "ask" },
    { kind: "waiting", var: "problem", deadline: "через 24 ч" },
  ],
  state: {
    step: "ask_problem",
    waiting: { kind: "ask", var: "problem", deadline: "через 24 ч" },
    vars: { problem: "разбил экран айфона" },
    counters: { steps_total: 2, bot_msgs_row: 1, offscript_msgs: 0, ai_calls: 0 },
  },
};

const AFTER_TIMEOUT = {
  events: [
    { kind: "handoff", reason: "ask_timeout", comment: "Клиент не ответил — передаю мастеру" },
    { kind: "tags", tags: ["ночной-лид"] },
  ],
  state: {
    step: "handoff_night",
    waiting: null,
    vars: { problem: "разбил экран айфона" },
    counters: { steps_total: 4, bot_msgs_row: 1, offscript_msgs: 0, ai_calls: 1 },
  },
};

describe("Песочница сценария", () => {
  let sandboxCalls: Array<{ path: string; body: unknown }>;

  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    sandboxCalls = [];
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && (init?.method ?? "GET") === "GET") {
          return jsonResponse(200, botDetail());
        }
        if (url.pathname.startsWith("/api/v1/bots/sandbox")) {
          sandboxCalls.push({ path: url.pathname, body });
          if (url.pathname.endsWith("/start")) return jsonResponse(200, START);
          if (url.pathname.endsWith("/message")) return jsonResponse(200, AFTER_MESSAGE);
          if (url.pathname.endsWith("/fire-timeout")) return jsonResponse(200, AFTER_TIMEOUT);
          return jsonResponse(204, null);
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const openSandbox = async (user: ReturnType<typeof userEvent.setup>) => {
    const utils = renderBotEditor();
    await screen.findByRole("textbox", { name: "Имя бота" });
    await user.click(screen.getByRole("button", { name: "Протестировать" }));
    await waitFor(() => expect(sandboxCalls.some((c) => c.path.endsWith("/start"))).toBe(true));
    // Drawer открывается с анимацией — дожидаемся его содержимого.
    await screen.findByRole("textbox", { name: "Ответ клиента" });
    return utils;
  };

  it("стартует на несохранённом черновике и прогоняет диалог с трассировкой", async () => {
    const user = userEvent.setup();
    await openSandbox(user);

    // Черновик уходит целиком: сценарий, база знаний и расписание (02 §5.3).
    const startBody = sandboxCalls[0].body as SandboxStartBody;
    expect(startBody.scenario.entry).toBe("greet");
    expect(startBody.knowledge_base).toContain("Замена экрана");
    expect(startBody.ai_mode).toBe("stub");
    expect(startBody.now_override).toBeNull();

    const input = await screen.findByRole("textbox", { name: "Ответ клиента" });
    await user.type(input, "разбил экран айфона");
    await user.click(screen.getByRole("button", { name: "Отправить ответ клиента" }));

    const chat = await screen.findByTestId("sandbox-chat");
    await waitFor(() => expect(chat).toHaveTextContent("Здравствуйте, Иван!"));
    // Реплика «клиента» и серая строка трассировки шага.
    expect(chat).toHaveTextContent("разбил экран айфона");
    // Стрелка из начала строки трассировки убрана 04.09: символ в начале
    // содержимого — это подмена значка, и её ловит ownIcons0409.test.ts.
    expect(chat).toHaveTextContent("Шаг ask_problem (ask)");

    // Панель рисуется в портале (иначе уезжает за экран) — ищем по документу.
    const state = document.querySelector(".sandbox-state") as HTMLElement;
    expect(state.textContent).toContain("ask_problem");
    expect(state.textContent).toContain("problem");
    expect(state.textContent).toContain("шагов: 2/100");
    expect(state.textContent).toContain("подряд бота: 1/5");
  });

  it("«Промотать таймаут» активна в WAITING и доводит сценарий до handoff", async () => {
    const user = userEvent.setup();
    await openSandbox(user);

    const timeoutButton = screen.getByRole("button", { name: "⏩ Промотать таймаут" });
    expect(timeoutButton).toBeDisabled(); // бот ещё ничего не ждёт

    const input = await screen.findByRole("textbox", { name: "Ответ клиента" });
    await user.type(input, "разбил экран айфона");
    await user.click(screen.getByRole("button", { name: "Отправить ответ клиента" }));
    await waitFor(() => expect(timeoutButton).toBeEnabled());

    await user.click(timeoutButton);
    await waitFor(() => expect(sandboxCalls.some((c) => c.path.endsWith("/fire-timeout"))).toBe(true));

    const chat = await screen.findByTestId("sandbox-chat");
    await waitFor(() => expect(chat).toHaveTextContent("HANDOFF: ask_timeout"));
    expect(chat).toHaveTextContent("Клиент не ответил — передаю мастеру");
    expect((document.querySelector(".sandbox-state") as HTMLElement).textContent).toContain(
      "handoff_night",
    );
  });

  /*
   * Ящик обязан жить в портале, а не внутри колонки настроек.
   *
   * Пока стоял `withinPortal={false}`, его `position: fixed` считался от этой
   * колонки: на 1280 px ящик уезжал вправо на ширину двух меню, и правая
   * колонка состояния (шаг, переменные, счётчики, «⏩ Промотать таймаут»)
   * оказывалась за краем экрана целиком, без всякой прокрутки.
   *
   * Сам сдвиг — вопрос раскладки, в jsdom её нет; проверяем ПРИЧИНУ: панель
   * не должна быть потомком поддерева редактора.
   */
  it("рисуется в портале, а не внутри колонки настроек", async () => {
    const user = userEvent.setup();
    const { container } = await openSandbox(user);

    const drawer = document.querySelector(".mantine-Drawer-content");
    expect(drawer).not.toBeNull();
    expect(container.contains(drawer)).toBe(false);
  });

  it("режим «настоящий AI» предупреждает про расход токенов", async () => {
    const user = userEvent.setup();
    await openSandbox(user);

    await user.click(await screen.findByRole("radio", { name: "настоящий" }));
    expect(await screen.findByText(/расходует токены/)).toBeInTheDocument();
  });

  it("422 при старте показывает список ошибок вместо чата", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && (init?.method ?? "GET") === "GET") {
          return jsonResponse(200, botDetail());
        }
        if (url.pathname.endsWith("/sandbox/start")) {
          sandboxCalls.push({ path: url.pathname, body: undefined });
          return jsonResponse(
            422,
            errorEnvelope("bot_scenario_invalid", "Шаг greet: переход на несуществующий шаг", {
              step_id: "greet",
              reason: "broken_ref",
            }),
          );
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );

    await openSandbox(user);
    expect(await screen.findByText("Сначала почините сценарий")).toBeInTheDocument();
    expect(screen.getByText(/greet: Шаг greet: переход на несуществующий шаг/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Ответ клиента" })).toBeDisabled();
  });

  /*
   * FUNC-67: у ленты не было состояния ошибки вовсе. Сессия живёт час, и когда
   * она истекала посреди отладки, кнопка «отправить» отщёлкивала, а на экране
   * не появлялось ни строчки — «песочница сломалась и молчит».
   */
  it("истёкшая сессия объясняется словами и предлагает начать заново", async () => {
    const user = userEvent.setup();
    let sessionAlive = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && (init?.method ?? "GET") === "GET") {
          return jsonResponse(200, botDetail());
        }
        if (url.pathname.endsWith("/sandbox/start")) {
          sandboxCalls.push({ path: url.pathname, body: undefined });
          return jsonResponse(200, START);
        }
        if (url.pathname.endsWith("/message")) {
          sandboxCalls.push({ path: url.pathname, body: undefined });
          return sessionAlive
            ? jsonResponse(200, AFTER_MESSAGE)
            : jsonResponse(
                404,
                errorEnvelope("not_found", "Сессия песочницы истекла — начните заново"),
              );
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );

    await openSandbox(user);
    sessionAlive = false;
    const input = await screen.findByRole("textbox", { name: "Ответ клиента" });
    await user.type(input, "привет");
    await user.click(screen.getByRole("button", { name: "Отправить ответ клиента" }));

    expect(await screen.findByTestId("sandbox-failure")).toHaveTextContent(/истекла/);
    // Печатать в мёртвую сессию бессмысленно — поле гасим.
    expect(screen.getByRole("textbox", { name: "Ответ клиента" })).toBeDisabled();
    // Кнопок с этим именем теперь ДВЕ, и это не дефект: одна в панели
    // управления песочницей, вторая — в плашке истёкшей сессии. Обе зовут один
    // и тот же `reset`. Раньше панельная называлась «Сбросить» — тем же
    // словом, которым на соседних экранах снимают фильтры.
    expect(screen.getAllByRole("button", { name: "Начать заново" }).length).toBeGreaterThanOrEqual(
      1,
    );
    expect(
      within(screen.getByTestId("sandbox-failure")).getByRole("button", { name: "Начать заново" }),
    ).toBeInTheDocument();
  });

  it("сообщение, съеденное пустой подстановкой, видно в ленте", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && (init?.method ?? "GET") === "GET") {
          return jsonResponse(200, botDetail());
        }
        if (url.pathname.endsWith("/sandbox/start")) {
          sandboxCalls.push({ path: url.pathname, body: undefined });
          return jsonResponse(200, START);
        }
        if (url.pathname.endsWith("/message")) {
          return jsonResponse(200, {
            events: [{ kind: "empty_render", step: "greet", placeholders: ["item_title"] }],
            state: AFTER_MESSAGE.state,
          });
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );

    await openSandbox(user);
    const input = await screen.findByRole("textbox", { name: "Ответ клиента" });
    await user.type(input, "привет");
    await user.click(screen.getByRole("button", { name: "Отправить ответ клиента" }));

    const chat = await screen.findByTestId("sandbox-chat");
    await waitFor(() => expect(chat).toHaveTextContent(/Шаг greet/));
    expect(chat).toHaveTextContent(/item_title/);
  });
});
