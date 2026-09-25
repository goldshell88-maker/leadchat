import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { AppLayout } from "@/app/AppLayout";
import { ErrorBoundary, RouteErrorScreen, pageActions } from "@/app/ErrorBoundary";
import { queryClient } from "@/app/queryClient";
import { router as appRouter } from "@/app/router";
import { theme } from "@/app/theme";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders, wrap } from "./render";

/**
 * Экран отказа вместо стек-трейса (аудит SHELL-01 и SHELL-02).
 *
 * ЧТО ЛОВИТ ЭТОТ ФАЙЛ. До правки в приложении не было ни одного ErrorBoundary
 * и ни одного `errorElement`: исключение в отрисовке ловил собственный
 * обработчик react-router и рисовал страницу без стилей с английским
 * «Unexpected Application Error!» и `<pre>`, в котором лежал полный стек
 * вызовов — в собранном бандле тоже, ветка `NODE_ENV` его не вырезает.
 *
 * Отсюда три требования, и каждое проверяется отдельно:
 *  1. текст по-русски и кнопка «Обновить страницу»;
 *  2. НИ стека, НИ текста исключения на экране;
 *  3. шапка и рельса остаются — падение экрана не уносит рабочее место.
 */

/** Компонент, который честно падает при отрисовке. */
function Boom({
  message = "Cannot read properties of undefined (reading 'id')",
}: {
  message?: string;
}): React.ReactElement {
  throw new TypeError(message);
}

/** Провайдеры без MemoryRouter: у теста с `createMemoryRouter` роутер свой. */
function withProviders(ui: React.ReactElement) {
  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        {ui}
      </MantineProvider>
    </QueryClientProvider>
  );
}

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  queryClient.clear();
  window.sessionStorage.clear();
  // React печатает пойманное исключение сам, и наш `reportCrash` печатает тоже:
  // без глушителя вывод теста тонет в стеке, ради которого всё и затевалось.
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse(200, { status: "ok", version: "test" })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete window.Sentry;
});

