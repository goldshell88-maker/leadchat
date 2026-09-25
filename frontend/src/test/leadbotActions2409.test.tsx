/**
 * Лид-бот: отказ ручки не молчит, «Открыть диалог» — переход роутером (проверка 24.09).
 *
 * «Проверить связь», «Спросить лид-бота», «Вернуть как при установке» при
 * HTTP-отказе нашей ручки (выкатка, 429, истёкшая сессия) гасили крутилку и
 * больше ничего не показывали. «Открыть диалог» был обычной `<a href>`:
 * вместо перехода перезагружалось всё приложение.
 *
 * ДИВЕРСИИ: убрать `onError` из `useProbe` — краснеет «Проверить связь»;
 * вернуть `<a href>` — краснеет «переход роутером» (клик больше не
 * отменяется по умолчанию).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { LeadbotTab } from "@/features/settings/leadbot/LeadbotTab";
import { showToast } from "@/shared/ui/toast";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

const OVERVIEW = {
  connection: { url: "http://10.10.0.2:8790", token_set: true, source: "db" },
  is_ready: true,
  enabled: false,
  mode: "suggest" as const,
  context_messages: 30,
  account_ids: [],
  accounts: [],
};

const CALL = {
  id: "1",
  at: "2026-09-24T10:00:00Z",
  conversation_id: "conv-7",
  account_id: null,
  request_id: null,
  question: "Сколько стоит?",
  reply: "От 1500",
  layer: "роутер",
  flag: null,
  confidence: 1,
  needs_operator: false,
  outcome: "sent",
  outcome_label: "Ответ ушёл клиенту",
  escalation: null,
  lead_ready: false,
  warnings: [],
  ms: 4,
  error: null,
};

describe("лид-бот: отказы и переходы", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.mocked(showToast).mockClear();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["bots:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/leadbot/probe") && init?.method === "POST") {
          return jsonResponse(502, errorEnvelope("upstream_unavailable", "Сервер перезапускается"));
        }
        if (url.includes("/leadbot/calls")) return jsonResponse(200, { items: [CALL] });
        return jsonResponse(200, OVERVIEW);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("«Проверить связь» при отказе ручки говорит, что не получилось", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LeadbotTab />, { route: "/settings/leadbot" });
    await user.click(await screen.findByRole("button", { name: /Проверить связь/ }));

    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const toast = vi.mocked(showToast).mock.calls.at(-1)![0] as { title?: string; message?: string };
    expect(toast.title).toBe("Не получилось: Проверка связи с лид-ботом");
    expect(toast.message).toContain("Сервер перезапускается");
  });

  it("«Открыть диалог» — переход роутером, без перезагрузки страницы", async () => {
    renderWithProviders(<LeadbotTab />, { route: "/settings/leadbot" });
    const link = await screen.findByRole("link", { name: "Открыть диалог" });
    expect(link).toHaveAttribute("href", "/chats/conv-7");
    // Ссылка роутера отменяет переход браузера и идёт своим путём.
    expect(fireEvent.click(link)).toBe(false);
  });
});
