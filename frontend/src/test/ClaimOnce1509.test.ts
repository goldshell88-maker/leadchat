/**
 * Одно принятие на диалог, пока запрос в полёте (замер боя 14–15.09: 357 из
 * 363 ответов 409 за сутки — второе принятие того же человека через ~100 мс
 * после первого: клавиша, кнопка и первый набранный символ шлют его каждый
 * по-своему).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { claimConversation } from "@/features/chats/inbox/api";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

describe("claimConversation", () => {
  let запросов: number;

  beforeEach(() => {
    resetSessionStore({
      accessToken: "token",
      bootstrapped: true,
      user: fakeUser,
    });
    запросов = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/claim")) {
          запросов += 1;
          await new Promise((r) => setTimeout(r, 5));
          return jsonResponse(200, {
            conversation: {
              id: "c1",
              status: "in_progress",
              assignee: fakeUser,
            },
            count: 0,
            escalated: 0,
          });
        }
        return jsonResponse(404, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("второй вызов до ответа получает тот же запрос", async () => {
    const [a, b] = await Promise.all([
      claimConversation("c1"),
      claimConversation("c1"),
    ]);
    expect(запросов).toBe(1);
    expect(a).toBe(b);
  });

  it("после ответа принятие того же диалога снова уходит на сервер", async () => {
    await claimConversation("c1");
    await claimConversation("c1");
    expect(запросов).toBe(2);
  });

  it("разные диалоги не мешают друг другу", async () => {
    await Promise.all([claimConversation("c1"), claimConversation("c2")]);
    expect(запросов).toBe(2);
  });
});
