/**
 * Список каналов листается до конца (проверка 24.09).
 *
 * Один запрос без параметров отдавал первые 50: 51-й канал — самый новый —
 * не появлялся ни на экране каналов, ни в фильтрах, и предупреждения не было.
 *
 * ДИВЕРСИЯ: вернуть один запрос — краснеет «все 120».
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchAvitoAccounts } from "@/shared/api/reference";
import { jsonResponse, resetSessionStore } from "./helpers";

function channel(i: number) {
  return { id: `acc-${i}`, title: `Канал ${i}` };
}

describe("fetchAvitoAccounts", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("догружает страницы по total: все 120", async () => {
    resetSessionStore({ accessToken: "t", bootstrapped: true });
    const total = 120;
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        seen.push(url.search);
        const limit = Number(url.searchParams.get("limit"));
        const offset = Number(url.searchParams.get("offset"));
        const items = Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, k) =>
          channel(offset + k),
        );
        return jsonResponse(200, { items, page: { limit, offset, total } });
      }),
    );

    const page = await fetchAvitoAccounts();

    expect(page.items).toHaveLength(120);
    expect(page.items.at(-1)?.id).toBe("acc-119");
    expect(page.page.total).toBe(120);
    expect(seen).toEqual(["?limit=100&offset=0", "?limit=100&offset=100"]);
  });
});
