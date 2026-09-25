/**
 * Проверка обновлений в сборке для Windows: сбой — исключение, а не «обновления
 * нет» (проверка 24.09). Раньше `invokeSafe` отдавал `null` на любую ошибку, и
 * экран писал «У вас последняя версия».
 *
 * ДИВЕРСИЯ: вернуть `invokeSafe(..., null)` в `checkForUpdates` — краснеет
 * «строка Tauri становится причиной».
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const invoke = vi.fn();
vi.mock("@/platform/tauri/ipc", () => ({
  invoke: (...args: unknown[]) => invoke(...args),
  listenSafe: vi.fn(() => () => {}),
}));

import { checkForUpdates } from "@/platform/tauri/updater";
import { useUpdateStore } from "@/platform/updateStore";

describe("checkForUpdates", () => {
  afterEach(() => {
    invoke.mockReset();
    useUpdateStore.getState().reset();
  });

  it("строка ошибки Tauri становится причиной", async () => {
    invoke.mockRejectedValueOnce("error sending request for url");
    await expect(checkForUpdates()).rejects.toThrow("error sending request for url");
    expect(useUpdateStore.getState().checking).toBe(false);
  });

  it("нет обновления — null", async () => {
    invoke.mockResolvedValueOnce({ version: "1.4.2", available: false });
    await expect(checkForUpdates()).resolves.toBeNull();
  });
});
