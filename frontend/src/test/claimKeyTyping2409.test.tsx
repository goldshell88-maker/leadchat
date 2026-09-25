import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { useClaimKeyGuard } from "@/features/hotkeys/useChatHotkeys";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * КЛАВИША ПРИЁМА НЕ ОТНИМАЕТ КЛАВИШУ У ПОЛЯ И У ОКНА (проверка 24.09).
 *
 * У четырёх сотрудников приём назначен на голый Tab. Заслон приёма висит на
 * перехвате по всему приложению и гасил клавишу, не спросив, где она нажата:
 * Tab в поле пароля не переводил фокус, а забирал из очереди чужой диалог и
 * уводил человека в переписку постороннего клиента. В окне Tab не работал
 * вовсе.
 *
 * Правило то же, что у всех сочетаний: голое сочетание при наборе не
 * срабатывает, а поверх окна клавиша принадлежит окну. Ctrl+R при наборе
 * по-прежнему принимает — ради этого заслон и заведён.
 */

function Guard({ dialog }: { dialog: boolean }) {
  useClaimKeyGuard();
  return (
    <>
      <input aria-label="Пароль" />
      <button type="button">Кнопка</button>
      {dialog && (
        <div role="dialog">
          <input aria-label="Поле окна" />
        </div>
      )}
    </>
  );
}

function press(target: HTMLElement, init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

describe("Клавиша приёма и поле ввода", () => {
  const urls: string[] = [];

  beforeEach(() => {
    urls.length = 0;
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  function renderWith(claim: string[], dialog = false) {
    useSessionStore.setState({ hotkeys: { claim } });
    return renderWithProviders(<Guard dialog={dialog} />);
  }

  it("голый Tab в поле ввода переводит фокус и ничего не принимает", () => {
    const { getByLabelText } = renderWith(["Tab"]);

    const event = press(getByLabelText("Пароль"), { key: "Tab", code: "Tab" });

    expect(event.defaultPrevented).toBe(false);
    expect(urls).toEqual([]);
  });

  it("голый Tab поверх окна остаётся окну", () => {
    const { getByLabelText } = renderWith(["Tab"], true);

    const event = press(getByLabelText("Поле окна"), { key: "Tab", code: "Tab" });

    expect(event.defaultPrevented).toBe(false);
    expect(urls).toEqual([]);
  });

  it("вне поля и окна голый Tab принимает, как назначил человек", async () => {
    const { getByText } = renderWith(["Tab"]);

    const event = press(getByText("Кнопка"), { key: "Tab", code: "Tab" });

    expect(event.defaultPrevented).toBe(true);
    await waitFor(() => expect(urls.some((u) => u.includes("/inbox"))).toBe(true));
  });

  it("Ctrl+R при наборе по-прежнему принимает и не перезагружает страницу", async () => {
    const { getByLabelText } = renderWith(["Mod+KeyR"]);

    const event = press(getByLabelText("Пароль"), { key: "r", code: "KeyR", ctrlKey: true });

    expect(event.defaultPrevented).toBe(true);
    await waitFor(() => expect(urls.some((u) => u.includes("/inbox"))).toBe(true));
  });

  it("Ctrl+R поверх окна гасится, но диалог не принимается", () => {
    const { getByLabelText } = renderWith(["Mod+KeyR"], true);

    const event = press(getByLabelText("Поле окна"), { key: "r", code: "KeyR", ctrlKey: true });

    expect(event.defaultPrevented).toBe(true);
    expect(urls).toEqual([]);
  });
});
