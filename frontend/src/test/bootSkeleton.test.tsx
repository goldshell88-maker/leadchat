import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { RequireAuth, router as appRouter } from "@/app/router";
import { theme } from "@/app/theme";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * СТАРТ ВКЛАДКИ БЕЗ РЫВКА.
 *
 * ЧТО БЫЛО. Пока идёт тихий вход по cookie (`RequireAuth`, `bootstrapped`),
 * на весь экран рисовался `FullscreenLoader`: логотип и крутилка по центру
 * пустого поля. Потом — разом — появлялось всё рабочее место: шапка, рельса,
 * колонки. Глаз читает это как рывок, потому что рывок и есть: между двумя
 * кадрами меняется вся геометрия экрана.
 *
 * ПОЧЕМУ ЭТО СТАЛО ВАЖНО ИМЕННО СЕЙЧАС. Полоса «Вышло обновление» (SHELL-02)
 * заканчивается перезагрузкой вкладки. То есть теперь мы САМИ просим человека
 * перезагрузиться посреди смены — и обязаны сделать так, чтобы плата за это
 * была не «вспышка пустого экрана», а «рамка на месте, содержимое приезжает».
 *
 * Проверяется здесь то, что видно человеку, а не факт существования
 * компонента: каркас на экране в момент входа, у каркаса те же части, что у
 * рабочего места, крутилки нет, и каркас уходит, когда вход закончился.
 */

function withProviders(ui: React.ReactElement) {
  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        {ui}
      </MantineProvider>
    </QueryClientProvider>
  );
}

/** Guard на memory-роутере: за ним — заглушка вместо настоящего экрана чатов. */
function renderGuard() {
  const router = createMemoryRouter(
    [{ element: <RequireAuth />, children: [{ path: "/chats", element: <div>рабочее место</div> }] }],
    { initialEntries: ["/chats"] },
  );
  return render(withProviders(<RouterProvider router={router} />));
}

const skeleton = () => screen.queryByRole("status", { name: "Загружаем рабочее место" });

describe("Каркас на время входа", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { status: "ok", db: true, redis: true, version: "test" })),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("до конца входа на экране каркас оболочки, а не пустота", () => {
    resetSessionStore({ bootstrapped: false });
    const { container } = renderGuard();

    expect(skeleton()).toBeInTheDocument();
    // Части — те же, что у рабочего места: рельса слева, три колонки. Если
    // исчезнет любая, подмена сдвинет всё остальное — ровно тот рывок, ради
    // которого каркас и заводился.
    expect(container.querySelector(".lc-boot__rail")).not.toBeNull();
    /*
     * ⚠ 5 сентября панелей стало ТРИ, и это не «поправили число под код».
     *
     * Карточка клиента стоит в потоке рабочего места на всём диапазоне от 1360
     * и выше — всегда, даже когда диалог не выбран (ChatsPage.tsx). Скелет
     * рисовал две панели, то есть обещал раскладку, которой не бывает ни на
     * одном мониторе команды: замер на стенде при 1440 показал, что в кадре
     * подмены работа прыгала вправо на 146 пикселей.
     *
     * Число точное, а не «не меньше»: колонок в рабочем месте столько же, и
     * следующий, кто их добавит или уберёт, обязан прийти сюда. Прячет лишнюю
     * колонку на узких экранах CSS, а не разметка (app-layout.css), поэтому в
     * jsdom, где стилей нет, их всегда три.
     */
    expect(container.querySelectorAll(".lc-boot__pane")).toHaveLength(3);
    /*
     * ⚠ ШАПКИ ЗДЕСЬ БЫТЬ НЕ ДОЛЖНО (разбор дизайна 28.08). Скелет рисовал
     * полосу с логотипом, а у настоящего рабочего места такой полосы нет:
     * `--lc-header-h` равен нулю. Правило брало высоту оттуда же, получало
     * 0px — и логотип ложился ПОВЕРХ тела скелета. Каркас обязан повторять
     * настоящую раскладку, а не отменённую: иначе он обещает экран, которого
     * через мгновение не окажется.
     */
    expect(
      container.querySelector(".lc-boot__header"),
      "шапка вернулась в скелет — логотип снова ляжет поверх тела",
    ).toBeNull();
  });

  it("крутилки на полэкрана больше нет", () => {
    // Именно вращающийся кружок делает ожидание заметным: он приковывает
    // взгляд к пустому экрану. Каркас показывает будущую раскладку и молчит.
    resetSessionStore({ bootstrapped: false });
    const { container } = renderGuard();

    expect(container.querySelector(".mantine-Loader-root")).toBeNull();
  });

  it("каркас не залипает: вход закончился — на его месте работа", async () => {
    // Обратная половина. Каркас, который остался на экране, — это зависшее
    // приложение, и выглядит оно хуже любой крутилки.
    resetSessionStore({ bootstrapped: false });
    renderGuard();
    expect(skeleton()).toBeInTheDocument();

    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });

    await waitFor(() => expect(screen.getByText("рабочее место")).toBeInTheDocument());
    expect(skeleton()).toBeNull();
  });

  it("вся авторизованная часть приложения закрыта этим guard'ом", () => {
    // Тест выше проверяет guard на своём маленьком роутере. Здесь — что в
    // настоящей карте маршрутов рабочее место стоит именно за ним, а не за
    // какой-то другой веткой, до которой правка не дошла.
    type RouteNode = { element?: React.ReactElement; children?: RouteNode[] };
    const top = appRouter.routes as unknown as RouteNode[];
    const guarded = top.filter((r) => r.element?.type === RequireAuth);

    expect(guarded).toHaveLength(1);
    expect(guarded[0].children?.length).toBeGreaterThan(0);
  });
});
