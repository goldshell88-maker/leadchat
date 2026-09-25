import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * Ход загрузки истории на карточке канала (требование владельца 11 августа).
 *
 * ЧТО БЫЛО. Одна строка на все случаи: «Загружаем историю… 300 чатов» и
 * бесконечная бегущая полоса. На девяти аккаунтах по 300+ объявлений загрузка
 * идёт часами, и такая строка одинаково выглядит на первой минуте и на
 * последней — то есть не отвечает на единственный вопрос, который к ней есть.
 *
 * Плюс два состояния, которых карточка не знала вовсе: перепись (знаменателя
 * ещё нет) и остановка человеком. Последнее особенно важно: ответить на
 * нажатие «Остановить» словами «Загружаем историю…» — значит заставить нажать
 * ещё раз.
 */

const BASE = {
  id: "acc-1",
  title: "Мастер СПб",
  avito_user_id: 100200301,
  status: "active",
  token_expires_at: "2026-08-12T10:00:00Z",
  created_at: "2026-07-01T10:00:00Z",
  webhook: { status: "registered" },
  backfill: { status: "idle" },
  operators: { count: 0, items: [] },
};

function mount(backfill: Record<string, unknown>) {
  const account = { ...BASE, backfill };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/avito-accounts")) {
        return jsonResponse(200, { items: [account], page: { limit: 50, offset: 0, total: 1 } });
      }
      return jsonResponse(200, {});
    }),
  );
  renderWithProviders(<AccountsPage />);
}

/**
 * ⚠ 27.08 каналы стали списком, и ход загрузки истории — отчётный блок: он
 * живёт под раскрытием строки. Сигнальное (статус канала, токен, подписка)
 * осталось видно и в свёрнутой строке — ради него экран и открывают.
 */
async function раскрыть(): Promise<void> {
  const кнопки = await screen.findAllByRole("button", { name: /^Развернуть / });
  await userEvent.click(кнопки[0]);
}

describe("Ход загрузки истории", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("называет числитель и знаменатель, а не просто «N чатов»", async () => {
    mount({ status: "running", phase: "loading", loaded: 137, total: 940, queued: 4 });
    await раскрыть();

    expect(await screen.findByText(/Загружаем историю… 137 из 940 диалогов/)).toBeInTheDocument();
    // «Загружено 137» и «из них 4 ждут ответа» — разные новости, и вторая
    // требует действия сегодня.
    expect(screen.getByText(/из них 4 в «Входящих»/)).toBeInTheDocument();
  });

  it("во время переписи не выдумывает знаменатель", async () => {
    mount({ status: "running", phase: "census", loaded: 0, total: 212 });
    await раскрыть();

    expect(await screen.findByText(/Считаем чаты… нашли 212/)).toBeInTheDocument();
    expect(screen.queryByText(/из 212 диалогов/)).not.toBeInTheDocument();
  });

  it("остановленную загрузку не выдаёт за идущую", async () => {
    mount({ status: "stopped", phase: "stopped", loaded: 300, total: 940 });
    await раскрыть();

    expect(
      await screen.findByText(/Загрузка истории остановлена: 300 из 940 диалогов/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Загружаем историю/)).not.toBeInTheDocument();
  });

  it("переживает прогон, начатый до выкатки: только смещение и ничего больше", async () => {
    mount({ status: "running", chats_offset: 250 });
    await раскрыть();

    expect(await screen.findByText(/Загружаем историю… 250 диалогов/)).toBeInTheDocument();
  });
});
