/**
 * Лид-бот → Каналы: второй щелчок не теряет первый (проверка 24.09).
 *
 * Каналы уходят на сервер полным списком. Два быстрых щелчка по разным
 * каналам собирали второй список из ещё не обновлённых данных — без первого
 * канала, и сервер его отвязывал. Флажки не двигались до ответа, что само
 * провоцировало щёлкнуть ещё раз.
 *
 * ДИВЕРСИЯ: убрать `disabled={save.isPending}` у флажков — тест краснеет:
 * второй щелчок уходит во время первого запроса и без первого канала.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { LeadbotTab } from "@/features/settings/leadbot/LeadbotTab";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const OVERVIEW = {
  connection: { url: "http://10.10.0.2:8790", token_set: true, source: "db" },
  is_ready: true,
  enabled: false,
  mode: "suggest" as const,
  context_messages: 30,
  account_ids: [] as string[],
  accounts: [
    { id: "acc-a", title: "Канал А", busy_with_other_bot: false },
    { id: "acc-b", title: "Канал Б", busy_with_other_bot: false },
  ],
};

describe("лид-бот: флажки каналов", () => {
  let patches: string[][];
  let release: () => void;

  beforeEach(() => {
    queryClient.clear();
    patches = [];
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
        if (url.includes("/leadbot/calls")) return jsonResponse(200, { items: [] });
        if (init?.method === "PATCH") {
          const ids = (JSON.parse(String(init.body)) as { account_ids: string[] }).account_ids;
          patches.push(ids);
          // Сервер отвечает не сразу — ровно то окно, в которое щёлкают второй раз.
          await new Promise<void>((resolve) => {
            release = resolve;
          });
          return jsonResponse(200, { ...OVERVIEW, account_ids: ids });
        }
        return jsonResponse(200, OVERVIEW);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("пока канал сохраняется, флажки заперты, а нажатый уже отмечен", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LeadbotTab />);

    const a = await screen.findByRole("checkbox", { name: "Канал А" });
    await user.click(a);
    await waitFor(() => expect(patches).toEqual([["acc-a"]]));

    expect(a).toBeChecked();
    const b = screen.getByRole("checkbox", { name: "Канал Б" });
    expect(b).toBeDisabled();
    await user.click(b);
    expect(patches).toHaveLength(1);

    release();
    await waitFor(() => expect(b).toBeEnabled());
    await user.click(b);
    await waitFor(() => expect(patches).toEqual([["acc-a"], ["acc-a", "acc-b"]]));
    release();
  });
});
