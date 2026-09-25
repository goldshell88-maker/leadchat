/**
 * Ссылка «Скачать приложение для Windows» в подвале входа (27.08).
 *
 * ⚠ ЧТО БЫЛО. Ссылка висела всегда и вела на `/download`, где nginx редиректит
 * на `LeadChat-Setup.exe`. Релиз десктопа ни разу не выпускался — файла нет, и
 * боевой адрес отвечает 404. Проверено курлом по проду 27.08.
 *
 * Это первое, что видит новый сотрудник ПЕРЕД входом, и обещание там должно
 * быть выполнимым.
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { LoginPage } from "@/features/auth/LoginPage";
import { renderWithProviders } from "./render";
import { resetSessionStore } from "./helpers";

const ССЫЛКА = /Скачать приложение для Windows/;

function монтировать(манифест: { ok: boolean } | "сеть упала") {
  resetSessionStore({ user: null, permissions: [], accessToken: null, bootstrapped: true });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/download/latest.json")) {
        if (манифест === "сеть упала") throw new TypeError("Failed to fetch");
        return new Response(null, { status: манифест.ok ? 200 : 404 });
      }
      return new Response("{}", { status: 200 });
    }),
  );
  renderWithProviders(<LoginPage />, { route: "/login" });
}

describe("Ссылка на десктоп в подвале входа", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("релиз выложен — ссылка есть", async () => {
    монтировать({ ok: true });
    expect(await screen.findByText(ССЫЛКА)).toBeInTheDocument();
  });

  it("релиза нет — ссылки нет, а не 404 по нажатию", async () => {
    монтировать({ ok: false });
    // Ждём, пока проверка отработает: иначе тест зелёный просто потому, что
    // ссылка не успела появиться, и такой же зелёный он был бы у сломанной.
    await waitFor(() => expect(screen.queryByText(ССЫЛКА)).toBeNull());
    expect(screen.queryByText(ССЫЛКА)).toBeNull();
  });

  it("проверка не достучалась — ссылки тоже нет", async () => {
    // Ошибиться в эту сторону дешевле: показать битую ссылку хуже, чем не
    // показать рабочую.
    монтировать("сеть упала");
    await waitFor(() => expect(screen.queryByText(ССЫЛКА)).toBeNull());
  });

  it("второй адрес подвала на месте при любом исходе", async () => {
    монтировать({ ok: false });
    expect(await screen.findByText("partner-lead-centre.ru")).toBeInTheDocument();
  });
});
