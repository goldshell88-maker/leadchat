/**
 * Тост «Аккаунт Авито требует переподключения» не врёт про приём (проверка 24.09).
 *
 * Канал в needs_reauth продолжает ПРИНИМАТЬ обращения — вебхукам токен не
 * нужен, — не уходят только ответы. Тост говорил «приём сообщений остановлен»,
 * и администратор мог отключить или удалить канал, а это стирает переписку.
 *
 * ДИВЕРСИЯ: вернуть прежний текст — краснеет.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { showToast } from "@/shared/ui/toast";

describe("тост needs_reauth", () => {
  it("говорит, что обращения приходят, а ответы не уходят", () => {
    applyWsEvent({
      type: "account:needs_reauth",
      ts: new Date().toISOString(),
      data: { account_id: "acc-1", title: "Парт-7" },
    } as never);

    const toast = vi.mocked(showToast).mock.calls.at(-1)![0] as { message?: string };
    expect(toast.message).toBe("«Парт-7»: ответы клиентам не уходят, обращения продолжают приходить");
    expect(toast.message).not.toContain("остановлен");
  });
});
