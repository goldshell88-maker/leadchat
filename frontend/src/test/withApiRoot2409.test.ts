/**
 * Относительные ссылки вложений — к адресу сервера в сборке для Windows (проверка 24.09).
 *
 * API отдаёт `/api/v1/media/…?sig=` и `/api/v1/avito-img/…`; в вебе они ведут
 * на тот же сервер, а в приложении для Windows страница живёт на
 * `tauri.localhost`, и фото, голосовые и файлы не загрузились бы.
 *
 * ДИВЕРСИЯ: убрать приставку `ROOT_BASE` в `withApiRoot` — краснеет первый
 * тест; приставлять её всегда — краснеет второй (веб).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

describe("withApiRoot", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("в сборке для Windows относительный адрес ведёт на сервер", async () => {
    vi.stubEnv("VITE_API_BASE", "https://leadchat.example");
    vi.resetModules();
    const { withApiRoot } = await import("@/shared/api/http");

    expect(withApiRoot("/api/v1/media/m-1?sig=abc")).toBe(
      "https://leadchat.example/api/v1/media/m-1?sig=abc",
    );
    // Чужие адреса и адреса без схемы не трогаются.
    expect(withApiRoot("https://00.img.avito.st/image/1.jpg")).toBe(
      "https://00.img.avito.st/image/1.jpg",
    );
    expect(withApiRoot("//cdn.example/x.png")).toBe("//cdn.example/x.png");
  });

  it("в вебе адрес не меняется", async () => {
    vi.stubEnv("VITE_API_BASE", "");
    vi.resetModules();
    const { withApiRoot } = await import("@/shared/api/http");

    expect(withApiRoot("/api/v1/media/m-1?sig=abc")).toBe("/api/v1/media/m-1?sig=abc");
  });
});
