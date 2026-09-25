import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchConversations } from "@/features/chats/api";
import { jsonResponse } from "./helpers";

/**
 * КАЖДОЕ НОВОЕ ПОЛЕ ДОХОДИТ ДО СЕРВЕРА СВОИМ ПАРАМЕТРОМ.
 *
 * Правило записано в самом файле панели и старше этой правки: «поле без
 * параметра в запросе — контрол, который ничего не меняет, а это хуже его
 * отсутствия». Контрол-обманка не просто бесполезен: человек, дёрнув его без
 * последствий, перестаёт верить и соседним полям, которые работают.
 *
 * Здесь проверяется провод, а не экран: `ChatFilterFieldsByTab0509` следит за
 * тем, какие поля показаны, а этот сторож — за тем, что каждое из них
 * превращается в параметр, который `GET /conversations` действительно
 * принимает (`app/api/routes/conversations.py:list_conversations`).
 */
describe("Новые сужения списка: что уходит на сервер", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } }));
    vi.stubGlobal("fetch", fetchMock);
  });

  const запрос = (): URLSearchParams =>
    new URL(String(fetchMock.mock.calls[0][0]), "http://x").searchParams;

  it("«Без ответственного» едет отдельным флагом, а не спецзначением в assignee_id", async () => {
    /*
     * Довод серверный и повторён у поля в `queryKeys.ts`: тип `assignee_id`
     * обязан остаться uuid, иначе опечатка вместо честного 422 давала бы
     * «показал всё».
     */
    await fetchConversations({ tab: "all", unassigned: true }, 0);

    expect(запрос().get("unassigned")).toBe("true");
    expect(запрос().get("assignee_id")).toBeNull();
  });

  it("ответственный едет как assignee_id, и флага «ничей» при нём нет", async () => {
    await fetchConversations({ tab: "all", assigneeId: "u-7" }, 0);

    expect(запрос().get("assignee_id")).toBe("u-7");
    expect(запрос().get("unassigned")).toBeNull();
  });

  it("состояние едет отдельным параметром и не подменяет вкладку", async () => {
    /*
     * `tab=any` у «Всех» — решение владельца 17.08 (архив всегда), а статус
     * поверх него переопределяет серверное умолчание «кроме закрытых». Подмени
     * статус вкладку — «Мои + Закрытые» давало бы пустой список.
     */
    await fetchConversations({ tab: "all", status: "closed" }, 0);

    expect(запрос().get("status")).toBe("closed");
    expect(запрос().get("tab")).toBe("any");
  });

  it("«Ждут ответа» на «Всех» уходит параметром waiting_only", async () => {
    await fetchConversations({ tab: "all", waitingOnly: true }, 0);

    expect(запрос().get("waiting_only")).toBe("true");
  });

  it("выключенные поля не уходят вовсе — пустой параметр сузил бы выборку молча", async () => {
    await fetchConversations({ tab: "all" }, 0);

    const p = запрос();
    expect(p.get("unassigned")).toBeNull();
    expect(p.get("waiting_only")).toBeNull();
    expect(p.get("status")).toBeNull();
    expect(p.get("assignee_id")).toBeNull();
  });
});

/**
 * ОФЛАЙН-СНИМОК ДЕСКТОПА НЕ РИСУЕТ ТО, ЧЕГО НЕ УМЕЕТ СЧИТАТЬ.
 *
 * Снимок из локального кэша показывается ДО первого ответа сервера, и правило
 * вкладок в нём — копия серверного. Про состояние `matchesTab` не знает вовсе,
 * а предикат ожидания и «ничей» живут только на сервере. Пока эти три сужения
 * ставились хоткеем и пилюлей, промах был редким; с полями в панели он стал бы
 * ежедневным: под чипом «Состояние: закрытые» на долю секунды показывался бы
 * ВЕСЬ кэш, а потом список сам себя переписывал ответом сервера.
 */
describe("Прогрев кэша: сужения, которые считает только сервер", () => {
  it("снимок пропускается на состоянии, «ждут ответа» и «без ответственного»", async () => {
    // @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
    const { readFileSync } = await import("node:fs");
    const src = readFileSync("src/platform/warmup.ts", "utf-8") as string;

    const тело = src.slice(src.indexOf("function hasServerSideFilters"));
    for (const поле of ["f.status", "f.waitingOnly", "f.unassigned"]) {
      expect(тело.slice(0, тело.indexOf("}")), `${поле} снова не считается серверным`).toContain(поле);
    }
  });
});
