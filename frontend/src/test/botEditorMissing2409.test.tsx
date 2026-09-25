/**
 * Редактор бота по ссылке на удалённого бота или при сбое загрузки (проверка 24.09).
 *
 * Ветка ошибки стояла ПОСЛЕ ветки «черновик не загружен», а при ошибке
 * черновик не загружается никогда: экран навсегда оставался четырьмя серыми
 * прямоугольниками, и «Бот не найден» был недостижим.
 *
 * ДИВЕРСИЯ: вернуть ветку ошибки ниже скелета — краснеют оба теста.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ADMIN_PERMISSIONS, BOT_ID, avitoAccountsPage, resetBotDraft } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

vi.setConfig({ testTimeout: 20_000 });

describe("Редактор бота — бота нет или сервер отказал", () => {
  let detail: () => Response;

  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}`) return detail();
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("удалённый бот — «Бот не найден» и путь к списку", async () => {
    detail = () => jsonResponse(404, errorEnvelope("not_found", "Бот не найден"));
    renderBotEditor();

    expect(await screen.findByText("Бот не найден")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "К списку ботов" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
  });

  it("сбой сервера — словами и с «Повторить»", async () => {
    detail = () => jsonResponse(500, errorEnvelope("internal_error", "Внутренняя ошибка сервера"));
    renderBotEditor();

    expect(await screen.findByText("Не получилось загрузить бота")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "К списку ботов" })).toBeInTheDocument();
  });
});
