// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно. Так же сделано в
// railChrome.test.ts и cssDeadClasses.test.ts.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter, type RouteObject } from "react-router-dom";
import { AppLayout } from "@/app/AppLayout";
import { forgetChunks, routeChunks } from "@/app/lazyRoutes";
import { queryClient } from "@/app/queryClient";
import { router as appRouter } from "@/app/router";
import { theme } from "@/app/theme";
import { SettingsLayout } from "@/features/settings/SettingsLayout";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ОТКЛИК НА ПЕРЕХОД В ЛЕНИВЫЙ РАЗДЕЛ (жалоба владельца 06.09: «задержки»).
 *
 * ЧТО БЫЛО. `lazy` намеренно оставляет прежний экран на месте, пока едет чанк
 * (довод у `/dialogs` в router.tsx), — и это правильно, но у нажатия не было
 * НИКАКОГО следа: ни на пункте, ни на экране, а сам чанк начинал ехать только
 * после нажатия. На медленной сети пункт секунду-две выглядел неработающим.
 *
 * Здесь три сторожа на три части ответа: чанк греется по наведению (и ровно
 * один раз), пункт помечается на время перехода, над экраном стоит полоса.
 * Поведение `lazy` не трогаем: без фолбэка — осознанно.
 */

/** Состояние перехода для полосы — подменяется на уровне модуля (см. `vi.mock`). */
const переход = vi.hoisted(() => ({ state: "idle" as "idle" | "loading" }));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigation: () => ({ state: переход.state }) as ReturnType<typeof actual.useNavigation>,
  };
});

function withProviders(ui: React.ReactElement) {
  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        {ui}
      </MantineProvider>
    </QueryClientProvider>
  );
}

/** Каркас на memory-роутере: у него есть состояние перехода, как у боевого. */
function renderShell(children: RouteObject[], initial: string) {
  const router = createMemoryRouter([{ element: <AppLayout />, children }], {
    initialEntries: [initial],
  });
  return render(withProviders(<RouterProvider router={router} />));
}

/** Пункты, которых у менеджера нет: «Разбор диалогов» и «Команда». */
const ПРАВА = [...fakeMe.permissions, "dialogs:read", "users:manage"] as Permission[];

/** Никогда не приезжающий чанк: сторож считает вызовы, а не ждёт модуль. */
const вечноЕдет = () => new Promise<never>(() => {});

function stripCssComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, " ");
}

/**
 * ⚠ ПЕРЕХОД В DATA-РОУТЕРЕ ПОД JSDOM. На переходе react-router собирает
 * `Request`, а `Request` здесь — из undici (Node), и он бракует `AbortSignal`
 * из jsdom необработанным отклонением (тот же капкан описан у `RequireAuth` в
 * router.tsx). Загрузчиков у маршрутов нет, сигнал никому не нужен — на время
 * теста `Request` его просто не получает. Снимается общим `unstubAllGlobals`.
 */
function requestБезСигнала(): void {
  const НастоящийRequest = globalThis.Request;
  vi.stubGlobal(
    "Request",
    class extends НастоящийRequest {
      constructor(input: RequestInfo | URL, init?: RequestInit) {
        super(input, init ? { ...init, signal: undefined } : init);
      }
    },
  );
}

