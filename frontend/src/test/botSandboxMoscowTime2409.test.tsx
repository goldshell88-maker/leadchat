/**
 * Песочница: «Время: задать» — это время по Москве (проверка 24.09).
 *
 * Фронт переводил введённое в UTC через пояс БРАУЗЕРА: владелец на UTC+10
 * задавал 23:00, чтобы проверить ночную ветку, а сервер получал 13:00 UTC,
 * то есть 16:00 по Москве, и показывал дневной сценарий. Сервер читает время
 * без пояса как московское — оно и уходит, как его ввели.
 *
 * ДИВЕРСИЯ: вернуть `new Date(nowOverride).toISOString()` — тест краснеет
 * (строка приходит с `Z` и сдвинутыми часами).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import type { SandboxStartBody } from "@/shared/api/types";
import { ADMIN_PERMISSIONS, BOT_ID, avitoAccountsPage, botDetail, resetBotDraft } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

vi.setConfig({ testTimeout: 20_000 });

describe("Песочница — время симуляции", () => {
  let starts: SandboxStartBody[];

  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    starts = [];
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
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}`) return jsonResponse(200, botDetail());
        if (url.pathname === "/api/v1/bots/sandbox/start") {
          starts.push(JSON.parse(String(init?.body)) as SandboxStartBody);
          return jsonResponse(200, { session_id: `sess-${starts.length}`, events: [], state: null });
        }
        if (url.pathname.startsWith("/api/v1/bots/sandbox")) return jsonResponse(204, null);
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("заданное время уходит московским, без перевода в пояс браузера", async () => {
    const user = userEvent.setup();
    renderBotEditor();
    await screen.findByRole("textbox", { name: "Имя бота" });
    await user.click(screen.getByRole("button", { name: "Протестировать" }));
    await waitFor(() => expect(starts).toHaveLength(1));

    await user.click(await screen.findByRole("radio", { name: "задать" }));
    fireEvent.change(screen.getByLabelText("Время симуляции по Москве"), {
      target: { value: "2026-09-24T23:00" },
    });
    await user.click(screen.getAllByRole("button", { name: "Начать заново" })[0]);

    await waitFor(() => expect(starts).toHaveLength(2));
    expect(starts[1].now_override).toBe("2026-09-24T23:00");
  });
});
