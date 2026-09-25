import { vi } from "vitest";
import {
  EMPTY_FLUSH_REPORT,
  __setBridge,
  type CachedSnapshot,
  type NotifyRequest,
  type OutgoingDraft,
  type PlatformBridge,
  type UpdateInfo,
} from "@/platform/bridge";

/**
 * Десктоп в jsdom: `isTauri()` смотрит на инжектируемый `__TAURI_INTERNALS__`,
 * а сам мост подменяется через `__setBridge` — реального IPC в тестах нет.
 */
export function makeFakeTauriBridge(overrides: Partial<PlatformBridge> = {}) {
  const calls = {
    // Rust возвращает тот же client_msg_id, что прислал фронт (04 §5.3 п.1).
    push: vi.fn(async (item: OutgoingDraft): Promise<string> => item.clientMessageId ?? "outbox-id"),
    drain: vi.fn(async () => EMPTY_FLUSH_REPORT),
    retry: vi.fn<(clientMessageId: string) => Promise<void>>(async () => {}),
    remove: vi.fn<(clientMessageId: string) => Promise<void>>(async () => {}),
    setBadge: vi.fn<(count: number) => Promise<void>>(async () => {}),
    // Типизируем по сигнатуре моста: тесты читают аргумент тоста (kind, direction…).
    notify: vi.fn<(n: NotifyRequest) => Promise<void>>(async () => {}),
    warmup: vi.fn<() => Promise<CachedSnapshot | null>>(async () => null),
    persist: vi.fn<(s: CachedSnapshot) => Promise<void>>(async () => {}),
    clearCache: vi.fn<() => Promise<void>>(async () => {}),
    installUpdate: vi.fn(async () => {}),
    checkForUpdates: vi.fn<() => Promise<UpdateInfo | null>>(async () => null),
    setApiBase: vi.fn<(base: string) => Promise<void>>(async () => {}),
  };

  const bridge: PlatformBridge = {
    kind: "tauri",
    notify: calls.notify,
    setBadge: calls.setBadge,
    onNotificationAction: () => () => {},
    offlineQueue: {
      push: calls.push,
      drain: calls.drain,
      size: async () => 0,
      list: async () => [],
      retry: calls.retry,
      remove: calls.remove,
      onReport: () => () => {},
    },
    convCache: {
      warmup: calls.warmup,
      persist: calls.persist,
      loadMessages: async () => [],
      clear: calls.clearCache,
    },
    openExternal: async () => {},
    desktop: {
      appInfo: async () => ({ version: "1.4.2", channel: "stable" as const }),
      checkForUpdates: calls.checkForUpdates,
      installUpdate: calls.installUpdate,
      autostart: { isEnabled: async () => false, set: async () => {} },
      setSessionToken: async () => {},
      setApiBase: calls.setApiBase,
    },
    ...overrides,
  };

  return { bridge, calls };
}

/** Включить «десктопный» рантайм: инжект внутренностей Tauri + подмена моста. */
export function enterTauriRuntime(bridge: PlatformBridge): void {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
    invoke: async () => undefined,
    transformCallback: () => 1,
  };
  __setBridge(bridge);
}

export function leaveTauriRuntime(): void {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  __setBridge(null);
}
