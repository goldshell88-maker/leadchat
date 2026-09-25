/**
 * ЭКРАН СТАТИСТИКИ НЕ ГАСНЕТ НА КАЖДОЕ НАЖАТИЕ.
 *
 * ЧТО БЫЛО. Ключ каждого запроса содержит период, канал, менеджеров, метрику,
 * группировку и сортировку. Любая их смена — новый ключ, а новый ключ без
 * `placeholderData` означает `isPending`, то есть каждый блок честно уходил в
 * свою ветку скелетона.
 *
 * Клик по заголовку колонки схлопывал таблицу с двенадцатью строками в серый
 * прямоугольник высотой 200px: страница укорачивалась, прокрутка прыгала
 * вверх, а кнопка сортировки исчезала из-под курсора вместе с фокусом — то
 * есть у клавиатурного пользователя после Enter фокус улетал на body. Смена
 * периода гасила разом карточки, график и таблицу: экран на секунду
 * становился пустым. Владелец описал это как «зависания страницы».
 *
 * ЧТО СТАЛО. Прежний срез остаётся на экране, пока считается новый.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { StatsPage } from "@/features/stats/StatsPage";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const МЕНЕДЖЕРЫ = {
  period: { date_from: "2026-08-16", date_to: "2026-08-22" },
  refreshed_at: "2026-08-22T11:05:12Z",
  rows: [
    {
      manager_id: "m-1",
      full_name: "Анна Смирнова",
      is_active: true,
      taken: 34,
      answered: 31,
      closed: 28,
      frt_avg_sec: 210,
      frt_median_sec: 74,
      frt_median_biz_sec: 71,
      messages_sent: 412,
    },
  ],
};

const ПУСТО = { refreshed_at: null, rows: [], cells: [], points: [], items: [] };

/** Ответ таблицы держим за руку: между нажатием и ответом и живёт дефект. */
let отпуститьТаблицу: (() => void) | null = null;

describe("Статистика: прежние данные держатся, пока едут новые", () => {
  beforeEach(() => {
    queryClient.clear();
    отпуститьТаблицу = null;
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "stats:all"],
      accessToken: "t",
      bootstrapped: true,
    });
    let первыйОтвет = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (!url.pathname.endsWith("/stats/managers")) return jsonResponse(200, ПУСТО);
        if (первыйОтвет) {
          первыйОтвет = false;
          return jsonResponse(200, МЕНЕДЖЕРЫ);
        }
        // Второй запрос (после клика по заголовку) держим, пока тест не отпустит.
        await new Promise<void>((resolve) => {
          отпуститьТаблицу = resolve;
        });
        return jsonResponse(200, МЕНЕДЖЕРЫ);
      }),
    );
  });

  afterEach(() => {
    отпуститьТаблицу?.();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("смена сортировки не стирает таблицу, пока считается новая", async () => {
    const user = userEvent.setup();
    renderWithProviders(<StatsPage />, { route: "/stats" });

    /*
     * Ищем по РОЛИ, а не по тексту: имя менеджера попадает ещё и в опции
     * фильтра «Менеджеры», а кнопка строки есть только в самой таблице.
     */
    const строкаТаблицы = () =>
      screen.queryByRole("button", { name: "Показать статистику: Анна Смирнова" });
    await waitFor(() => expect(строкаТаблицы()).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Закрыто/ }));

    // Ответ ЕЩЁ НЕ ПРИШЁЛ — и именно сейчас таблица обязана стоять на месте.
    await waitFor(() => expect(отпуститьТаблицу).not.toBeNull());
    expect(строкаТаблицы()).toBeInTheDocument();

    отпуститьТаблицу?.();
    await waitFor(() => expect(строкаТаблицы()).toBeInTheDocument());
  });
});
