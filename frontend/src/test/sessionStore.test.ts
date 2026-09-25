import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/shared/api/http";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

describe("sessionStore", () => {
  beforeEach(() => {
    resetSessionStore();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("login stores the access token, user and permissions from /auth/me", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/login")) {
          return jsonResponse(200, {
            access_token: "access-1",
            token_type: "bearer",
            expires_in: 900,
            user: fakeUser,
          });
        }
        if (url.endsWith("/api/v1/auth/me")) {
          return jsonResponse(200, fakeMe);
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    await useSessionStore.getState().login("anna@partner-lead-centre.ru", "secret", true);

    const s = useSessionStore.getState();
    expect(s.accessToken).toBe("access-1");
    expect(s.user?.email).toBe(fakeUser.email);
    expect(s.permissions).toContain("messages:send");
    expect(s.permissions).toContain("conversations:read");
  });

  it("login with invalid credentials throws ApiError and leaves the session empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(401, errorEnvelope("invalid_credentials", "Неверный email или пароль")),
      ),
    );

    const err = await useSessionStore
      .getState()
      .login("anna@partner-lead-centre.ru", "wrong", true)
      .catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("invalid_credentials");
    expect(useSessionStore.getState().accessToken).toBeNull();
    expect(useSessionStore.getState().user).toBeNull();
  });

  it("bootstrap without a live refresh cookie ends unauthenticated but bootstrapped", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(401, errorEnvelope("unauthorized", "Нет refresh-cookie"))),
    );

    await useSessionStore.getState().bootstrap();

    const s = useSessionStore.getState();
    expect(s.bootstrapped).toBe(true);
    expect(s.user).toBeNull();
    expect(s.accessToken).toBeNull();
  });

  it("bootstrap with a live cookie restores the session silently (refresh → me)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/refresh")) {
          return jsonResponse(200, {
            access_token: "rotated",
            token_type: "bearer",
            expires_in: 900,
            user: fakeUser,
          });
        }
        if (url.endsWith("/api/v1/auth/me")) {
          return jsonResponse(200, fakeMe);
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    await useSessionStore.getState().bootstrap();

    const s = useSessionStore.getState();
    expect(s.bootstrapped).toBe(true);
    expect(s.accessToken).toBe("rotated");
    expect(s.user?.id).toBe(fakeUser.id);
    expect(s.permissions.length).toBeGreaterThan(0);
  });

  it("logout clears the session even when the API call fails", async () => {
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network down");
      }),
    );

    await useSessionStore.getState().logout();

    const s = useSessionStore.getState();
    expect(s.user).toBeNull();
    expect(s.accessToken).toBeNull();
    expect(s.permissions).toEqual([]);
  });
});
