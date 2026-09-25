import { beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { взятьПервогоССервера } from "@/features/chats/inbox/claimFromQueue";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ПРИЁМ БЕЗ МЫШИ И БЕЗ «ВХОДЯЩИХ» — сам добор очереди с сервера.
 *
 * Нажатия здесь идут не через клавиатурный слой (он проверен в
 * PinnedDialog3008), а прямо в добор: важно, что он ЧЕСТНО ходит по сети,
 * принимает ровно первого и не молчит ни на пустой очереди, ни на отказе.
 */

const toast = vi.fn();
vi.mock("@/shared/ui/toast", () => ({ showToast: (...a: unknown[]) => toast(...a) }));

const navigate = vi.fn();

function очередьНаСервере(items: Array<{ id: string }>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/inbox")) {
      return jsonResponse(200, {
        items: items.map((i) => ({ id: i.id, unread_count: 0, escalated: false, tags: [] })),
        page: { limit: 50, offset: 0, total: items.length },
      });
    }
    if (url.includes("/claim")) {
      return jsonResponse(200, { conversation: { id: url.split("/").at(-2) }, inbox_count: items.length - 1 });
    }
    return jsonResponse(404, {});
  });
}

describe("Добор первого ждущего с сервера", () => {
  beforeEach(() => {
    queryClient.clear();
    toast.mockClear();
    navigate.mockClear();
    useInboxStore.setState({ ids: {} } as never);
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
  });

  it("принимает ровно первого и открывает его", async () => {
    const fetchMock = очередьНаСервере([{ id: "ждёт-дольше-всех" }, { id: "ждёт-меньше" }]);
    vi.stubGlobal("fetch", fetchMock);

    await взятьПервогоССервера(navigate);

    const принят = fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0]))).find((u) => u.includes("/claim"));
    expect(принят, "приём не ушёл на сервер").toContain("ждёт-дольше-всех");
    expect(navigate, "принятый диалог не открылся").toHaveBeenCalledWith("/chats/ждёт-дольше-всех");
  });

  it("очередь пуста — говорит об этом, а не молчит", async () => {
    // Молчание читается как «клавиша сломана» — ровно сегодняшняя жалоба.
    vi.stubGlobal("fetch", очередьНаСервере([]));

    await взятьПервогоССервера(navigate);

    expect(navigate).not.toHaveBeenCalled();
    expect(toast, "пустая очередь прошла молча").toHaveBeenCalledWith(
      expect.objectContaining({ title: "Очередь пуста" }),
    );
  });

  it("сеть упала — тоже говорит, а не молчит", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));

    await взятьПервогоССервера(navigate);

    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ color: "red" }));
  });
});
