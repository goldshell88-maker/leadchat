import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { AppLayout } from "@/app/AppLayout";
import { queryClient } from "@/app/queryClient";
import { RequireAuth, router as appRouter } from "@/app/router";
import { theme } from "@/app/theme";
import { SettingsLayout } from "@/features/settings/SettingsLayout";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * НЕИЗВЕСТНЫЙ АДРЕС НЕ УНОСИТ РАБОЧЕЕ МЕСТО (аудит SHELL-09).
 *
 * ЧТО БЫЛО. Маршрут `*` стоял на верхнем уровне карты — вне `RequireAuth` и
 * вне `AppLayout`. Опечатка в адресе, устаревшая ссылка от коллеги,
 * `/settings/что-угодно-неизвестное` — и залогиненный человек оставался на
 * голой странице с одной кнопкой: ни шапки, ни рельсы разделов, ни
 * колокольчика. Выглядит это не как «такой страницы нет», а как «приложение
 * сломалось», и на живой переписке разница существенная.
 *
 * Проверяется и место маршрута в карте, и то, что видно человеку.
 */

type RouteNode = {
  path?: string;
  element?: React.ReactElement;
  children?: RouteNode[];
};

function withProviders(ui: React.ReactElement) {
  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        {ui}
      </MantineProvider>
    </QueryClientProvider>
  );
}

/** Ветка авторизованной части: RequireAuth → AppLayout → разделы. */
function shellChildren(): RouteNode[] {
  const top = appRouter.routes as unknown as RouteNode[];
  const guarded = top.find((r) => r.element?.type === RequireAuth);
  const layout = (guarded?.children ?? []).find((r) => r.element?.type === AppLayout);
  return layout?.children ?? [];
}

/** Дети `SettingsLayout` — второй оболочки, со своей колонкой разделов. */
function settingsChildren(): RouteNode[] {
  const node = shellChildren().find((r) => r.element?.type === SettingsLayout);
  return node?.children ?? [];
}

describe("Неизвестный адрес (SHELL-09)", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as Permission[],
      accessToken: "t",
      bootstrapped: true,
    });
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

  it("маршрут «*» живёт внутри оболочки, а не на верхнем уровне карты", () => {
    const top = appRouter.routes as unknown as RouteNode[];
    expect(top.filter((r) => r.path === "*")).toHaveLength(0);
    expect(shellChildren().filter((r) => r.path === "*")).toHaveLength(1);
  });

  it("на неизвестном адресе рабочее место остаётся на экране", () => {
    // Элемент берётся ИЗ КАРТЫ, а не импортом компонента: проверять надо то,
    // что реально стоит на маршруте, иначе тест переживёт любую подмену.
    const notFound = shellChildren().find((r) => r.path === "*");
    const router = createMemoryRouter(
      [{ element: <AppLayout />, children: [{ path: "/чепуха", element: notFound?.element }] }],
      { initialEntries: ["/чепуха"] },
    );

    render(withProviders(<RouterProvider router={router} />));

    expect(screen.getByText("Страница не найдена")).toBeInTheDocument();
    // Оболочка на месте: название в шапке и рельса разделов. Уйти можно одним
    // нажатием, а не только кнопкой «К диалогам» посреди пустоты.
    expect(screen.getAllByText("LeadChat").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /Чаты/ })).toBeInTheDocument();
  });

  /*
   * ВНУТРИ НАСТРОЕК ОБОЛОЧЕК ДВЕ, И ВТОРУЮ ТОЖЕ ТЕРЯЛИ (разбор 12 августа).
   *
   * `/settings/опечатка` ловил верхний `*` — тот, что ребёнок `AppLayout`.
   * Шапка и рельса оставались, а колонка разделов «Профиль · Каналы · Быстрые
   * ответы · Команда» исчезала: единственный способ перейти в соседний раздел
   * настроек. Человек попадает сюда как раз оттуда — по чужой ссылке на
   * переименованный раздел, — и «К диалогам» посреди пустоты выбрасывает его
   * из того места, куда он шёл.
   */
  it("неизвестный адрес внутри настроек ловится маршрутом самих настроек", () => {
    expect(settingsChildren().filter((r) => r.path === "/settings/*")).toHaveLength(1);
  });

  it("на неизвестном адресе в настройках колонка разделов остаётся на экране", () => {
    const notFound = settingsChildren().find((r) => r.path === "/settings/*");
    const router = createMemoryRouter(
      [
        {
          element: <SettingsLayout />,
          children: [{ path: "/settings/чепуха", element: notFound?.element }],
        },
      ],
      { initialEntries: ["/settings/чепуха"] },
    );

    render(withProviders(<RouterProvider router={router} />));

    expect(screen.getByText("Страница не найдена")).toBeInTheDocument();
    // Меню настроек на месте — уйти в соседний раздел можно одним нажатием.
    expect(screen.getByRole("navigation", { name: "Разделы настроек" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Профиль" })).toHaveAttribute(
      "href",
      "/settings/profile",
    );
  });
});