describe("Отклик на переход в ленивый раздел (06.09)", () => {
  beforeEach(() => {
    queryClient.clear();
    forgetChunks();
    переход.state = "idle";
    resetSessionStore({ user: fakeUser, permissions: ПРАВА, accessToken: "t", bootstrapped: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/health")) {
          return jsonResponse(200, { status: "ok", db: true, redis: true, version: "test" });
        }
        return jsonResponse(200, { items: [], page: { next_cursor: null } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /**
   * Карта чанков и карта маршрутов — одно множество путей.
   *
   * Лишний ключ в карте — чанк, который греется, но никуда не ведёт; ленивый
   * маршрут мимо карты — раздел, который никто не греет и который снова
   * «ничего не делает» по нажатию.
   *
   * ⚠ ДИВЕРСИЯ: добавить в `routeChunks` ключ `"/nowhere"` — тест краснеет.
   */
  it("карта чанков совпадает с ленивыми маршрутами в обе стороны", () => {
    type RouteNode = { path?: string; lazy?: unknown; children?: RouteNode[] };
    const flatten = (nodes: RouteNode[]): RouteNode[] =>
      nodes.flatMap((n) => [n, ...flatten(n.children ?? [])]);
    const lazyPaths = flatten(appRouter.routes as unknown as RouteNode[])
      .filter((r) => typeof r.lazy === "function")
      .map((r) => r.path)
      .sort();

    expect(lazyPaths.length).toBeGreaterThan(0);
    expect(lazyPaths).toEqual(Object.keys(routeChunks).sort());
  });

  /**
   * Совпадение множеств — не совпадение пар: маршрут `/feed`, зовущий
   * `loadChunk("/stats")`, оставил бы предыдущего сторожа зелёным, а
   * предзагрузка грела бы один файл, тогда как переход ждал бы другой — ровно
   * тот разрыв, ради которого карта заведена (шапка lazyRoutes.ts). Здесь
   * каждый ленивый маршрут запускается и обязан спросить свой чанк и только его.
   *
   * ⚠ ДИВЕРСИЯ: в router.tsx у маршрута `/feed` заменить `loadChunk("/feed")`
   * на `loadChunk("/stats")` — тест краснеет.
   */
  it("каждый ленивый маршрут ждёт ровно свой чанк, а не соседний", () => {
    type RouteNode = { path?: string; lazy?: () => unknown; children?: RouteNode[] };
    const flatten = (nodes: RouteNode[]): RouteNode[] =>
      nodes.flatMap((n) => [n, ...flatten(n.children ?? [])]);
    const пути = Object.keys(routeChunks) as (keyof typeof routeChunks)[];
    const spies = Object.fromEntries(
      пути.map((p) => [p, vi.spyOn(routeChunks, p).mockImplementation(вечноЕдет)]),
    );

    for (const route of flatten(appRouter.routes as unknown as RouteNode[])) {
      if (typeof route.lazy !== "function") continue;
      forgetChunks();
      for (const spy of Object.values(spies)) spy.mockClear();
      // `import()` вызывается до первого `await` — синхронно внутри `lazy()`.
      void route.lazy();
      const спрошены = пути.filter((p) => spies[p].mock.calls.length > 0);
      expect(спрошены, `маршрут ${route.path}`).toEqual([route.path]);
    }
  });

  /**
   * ⚠ ДИВЕРСИЯ: убрать `{...preloadOnHover("/dialogs")}` у пункта рельсы в
   * AppRail.tsx — тест краснеет (ноль вызовов вместо одного).
   */
  it("наведение на пункт рельсы греет чанк раздела — один раз на любые повторы", () => {
    const чанк = vi.spyOn(routeChunks, "/dialogs").mockImplementation(вечноЕдет);
    renderShell([{ path: "/chats", element: <div /> }], "/chats");
    const пункт = screen.getByRole("link", { name: "Разбор диалогов" });

    // Само по себе появление пункта ничего не тянет: греем по намерению.
    expect(чанк).not.toHaveBeenCalled();

    fireEvent.pointerEnter(пункт);
    fireEvent.pointerLeave(пункт);
    fireEvent.pointerEnter(пункт);
    fireEvent.focus(пункт);

    expect(чанк).toHaveBeenCalledTimes(1);
  });

  /**
   * Фокус — САМ ПО СЕБЕ, без мыши. В предыдущем сторожe фокус идёт четвёртым
   * повтором после наведения, и снятый `onFocus` он не заметит: один вызов
   * уже есть. А с клавиатуры пункт выбирают не реже, чем мышью
   * (`features/hotkeys`), и обещание «фокус греет» записано у `preloadOnHover`.
   *
   * ⚠ ДИВЕРСИЯ: убрать `onFocus` из `preloadOnHover` в lazyRoutes.ts — тест
   * краснеет.
   */
  it("фокус с клавиатуры греет чанк и без мыши", () => {
    const чанк = vi.spyOn(routeChunks, "/dialogs").mockImplementation(вечноЕдет);
    renderShell([{ path: "/chats", element: <div /> }], "/chats");

    fireEvent.focus(screen.getByRole("link", { name: "Разбор диалогов" }));

    expect(чанк).toHaveBeenCalledTimes(1);
  });

  /**
   * ⚠ ДИВЕРСИЯ: убрать `{...preloadOnHover(to)}` у `Item` в SettingsLayout.tsx
   * — тест краснеет.
   */
  it("наведение на пункт меню настроек греет его чанк — тоже один раз", () => {
    const чанк = vi.spyOn(routeChunks, "/settings/team").mockImplementation(вечноЕдет);
    const router = createMemoryRouter(
      [{ element: <SettingsLayout />, children: [{ path: "/settings/profile", element: <div /> }] }],
      { initialEntries: ["/settings/profile"] },
    );
    render(withProviders(<RouterProvider router={router} />));
    const пункт = screen.getByRole("link", { name: "Команда" });

    expect(чанк).not.toHaveBeenCalled();
    fireEvent.pointerEnter(пункт);
    fireEvent.pointerEnter(пункт);
    fireEvent.focus(пункт);

    expect(чанк).toHaveBeenCalledTimes(1);
  });

  /**
   * Полоса над рабочей областью: есть, пока переход в состоянии `loading`, и
   * её нет в покое. Состояние подменено на уровне модуля, поэтому каркас
   * поднимается дважды, а не перерисовывается: элемент маршрута тот же самый,
   * и React пропустил бы его перерисовку.
   *
   * ⚠ ДИВЕРСИЯ: убрать `<NavProgress />` из AppLayout.tsx — тест краснеет.
   */
  it("полоса загрузки стоит при loading и отсутствует при idle", () => {
    переход.state = "idle";
    const покой = renderShell([{ path: "/chats", element: <div /> }], "/chats");
    expect(screen.queryByRole("progressbar", { name: "Загружаем раздел" })).toBeNull();
    покой.unmount();

    переход.state = "loading";
    renderShell([{ path: "/chats", element: <div /> }], "/chats");
    expect(screen.getByRole("progressbar", { name: "Загружаем раздел" })).toHaveClass(
      "lc-nav-progress",
    );
  });

  /**
   * Пункт, К КОТОРОМУ идём, помечен классом `pending`, пока чанк едет, и
   * метка снимается, когда раздел приехал. Класс вешает сам `NavLink` — а
   * значит, пункт ОБЯЗАН быть `NavLink`, не `Link` и не кнопкой.
   *
   * ⚠ ДИВЕРСИЯ: заменить `NavLink` на `Link` у пункта «Разбор диалогов» в
   * AppRail.tsx — тест краснеет.
   */
  it("пункт, к которому идём, помечен pending, пока едет чанк", async () => {
    requestБезСигнала();

    let приехал!: (module: { Component: () => React.ReactElement }) => void;
    const router = createMemoryRouter(
      [
        {
          element: <AppLayout />,
          children: [
            { path: "/chats", element: <div /> },
            {
              path: "/dialogs",
              lazy: () =>
                new Promise<{ Component: () => React.ReactElement }>((resolve) => {
                  приехал = resolve;
                }),
            },
          ],
        },
      ],
      { initialEntries: ["/chats"] },
    );
    render(withProviders(<RouterProvider router={router} />));
    const пункт = screen.getByRole("link", { name: "Разбор диалогов" });
    expect(пункт).not.toHaveClass("pending");

    fireEvent.click(пункт);
    await waitFor(() => expect(пункт).toHaveClass("pending"));

    приехал({ Component: () => <div>разбор приехал</div> });
    await waitFor(() => expect(пункт).not.toHaveClass("pending"));
    expect(screen.getByText("разбор приехал")).toBeInTheDocument();
  });

  /**
   * То же для меню настроек: `Item` обязан быть `NavLink` со строковым
   * `className`. Сторож предзагрузки этого не видит — `Link` тоже ссылка и
   * тоже греет чанк, — а правило `.settings-nav__item.pending` в CSS без
   * класса на элементе мёртвое.
   *
   * ⚠ ДИВЕРСИЯ: в SettingsLayout.tsx заменить `NavLink` у `Item` на `Link`
   * — тест краснеет.
   */
  it("пункт меню настроек помечен pending, пока едет его чанк", async () => {
    requestБезСигнала();

    let приехал!: (module: { Component: () => React.ReactElement }) => void;
    const router = createMemoryRouter(
      [
        {
          element: <SettingsLayout />,
          children: [
            { path: "/settings/profile", element: <div /> },
            {
              path: "/settings/team",
              lazy: () =>
                new Promise<{ Component: () => React.ReactElement }>((resolve) => {
                  приехал = resolve;
                }),
            },
          ],
        },
      ],
      { initialEntries: ["/settings/profile"] },
    );
    render(withProviders(<RouterProvider router={router} />));
    const пункт = screen.getByRole("link", { name: "Команда" });
    expect(пункт).not.toHaveClass("pending");

    fireEvent.click(пункт);
    await waitFor(() => expect(пункт).toHaveClass("pending"));

    приехал({ Component: () => <div>команда приехала</div> });
    await waitFor(() => expect(пункт).not.toHaveClass("pending"));
    expect(screen.getByText("команда приехала")).toBeInTheDocument();
  });

  /**
   * Стили ожидания: оба признака на токенах, полоса цветом действия, при
   * «меньше движения» — без анимации, и файл подключён каркасом (иначе правила
   * не едут в бандл, а jsdom со своим `css: false` этого не заметит).
   *
   * ⚠ ДИВЕРСИЯ: удалить блок `@media (prefers-reduced-motion: reduce)` из
   * nav-pending.css — тест краснеет.
   */
  it("стили ожидания стоят на токенах и уважают «меньше движения»", () => {
    const css = stripCssComments(readFileSync("src/app/nav-pending.css", "utf-8") as string);

    expect(css).toMatch(/\.lc-rail__item\.pending/);
    expect(css).toMatch(/\.settings-nav__item\.pending/);
    expect(css).toMatch(/\.lc-nav-progress\s*\{[^}]*background:\s*var\(--lc-primary\)/);
    // Цвета — только токенами: ни одного hex и ни одного rgb().
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(/i);

    const reduced = css.match(/@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/)?.[1];
    expect(reduced, "нет блока prefers-reduced-motion").toBeTruthy();
    expect(reduced).toMatch(/\.lc-nav-progress\s*\{[^}]*animation:\s*none/);

    const layout = readFileSync("src/app/AppLayout.tsx", "utf-8") as string;
    expect(layout).toContain('import "./nav-pending.css"');
  });
});
