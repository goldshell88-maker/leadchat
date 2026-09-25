import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetPlatform, initPlatform } from "@/platform";
import { __setBridge } from "@/platform/bridge";
import { createWebBridge } from "@/platform/web";
import { createTauriBridge } from "@/platform/tauri";
import { __сброситьВыборы, наЭкранеЛи } from "@/platform/наЭкранеЛи";
import { useChatUiStore } from "@/shared/stores/chatUiStore";

/**
 * УВЕДОМЛЕНИЕ ОБЯЗАНО ПРИХОДИТЬ, КОГДА ЧЕЛОВЕК НЕ СМОТРИТ НА LEADCHAT.
 *
 * ⚠ Веб-мост считал это по ВИДИМОСТИ вкладки: `visibilityState === "visible"`
 * → выйти. Диспетчер работает с CRM поверх LeadChat — вкладка при этом видима,
 * окно чужое, — и не получал ничего ровно в том случае, ради которого
 * уведомления и заводились. Десктоп считал по фокусу с самого начала.
 *
 * Проверяется не «есть ли строка с hasFocus», а поведение: показ при потере
 * фокуса, молчание при фокусе, и что ОБЕ платформы судят по одному предикату.
 * Отдельно — что клик по уведомлению в вебе доводит до диалога: показ работал,
 * а подписка на нажатие стояла только в десктопной обвязке.
 */

const { navigateSpy } = vi.hoisted(() => ({ navigateSpy: vi.fn(async () => {}) }));
// Настоящая карта маршрутов в jsdom на переходе собирает Request, который
// undici бракует; здесь важен сам факт перехода и его адрес.
vi.mock("@/app/router", () => ({ router: { navigate: navigateSpy } }));

interface Показанное {
  title: string;
  options?: NotificationOptions;
  нажать(): void;
}

let показанные: Показанное[] = [];

/** Подмена браузерного `Notification`: jsdom его не даёт вовсе. */
function поставитьNotification(permission: NotificationPermission = "granted") {
  показанные = [];
  class ЛожноеУведомление {
    static permission: NotificationPermission = permission;
    static requestPermission = vi.fn(async () => permission);
    onclick: (() => void) | null = null;
    constructor(title: string, options?: NotificationOptions) {
      показанные.push({ title, options, нажать: () => this.onclick?.() });
    }
    close() {}
  }
  vi.stubGlobal("Notification", ЛожноеУведомление);
}

/** Вкладка видима всегда — различает случаи только фокус окна. */
function поставитьЭкран({ видима, вФокусе }: { видима: boolean; вФокусе: boolean }) {
  Object.defineProperty(document, "visibilityState", {
    value: видима ? "visible" : "hidden",
    configurable: true,
  });
  vi.spyOn(document, "hasFocus").mockReturnValue(вФокусе);
}

/** Десктопный рантайм: IPC-вызовы складываем, чтобы видеть, ушёл ли тост. */
function поставитьIPC(): Array<{ cmd: string }> {
  const вызовы: Array<{ cmd: string }> = [];
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
    invoke: async (cmd: string) => {
      вызовы.push({ cmd });
      return undefined;
    },
    transformCallback: () => 1,
  };
  return вызовы;
}

describe("Уведомления веба: заслон по фокусу, а не по видимости (07.09)", () => {
  beforeEach(() => {
    поставитьNotification();
    navigateSpy.mockClear();
  });

  afterEach(() => {
    __resetPlatform();
    __сброситьВыборы();
    __setBridge(null);
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    useChatUiStore.setState({ activeConversationId: null });
  });

  it("вкладка видима, но окно чужое (CRM поверх) — уведомление показано", async () => {
    поставитьЭкран({ видима: true, вФокусе: false });
    expect(наЭкранеЛи()).toBe(false);

    await createWebBridge().notify({ title: "Иван Петров", body: "здравствуйте", conversationId: "c1" });

    expect(показанные.map((п) => п.title)).toEqual(["Иван Петров"]);
  });

  it("вкладка видима и в фокусе — молчим: человек и так смотрит", async () => {
    поставитьЭкран({ видима: true, вФокусе: true });

    await createWebBridge().notify({ title: "Иван Петров", body: "здравствуйте", conversationId: "c1" });

    expect(показанные).toEqual([]);
  });

  it("скрытая вкладка уведомляет по-прежнему", async () => {
    // Прежнее поведение не потеряно: скрытая вкладка — частный случай «не на экране».
    поставитьЭкран({ видима: false, вФокусе: false });

    await createWebBridge().notify({ title: "Иван Петров", body: "здравствуйте", conversationId: "c1" });

    expect(показанные).toHaveLength(1);
  });

  it("десктоп судит по тому же предикату: без фокуса тост уходит, в фокусе — нет", async () => {
    const вызовы = поставитьIPC();

    поставитьЭкран({ видима: true, вФокусе: true });
    await createTauriBridge().notify({ title: "Иван", body: "привет", conversationId: "c1" });
    expect(вызовы.filter((в) => в.cmd === "notify_show")).toHaveLength(0);

    поставитьЭкран({ видима: true, вФокусе: false });
    await createTauriBridge().notify({ title: "Иван", body: "привет", conversationId: "c1" });
    expect(вызовы.filter((в) => в.cmd === "notify_show")).toHaveLength(1);
  });

  it("клик по уведомлению ведёт в диалог И В ВЕБЕ", async () => {
    поставитьЭкран({ видима: true, вФокусе: false });
    __setBridge(createWebBridge());
    const мост = await initPlatform();
    expect(мост?.kind).toBe("web");

    await мост?.notify({ title: "Иван Петров", body: "здравствуйте", conversationId: "c-42" });
    expect(показанные).toHaveLength(1);
    показанные[0].нажать();

    expect(useChatUiStore.getState().activeConversationId).toBe("c-42");
    // Переход асинхронный: сначала стор, потом динамический импорт роутера.
    await vi.waitFor(() => expect(navigateSpy).toHaveBeenCalledWith("/chats/c-42"));
  });

  it("подписка на нажатие ровно одна: десктоп не переходит дважды", async () => {
    /*
     * Подписка переехала из `wireDesktop` в общую часть `initPlatform`. Если
     * забыть убрать её из десктопной обвязки, на одно нажатие будет два
     * перехода — и оба уйдут в роутер, где второй перебьёт первый.
     */
    поставитьIPC();
    const нажатия: Array<(a: { conversationId: string }) => void> = [];
    const мост = createTauriBridge();
    __setBridge({
      ...мост,
      onNotificationAction: (cb) => {
        нажатия.push(cb);
        return () => {};
      },
    });
    await initPlatform();

    expect(нажатия).toHaveLength(1);
  });
});
