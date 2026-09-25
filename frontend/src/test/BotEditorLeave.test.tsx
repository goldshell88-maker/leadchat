import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ADMIN_PERMISSIONS, BOT_ID, avitoAccountsPage, botDetail, resetBotDraft } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

/**
 * Уход со страницы редактора с несохранённым сценарием (BOT-02).
 *
 * Предупреждение висело только на `beforeunload` — то есть на закрытии вкладки
 * и перезагрузке. Ссылка «К списку ботов» (стрелка стала значком 04.09), левое
 * меню настроек и кнопка
 * «Назад» браузера меняют адрес через router, страницу никто не выгружает, и
 * получасовая работа над сценарием пропадала молча.
 */

/*
 * Запас по времени на весь файл.
 *
 * Экран редактора — самый тяжёлый в jsdom: одиннадцать карточек-шагов Mantine,
 * настоящий data-роутер и полный круг сохранения. При параллельном прогоне
 * файлов дефолтных 5 секунд не хватает, и тесты падали по таймауту ещё до
 * правок этой ветки — на чистом HEAD тоже.
 */
vi.setConfig({ testTimeout: 20_000 });

function setupFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const method = init?.method ?? "GET";
      if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
      if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "GET") {
        return jsonResponse(200, botDetail());
      }
      if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "PUT") {
        return jsonResponse(200, botDetail({ name: "Первичный приём с правкой" }));
      }
      if (url.pathname === `/api/v1/bots/${BOT_ID}/accounts`) return jsonResponse(200, { accounts: [] });
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    }),
  );
}

/** Открыть редактор и сделать правку — с этого места черновик несохранённый. */
async function openDirtyEditor(user: ReturnType<typeof userEvent.setup>) {
  const utils = renderBotEditor();
  const name = await screen.findByRole("textbox", { name: "Имя бота" });
  await user.type(name, " — правка");
  await screen.findByText("Есть несохранённые изменения");
  return utils;
}

describe("Редактор бота — уход с несохранённым сценарием", () => {
  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    setupFetch();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("«К списку ботов» с несохранённым спрашивает, «Остаться» держит на странице", async () => {
    const user = userEvent.setup();
    const { router } = await openDirtyEditor(user);

    await user.click(screen.getByRole("link", { name: "К списку ботов" }));

    expect(await screen.findByText(/Уйти со страницы и потерять правки сценария\?/)).toBeInTheDocument();
    // Никуда не ушли: адрес прежний, поля на месте.
    expect(router.state.location.pathname).toBe(`/settings/bots/${BOT_ID}`);

    await user.click(screen.getByRole("button", { name: "Остаться" }));
    await waitFor(() =>
      expect(screen.queryByText(/Уйти со страницы и потерять правки сценария\?/)).toBeNull(),
    );
    expect(router.state.location.pathname).toBe(`/settings/bots/${BOT_ID}`);
    expect(screen.getByRole("textbox", { name: "Имя бота" })).toHaveValue("Первичный приём — правка");
  });

  it("«Уйти без сохранения» отпускает переход", async () => {
    const user = userEvent.setup();
    const { router } = await openDirtyEditor(user);

    await user.click(screen.getByRole("link", { name: "К списку ботов" }));
    await screen.findByText(/Уйти со страницы и потерять правки сценария\?/);
    await user.click(screen.getByRole("button", { name: "Уйти без сохранения" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/bots"));
  });

  it("кнопка «Назад» браузера ловится тем же вопросом", async () => {
    // Отдельная проверка: именно этот выход чаще всего и жмут, когда решают
    // «посмотрю список и вернусь».
    const user = userEvent.setup();
    const { router } = await openDirtyEditor(user);

    await router.navigate(-1);

    expect(await screen.findByText(/Уйти со страницы и потерять правки сценария\?/)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/settings/bots/${BOT_ID}`);
  });

  it("без правок уход молчит, и после сохранения — тоже", async () => {
    const user = userEvent.setup();
    const { router } = renderBotEditor();
    await screen.findByRole("textbox", { name: "Имя бота" });

    // 1. Ничего не трогали — вопрос был бы шумом на каждом переходе.
    await user.click(screen.getByRole("link", { name: "К списку ботов" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/bots"));

    // 2. Сохранённый черновик — тоже не повод задерживать.
    await router.navigate(`/settings/bots/${BOT_ID}`);
    const name = await screen.findByRole("textbox", { name: "Имя бота" });
    await user.type(name, " с правкой");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(screen.queryByText("Есть несохранённые изменения")).toBeNull());

    await user.click(screen.getByRole("link", { name: "К списку ботов" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/bots"));
  });
});
