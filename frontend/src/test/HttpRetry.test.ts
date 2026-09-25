import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http } from "@/shared/api/http";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * Повтор запроса при временной беде (frontend/src/shared/api/http.ts).
 *
 * Жалоба с боевой системы: отметка прочтения иногда не проходит, и бейдж
 * непрочитанного висит без причины. Вызывающий код глушит ошибку намеренно —
 * всплывашка «не удалось» на каждое открытие диалога хуже висящего бейджа, —
 * поэтому единственный способ не потерять отметку — повторить её здесь.
 *
 * Проверяются РОВНО ДВЕ вещи, и вторая не менее важна первой: повторяется то,
 * что повторять безопасно, и НЕ повторяется всё остальное. Повтор отправки
 * сообщения клиенту — это сообщение, ушедшее дважды.
 */

const CONV = "8dd4e600-c26f-4ce9-9d6a-f1c1c006ac33";

function setupSession() {
  resetSessionStore({ user: fakeUser, permissions: [] as never, accessToken: "t", bootstrapped: true });
}

describe("http: повтор запроса при временной беде", () => {
  beforeEach(() => {
    setupSession();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("отметка прочтения повторяется после 503 и доходит", async () => {
    const codes = [503, 503, 204];
    const fetchMock = vi.fn(async () => jsonResponse(codes.shift() ?? 204, null));
    vi.stubGlobal("fetch", fetchMock);

    await expect(http.post<void>(`/conversations/${CONV}/read`)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("после трёх неудач отметка прочтения сдаётся ошибкой, а не молотит вечно", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(503, { error: { code: "upstream_unavailable", message: "нет" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(http.post<void>(`/conversations/${CONV}/read`)).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("отправка сообщения НЕ повторяется: клиент получил бы его дважды", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(503, { error: { code: "upstream_unavailable", message: "нет" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(http.post(`/conversations/${CONV}/messages`, { body: "здравствуйте" })).rejects.toMatchObject({
      status: 503,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("отказ бизнес-правила не повторяется: второй раз ответят то же самое", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(422, { error: { code: "unprocessable", message: "нельзя" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(http.post<void>(`/conversations/${CONV}/read`)).rejects.toMatchObject({ status: 422 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("оборванная сеть тоже повод повторить — окно выката длится секунды", async () => {
    let calls = 0;
    const fetchMock = vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new TypeError("Failed to fetch");
      return jsonResponse(204, null);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(http.post<void>(`/conversations/${CONV}/read`)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
