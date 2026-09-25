/**
 * Обновление токена — по одной вкладке за раз (замер боя 15.09).
 *
 * Refresh-cookie одна на все вкладки, сервер её ротирует, и две вкладки,
 * проснувшиеся разом, приносили одну и ту же cookie: вторая читалась как
 * кража и обрывала цепочку всем. Замок Web Locks общий для вкладок домена —
 * здесь он имитируется очередью promise'ов, как в браузере.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { refreshSession } from "@/shared/api/http";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

describe("Замок вкладок на /auth/refresh", () => {
  let хвост: Promise<unknown>;
  let занят: boolean;
  let одновременно: number;
  let пикОдновременных: number;
  let замков: number;
  let запросов: number;

  beforeEach(() => {
    resetSessionStore({ accessToken: "old-token", bootstrapped: true });
    хвост = Promise.resolve();
    занят = false;
    одновременно = 0;
    пикОдновременных = 0;
    замков = 0;
    запросов = 0;
    // Web Locks как в браузере: одно имя — одна очередь на все вкладки.
    vi.stubGlobal("navigator", {
      locks: {
        request: (_name: string, работа: () => Promise<unknown>) => {
          замков += 1;
          const своя = хвост.then(async () => {
            expect(занят).toBe(false);
            занят = true;
            try {
              return await работа();
            } finally {
              занят = false;
            }
          });
          хвост = своя.catch(() => undefined);
          return своя;
        },
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/auth/refresh")) {
          запросов += 1;
          одновременно += 1;
          пикОдновременных = Math.max(пикОдновременных, одновременно);
          await new Promise((r) => setTimeout(r, 5));
          одновременно -= 1;
          return jsonResponse(200, {
            access_token: "fresh",
            token_type: "bearer",
            expires_in: 900,
            user: fakeUser,
          });
        }
        return jsonResponse(404, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("два обновления «из разных вкладок» идут по очереди, а не разом", async () => {
    // Вторая вкладка — второй экземпляр модуля: single-flight внутри вкладки
    // живёт в модуле, и один экземпляр вернул бы тот же promise, не дойдя до
    // замка (ревью 15.09: такой тест был зелёным и без замка).
    vi.resetModules();
    const вкладкаА = await import("@/shared/api/http");
    vi.resetModules();
    const вкладкаБ = await import("@/shared/api/http");
    const первое = вкладкаА.refreshSession();
    const второе = вкладкаБ.refreshSession();
    expect(await первое).toBe("ok");
    expect(await второе).toBe("ok");
    expect(замков).toBe(2);
    expect(запросов).toBe(2);
    expect(пикОдновременных).toBe(1);
  });

  it("без Web Locks обновление идёт как раньше", async () => {
    vi.stubGlobal("navigator", {});
    expect(await refreshSession()).toBe("ok");
  });
});
