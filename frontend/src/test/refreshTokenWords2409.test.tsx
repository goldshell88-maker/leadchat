/**
 * «Обновить токен» — словами сервера (проверка 24.09).
 *
 * Экран подменял ответ своими фразами по коду причины: у канала на своих
 * ключах сбой Авито читался «Авито отозвал доступ — нужно переподключение»,
 * а 502 — пустым «Попробуйте ещё раз». Сервер теперь различает исходы сам.
 *
 * ДИВЕРСИЯ: вернуть подмену по `details.reason` — краснеет.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { showToast } from "@/shared/ui/toast";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

const HOUR_MS = 3_600_000;
const SERVER_WORDS = "Авито не ответил — повторите через минуту. Токены и ключи канала целы";

function account() {
  const now = Date.now();
  return {
    id: "acc-1",
    title: "Ключи",
    avito_user_id: 424242,
    status: "active",
    own_keys: true,
    is_service: false,
    token_expires_at: new Date(now - HOUR_MS).toISOString(),
    created_at: new Date(now - 48 * HOUR_MS).toISOString(),
    token: {
      state: "critical",
      message: "Токен истёк — ответы клиентам не уходят.",
      last_refresh_at: null,
      action: "refresh_token",
    },
    webhook: {
      status: "ok",
      url: null,
      last_event_at: null,
      state: "quiet",
      message: "Подписка стоит, событий ещё не было.",
      action: null,
    },
    backfill: { status: "idle" },
    operators: { count: 0, preview: [] },
    stats: null,
  };
}

describe("«Обновить токен» при недоступном Авито", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.mocked(showToast).mockClear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["accounts:read", "accounts:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/refresh-token") && init?.method === "POST") {
          return jsonResponse(
            502,
            errorEnvelope("avito_unavailable", SERVER_WORDS, { reason: "avito_unavailable" }),
          );
        }
        if (url.includes("/avito-accounts")) {
          return jsonResponse(200, { items: [account()], page: { limit: 100, offset: 0, total: 1 } });
        }
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("тост повторяет причину сервера, а не выдумывает отзыв доступа", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AccountsPage />);
    await user.click(await screen.findByRole("button", { name: "Обновить токен" }));

    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const toast = vi.mocked(showToast).mock.calls.at(-1)![0] as { message?: string };
    expect(toast.message).toBe(SERVER_WORDS);
  });
});
