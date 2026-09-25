import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";
import { showToast } from "@/shared/ui/toast";

// Тосты рисует Mantine в портал, которого в тестовом дереве нет. Проверяем
// не пиксели, а СКАЗАННОЕ: какой текст получил бы человек.
vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * «Обновить токен» против «Переподключить» (блок 8.3).
 *
 * Две кнопки на одном экране, и спутать их легко, а цена разная:
 * переподключение уводит в Авито — вход под учёткой канала и подтверждение
 * доступа, минуты и чужой пароль под рукой. Обновление токена не требует ни
 * того, ни другого. Проверяется, что кнопка делает именно своё и что при
 * отзыве доступа человеку говорят правду, а не «попробуйте ещё раз».
 *
 * КНОПКА ПЕРЕЕХАЛА В МЕНЮ «…», И ЭТО ЧАСТЬ ПРОВЕРЯЕМОГО. Она стояла прямо под
 * строкой токена — а строка та горела жёлтым круглосуточно (суточный токен,
 * порог в 48 часов), и люди жали кнопку каждый день, приняв норму за аварию.
 * Наверх, к объясняющей строке, кнопка поднимается теперь только когда сервер
 * назвал действие; в штатном состоянии она доступна, но не просит нажать себя.
 * Разбор состояний — ChannelCardAlerts.test.tsx.
 */

const ADMIN: Permission[] = ["accounts:read", "accounts:manage"];

const ACCOUNT = {
  id: "acc-1",
  title: "LP-Москва",
  avito_user_id: 111222333,
  status: "active",
  token_expires_at: "2026-08-08T10:00:00Z",
  created_at: "2026-07-01T10:00:00Z",
  token: {
    state: "ok",
    message: "Токен активен. Обновится автоматически 13.08 в 16:07",
    last_refresh_at: null,
    action: null,
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
  operators: { count: 0, items: [] },
};

/** Тот же канал, но сервер просит вмешаться: последняя попытка не прошла. */
const ACCOUNT_WARNING = {
  ...ACCOUNT,
  token: {
    state: "warning",
    message:
      "Последнее автообновление не сработало: Авито ответил 502. Токен действует до 13.08 в 16:07; " +
      "если следующая попытка тоже не пройдёт, ответы перестанут уходить.",
    last_refresh_at: null,
    action: "refresh_token",
  },
};

describe("Обновление токена канала", () => {
  const calls: Array<{ url: string; method: string }> = [];
  let refreshResponse: Response;
  let account: Record<string, unknown> = ACCOUNT;

  beforeEach(() => {
    calls.length = 0;
    account = ACCOUNT;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ADMIN,
      accessToken: "t",
      bootstrapped: true,
    });
    refreshResponse = jsonResponse(200, ACCOUNT);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? "GET" });
        if (url.includes("/refresh-token")) return refreshResponse;
        if (url.includes("/avito-accounts")) {
          return jsonResponse(200, { items: [account], page: { limit: 50, offset: 0, total: 1 } });
        }
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** Открыть меню обслуживания карточки: в норме кнопка живёт там. */
  async function openMaintenance(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: /Обслуживание канала/ }));
  }

  it("обновляет токен из меню «…», НЕ уводя в Авито", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AccountsPage />);

    await openMaintenance(user);
    await user.click(await screen.findByRole("menuitem", { name: "Обновить токен" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/avito-accounts/acc-1/refresh-token"))).toBe(true);
    });
    // Главное отличие от «Переподключить»: похода в OAuth не было.
    expect(calls.some((c) => c.url.includes("/reconnect"))).toBe(false);
  });

  it("когда сервер просит вмешаться — кнопка стоит у самой строки", async () => {
    // Не «вместо меню», а «в дополнение»: там, где объяснено, почему нажимать.
    account = ACCOUNT_WARNING;
    const user = userEvent.setup();
    renderWithProviders(<AccountsPage />);

    await user.click(await screen.findByRole("button", { name: "Обновить токен" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/avito-accounts/acc-1/refresh-token"))).toBe(true);
    });
  });

  it("при отозванном доступе говорит правду, а не «попробуйте ещё раз»", async () => {
    // Молчаливое «ок» здесь было бы худшим исходом: администратор ушёл бы
    // уверенный, что починил, а канал продолжил бы молчать.
    // Причину называет сервер (проверка 24.09) — здесь его настоящие слова.
    refreshResponse = jsonResponse(422, {
      error: {
        code: "unprocessable",
        message: "Авито отозвал доступ — требуется переподключение",
        details: { reason: "needs_reauth" },
      },
    });
    const user = userEvent.setup();
    renderWithProviders(<AccountsPage />);

    await openMaintenance(user);
    await user.click(await screen.findByRole("menuitem", { name: "Обновить токен" }));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({ message: expect.stringContaining("отозвал доступ") }),
      ),
    );
  });
});
