import type { AppInfo, DesktopApi, PlatformBridge } from "../bridge";
import { invoke, invokeSafe } from "./ipc";
import { createNotifier } from "./notifier";
import { createConvCache, createOfflineQueue } from "./offline";
import { checkForUpdates, installUpdate } from "./updater";

/**
 * Реализация моста для десктопа (03 §7, 04 §8). Этот модуль грузится ТОЛЬКО
 * динамическим импортом из `bridge.ts` и только когда `isTauri()` — в веб-бандле
 * соответствующий чанк не запрашивается никогда.
 */

async function readVersion(): Promise<string> {
  // Ядро отдаёт версию из tauri.conf.json (источник истины, 04 §1.2).
  const core = await invokeSafe<string>("plugin:app|version", {}, "");
  if (core) return core;
  const custom = await invokeSafe<AppInfo | null>("app_info", {}, null);
  return custom?.version ?? "—";
}

function createDesktopApi(): DesktopApi {
  return {
    async appInfo() {
      return { version: await readVersion(), channel: "stable" };
    },
    checkForUpdates,
    installUpdate,
    autostart: {
      isEnabled: () => invokeSafe<boolean>("plugin:autostart|is_enabled", {}, false),
      async set(enabled) {
        await invoke(enabled ? "plugin:autostart|enable" : "plugin:autostart|disable", {});
      },
    },
    async setSessionToken(token) {
      // Только в память Rust-процесса (04 §5.5) — на диск не ложится.
      await invokeSafe("set_session_token", { token: token ?? "" }, null);
    },
    async setApiBase(base) {
      await invokeSafe("set_api_base", { base }, null);
    },
  };
}

export function createTauriBridge(): PlatformBridge {
  const notifier = createNotifier();

  return {
    kind: "tauri",

    notify: (n) => notifier.notify(n),

    async setBadge(count) {
      // Иконка трея + overlay таскбара + пункт меню «Новые чаты: N» (04 §3.2).
      await invokeSafe("set_badge", { count: Math.max(0, Math.trunc(count)) }, null);
    },

    onNotificationAction: (cb) => notifier.onNotificationAction(cb),

    offlineQueue: createOfflineQueue(),
    convCache: createConvCache(),

    async openExternal(url) {
      // Штатный путь — tauri-plugin-opener (`opener:allow-open-url` в
      // capabilities): ссылка уходит в системный браузер. `open_external` —
      // запасной вариант на случай собственной команды в оболочке.
      // window.open в Tauri открывает ВНУТРИ WebView, поэтому он последний.
      try {
        await invoke("plugin:opener|open_url", { url });
        return;
      } catch (e) {
        console.warn("[platform] opener недоступен, пробуем запасной путь:", e);
      }
      try {
        await invoke("open_external", { url });
        return;
      } catch {
        window.open(url, "_blank", "noopener");
      }
    },

    desktop: createDesktopApi(),
  };
}
