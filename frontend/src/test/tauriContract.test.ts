import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __setBridge } from "@/platform/bridge";
import { __resetPlatform, initPlatform } from "@/platform";
import { createTauriBridge } from "@/platform/tauri";
import { setAwayMode, toRustKind } from "@/platform/tauri/notifier";
import { makeFakeTauriBridge, leaveTauriRuntime } from "./fakeBridge";

/**
 * Контракт фронта с ядром Tauri (04 §8.1/§8.2). Здесь проверяются ИМЕНА команд
 * и форма payload'а: разойтись с Rust легко, а последствия молчаливые —
 * `invoke` отвергается на разборе аргументов, ошибка уходит в console.warn,
 * и уведомления просто перестают появляться.
 */

interface InvokeCall {
  cmd: string;
  args: Record<string, unknown>;
}

let calls: InvokeCall[];

function installIpc(impl?: (cmd: string, args: Record<string, unknown>) => unknown) {
  calls = [];
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
    invoke: async (cmd: string, args: Record<string, unknown> = {}) => {
      calls.push({ cmd, args });
      return impl?.(cmd, args);
    },
    transformCallback: () => 1,
  };
}

function findCall(cmd: string): InvokeCall | undefined {
  return calls.find((c) => c.cmd === cmd);
}

/** Дать отработать динамическим импортам платформенного слоя. */
async function settle(): Promise<void> {
  for (let i = 0; i < 12; i++) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  for (let i = 0; i < 12; i++) await Promise.resolve();
}

describe("Тосты уходят в notify_show (04 §8.1)", () => {
  beforeEach(() => {
    installIpc();
    setAwayMode(false);
    // Окно «свёрнуто»: иначе локальный отсев не пустит обычное сообщение дальше.
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    setAwayMode(false);
    leaveTauriRuntime();
  });

  it("команда показа — notify_show, а НЕ notify_reply (та принимает {conversation_id, text})", async () => {
    const bridge = createTauriBridge();
    await bridge.notify({ title: "Иван", body: "привет", conversationId: "c1" });

    expect(findCall("notify_show")).toBeDefined();
    expect(findCall("notify_reply")).toBeUndefined();
  });

  it("kind переводится в вариант enum'а Rust: handoff→assigned, account/system→system", async () => {
    expect(toRustKind("handoff")).toBe("assigned");
    expect(toRustKind("account")).toBe("system");
    expect(toRustKind("system")).toBe("system");
    expect(toRustKind("message")).toBe("message");
    expect(toRustKind(undefined)).toBe("message");

    const bridge = createTauriBridge();
    await bridge.notify({ title: "t", body: "b", conversationId: "c1", kind: "handoff" });
    const req = findCall("notify_show")?.args.req as Record<string, unknown>;
    expect(req.kind).toBe("assigned"); // «handoff» Rust не разберёт — упадёт весь запрос
  });

  /**
   * Критичное системное уведомление центра (14 §4) диалога не имеет. Поле
   * `conversation_id` в Rust — `String` без Option: пропуск валит разбор
   * аргументов и тоста не будет вовсе, поэтому шлём пустую строку.
   */
  it("системный тост без диалога шлёт conversationId пустой строкой, а не undefined", async () => {
    const bridge = createTauriBridge();
    await bridge.notify({ title: "На диске мало места", body: "4%", kind: "system" });

    const req = findCall("notify_show")?.args.req as Record<string, unknown>;
    expect(req.kind).toBe("system");
    expect(req.conversationId).toBe("");
  });

  it("шлём поля матрицы: direction, senderType, canReply и подсказку foreground", async () => {
    const bridge = createTauriBridge();
    await bridge.notify({
      title: "Иван",
      body: "привет",
      conversationId: "c1",
      direction: "in",
      senderType: "client",
      canReply: false,
    });

    const req = findCall("notify_show")?.args.req as Record<string, unknown>;
    expect(req).toMatchObject({
      conversationId: "c1",
      direction: "in",
      senderType: "client",
      canReply: false,
      foreground: false,
    });
  });

  it("статус «отошёл» из трея делает тосты беззвучными", async () => {
    const bridge = createTauriBridge();
    setAwayMode(true);
    await bridge.notify({ title: "t", body: "b", conversationId: "c1" });

    const req = findCall("notify_show")?.args.req as Record<string, unknown>;
    expect(req.silent).toBe(true);
  });
});

describe("Внешние ссылки открываются системным браузером (03 §7)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    leaveTauriRuntime();
  });

  it("openExternal идёт в tauri-plugin-opener, а не в window.open", async () => {
    installIpc();
    const open = vi.spyOn(window, "open").mockReturnValue(null);

    await createTauriBridge().openExternal("https://avito.ru/1");

    expect(findCall("plugin:opener|open_url")?.args).toEqual({ url: "https://avito.ru/1" });
    expect(open).not.toHaveBeenCalled(); // в WebView это открылось бы ВНУТРИ приложения
  });

  it("если плагина нет — пробуем свою команду, и лишь потом window.open", async () => {
    installIpc((cmd) => {
      if (cmd === "plugin:opener|open_url" || cmd === "open_external") throw new Error("нет команды");
    });
    const open = vi.spyOn(window, "open").mockReturnValue(null);

    await createTauriBridge().openExternal("https://avito.ru/1");

    expect(findCall("open_external")).toBeDefined();
    expect(open).toHaveBeenCalledWith("https://avito.ru/1", "_blank", "noopener");
  });
});

describe("Холодный старт deep-link: фронт обязан прислать app:ready (lib.rs)", () => {
  afterEach(() => {
    __resetPlatform();
    leaveTauriRuntime();
    vi.restoreAllMocks();
  });

  it("после навешивания слушателей уходит plugin:event|emit с событием app:ready", async () => {
    installIpc();
    const { bridge } = makeFakeTauriBridge();
    __setBridge(bridge);

    await initPlatform();
    await settle();

    const emit = calls.find((c) => c.cmd === "plugin:event|emit" && c.args.event === "app:ready");
    // Без этого Rust держит маршрут из тоста в очереди вечно (queue_navigate).
    expect(emit).toBeDefined();
  });

  it("set_api_base уходит из десктоп-бандла раньше любого outbox_flush", async () => {
    installIpc();
    const { bridge, calls: bridgeCalls } = makeFakeTauriBridge();
    __setBridge(bridge);

    await initPlatform();
    await settle();

    // В веб-режиме VITE_API_BASE пуст — тогда база не переопределяется вовсе.
    const base = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
    if (base) expect(bridgeCalls.setApiBase).toHaveBeenCalledWith(base);
    else expect(bridgeCalls.setApiBase).not.toHaveBeenCalled();
    expect(bridgeCalls.drain).not.toHaveBeenCalled(); // флаш не стартует раньше базы/токена
  });

  it("подписка на presence:set из меню трея навешивается", async () => {
    installIpc();
    const { bridge } = makeFakeTauriBridge();
    __setBridge(bridge);

    await initPlatform();
    await settle();

    const listened = calls.filter((c) => c.cmd === "plugin:event|listen").map((c) => c.args.event);
    expect(listened).toContain("presence:set");
    expect(listened).toContain("session:refresh-needed");
  });
});
