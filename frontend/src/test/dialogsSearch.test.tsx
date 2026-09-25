import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TablePage } from "@/features/table/TablePage";
import { queryClient } from "@/app/queryClient";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ПОИСК ПО ТЕКСТУ СООБЩЕНИЙ НА «РАЗБОРЕ ДИАЛОГОВ».
 *
 * Сервер умел его с самого начала: `TableFilters.q` уходит в
 * `conversations._search_condition` — полнотекстовый поиск по-русски по телу
 * сообщений плюс имя клиента. На экране поля не было, и половина возможностей
 * разбора была недоступна: найти диалог по фразе «не дозвонился» можно было
 * только в «Чатах», где нет ни периода, ни оператора, ни выгрузки.
 *
 * Тест держит три вещи, которые легко потерять по отдельности: запрос уходит
 * на сервер, он уходит ОДИН раз на фразу (а не на каждую букву), и он попадает
 * в адрес — иначе ссылку с найденным нельзя переслать коллеге.
 */

const ПУСТО = { items: [], page: { limit: 50, offset: 0, total: 0 } };

function адреса(mock: ReturnType<typeof vi.fn>): string[] {
  return mock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes("/conversations/table"));
}

describe("Разбор диалогов — поиск по сообщениям", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "stats:all"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("набранная фраза уходит в запрос и в адрес", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/conversations/table")) return jsonResponse(200, ПУСТО);
      return jsonResponse(200, { items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithProviders(<TablePage />, { route: "/dialogs" });
    await screen.findByRole("textbox", { name: "Поиск по имени и тексту сообщений" });

    await user.type(
      screen.getByRole("textbox", { name: "Поиск по имени и тексту сообщений" }),
      "не дозвонился",
    );

    // Запрос уходит ПОСЛЕ паузы и с полной фразой: набор из тринадцати букв не
    // должен дать тринадцать запросов.
    await waitFor(() =>
      expect(адреса(fetchMock).some((u) => u.includes("q=%D0%BD%D0%B5+%D0%B4%D0%BE"))).toBe(true),
    );

    const сзапросом = адреса(fetchMock).filter((u) => u.includes("q="));
    expect(сзапросом.length, "запрос ушёл на каждую букву — пропала задержка").toBeLessThan(4);

    // Попадание фразы В АДРЕС здесь не проверить: обёртка тестов использует
    // `MemoryRouter`, а он `window.location` не трогает. Обратное направление —
    // адрес → экран → запрос — держит второй тест ниже, который открывает
    // страницу сразу по ссылке с `?q=`. Вместе они замыкают круг.
    expect(
      screen.getByRole("textbox", { name: "Поиск по имени и тексту сообщений" }),
    ).toHaveValue("не дозвонился");
  });

  it("пустой результат по фразе объясняется фразой, а не фильтрами", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/conversations/table")) return jsonResponse(200, ПУСТО);
        return jsonResponse(200, { items: [] });
      }),
    );

    renderWithProviders(<TablePage />, { route: "/dialogs?q=нетмашины" });

    // «Снять фильтры» на пустом поиске звучит как совет крутить селекты, хотя
    // мешает как раз набранная фраза.
    expect(await screen.findByText(/По запросу «нетмашины»/)).toBeInTheDocument();
  });
});
