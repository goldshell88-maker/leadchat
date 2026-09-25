import type { Permission } from "@/shared/auth/usePermissions";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { errorEnvelope, fakeMe, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * «ПАРТНЁР» И «ИСТОЧНИК» — ПОЛЯ, ПО КОТОРЫМ УЕЗЖАЕТ ЗАЯВКА.
 *
 * Обе беды тихие, и цена у них одинаковая: заявки продолжают уходить со старым
 * кодом, а на экране стоит новый.
 *
 * 1. ОТКАЗ СЕРВЕРА НЕ ОТКАТЫВАЛ ПОЛЕ. Значение живёт в состоянии компонента, на
 *    сервер уходит по уходу курсора. Сохранение падало — всплывал тост, а в
 *    поле оставалось набранное. Тост уезжает через несколько секунд, поле
 *    остаётся, и дальше оно неотличимо от сохранённого.
 *
 * 2. РУКОВОДИТЕЛЮ ПОЛЯ РИСОВАЛИСЬ, ХОТЯ ПРАВИТЬ ИХ ОН НЕ МОЖЕТ. Ручка
 *    `PATCH /avito-accounts/{id}` требует `accounts:manage` — право админа;
 *    у руководителя `accounts:read`. Экран предлагал работу, которую заведомо
 *    не примут: всё набранное улетало в 403.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел отдельно):
 *  - убрали откат из `onError` — падает «после отказа в поле стоит серверное»;
 *  - сняли условие `can("accounts:manage")` вокруг полей — падает «руководитель
 *    видит значения, но не поля ввода».
 */

const DAY_MS = 86_400_000;

function account(over: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    id: "acc-1",
    title: "Канал",
    avito_user_id: 111222333,
    status: "active",
    own_keys: false,
    token_expires_at: new Date(now + 30 * DAY_MS).toISOString(),
    created_at: new Date(now - 8 * DAY_MS).toISOString(),
    token: { state: "ok", message: "Токен активен", last_refresh_at: null, action: null },
    webhook: {
      status: "ok",
      url: "https://leadchat.example/hook",
      last_event_at: new Date(now - 60_000).toISOString(),
      state: "ok",
      message: "События приходят",
      action: null,
    },
    backfill: { status: "idle" },
    operators: { count: 0, preview: [] },
    stats: null,
    lead_partner_number: "7",
    lead_origin: "В43",
    ...over,
  };
}

function mount(over: Record<string, unknown> = {}, patchStatus = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/avito-accounts") && init?.method === "PATCH") {
        return patchStatus === 200
          ? jsonResponse(200, account(over))
          : jsonResponse(patchStatus, errorEnvelope("forbidden", "Нет права"));
      }
      if (url.includes("/avito-accounts")) {
        return jsonResponse(200, {
          items: [account(over)],
          page: { limit: 50, offset: 0, total: 1 },
        });
      }
      return jsonResponse(200, {});
    }),
  );
  return renderWithProviders(<AccountsPage />);
}

describe("Партнёр и источник канала", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeMe, role: "admin" },
      permissions: [...fakeMe.permissions, "accounts:read", "accounts:manage"] as Permission[],
      bootstrapped: true,
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * ⚠ ПОЛЯ ПЕРЕЕХАЛИ ПОД РАСКРЫТИЕ (04.09). В свёрнутой строке они стояли двумя
   * безымянными окошками шириной в восемь знаков — ровно то, из-за чего
   * владелец назвал экран «кривым и косым» и показал строку Jivo как эталон.
   * Работа «видно, у какого канала не проставлен источник» осталась: она
   * теперь пометкой под именем канала, и её стережёт соседний тест.
   */
  async function раскрыть() {
    await userEvent.click((await screen.findAllByRole("button", { name: /^Развернуть / }))[0]);
  }

  it("после отказа сервера в поле стоит серверное значение, а не набранное", async () => {
    mount({}, 403);
    await раскрыть();
    const поле = await screen.findByLabelText("Источник");
    await userEvent.clear(поле);
    await userEvent.type(поле, "В95");
    await userEvent.tab(); // уход курсора = попытка сохранить

    await vi.waitFor(() => expect((поле as HTMLInputElement).value).toBe("В43"));
  });

  it("руководитель видит значения, но не поля ввода", async () => {
    resetSessionStore({
      user: { ...fakeMe, role: "head" },
      permissions: ["conversations:read", "accounts:read"] as Permission[],
      bootstrapped: true,
    });
    mount();
    await раскрыть();

    expect(await screen.findByText("Источник: В43")).toBeTruthy();
    expect(screen.queryByLabelText("Источник")).toBeNull();
    expect(screen.queryByLabelText("Партнёр")).toBeNull();
  });

  it("в СТРОКЕ видно, у какого канала не задан источник", async () => {
    /*
     * Ради этого поля и стояли в свёрнутой строке: по списку из тридцати пяти
     * каналов надо видеть, где источник не проставлен. Убрав поля, работу
     * нельзя терять — она переехала в подпись под именем.
     */
    mount({ lead_origin: null, lead_partner_number: null });

    expect(await screen.findByText(/источник не задан/)).toBeTruthy();
    expect(screen.getByText(/партнёр не задан/)).toBeTruthy();
  });
});
