import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, http } from "@/shared/api/http";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

function authHeader(init?: RequestInit): string | null {
  return new Headers(init?.headers).get("Authorization");
}

describe("http 401 → single refresh with a queue of waiting requests", () => {
  beforeEach(() => {
    resetSessionStore({ accessToken: "old-token", bootstrapped: true });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("two concurrent 401s trigger exactly one refresh and both retry with the new token", async () => {
    let refreshCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        await new Promise((r) => setTimeout(r, 20)); // keep refresh in flight while both 401s queue up
        return jsonResponse(200, {
          access_token: "new-token",
          token_type: "bearer",
          expires_in: 900,
          user: fakeUser,
        });
      }
      if (authHeader(init) === "Bearer new-token") {
        return jsonResponse(200, { path: url });
      }
      return jsonResponse(401, errorEnvelope("unauthorized", "Токен просрочен"));
    });
    vi.stubGlobal("fetch", fetchMock);

    const [a, b] = await Promise.all([
      http.get<{ path: string }>("/one"),
      http.get<{ path: string }>("/two"),
    ]);

    expect(refreshCalls).toBe(1);
    expect(a.path).toContain("/api/v1/one");
    expect(b.path).toContain("/api/v1/two");
    expect(useSessionStore.getState().accessToken).toBe("new-token");

    // 2 initial + 1 refresh + 2 retries = 5 fetch calls total
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("failed refresh clears the session and rethrows the original 401 envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/refresh")) {
          return jsonResponse(401, errorEnvelope("unauthorized", "Refresh отозван"));
        }
        return jsonResponse(401, errorEnvelope("unauthorized", "Токен просрочен"));
      }),
    );

    const err = await http.get("/protected").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).code).toBe("unauthorized");
    expect(useSessionStore.getState().accessToken).toBeNull();
    expect(useSessionStore.getState().user).toBeNull();
  });

  it("auth:false requests surface their 401 as-is without touching refresh", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(401, errorEnvelope("invalid_credentials", "Неверный email или пароль")),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await http
      .post("/auth/login", { email: "a@b.c", password: "x", remember: true }, { auth: false })
      .catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("invalid_credentials");
    // Only the login call itself — no refresh, no retry.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("parses the error envelope of 01 §1.3 into ApiError fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          403,
          errorEnvelope("account_locked", "Слишком много попыток", { retry_after_sec: 899 }),
        ),
      ),
    );

    const err = await http
      .post("/auth/login", { email: "a@b.c", password: "x", remember: true }, { auth: false })
      .catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(403);
    expect(apiErr.code).toBe("account_locked");
    expect(apiErr.message).toBe("Слишком много попыток");
    expect(apiErr.details).toEqual({ retry_after_sec: 899 });
    expect(apiErr.requestId).toBe("req_test");
  });
});
