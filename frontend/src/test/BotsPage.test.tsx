import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { BotsPage } from "@/features/settings/bots/BotsPage";
import type { BotCreateInput, BotInput } from "@/shared/api/types";
import { ADMIN_PERMISSIONS, BOT_ID, botDetail, botsPage } from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";
import { showToast } from "@/shared/ui/toast";

// Всплывашка живёт вне дерева экрана — проверяем вызов, а не разметку.
vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/** Список ботов `/settings/bots` (11 §5.1): таблица, Switch и создание из шаблона. */
type Handler = (url: URL, init?: RequestInit) => Response | null;

describe("Список ботов", () => {
  let calls: Array<{ path: string; method: string; body?: BotInput }>;
  let extraHandler: Handler | null = null;

  const renderList = () =>
    renderWithProviders(
      <Routes>
        <Route path="/settings/bots" element={<BotsPage />} />
        {/* Заглушка редактора: проверяем сам переход, не его содержимое. */}
        <Route path="/settings/bots/:id" element={<div>Редактор сценария</div>} />
      </Routes>,
      { route: "/settings/bots" },
    );

  beforeEach(() => {
    queryClient.clear();
    calls = [];
    extraHandler = null;
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
        calls.push({
          path: url.pathname,
          method,
          body: init?.body ? (JSON.parse(String(init.body)) as BotInput) : undefined,
        });
        const custom = extraHandler?.(url, init);
        if (custom) return custom;
        if (url.pathname === "/api/v1/bots" && method === "GET") return jsonResponse(200, botsPage());
        if (url.pathname === "/api/v1/bots" && method === "POST") {
          return jsonResponse(201, botDetail({ id: "bot-new", name: "Новый бот" }));
        }
        if (url.pathname === `/api/v1/bots/${BOT_ID}/disable`) return jsonResponse(200, botDetail());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "GET") return jsonResponse(200, botDetail());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "DELETE") {
          return new Response(null, { status: 204 });
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("рисует колонки: имя, вкл, аккаунты, расписание и диалоги за 7 дней", async () => {
    renderList();

    const first = (await screen.findByText("Первичный приём")).closest("tr") as HTMLElement;
    expect(within(first).getByText("LP-Москва")).toBeInTheDocument();
    expect(within(first).getByText("24/7")).toBeInTheDocument();
    expect(within(first).getByText("154")).toBeInTheDocument();
    expect(within(first).getByRole("switch", { name: "Включить бота Первичный приём" })).toBeChecked();

    const second = screen.getByText("Ночной дежурный").closest("tr") as HTMLElement;
    expect(within(second).getByText("пн–вс 20:00–10:00 МСК")).toBeInTheDocument();
    expect(within(second).getByRole("switch", { name: "Включить бота Ночной дежурный" })).not.toBeChecked();
  });

  it("Switch дёргает disable сразу, без сохранения формы (01 §8.4)", async () => {
    const user = userEvent.setup();
    renderList();

    await screen.findByText("Первичный приём");
    await user.click(screen.getByRole("switch", { name: "Включить бота Первичный приём" }));
    await waitFor(() => expect(calls.some((c) => c.path === `/api/v1/bots/${BOT_ID}/disable`)).toBe(true));
  });

  /*
   * BOT-01. `checked` читался прямо из ответа запроса: тумблер не двигался,
   * пока сервер не ответит, и при этом не блокировался. На неспешной сети
   * админ щёлкал второй и третий раз — уходили лишние enable/disable.
   */
  it("тумблер двигается сразу и не даёт щёлкнуть себя второй раз", async () => {
    const user = userEvent.setup();
    let release: () => void = () => undefined;
    extraHandler = (url) =>
      url.pathname === `/api/v1/bots/${BOT_ID}/disable`
        ? (new Promise<Response>((resolve) => {
            release = () => resolve(jsonResponse(200, botDetail({ is_enabled: false })));
          }) as unknown as Response)
        : null;

    renderList();
    await screen.findByText("Первичный приём");
    const toggle = screen.getByRole("switch", { name: "Включить бота Первичный приём" });
    await user.click(toggle);

    // Ответа ещё нет, а тумблер уже в новом положении и щёлкнуть его нельзя.
    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(toggle).toBeDisabled();

    release();
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(calls.filter((c) => c.path.endsWith("/disable"))).toHaveLength(1);
  });

  it("отказ сервера возвращает тумблер на место и объясняет это", async () => {
    const user = userEvent.setup();
    extraHandler = (url) =>
      url.pathname === `/api/v1/bots/${BOT_ID}/disable`
        ? jsonResponse(500, errorEnvelope("internal_error", "упало"))
        : null;

    renderList();
    await screen.findByText("Первичный приём");
    const toggle = screen.getByRole("switch", { name: "Включить бота Первичный приём" });
    await user.click(toggle);

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        // 18.08: тост называет МЕСТО и ПРИЧИНУ сервера, а не заготовку
        expect.objectContaining({
          title: "Не получилось: Переключение бота",
          message: expect.stringContaining("упало"),
        }),
      ),
    );
    // Оптимистичное значение откачено: бот как был включён, так и остался.
    await waitFor(() => expect(toggle).toBeChecked());
  });

  /*
   * BOT-05. Один и тот же текст «Проверьте соединение» показывался и на 403,
   * и на 500 — человек шёл проверять вайфай вместо того, чтобы просить права.
   */
  it("403 говорит про права, а не про соединение, и не предлагает «Повторить»", async () => {
    extraHandler = (url, init) =>
      url.pathname === "/api/v1/bots" && (init?.method ?? "GET") === "GET"
        ? jsonResponse(403, errorEnvelope("forbidden", "Недостаточно прав"))
        : null;

    renderList();
    // `retry: 2` в общем queryClient: до отказа экран честно пробует трижды.
    expect(
      await screen.findByText(/Права на настройку ботов сняты/, {}, { timeout: 6000 }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
    // Три попытки с паузами — тесту нужен запас против дефолтных 5 секунд.
  }, 15_000);

  it("500 говорит про сервер и оставляет «Повторить»", async () => {
    extraHandler = (url, init) =>
      url.pathname === "/api/v1/bots" && (init?.method ?? "GET") === "GET"
        ? jsonResponse(500, errorEnvelope("internal_error", "упало"))
        : null;

    renderList();
    expect(
      await screen.findByText(/Дело не в вашем соединении/, {}, { timeout: 6000 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  }, 15_000);

  it("«Создать бота» берёт заготовку сервера и открывает редактор", async () => {
    const user = userEvent.setup();
    renderList();

    await screen.findByText("Первичный приём");
    await user.click(screen.getByRole("button", { name: "Создать бота" }));

    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/bots" && c.method === "POST")).toBe(true));
    const posted = calls.find((c) => c.path === "/api/v1/bots" && c.method === "POST")?.body as BotCreateInput;
    // Своего сценария фронт не шлёт (проверка 24.09): его копия разошлась с
    // серверной и закрывала диалог по таймауту, которое владелец отменил.
    expect(posted).not.toHaveProperty("scenario");
    expect(posted).not.toHaveProperty("knowledge_base");
    // Новый бот выключен: сначала прогон в песочнице, потом включение.
    expect(posted.is_enabled).toBe(false);

    expect(await screen.findByText("Редактор сценария")).toBeInTheDocument();
  });

  it("«Дублировать» копирует сценарий без аккаунтов", async () => {
    const user = userEvent.setup();
    renderList();

    await screen.findByText("Первичный приём");
    const row = screen.getByText("Первичный приём").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Дублировать" }));

    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/bots" && c.method === "POST")).toBe(true));
    const posted = calls.find((c) => c.path === "/api/v1/bots" && c.method === "POST")?.body as BotInput;
    expect(posted.name).toBe("Первичный приём (копия)");
    expect(posted.is_enabled).toBe(false);
  });

  // Заказчик: «ботов я могу только создать и отключить, но не удалить».
  // Ручка DELETE /bots/{id} была готова, кнопки не было.

  it("«Удалить» спрашивает подтверждение НАШИМ окном и уходит DELETE-ом", async () => {
    const user = userEvent.setup();
    // Нативного `window.confirm` здесь быть не должно (FUNC-68): если он
    // всплывёт, тест увидит это по вызову шпиона.
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderList();

    await screen.findByText("Первичный приём");
    const row = screen.getByText("Первичный приём").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Удалить" }));

    const dialog = await screen.findByRole("dialog");
    expect(confirm).not.toHaveBeenCalled();
    // Сценарий и база знаний пропадают без возврата — об этом надо сказать до,
    // а не после.
    expect(dialog).toHaveTextContent(/вернуть их будет неоткуда/i);
    expect(dialog).toHaveTextContent("Первичный приём");

    // Пока не подтвердили — запроса нет.
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Удалить" }));

    await waitFor(() => {
      expect(calls.some((c) => c.path === `/api/v1/bots/${BOT_ID}` && c.method === "DELETE")).toBe(
        true,
      );
    });
    confirm.mockRestore();
  });

  it("«Отмена» в окне удаления не отправляет ничего", async () => {
    const user = userEvent.setup();
    renderList();

    await screen.findByText("Первичный приём");
    const row = screen.getByText("Первичный приём").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Удалить" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  });

  it("отказ показывает, какие аккаунты Авито держат бота", async () => {
    const user = userEvent.setup();
    extraHandler = (url, init) =>
      url.pathname.endsWith(`/bots/${BOT_ID}`) && init?.method === "DELETE"
        ? jsonResponse(
            409,
            errorEnvelope("conflict", "Бот привязан к аккаунтам — сначала отвяжите их", {
              reason: "bot_in_use",
              accounts: [{ id: "a-1", title: "Парт - 900 / Центр" }],
            }),
          )
        : null;

    renderList();
    await screen.findByText("Первичный приём");
    const row = screen.getByText("Первичный приём").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Удалить" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Удалить" }));

    // «Бот привязан к аккаунтам» без перечисления означало бы «идите ищите,
    // к каким» — на девяти аккаунтах это минуты поиска на ровном месте.
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({ message: expect.stringContaining("Парт - 900 / Центр") }),
      );
    });
  });
});
