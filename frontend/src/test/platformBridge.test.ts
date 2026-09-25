import { afterEach, describe, expect, it } from "vitest";
import { OfflineUnsupported, __setBridge, initBridge, isTauri } from "@/platform/bridge";
import { conversationIdFromPath, shouldToast, ToastThrottle, THROTTLE } from "@/platform/tauri/notifier";
import { leaveTauriRuntime } from "./fakeBridge";

describe("PlatformBridge: веб-реализация (03 §7)", () => {
  afterEach(() => {
    leaveTauriRuntime();
  });

  it("в браузере isTauri() ложно и initBridge даёт веб-мост", async () => {
    __setBridge(null);
    expect(isTauri()).toBe(false);
    const bridge = await initBridge();
    expect(bridge.kind).toBe("web");
    expect(bridge.desktop).toBeUndefined(); // «О приложении» в вебе не показываем
  });

  it("офлайн-очереди в вебе нет: push кидает OfflineUnsupported, кэш пуст", async () => {
    __setBridge(null);
    const bridge = await initBridge();

    await expect(
      bridge.offlineQueue.push({ conversationId: "c1", kind: "message", text: "привет" }),
    ).rejects.toBeInstanceOf(OfflineUnsupported);
    await expect(bridge.offlineQueue.size()).resolves.toBe(0);
    await expect(bridge.convCache.warmup()).resolves.toBeNull();
    await expect(bridge.setBadge(3)).resolves.toBeUndefined(); // бейдж = title (03 §3.5)
  });

  it("onNotificationAction возвращает отписку", async () => {
    __setBridge(null);
    const bridge = await initBridge();
    const off = bridge.onNotificationAction(() => {});
    expect(() => off()).not.toThrow();
  });
});

describe("Правила тостов (04 §4.1/§4.3)", () => {
  it("окно в фокусе — тоста нет; свёрнуто — есть; handoff всегда", () => {
    const msg = { title: "t", body: "b", conversationId: "c1" as const };
    expect(shouldToast(msg, { focused: true, activeConversationId: "c1" })).toBe(false);
    expect(shouldToast(msg, { focused: true, activeConversationId: null })).toBe(false);
    expect(shouldToast(msg, { focused: false, activeConversationId: null })).toBe(true);
    expect(
      shouldToast({ ...msg, kind: "handoff" }, { focused: true, activeConversationId: "c1" }),
    ).toBe(true);
  });

  it("троттлинг: 30 с на диалог, >3 за 10 с → сводный режим, 30 с тишины → выход", () => {
    const t = new ToastThrottle();
    const t0 = 1_000_000;

    expect(t.decide("a", t0)).toBe("toast");
    expect(t.decide("a", t0 + 1_000)).toBe("skip"); // кулдаун диалога
    expect(t.decide("b", t0 + 2_000)).toBe("toast");
    expect(t.decide("c", t0 + 3_000)).toBe("toast");
    expect(t.decide("d", t0 + 4_000)).toBe("summary"); // 4-й за 10 с
    expect(t.decide("e", t0 + 5_000)).toBe("skip"); // сводный раз в минуту
    expect(t.summaryText()).toContain("2");

    // 30 с тишины — обычный режим вернулся.
    const quiet = t0 + 5_000 + THROTTLE.quietResumeMs + 1;
    expect(t.decide("f", quiet)).toBe("toast");
  });

  it("deep-link превращается в id диалога", () => {
    expect(conversationIdFromPath("leadchat://chats/abc-123")).toBe("abc-123");
    expect(conversationIdFromPath("/chats/abc-123")).toBe("abc-123");
    expect(conversationIdFromPath("/settings/profile")).toBeNull();
  });
});