describe("Экран отказа (SHELL-01)", () => {
  it("показывает человеческий текст вместо «Unexpected Application Error!»", () => {
    renderWithProviders(
      <ErrorBoundary where="test">
        <Boom />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Что-то сломалось на этом экране")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Обновить страницу" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "К диалогам" })).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected Application Error/i)).toBeNull();
  });

  it("не показывает ни стек вызовов, ни текст исключения", () => {
    const { container } = renderWithProviders(
      <ErrorBoundary where="test">
        <Boom message="Cannot read properties of undefined (reading 'assignee')" />
      </ErrorBoundary>,
    );

    // Именно так стек попадал на экран у react-router: <pre> с полным трейсом.
    expect(container.querySelector("pre")).toBeNull();
    expect(screen.queryByText(/Cannot read properties of undefined/)).toBeNull();
    expect(screen.queryByText(/TypeError/)).toBeNull();
    // …при этом в консоли администратора подробности остаются.
    expect(consoleError.mock.calls.some((c) => String(c[0]).includes("сбой отрисовки"))).toBe(true);
  });

  it("«Обновить страницу» действительно перезагружает вкладку", async () => {
    const reload = vi.spyOn(pageActions, "reload").mockImplementation(() => {});
    const user = userEvent.setup();

    renderWithProviders(
      <ErrorBoundary where="test">
        <Boom />
      </ErrorBoundary>,
    );
    await user.click(screen.getByRole("button", { name: "Обновить страницу" }));

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("«К диалогам» уводит на рабочее место", async () => {
    const goHome = vi.spyOn(pageActions, "goHome").mockImplementation(() => {});
    const user = userEvent.setup();

    renderWithProviders(
      <ErrorBoundary where="test">
        <Boom />
      </ErrorBoundary>,
    );
    await user.click(screen.getByRole("button", { name: "К диалогам" }));

    expect(goHome).toHaveBeenCalledTimes(1);
  });

  it("переход в другой раздел снимает экран отказа", () => {
    // Без сброса по маршруту человек оставался бы на экране отказа навсегда:
    // граница React сама себя не чинит, а смена раздела меняет только Outlet.
    /*
     * `wrap` вокруг обоих деревьев — не украшение. Первая версия теста
     * перерисовывала голый `ErrorBoundary` поверх обёрнутого провайдерами:
     * React видел другую структуру корня, размонтировал старое дерево и
     * монтировал новую границу с чистым состоянием. Тест проходил и БЕЗ сброса
     * по маршруту, то есть не проверял ничего.
     */
    const view = renderWithProviders(
      <ErrorBoundary where="test" resetKey="/stats">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Что-то сломалось на этом экране")).toBeInTheDocument();

    view.rerender(
      wrap(
        <ErrorBoundary where="test" resetKey="/chats">
          <div>Рабочее место</div>
        </ErrorBoundary>,
      ),
    );

    expect(screen.getByText("Рабочее место")).toBeInTheDocument();
    expect(screen.queryByText("Что-то сломалось на этом экране")).toBeNull();
  });

  it("сообщает о поломке в Sentry, если он подключён", () => {
    const captureException = vi.fn();
    window.Sentry = { captureException };

    renderWithProviders(
      <ErrorBoundary where="screen">
        <Boom message="Boom for Sentry" />
      </ErrorBoundary>,
    );

    expect(captureException).toHaveBeenCalledTimes(1);
    const [error, hint] = captureException.mock.calls[0];
    expect((error as Error).message).toBe("Boom for Sentry");
    expect((hint as { tags: { where: string } }).tags.where).toBe("screen");
  });

  it("падение самой телеметрии не отменяет экран отказа", () => {
    window.Sentry = {
      captureException: () => {
        throw new Error("Sentry down");
      },
    };

    renderWithProviders(
      <ErrorBoundary where="screen">
        <Boom />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Что-то сломалось на этом экране")).toBeInTheDocument();
  });
});

describe("Падение экрана не уносит рабочее место (SHELL-01)", () => {
  beforeEach(() => {
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["conversations:read", "conversations:manage", "stats:all"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  it("шапка, рельса и колокольчик остаются на месте", () => {
    renderWithProviders(
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/chats" element={<Boom />} />
        </Route>
      </Routes>,
      { route: "/chats" },
    );

    expect(screen.getByText("Что-то сломалось на этом экране")).toBeInTheDocument();
    // Ровно то, что раньше исчезало вместе с экраном.
    expect(screen.getByText("LeadChat")).toBeInTheDocument();
    expect(screen.getByLabelText("Основная навигация")).toBeInTheDocument();
    expect(screen.getByLabelText("Уведомления")).toBeInTheDocument();
  });
});

describe("Ленивый раздел после выкатки (SHELL-02)", () => {
  beforeEach(() => {
    /*
     * Роутер данных на каждый переход собирает `new Request(...)` с сигналом
     * прерывания. В node+jsdom это разные реализации: `AbortSignal` из jsdom,
     * `Request` из undici, и undici такой сигнал не принимает. К проверяемому
     * поведению отношения не имеет — в браузере они одни и те же, — поэтому
     * подменяем заглушкой: сам объект запроса здесь никем не читается.
     */
    vi.stubGlobal(
      "Request",
      class {
        readonly signal: unknown;
        constructor(
          public url: string,
          init?: { signal?: unknown },
        ) {
          this.signal = init?.signal;
        }
      },
    );
  });

  /** Тот самый отказ: чанк, которого больше нет на сервере после деплоя. */
  function staleRouter() {
    return createMemoryRouter(
      [
        {
          path: "/stats",
          lazy: async () => {
            throw new TypeError(
              "Failed to fetch dynamically imported module: https://chat.partner-lead-centre.ru/assets/StatsPage-D1r7yQ.js",
            );
          },
          errorElement: <RouteErrorScreen />,
        },
      ],
      { initialEntries: ["/stats"] },
    );
  }

  it("предлагает обновиться, а не показывает «Failed to fetch dynamically imported module»", async () => {
    vi.spyOn(pageActions, "reload").mockImplementation(() => {});

    render(withProviders(<RouterProvider router={staleRouter()} />));

    expect(await screen.findByText("Вышла новая версия")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Обновить страницу" })).toBeInTheDocument();
    expect(screen.queryByText(/Failed to fetch dynamically imported module/)).toBeNull();
  });

  it("перезагружает вкладку сама — но ровно один раз, без петли", async () => {
    const reload = vi.spyOn(pageActions, "reload").mockImplementation(() => {});

    const first = render(withProviders(<RouterProvider router={staleRouter()} />));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    first.unmount();

    // Сломанный сервер отдаёт 404 и после перезагрузки. Если бы отметка в
    // sessionStorage не ставилась, рабочее место превратилось бы в мигающую
    // страницу, которую нечем остановить.
    render(withProviders(<RouterProvider router={staleRouter()} />));
    expect(await screen.findByText("Вышла новая версия")).toBeInTheDocument();
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe("Карта маршрутов", () => {
  interface RouteNode {
    path?: string;
    lazy?: unknown;
    errorElement?: unknown;
    children?: RouteNode[];
  }

  function flatten(nodes: readonly RouteNode[]): RouteNode[] {
    return nodes.flatMap((n) => [n, ...flatten(n.children ?? [])]);
  }

  const all = flatten(appRouter.routes as unknown as RouteNode[]);

  it("у каждого ленивого раздела свой экран отказа", () => {
    // Общий errorElement на родителе унёс бы шапку и рельсу: он рисуется на
    // месте СВОЕГО маршрута. Поэтому проверяем поимённо каждый ленивый лист.
    const lazyRoutes = all.filter((r) => typeof r.lazy === "function");
    expect(lazyRoutes.length).toBeGreaterThanOrEqual(9);
    expect(lazyRoutes.filter((r) => !r.errorElement).map((r) => r.path)).toEqual([]);
  });

  it("верхний ярус тоже закрыт — падение каркаса и входа не даёт стека", () => {
    const top = appRouter.routes as unknown as RouteNode[];
    const uncovered = top.filter((r) => !r.errorElement && r.path !== "*").map((r) => r.path ?? "(каркас)");
    expect(uncovered).toEqual([]);
  });
});
