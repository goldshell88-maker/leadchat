import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TablePage } from "@/features/table/TablePage";
import type { TableRow } from "@/features/table/api";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * «КАНАЛ: ВСЕ» У МЕНЕДЖЕРА НЕ ПУСТ (проверка 24.09).
 *
 * Варианты фильтра брались только из справочника `/avito-accounts`, а он
 * закрыт правом `accounts:read`, которого у менеджера и наблюдателя нет.
 * «Разбор диалогов» открыт всем ролям, и у них выбрать канал было не из чего.
 */

function row(id: string, accountId: string, accountTitle: string): TableRow {
  return {
    id,
    status: "in_progress",
    client_name: "Павел Ушаков",
    client_phone: null,
    assignee_id: null,
    assignee_name: null,
    item_title: null,
    account_id: accountId,
    account_title: accountTitle,
    tags: [],
    bot_active: false,
    unread_count: 0,
    last_message_at: "2026-09-24T10:00:00Z",
    messages_count: 2,
    first_response_sec: 60,
    duration_sec: 120,
  };
}

describe("Фильтр «Канал» в «Разборе диалогов»", () => {
  let accountsAsked = false;

  beforeEach(() => {
    queryClient.clear();
    accountsAsked = false;
    resetSessionStore({
      user: { ...fakeUser, role: "manager" },
      permissions: ["conversations:read", "dialogs:read", "messages:send"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/conversations/table")) {
          return jsonResponse(200, {
            items: [row("c-1", "acc-1", "Парт-7"), row("c-2", "acc-2", "Парт-900")],
            page: { limit: 50, offset: 0, total: 2 },
          });
        }
        if (url.pathname.endsWith("/avito-accounts")) accountsAsked = true;
        return jsonResponse(403, { error: { code: "forbidden", message: "нет права" } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("у менеджера варианты собраны из строк таблицы", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TablePage />, { route: "/dialogs" });
    await screen.findAllByText("Павел Ушаков");

    await user.click(screen.getAllByLabelText("Фильтр по каналу")[0]);

    expect(await screen.findByRole("option", { name: "Парт-7" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Парт-900" })).toBeInTheDocument();
    expect(accountsAsked).toBe(false);
  });

  it("канал из ссылки виден в поле, даже когда его нет ни в строках, ни в справочнике", async () => {
    renderWithProviders(<TablePage />, { route: "/dialogs?account=acc-9" });
    await screen.findAllByText("Павел Ушаков");

    expect(screen.getAllByLabelText("Фильтр по каналу")[0]).toHaveValue("Канал из ссылки");
  });
});
