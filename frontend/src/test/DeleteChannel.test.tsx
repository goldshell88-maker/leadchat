import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * Два необратимых действия внизу карточки канала (docs/40, «удаление канала»).
 *
 * Отказ удалять канал с перепиской снят 8 августа — это намерение заказчика,
 * а не дефект. Дефекты были рядом и все три про ТЕКСТ, то есть ровно про то,
 * что человек читает перед нажатием:
 *
 *  - подсказка обещала «сервер откажет, если в канале есть переписка» —
 *    приглашая нажать «Удалить» у живого канала как у ошибочной строки;
 *  - подтверждение обещало (в комментарии рядом) называть число диалогов, а
 *    числа в окне не было ни одного;
 *  - «Отключить и стереть» и «Удалить» стирают переписку одинаково, и чем
 *    они отличаются, не было написано нигде.
 *
 * Здесь заперт результат: числа спрашиваются у сервера и попадают в окно,
 * оба окна называют судьбу самого канала, и ни одно не обещает отказа.
 */

const ACCOUNT = {
  id: "acc-1",
  title: "LP-Москва",
  avito_user_id: 111222333,
  status: "active",
  token_expires_at: "2026-08-20T10:00:00Z",
  created_at: "2026-07-01T10:00:00Z",
  webhook: { status: "ok" },
  backfill: { status: "idle" },
  operators: { count: 0, items: [] },
};

describe("Удаление канала и отключение со стиранием", () => {
  const calls: Array<{ url: string; method: string }> = [];
  let historySize: Response;
  let confirmSpy: MockInstance<(message?: string) => boolean>;

  beforeEach(() => {
    calls.length = 0;
    queryClient.clear();
    // Кнопки необратимых действий видит только администратор (11 §4.1) —
    // с набором прав менеджера из `fakeMe` их на карточке нет вовсе.
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["accounts:read", "accounts:manage"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    historySize = jsonResponse(200, { conversations: 3, messages: 128 });
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? "GET" });
        if (url.includes("/history-size")) return historySize;
        if (url.includes("/avito-accounts/acc-1")) return jsonResponse(200, ACCOUNT);
        if (url.includes("/avito-accounts")) {
          return jsonResponse(200, { items: [ACCOUNT], page: { limit: 50, offset: 0, total: 1 } });
        }
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /*
   * ⚠ С 15 АВГУСТА ОБА РАЗРУШИТЕЛЬНЫХ ДЕЙСТВИЯ ЖИВУТ В МЕНЮ «…» (п. 22 отчёта
   * тестирования), а не кнопками в ряду. Путь до них теперь двухшаговый —
   * открыть меню, выбрать пункт, — и это ЧАСТЬ починки: курок, до которого
   * один клик, лежал на столе. Сторож размещения — buttonCanon.test.ts;
   * здесь проверяются подтверждения, и они не изменились ни словом.
   */
  async function press(name: string): Promise<string> {
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: /Обслуживание канала/ }),
    );
    await user.click(await screen.findByRole("menuitem", { name }));
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
    return String(confirmSpy.mock.calls.at(-1)?.[0] ?? "");
  }

  it("подтверждение удаления называет число диалогов и сообщений", async () => {
    renderWithProviders(<AccountsPage />);
    const question = await press("Удалить");

    expect(calls.some((c) => c.url.endsWith("/avito-accounts/acc-1/history-size"))).toBe(true);
    expect(question).toContain("3 диалога");
    expect(question).toContain("128 сообщений");
  });

  it("подтверждение говорит про потерю, а не про отказ сервера", async () => {
    renderWithProviders(<AccountsPage />);
    // Пункт меню подсказки при наведении не несёт — различие «Удалить» и
    // «Отключить и стереть» обязан объяснять сам текст подтверждения. Прежняя
    // подсказка обещала «сервер откажет, если в канале есть переписка»;
    // отказ снят 8 августа, и надеяться на него — это удалить живой канал.
    const question = await press("Удалить");
    expect(question).toContain("исчезнет из списка");
    expect(question).not.toContain("откаж");
  });

  it("разводит по смыслу «Удалить» и «Отключить и стереть»", async () => {
    renderWithProviders(<AccountsPage />);

    const remove = await press("Удалить");
    expect(remove).toContain("исчезнет из списка");
    expect(remove).toContain("Отключить и стереть"); // куда идти, если надо мягче

    confirmSpy.mockClear();
    const disable = await press("Отключить и стереть");
    expect(disable).toContain("останется в списке");
    expect(disable).toContain("3 диалога");
  });

  it("не выдумывает ноль, когда числа не пришли", async () => {
    // Показать «0 диалогов» вместо «посчитать не удалось» — худшая из ошибок
    // в подтверждении необратимого действия: человек нажмёт спокойно.
    historySize = jsonResponse(500, { error: { code: "internal", message: "нет" } });
    renderWithProviders(<AccountsPage />);
    const question = await press("Удалить");

    expect(question).toContain("посчитать не удалось");
    expect(question).not.toContain("0 диалогов");
  });

  it("отказ в окне не удаляет ничего", async () => {
    renderWithProviders(<AccountsPage />);
    await press("Удалить");

    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  });

  it("согласие в окне удаляет канал", async () => {
    confirmSpy.mockReturnValue(true);
    renderWithProviders(<AccountsPage />);
    await press("Удалить");

    await waitFor(() =>
      expect(
        calls.some((c) => c.method === "DELETE" && c.url.endsWith("/avito-accounts/acc-1")),
      ).toBe(true),
    );
  });
});
