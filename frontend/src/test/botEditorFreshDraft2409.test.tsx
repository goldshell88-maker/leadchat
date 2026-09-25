/**
 * Редактор бота: черновик из свежих данных, «Сохранить» без включения, пул
 * формулировок (проверка 24.09).
 *
 * * Черновик грузился один раз и из первого же кэша. Админ открывал бота,
 *   уходил в список, выключал его там и возвращался: черновик вставал из
 *   кэша «включён», ответ сервера «выключен» его уже не менял, и PUT с
 *   `is_enabled: true` включал бота обратно — в авто-режиме он снова писал
 *   клиентам.
 * * Бот с серверной заготовкой (пул формулировок в дожиме) ронял редактор.
 *
 * ДИВЕРСИИ: убрать `detail.isFetching` из загрузки черновика — краснеет
 * первый тест (переключатель «Включён»); вернуть `is_enabled` в `toInput` —
 * он же (тело PUT); вернуть `text: string` в сводку шага — второй.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { BotDetail } from "@/shared/api/types";
import { ADMIN_PERMISSIONS, BOT_ID, SAMPLE_SCENARIO, avitoAccountsPage, botDetail, resetBotDraft } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

vi.setConfig({ testTimeout: 20_000 });

const ACCOUNTS = [{ id: "acc-1", title: "LP-Москва" }];

describe("Редактор бота — черновик и сохранение", () => {
  let served: BotDetail;
  let calls: Array<{ method: string; path: string; body: unknown }>;

  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    calls = [];
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        const method = init?.method ?? "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        calls.push({ method, path: url.pathname, body });
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "GET") return jsonResponse(200, served);
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "PUT") {
          return jsonResponse(200, { ...served, ...body });
        }
        if (url.pathname === `/api/v1/bots/${BOT_ID}/accounts`) return jsonResponse(200, { accounts: ACCOUNTS });
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("черновик ждёт свежую деталь, и «Сохранить» не включает выключенного бота", async () => {
    const user = userEvent.setup();
    // Кэш прошлого захода: бот был включён. Потом его выключили со списка.
    queryClient.setQueryData(qk.bots.detail(BOT_ID), { ...botDetail({ is_enabled: true }), accounts: ACCOUNTS });
    served = { ...botDetail({ is_enabled: false }), accounts: ACCOUNTS };
    renderBotEditor();

    const name = await screen.findByRole("textbox", { name: "Имя бота" });
    expect(screen.getByRole("switch", { name: "Включён" })).not.toBeChecked();

    await user.type(name, " (правка)");
    await user.click(screen.getByRole("button", { name: /Сохранить/ }));

    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.path === `/api/v1/bots/${BOT_ID}`)).toBe(true));
    const put = calls.find((c) => c.method === "PUT" && c.path === `/api/v1/bots/${BOT_ID}`);
    expect(put?.body).not.toHaveProperty("is_enabled");
  });

  it("бот с пулом формулировок открывается, карточка называет размер пула", async () => {
    const scenario = structuredClone(SAMPLE_SCENARIO);
    const greet = scenario.steps.find((s) => s.id === "greet");
    if (greet?.type === "send") greet.params.text = ["Здравствуйте, {client_name}!", "Добрый день, {client_name}!"];
    served = { ...botDetail({ scenario }), accounts: ACCOUNTS };
    renderBotEditor();

    expect(await screen.findByRole("textbox", { name: "Имя бота" })).toBeInTheDocument();
    expect(screen.getByText(/вариантов: 2/)).toBeInTheDocument();
  });
});
