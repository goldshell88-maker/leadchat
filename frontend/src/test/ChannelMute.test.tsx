import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * Канал, который не может отвечать: что предлагают человеку.
 *
 * Способ подключения решает, чем чинить. `client_id` и `client_secret` Авито
 * выдаёт ОДИН РАЗ и не меняет, поэтому канал на своих ключах чинится повтором
 * запроса токена — переподключать нечем, других ключей не существует. Канал
 * через согласие — наоборот: refresh-токен одноразовый, отозванный не оживёт,
 * и нужен человек с доступом к аккаунту Авито.
 *
 * До 11 августа обе карточки предлагали «Переподключить» через согласие, и для
 * канала на ключах это лечило максимум на сутки: ключи кнопка не трогает.
 */

const BASE = {
  id: "acc-1",
  title: "LP-Тимофей",
  avito_user_id: 100200301,
  status: "needs_reauth",
  token_expires_at: "2026-08-10T10:00:00Z",
  created_at: "2026-07-01T10:00:00Z",
  webhook: { status: "registered" },
  backfill: { status: "idle" },
  operators: { count: 0, items: [] },
};

describe("Канал не может отвечать", () => {
  const calls: Array<{ url: string; method: string }> = [];
  let account: Record<string, unknown> = BASE;

  beforeEach(() => {
    calls.length = 0;
    account = BASE;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? "GET" });
        if (url.includes("/refresh-token")) return jsonResponse(200, { ...account, status: "active" });
        if (url.includes("/reconnect")) return jsonResponse(200, { url: "https://avito.ru/oauth" });
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

  it("канал на своих ключах предлагает повтор, а не поход в Авито", async () => {
    account = { ...BASE, own_keys: true };
    const user = userEvent.setup();
    renderWithProviders(<AccountsPage />);
    /*
     * ⚠ ПОЧИНКА СЛОМАННОГО КАНАЛА ЖИВЁТ ПОД РАСКРЫТИЕМ (04.09). Раньше два
     * абзаца и кнопка во всю ширину вставали прямо в горизонтальный ряд, и один
     * сломанный канал перекашивал колонки всего списка — то, из-за чего
     * владелец назвал экран «кривым и косым».
     */
    await userEvent.click((await screen.findAllByRole("button", { name: /^Развернуть / }))[0]);

    await user.click(await screen.findByRole("button", { name: "Повторить сейчас" }));

    await waitFor(() =>
      expect(calls.some((c) => c.url.endsWith("/avito-accounts/acc-1/refresh-token"))).toBe(true),
    );
    expect(calls.some((c) => c.url.includes("/reconnect"))).toBe(false);
  });

  it("канал через согласие по-прежнему ведёт на переподключение", async () => {
    account = { ...BASE, own_keys: false };
    renderWithProviders(<AccountsPage />);
    /*
     * ⚠ ПОЧИНКА СЛОМАННОГО КАНАЛА ЖИВЁТ ПОД РАСКРЫТИЕМ (04.09). Раньше два
     * абзаца и кнопка во всю ширину вставали прямо в горизонтальный ряд, и один
     * сломанный канал перекашивал колонки всего списка — то, из-за чего
     * владелец назвал экран «кривым и косым».
     */
    await userEvent.click((await screen.findAllByRole("button", { name: /^Развернуть / }))[0]);

    expect(await screen.findByRole("button", { name: /Переподключить/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Повторить сейчас" })).toBeNull();
  });

  it("не утверждает, что приём остановлен: обращения продолжают приходить", async () => {
    // Прежний текст говорил «Приём сообщений остановлен» — неправда. Вебхуки
    // Авито нашего токена не требуют, обращения видны; не уходят только ответы.
    // Человек, поверивший старому тексту, пошёл бы искать пропавшие обращения.
    account = { ...BASE, own_keys: true };
    renderWithProviders(<AccountsPage />);
    /*
     * ⚠ ПОЧИНКА СЛОМАННОГО КАНАЛА ЖИВЁТ ПОД РАСКРЫТИЕМ (04.09). Раньше два
     * абзаца и кнопка во всю ширину вставали прямо в горизонтальный ряд, и один
     * сломанный канал перекашивал колонки всего списка — то, из-за чего
     * владелец назвал экран «кривым и косым».
     */
    await userEvent.click((await screen.findAllByRole("button", { name: /^Развернуть / }))[0]);

    expect(await screen.findByText(/Обращения приходят, но ответы не уходят/)).toBeTruthy();
    expect(screen.queryByText(/Приём сообщений остановлен/)).toBeNull();
  });
});
