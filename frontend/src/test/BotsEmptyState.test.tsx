import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { BotsPage } from "@/features/settings/bots/BotsPage";
import { ADMIN_PERMISSIONS } from "./botFixtures";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * НА ПУСТОМ ЭКРАНЕ ПРИЗЫВ ОДИН (разбор интерфейса 13.08).
 *
 * Ботов нет — и «Создать бота» стояло ДВАЖДЫ: в шапке раздела и в пустом состоянии
 * посреди экрана. Две одинаковые кнопки в одном кадре заставляют выбирать между ними,
 * хотя выбора нет: это одно и то же действие, один и тот же запрос.
 *
 * Право первой кнопки за центральной: она объясняет, ЧТО создастся («из шаблона
 * „Первичный приём"»), а кнопка в шапке — просто кнопка.
 *
 * ⚠ ВТОРАЯ ПОЛОВИНА ПРОВЕРКИ ВАЖНЕЕ ПЕРВОЙ. Как только боты появились, кнопка в
 * шапке обязана вернуться: без неё создать второго бота будет нечем — центральное
 * состояние к тому времени уже не рисуется.
 */

function setupFetch(items: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/bots")) return jsonResponse(200, { items });
      return jsonResponse(200, { items: [] });
    }),
  );
}

function render() {
  resetSessionStore({
    user: fakeUser,
    permissions: ADMIN_PERMISSIONS,
    accessToken: "t",
    bootstrapped: true,
  });
  return renderWithProviders(<BotsPage />, { route: "/settings/bots" });
}

describe("Боты: пустое состояние", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("ботов нет — кнопка «Создать бота» ровно одна", async () => {
    setupFetch([]);
    render();
    await screen.findByText("Ботов пока нет");
    expect(screen.getAllByRole("button", { name: /Создать бота/ })).toHaveLength(1);
  });

  it("боты есть — кнопка в шапке возвращается", async () => {
    setupFetch([
      { id: "b-1", name: "Первичный приём", enabled: false, mode: "hint", accounts: [] },
    ]);
    render();
    expect(await screen.findByRole("button", { name: /Создать бота/ })).toBeInTheDocument();
    expect(screen.queryByText("Ботов пока нет")).toBeNull();
  });
});
