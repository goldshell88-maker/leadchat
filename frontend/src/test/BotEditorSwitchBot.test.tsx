import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { BotEditor } from "@/features/settings/bots/BotEditor";
import { useBotDraft } from "@/features/settings/bots/draftStore";
import { qk } from "@/shared/api/queryKeys";
import { ADMIN_PERMISSIONS, BOT_ID, avitoAccountsPage, botDetail, resetBotDraft } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import "./renderBotEditor";

/**
 * Возврат к ранее открытому боту (BOT-06).
 *
 * Стор черновика глобальный и при уходе со страницы не сбрасывается. Деталь
 * второго бота приходит из кэша react-query мгновенно (`isPending === false`),
 * а `draft.loaded` всё ещё true — с именем и сценарием ПЕРВОГО. Эффект
 * загрузки отрабатывает только после рендера, поэтому один кадр показывал
 * чужие данные: в лучшем случае мелькание, в худшем — правка не того бота.
 */

const SECOND_ID = "bot-2";

function renderTwoBots() {
  const router = createMemoryRouter([{ path: "/settings/bots/:id", element: <BotEditor /> }], {
    initialEntries: [`/settings/bots/${BOT_ID}`],
  });
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MantineProvider theme={theme} defaultColorScheme="light">
          <RouterProvider router={router} />
        </MantineProvider>
      </QueryClientProvider>,
    ),
    router,
  };
}

describe("Редактор бота — переключение между ботами", () => {
  /** Список аккаунтов держим «в пути», пока тест не отпустит его сам. */
  let releaseAccounts: () => void;

  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    const accounts = new Promise<Response>((resolve) => {
      releaseAccounts = () => resolve(jsonResponse(200, avitoAccountsPage()));
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname === "/api/v1/avito-accounts") return accounts;
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "GET") {
          return jsonResponse(200, botDetail());
        }
        if (url.pathname === `/api/v1/bots/${SECOND_ID}` && method === "GET") {
          return jsonResponse(200, botDetail({ id: SECOND_ID, name: "Ночной дежурный" }));
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("не показывает данные предыдущего бота, пока черновик не перезагружен", async () => {
    /*
     * Воспроизводим ровно тот момент, который ловил человек:
     *
     * 1. черновик остался от прошлого бота (стор глобальный, при уходе со
     *    страницы не сбрасывается);
     * 2. деталь нужного бота уже в кэше, поэтому `detail.isPending` — false и
     *    скелет по старому условию не показывался;
     * 3. список аккаунтов ещё едет — а эффект загрузки черновика ждёт именно
     *    его и до тех пор не трогает стор.
     *
     * Пункт 3 — не искусственный: аккаунтов девять, и на медленной сети это
     * не «один кадр», а секунды чужого сценария на экране.
     */
    useBotDraft.getState().load(botDetail({ id: SECOND_ID, name: "Ночной дежурный" }), []);
    queryClient.setQueryData(qk.bots.detail(BOT_ID), botDetail());

    renderTwoBots();

    expect(screen.queryByDisplayValue("Ночной дежурный")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Имя бота" })).toBeNull();

    releaseAccounts();
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Имя бота" })).toHaveValue("Первичный приём"),
    );
  });
});
