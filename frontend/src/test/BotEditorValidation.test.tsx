import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import type { BotDetail } from "@/shared/api/types";
import {
  ADMIN_PERMISSIONS,
  BOT_ID,
  avitoAccountsPage,
  botDetail,
  brokenScenario,
  resetBotDraft,
  warningScenario,
} from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

/**
 * Валидация редактора (02 §5.2, 11 §5.3): ошибки блокируют сохранение,
 * предупреждения — нет, а серверный `422 bot_scenario_invalid` (истина)
 * рендерится тем же списком со скроллом к проблемному шагу.
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

const BROKEN_REF_MESSAGE = "Шаг ask_phone: ссылка next на несуществующий шаг «tag_contct»";

function renderEditor() {
  return renderBotEditor();
}

describe("Редактор бота — валидация сценария", () => {
  let putBodies: unknown[];
  let scrollSpy: ReturnType<typeof vi.spyOn>;

  const mockApi = (detail: BotDetail, putResponse: () => Response) => {
    putBodies = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "GET") return jsonResponse(200, detail);
        if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "PUT") {
          putBodies.push(JSON.parse(String(init?.body ?? "{}")));
          return putResponse();
        }
        if (url.pathname === `/api/v1/bots/${BOT_ID}/accounts`) return jsonResponse(200, { accounts: [] });
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  };

  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    scrollSpy = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("ошибки бэкенда показываются списком, скроллят к шагу и подсвечивают карточку", async () => {
    const user = userEvent.setup();
    mockApi(botDetail(), () =>
      jsonResponse(
        422,
        errorEnvelope("bot_scenario_invalid", BROKEN_REF_MESSAGE, {
          issues: [
            {
              level: "error",
              code: "broken_ref",
              step_id: "ask_phone",
              field: "next",
              message: BROKEN_REF_MESSAGE,
            },
          ],
        }),
      ),
    );

    const { container } = renderEditor();
    const name = await screen.findByRole("textbox", { name: "Имя бота" });
    await user.type(name, "!"); // черновик стал грязным — «Сохранить» разблокировалось

    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    // Список ошибок под панелью действий — точный текст с сервера.
    const issues = await screen.findByTestId("bot-issues");
    expect(issues).toHaveTextContent(BROKEN_REF_MESSAGE);

    // Карточка проблемного шага подсвечена и раскрыта, к ней проскроллили.
    await waitFor(() => {
      expect(container.querySelector('[data-step-id="ask_phone"]')).toHaveAttribute("data-issue", "error");
    });
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());

    // Карточка раскрыта — форма проблемного шага перед глазами.
    expect(await screen.findByRole("textbox", { name: "ответ →" })).toBeInTheDocument();

    // Повторный клик по строке ошибки скроллит к шагу ещё раз.
    scrollSpy.mockClear();
    await user.click(screen.getByRole("button", { name: BROKEN_REF_MESSAGE }));
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());

    // Кнопка «Сохранить» заблокирована, пока ошибка не исправлена.
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  });

  it("локальный валидатор ловит битую ссылку и не пускает запрос на сервер", async () => {
    const user = userEvent.setup();
    mockApi(botDetail({ scenario: brokenScenario() }), () => jsonResponse(200, botDetail()));

    renderEditor();
    const name = await screen.findByRole("textbox", { name: "Имя бота" });
    await user.type(name, "!");

    expect(await screen.findByTestId("bot-issues")).toHaveTextContent("переход на несуществующий шаг «tag_contct»");
    const saveButton = screen.getByRole("button", { name: "Сохранить" });
    expect(saveButton).toBeDisabled();
    await user.click(saveButton);
    expect(putBodies).toHaveLength(0);
  });

  it("предупреждение не блокирует: кнопка становится «Сохранить с предупреждениями»", async () => {
    const user = userEvent.setup();
    const saved = botDetail({ scenario: warningScenario(), name: "Первичный приём!" });
    mockApi(botDetail({ scenario: warningScenario() }), () => jsonResponse(200, saved));

    renderEditor();
    const name = await screen.findByRole("textbox", { name: "Имя бота" });
    await user.type(name, "!");

    expect(await screen.findByTestId("bot-issues")).toHaveTextContent("бот может ждать вечно");
    await user.click(screen.getByRole("button", { name: "Сохранить с предупреждениями" }));

    await waitFor(() => expect(putBodies).toHaveLength(1));
    expect(putBodies[0]).toMatchObject({ name: "Первичный приём!", knowledge_base: expect.any(String) });
  });
});
