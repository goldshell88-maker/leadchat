/**
 * «Диалоги бота»: листание, загрузка и отказ (проверка 24.09).
 *
 * Сервер отдаёт 50 строк, а в бою диалогов бота 209 за 30 дней: остальные
 * открыть было нельзя, и экран об этом не говорил. При отказе сервера
 * рисовалась пустая таблица с заголовками — не отличить от «данных нет».
 *
 * ДИВЕРСИИ: вернуть `offset: 0` в запрос — краснеет «Дальше»; убрать ветку
 * `isError` — краснеет «отказ словами».
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { BotDialogsPage } from "@/features/bot-dialogs/BotDialogsPage";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const TOTAL = 209;

function row(i: number) {
  return {
    id: `c-${i}`,
    status: "closed",
    client_name: `Клиент ${i}`,
    account_title: "Парт-7",
    assignee_name: null,
    messages_count: 4,
    last_message_at: "2026-09-24T10:00:00Z",
    first_response_sec: 20,
    outcome: { group: null, label: "закрыл сам" },
  };
}

describe("Диалоги бота — листание", () => {
  let offsets: string[];
  let failList: boolean;

  beforeEach(() => {
    offsets = [];
    failList = false;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/bot-dialogs/live")) {
          return jsonResponse(200, { count: 0, items: [] });
        }
        if (url.pathname.endsWith("/bot-dialogs")) {
          if (failList) {
            return jsonResponse(503, errorEnvelope("upstream_unavailable", "База отвечает медленно"));
          }
          const offset = Number(url.searchParams.get("offset") ?? 0);
          offsets.push(String(offset));
          const items = Array.from({ length: Math.min(50, TOTAL - offset) }, (_, k) => row(offset + k));
          return jsonResponse(200, { items, page: { limit: 50, offset, total: TOTAL }, counters: [] });
        }
        if (url.pathname.endsWith("/avito-accounts")) {
          return jsonResponse(200, { items: [], page: { limit: 100, offset: 0, total: 0 } });
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("говорит «из N» и листает дальше", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BotDialogsPage />, { route: "/bot-dialogs" });

    expect(await screen.findByText("1–50 из 209")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Назад" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Дальше" }));

    expect(await screen.findByText("51–100 из 209")).toBeInTheDocument();
    expect(screen.getByText("Клиент 50")).toBeInTheDocument();
    await waitFor(() => expect(offsets).toContain("50"));
  });

  it("отказ сервера — словами и с «Повторить», а не пустая таблица", async () => {
    failList = true;
    renderWithProviders(<BotDialogsPage />, { route: "/bot-dialogs" });

    expect(await screen.findByText("Не получилось загрузить диалоги бота")).toBeInTheDocument();
    expect(screen.getByText("База отвечает медленно")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
