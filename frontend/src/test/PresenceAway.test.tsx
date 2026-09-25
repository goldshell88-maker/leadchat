import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { AppLayout } from "@/app/AppLayout";
import { ConnectionIndicator } from "@/app/ConnectionIndicator";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { PresenceBlock } from "@/features/presence/PresenceBlock";
import { usePresenceStore } from "@/features/presence/usePresence";
import type { Permission } from "@/shared/auth/usePermissions";
import { useRailStore } from "@/shared/stores/railStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Своё состояние «на месте» / «отошёл» (#34), переехавшее в настройки профиля
 * (требование владельца от 11 августа, №9).
 *
 * Диспетчер отходит от стола — обед, перекур, разговор по телефону. Сказать об
 * этом системе было нечем: присутствие двоичное, и единственный способ выпасть
 * из автораздачи — закрыть приложение, то есть перестать видеть собственные
 * диалоги и через три минуты потерять розданные сторожу возврата.
 *
 * Проверяется и обратная сторона: неудача запроса не должна оставить человека
 * в уверенности, что новых обращений ему не дают, — они пойдут.
 */
describe("Своё состояние в настройках профиля", () => {
  beforeEach(() => {
    queryClient.clear();
    usePresenceStore.setState({ status: "online" });
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  it("«Отошёл» уходит на сервер и запоминается", async () => {
    const calls: Array<{ method?: string; url: string; body?: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ method: init?.method, url: String(url), body: String(init?.body ?? "") });
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: "away" }),
        } as Response;
      }),
    );

    renderWithProviders(<PresenceBlock />);
    await userEvent.click(screen.getByText("Отошёл"));

    await waitFor(() => {
      const put = calls.find((c) => c.method === "PUT");
      expect(put?.url).toContain("/presence");
      // Тело проверяется вместе с путём: ручка различает состояния только по
      // нему, и «PUT ушёл» без «ушло именно away» — зелёный тест на молчащей
      // кнопке.
      expect(put?.body).toContain("away");
    });
    expect(usePresenceStore.getState().status).toBe("away");
  });

  it("не сохранилось — состояние откатывается, а не врёт", async () => {
    /*
     * Оставить выбор на «отошёл» после неудачи нельзя ни в коем случае:
     * человек будет уверен, что новых обращений ему не дают, — а они пойдут,
     * и клиент будет ждать у пустого стола.
     */
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response),
    );

    renderWithProviders(<PresenceBlock />);
    await userEvent.click(screen.getByText("Отошёл"));

    await waitFor(() => expect(usePresenceStore.getState().status).toBe("online"));
  });

  function правилоОсвобождения(минут: number | null) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ release_after_minutes: минут }),
      }) as Response),
    );
  }

  it("исключённому подпись обещает, что начатые диалоги остаются за ним", async () => {
    правилоОсвобождения(null);
    renderWithProviders(<PresenceBlock />);
    expect(await screen.findByText(/начатые диалоги остаются за вами/i)).toBeTruthy();
  });

  it("при включённом освобождении подпись говорит, через сколько диалоги уйдут", async () => {
    // Раньше подпись обещала «их никто не отберёт», а сторож забирал их через
    // 15 минут в «Отошёл».
    правилоОсвобождения(15);
    renderWithProviders(<PresenceBlock />);
    expect(await screen.findByText(/через 15 минут в «Отошёл»/i)).toBeTruthy();
    expect(screen.queryByText(/начатые диалоги остаются за вами/i)).toBeNull();
  });
});


/**
 * СОСТОЯНИЕ ВИДНО В ПАНЕЛИ, А НЕ ТОЛЬКО В РАСКРЫТОМ ОКНЕ.
 *
 * ЧТО БЫЛО. Признак жил внутри выпадающего списка под аватаром, а список
 * закрыт по умолчанию — то есть состояния не было видно нигде. Диспетчер
 * ставил «отошёл» в профиле, а шапка весь день показывала зелёную точку и
 * слово «В сети»: тем же словом, каким в любой переписке называют состояние
 * ЧЕЛОВЕКА, хотя это состояние КАНАЛА ДО СЕРВЕРА. Забытое «отошёл» тихо
 * выключало человека из автораздачи на всю смену, и замечали это по вопросу
 * «а почему мне сегодня ничего не приходит».
 *
 * ЧТО СТАЛО (макет от 12 августа). Состояние названо словом под именем в левой
 * панели, помечено точкой на аватаре и янтарной полосой вдоль всей панели —
 * три независимых признака, ни один не требует ничего открывать. Переключатель
 * вернулся туда же, в окно под именем; блок в профиле остался, и оба управляют
 * одним и тем же состоянием на сервере.
 *
 * Именно это здесь и сторожится: не «переключателя нет» (требование владельца
 * №9 от 11 августа), а то, ради чего его тогда убирали, — состояние обязано
 * читаться БЕЗ раскрытия чего бы то ни было.
 */
describe("Состояние видно в панели (окно не раскрыто)", () => {
  beforeEach(() => {
    queryClient.clear();
    usePresenceStore.setState({ status: "online" });
    useRailStore.setState({ expanded: true });
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

  function renderShell() {
    const router = createMemoryRouter(
      [{ element: <AppLayout />, children: [{ path: "/chats", element: <div /> }] }],
      { initialEntries: ["/chats"] },
    );
    return render(
      <QueryClientProvider client={queryClient}>
        <MantineProvider theme={theme} defaultColorScheme="light">
          <RouterProvider router={router} />
        </MantineProvider>
      </QueryClientProvider>,
    );
  }

  it("«отошёл» читается словом и признаком на корне панели", () => {
    usePresenceStore.setState({ status: "away" });
    const { container } = renderShell();

    // Окно действительно закрыто: подписи из него в документе нет.
    expect(screen.queryByText(/начатые диалоги остаются за вами/i)).toBeNull();
    // …а состояние всё равно названо словами, а не одним цветом.
    expect(screen.getByText("Отошёл")).toBeTruthy();
    // И то же самое — для тех, кто экрана не видит.
    expect(screen.getByRole("button", { name: /вы отошли/i })).toBeTruthy();
    // Признак на корне — от него красятся полоса, точка и подпись.
    expect(container.querySelector(".lc-rail[data-away]")).toBeTruthy();
  });

  it("«на месте» тревожным признаком не помечается", () => {
    const { container } = renderShell();

    expect(screen.getByText("На месте")).toBeTruthy();
    expect(screen.getByRole("button", { name: /вы на месте/i })).toBeTruthy();
    // Янтарная полоса — только про «отошёл»: иначе она перестаёт что-либо
    // значить, а вместе с ней и точка того же цвета.
    expect(container.querySelector(".lc-rail[data-away]")).toBeNull();
  });

  it("состояние связи не называется словами про человека", async () => {
    // Показ связи переехал внутрь окна, поэтому проверяется он отдельно —
    // но проверяется: «В сети» рядом с «Отошёл» и было тем самым враньём.
    renderWithProviders(<ConnectionIndicator />);

    await waitFor(() => expect(screen.getByText("На связи")).toBeTruthy());
    expect(screen.queryByText("В сети")).toBeNull();
  });
});
