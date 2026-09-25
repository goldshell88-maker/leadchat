import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, http, TOO_MANY_REQUESTS } from "@/shared/api/http";
import { errorEnvelope, jsonResponse, resetSessionStore } from "./helpers";

/**
 * 429 ОТ NGINX — «ПОДОЖДИТЕ СЕКУНДУ», А НЕ ОТКАЗ (проверка 24.09).
 *
 * Личная планка nginx отбивает запрос до приложения, поэтому повтор безопасен
 * для любого метода. Раньше такой ответ становился тостом «Запрос завершился с
 * кодом 429», а открытый диалог — «Диалог недоступен»: 5 583 отказа за
 * 08.09–24.09. Отказы самого приложения по лимиту (`rate_limited`, сроки в
 * часы) по-прежнему не повторяются.
 */

function htmlLimit(): Response {
  return new Response("<html><body><h1>429 Too Many Requests</h1></body></html>", {
    status: 429,
    headers: { "Content-Type": "text/html" },
  });
}

function edgeLimit(retryAfter: string): Response {
  return new Response(
    JSON.stringify(errorEnvelope(TOO_MANY_REQUESTS, "Слишком много запросов подряд — подождите пару секунд")),
    { status: 429, headers: { "Content-Type": "application/json", "Retry-After": retryAfter } },
  );
}

describe("Отказ nginx по частоте", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetSessionStore({ accessToken: "t", bootstrapped: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("GET переспрашивается через секунду и доходит", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(htmlLimit())
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    const answer = http.get<{ ok: boolean }>("/conversations/c-1");
    await vi.advanceTimersByTimeAsync(999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);

    await expect(answer).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("ждёт столько, сколько велит Retry-After", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(edgeLimit("2"))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    const answer = http.get<{ ok: boolean }>("/conversations/c-1/messages");
    await vi.advanceTimersByTimeAsync(1500);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(500);

    await expect(answer).resolves.toEqual({ ok: true });
  });

  it("«Принять» тоже повторяется: до приложения первая попытка не дошла", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(edgeLimit("1"))
      .mockResolvedValueOnce(jsonResponse(200, { id: "c-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const answer = http.post<{ id: string }>("/inbox/c-1/claim");
    await vi.advanceTimersByTimeAsync(1000);

    await expect(answer).resolves.toEqual({ id: "c-1" });
    expect(fetchMock.mock.calls.map((c) => (c[1] as RequestInit).method)).toEqual(["POST", "POST"]);
  });

  it("не прошло и после двух ожиданий — понятные слова вместо кода", async () => {
    const fetchMock = vi.fn(async () => htmlLimit());
    vi.stubGlobal("fetch", fetchMock);

    const answer = http.get("/conversations/c-1").catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(2000);
    const error = await answer;

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      code: TOO_MANY_REQUESTS,
      message: "Слишком много запросов подряд — подождите пару секунд",
    });
  });

  it("лимит самого приложения не повторяется", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(429, errorEnvelope("rate_limited", "Исчерпан дневной лимит выгрузок (20)")),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(http.post("/stats/export")).rejects.toMatchObject({ code: "rate_limited" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
