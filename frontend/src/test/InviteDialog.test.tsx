import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { InviteDialog } from "@/features/chats/components/card/InviteDialog";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, renderWithProviders } from "./render";

/**
 * «Позвать в диалог» (docs/19).
 *
 * Проверяется не «уходит ли запрос». Проверяется, ЧТО ЧЕЛОВЕК ПОНИМАЕТ,
 * нажимая кнопку. «Позвать» и «Передать» стоят рядом, открывают одинаковый
 * список сотрудников и различаются одним словом — а последствия разные: после
 * передачи диалог уходит вместе с ответственностью. Если окно не сказало об
 * этом до нажатия, оператор узнает разницу постфактум, потеряв диалог.
 */

const ASSIGNABLE = {
  items: [
    { id: "u-petr", full_name: "Пётр Ковалёв", role: "manager", is_online: true },
    { id: "u-oleg", full_name: "Олег Иванов", role: "manager", is_online: false },
  ],
};

describe("Позвать коллегу в диалог", () => {
  const calls: Array<{ url: string; method: string; body: Record<string, unknown> }> = [];

  beforeEach(() => {
    calls.length = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({
          url,
          method: init?.method ?? "GET",
          body: init?.body ? JSON.parse(String(init.body)) : {},
        });
        if (url.includes("/users/assignable")) return jsonResponse(200, ASSIGNABLE);
        if (url.includes("/participants")) {
          return jsonResponse(200, {
            participants: [
              { id: "u-petr", full_name: "Пётр Ковалёв", reason: null, invited_at: null },
            ],
          });
        }
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const open = (props: Partial<React.ComponentProps<typeof InviteDialog>> = {}) =>
    renderWithProviders(
      <InviteDialog
        convId={CONV_ID}
        opened
        currentAssigneeId={fakeUser.id}
        alreadyIn={[]}
        onClose={() => {}}
        {...props}
      />,
    );

  it("говорит, что диалог остаётся за вами", async () => {
    // Главное отличие от «Передать», и единственное место, где о нём можно
    // прочитать до нажатия. Без этой фразы две соседние кнопки выглядят
    // синонимами.
    open();
    expect(await screen.findByText(/Диалог останется за вами/)).toBeInTheDocument();
  });

  it("отправляет приглашение вместе с причиной", async () => {
    // Причина едет в уведомлении. Без неё позванный открывает чужой диалог,
    // чтобы выяснить, что от него хотели.
    const user = userEvent.setup();
    open();

    await user.click(await screen.findByRole("radio", { name: /Пётр Ковалёв/ }));
    await user.type(screen.getByLabelText("Зачем зовёте"), "чинится ли эта модель");
    await user.click(screen.getByRole("button", { name: "Позвать" }));

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/participants"));
      expect(post?.body).toEqual({ user_id: "u-petr", reason: "чинится ли эта модель" });
    });
  });

  it("не даёт позвать ответственного и уже позванных", async () => {
    // Ответственный уже здесь, позванный — тоже. Строки гасим, а не прячем:
    // исчезнувшее из списка имя читается как «сотрудника уволили».
    open({ currentAssigneeId: "u-oleg", alreadyIn: ["u-petr"] });

    expect(await screen.findByRole("radio", { name: /Пётр Ковалёв/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Олег Иванов/ })).toBeDisabled();
  });

  it("без выбранного человека кнопка не активна", async () => {
    open();
    expect(await screen.findByRole("button", { name: "Позвать" })).toBeDisabled();
  });

  it("приглашение не трогает ответственного", async () => {
    // Диагностика на случай, если кто-то однажды «упростит» приглашение до
    // вызова assign: тогда диалог начнёт уходить, а тест — краснеть.
    const user = userEvent.setup();
    open();

    await user.click(await screen.findByRole("radio", { name: /Пётр Ковалёв/ }));
    await user.click(screen.getByRole("button", { name: "Позвать" }));

    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/participants"))).toBe(true));
    expect(calls.some((c) => c.url.endsWith("/assign"))).toBe(false);
  });
});
