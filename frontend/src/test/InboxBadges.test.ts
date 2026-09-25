import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import {
  formatChatsRailLabel,
  formatDocumentTitle,
  readBadges,
  subscribeBadges,
} from "@/shared/stores/badges";
import { catchUpAfterReconnect } from "@/shared/realtime/applyWsEvent";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import { stopRealtime } from "@/shared/realtime/realtime";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

vi.mock("@/features/chats/inbox/sound", () => ({ playInboxChime: vi.fn() }));

const MANAGER = ["conversations:read", "messages:send"];
const OBSERVER = ["conversations:read"];

/**
 * Бейджи очереди и непрочитанного (7.1 п.5). Величины разные, и главное, что
 * проверяется здесь, — что их нигде не складывают: оператор должен отличать
 * «мне написали» от «клиент ждёт, чтобы его взяли».
 */
describe("Счётчик очереди: заголовок вкладки и бейдж рейки", () => {
  beforeEach(() => {
    queryClient.clear();
    useInboxStore.getState().clear();
    useUnreadStore.getState().clear();
    useConnectionStore.setState({ status: "idle", lastEventAt: null });
    resetSessionStore({
      user: fakeUser,
      permissions: MANAGER as never,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("пустые счётчики оставляют заголовок нетронутым", () => {
    expect(formatDocumentTitle({ unread: 0, queue: 0 })).toBe("LeadChat");
  });

  it("без очереди заголовок ровно тот же, что был до 7.1", () => {
    // Совместимость до символа: `(3) LeadChat` описан в 11 §2.6 и не должен
    // «поехать» из-за появления второго счётчика.
    expect(formatDocumentTitle({ unread: 3, queue: 0 })).toBe("(3) LeadChat");
  });

  it("очередь и непрочитанные — два разных числа, не сумма", () => {
    const title = formatDocumentTitle({ unread: 3, queue: 2 });
    expect(title).toBe("[2] (3) LeadChat");
    expect(title).not.toContain("(5)"); // складывать их нельзя
  });

  it("очередь стоит первой: в свёрнутой вкладке видно только начало", () => {
    expect(formatDocumentTitle({ unread: 0, queue: 2 }).startsWith("[2]")).toBe(true);
  });

  it("подпись иконки «Чаты» называет обе величины словами", () => {
    // Кружки на значке aria-hidden: без слов скринридер прочитал бы «2 3».
    expect(formatChatsRailLabel({ unread: 3, queue: 2 })).toBe(
      "Чаты — в очереди: 2, непрочитанных: 3",
    );
    expect(formatChatsRailLabel({ unread: 3, queue: 0 })).toBe("Чаты — непрочитанных: 3");
    expect(formatChatsRailLabel({ unread: 0, queue: 2 })).toBe("Чаты — в очереди: 2");
    expect(formatChatsRailLabel({ unread: 0, queue: 0 })).toBe("Чаты");
  });

  it("подписка отдаёт оба счётчика и молчит, когда ничего не изменилось", () => {
    const seen: Array<{ unread: number; queue: number }> = [];
    const stop = subscribeBadges((b) => seen.push({ ...b }));

    expect(seen).toEqual([{ unread: 0, queue: 0 }]); // текущее значение сразу

    useInboxStore.getState().add("q-1");
    useInboxStore.getState().showClaimed("q-1", "Пётр Ковалёв"); // не счётчик — не событие

    expect(seen).toEqual([
      { unread: 0, queue: 0 },
      { unread: 0, queue: 1 },
    ]);
    stop();

    useInboxStore.getState().add("q-2");
    expect(seen).toHaveLength(2); // после отписки — тишина
  });

  it("выход из аккаунта уносит счётчик очереди вместе с непрочитанными", () => {
    // Иначе на экране входа в заголовке вкладки осталось бы «[2] LeadChat»
    // ушедшего сотрудника. Точка одна на оба выхода — обычный и кадр 4403.
    useInboxStore.getState().seedFromServer(["q-1", "q-2"], 2);
    expect(formatDocumentTitle(readBadges())).toBe("[2] LeadChat");

    stopRealtime();

    expect(useInboxStore.getState().count).toBe(0);
    expect(useInboxStore.getState().ids).toEqual({});
    expect(formatDocumentTitle(readBadges())).toBe("LeadChat");
  });

  it("подключение сокета сверяет счётчик очереди с сервером", async () => {
    // Бейдж живёт дольше открытого списка: экран чатов может быть не открыт
    // вовсе, а очередь — уже расти. Засеять её больше неоткуда.
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/inbox/count")) {
        return jsonResponse(200, { count: 4, escalated: 1 });
      }
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });
    vi.stubGlobal("fetch", fetchMock);

    await catchUpAfterReconnect();

    await vi.waitFor(() => expect(useInboxStore.getState().count).toBe(4));
    expect(useInboxStore.getState().escalated).toBe(1);
    expect(readBadges().queue).toBe(4);
  });

  it("наблюдателю очередь не считают: запроса нет вовсе", async () => {
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: OBSERVER as never,
      accessToken: "t",
      bootstrapped: true,
    });
    const fetchMock = vi.fn(async () => jsonResponse(200, { count: 4, escalated: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await catchUpAfterReconnect();
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(useInboxStore.getState().count).toBe(0);
  });

  it("после разрыва счётчик не «догоняется» инкрементами, а спрашивается заново", async () => {
    // Пока сокет лежал, коллеги разобрали половину очереди. Локальная
    // арифметика этого не знает — знает только сервер.
    useInboxStore.getState().seedFromServer(["q-1", "q-2", "q-3"], 3);
    useConnectionStore.setState({ lastEventAt: new Date().toISOString() });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/inbox/count")) return jsonResponse(200, { count: 1, escalated: 0 });
      return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    });
    vi.stubGlobal("fetch", fetchMock);

    await catchUpAfterReconnect();

    await vi.waitFor(() => expect(useInboxStore.getState().count).toBe(1));
  });
});
