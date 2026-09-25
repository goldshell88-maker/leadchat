import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import {
  ADMIN_PERMISSIONS,
  BOT_ID,
  avitoAccountsPage,
  botDetail,
  resetBotDraft,
} from "./botFixtures";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderBotEditor } from "./renderBotEditor";

/**
 * Переименование шага сценария (BOT-04).
 *
 * Занятый идентификатор откатывался молча: поле возвращалось к прежнему
 * значению без единого слова, и это читается как «поле не сохраняется». Здесь
 * проверяется, что отказ называется вслух, а набранное остаётся в поле.
 */

function mockApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const method = init?.method ?? "GET";
      if (url.pathname === "/api/v1/avito-accounts") return jsonResponse(200, avitoAccountsPage());
      if (url.pathname === `/api/v1/bots/${BOT_ID}` && method === "GET") {
        return jsonResponse(200, botDetail());
      }
      if (url.pathname === `/api/v1/bots/${BOT_ID}/accounts`) {
        return jsonResponse(200, { accounts: [] });
      }
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    }),
  );
}

describe("Редактор бота — переименование шага", () => {
  beforeEach(() => {
    queryClient.clear();
    resetBotDraft();
    mockApi();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.spyOn(window.HTMLElement.prototype, "scrollIntoView").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("занятый идентификатор называется вслух, а набранное остаётся в поле", async () => {
    const user = userEvent.setup();
    renderBotEditor();

    const field = await screen.findByLabelText("Идентификатор шага greet");
    await user.clear(field);
    // `ask_problem` — второй шаг сценария по умолчанию, имя занято им.
    await user.type(field, "ask_problem");
    await user.tab();

    expect(await screen.findByText("«ask_problem» уже занят другим шагом")).toBeInTheDocument();
    // Набранное на месте: молчаливый откат к «greet» и был бедой — человек
    // видел прежнее значение и решал, что поле не работает.
    expect(field).toHaveValue("ask_problem");
    // Сценарий при этом не тронут: шаг остался под своим именем, дубликата нет.
    expect(screen.getByLabelText("Идентификатор шага greet")).toBe(field);
  });

  it("пустой идентификатор тоже объясняется, а не гасится", async () => {
    const user = userEvent.setup();
    renderBotEditor();

    const field = await screen.findByLabelText("Идентификатор шага greet");
    await user.clear(field);
    await user.tab();

    expect(await screen.findByText("Идентификатор пустой — на такой шаг не сослаться")).toBeInTheDocument();
  });

  it("правка снимает объяснение, и свободное имя принимается", async () => {
    const user = userEvent.setup();
    renderBotEditor();

    const field = await screen.findByLabelText("Идентификатор шага greet");
    await user.clear(field);
    await user.type(field, "ask_problem");
    await user.tab();
    await screen.findByText("«ask_problem» уже занят другим шагом");

    // Первая же правка гасит подпись: висящая ошибка под исправленным полем
    // врёт не меньше молчания.
    await user.type(field, "_2");
    expect(screen.queryByText("«ask_problem» уже занят другим шагом")).toBeNull();

    await user.tab();
    expect(await screen.findByLabelText("Идентификатор шага ask_problem_2")).toBeInTheDocument();
  });
});
