/**
 * Смена своего пароля: форма есть и говорит с сервером его словами.
 *
 * ⚠ ДЕФЕКТ АУДИТА FUNC-82, ЗАКРЫТ 05.09. Ручка `POST /auth/change-password`
 * была написана, покрыта тестами и умела всё нужное — спрашивала текущий
 * пароль и обрывала остальные сессии. А формы во фронте не было ни одной:
 * грепом по `src` эта строка встречалась только в комментарии профиля, который
 * сам себя и объяснял.
 *
 * Цена: на тринадцать человек «сменить пароль» означало переписку с
 * администратором на полдня — то есть пароли не меняли вовсе. И серверный отказ
 * «Свой пароль меняют в профиле — с вводом текущего» указывал в пустоту.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChangePasswordForm } from "@/features/settings/profile/ChangePasswordForm";
import { jsonResponse } from "./helpers";
import { renderWithProviders } from "./render";

describe("Смена своего пароля", () => {
  let отправлено: { url: string; body: unknown } | null = null;

  beforeEach(() => {
    отправлено = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        отправлено = { url: String(input), body: JSON.parse(String(init?.body ?? "{}")) };
        return jsonResponse(200, { status: "ok" });
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it("отправляет текущий и новый пароль на серверную ручку", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChangePasswordForm />);

    await user.type(screen.getByLabelText("Текущий пароль"), "старый-пароль");
    await user.type(screen.getByLabelText("Новый пароль"), "новый-пароль-1");
    await user.click(screen.getByRole("button", { name: "Сменить пароль" }));

    await waitFor(() => expect(отправлено).not.toBeNull());
    expect(отправлено!.url).toContain("/auth/change-password");
    expect(отправлено!.body).toEqual({
      current_password: "старый-пароль",
      new_password: "новый-пароль-1",
    });
  });

  it("короткий пароль не уходит на сервер — правило то же, что у него", async () => {
    /*
     * Десять знаков — правило СЕРВЕРА (app/schemas/auth.py: min_length=10).
     * Разойдись мы с ним, получили бы отказ, которого человек не ждал; здесь
     * оно повторено, чтобы он узнал о нём ДО отправки.
     */
    const user = userEvent.setup();
    renderWithProviders(<ChangePasswordForm />);

    await user.type(screen.getByLabelText("Текущий пароль"), "старый-пароль");
    await user.type(screen.getByLabelText("Новый пароль"), "коротко");

    expect(screen.getByRole("button", { name: "Сменить пароль" })).toBeDisabled();
    expect(отправлено, "короткий пароль всё-таки ушёл на сервер").toBeNull();
  });

  it("без текущего пароля отправить нельзя", async () => {
    /*
     * ⚠ ГРАНИЦА ВАЖНЕЕ УДОБСТВА. Сессия живёт долго, а незапертый ноутбук в
     * офисе — обычное дело: без текущего пароля любой, кто подошёл к чужому
     * столу, запер бы коллегу из системы. Правило держит сервер, здесь оно
     * лишь показано.
     */
    const user = userEvent.setup();
    renderWithProviders(<ChangePasswordForm />);

    await user.type(screen.getByLabelText("Новый пароль"), "достаточно-длинный");
    expect(screen.getByRole("button", { name: "Сменить пароль" })).toBeDisabled();
  });

  it("отказ сервера показывается ЕГО словами, а не своими", async () => {
    /*
     * Сервер различает «неверный текущий» и «новый совпадает со старым», и
     * лечатся эти два случая по-разному. Своё «не получилось» стёрло бы разницу.
     */
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(422, {
          error: {
            code: "unprocessable",
            message: "Текущий пароль неверный",
            details: { reason: "wrong_current_password" },
          },
        }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<ChangePasswordForm />);

    await user.type(screen.getByLabelText("Текущий пароль"), "не-тот-пароль");
    await user.type(screen.getByLabelText("Новый пароль"), "новый-пароль-1");
    await user.click(screen.getByRole("button", { name: "Сменить пароль" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Текущий пароль неверный");
  });
});
