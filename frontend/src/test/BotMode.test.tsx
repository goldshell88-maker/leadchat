import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ADMIN_PERMISSIONS, BOT_ID, avitoAccountsPage, botDetail, resetBotDraft } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

/**
 * Выбор режима работы бота: подсказка оператору или ответ клиенту.
 *
 * Смысл экрана — чтобы человек НЕ спутал два разных выключателя. «Включён» решает,
 * работает ли бот вообще; режим — попадает ли его текст клиенту в Авито. Бот в режиме
 * подсказки включён и работает, но клиент от него не получает ничего.
 *
 * Поэтому здесь проверяется не столько наличие контрола, сколько три вещи, на которых
 * такую пару выключателей обычно и ломают: что по умолчанию выбрано безопасное, что
 * переключение уезжает на сервер вместе со сценарием (а не раньше него), и что
 * выбранное состояние подписано словами, а не только подсветкой сегмента.
 */

vi.setConfig({ testTimeout: 20_000 });

/** Тела PUT, которые редактор отправил на сервер, по порядку. */
let puts: Record<string, unknown>[] = [];

function setupFetch(detail = botDetail()) {
  puts = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const method = init?.method ?? "GET";
      if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
      if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "GET") {
        return jsonResponse(200, detail);
      }
      if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "PUT") {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        puts.push(body);
        return jsonResponse(200, { ...detail, ...body });
      }
      if (url.pathname === `/api/v1/bots/${BOT_ID}/accounts`) return jsonResponse(200, { accounts: [] });
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    }),
  );
}

describe("Редактор бота — режим работы", () => {
  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("по умолчанию выбрана подсказка и сказано, что клиент ничего не получит", async () => {
    setupFetch(botDetail({ mode: "suggest" }));
    renderBotEditor();

    const suggest = await screen.findByRole("radio", { name: "Подсказка оператору" });
    expect(suggest).toBeChecked();
    expect(screen.getByRole("radio", { name: "Отвечать клиенту" })).not.toBeChecked();
    expect(screen.getByText(/Клиент не получает ничего/)).toBeInTheDocument();
  });

  it("переключение на автоответ помечает черновик и уезжает только по «Сохранить»", async () => {
    const user = userEvent.setup();
    setupFetch(botDetail({ mode: "suggest" }));
    renderBotEditor();

    await user.click(await screen.findByRole("radio", { name: "Отвечать клиенту" }));

    // Ключевое: сам щелчок ничего не отправил. Иначе бот начал бы писать клиентам
    // до того, как админ довёл сценарий до конца и нажал «Сохранить».
    expect(puts).toHaveLength(0);
    await screen.findByText("Есть несохранённые изменения");
    expect(screen.getByText(/Бот пишет клиенту в Авито сам/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].mode).toBe("auto");
  });

  it("бот на автоответе показывает это при открытии", async () => {
    setupFetch(botDetail({ mode: "auto" }));
    renderBotEditor();

    expect(await screen.findByRole("radio", { name: "Отвечать клиенту" })).toBeChecked();
  });
});
