/**
 * Справка по сочетаниям: «?» из любого места, но не из поля ввода.
 *
 * ЗАЧЕМ ТЕСТ ИМЕННО НА ЭТО. Клавиша «?» — обычный печатный знак. Обработчик,
 * не отличающий поле ввода от остального экрана, выбрасывал бы окно поверх
 * текста ровно в тот момент, когда оператор набирает клиенту вопрос. Поймать
 * это глазами трудно: в спокойной проверке в поля не печатают.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { useGlobalHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { useFocusBus } from "@/features/hotkeys/focusBus";

const wrapper = ({ children }: { children: ReactNode }) => (
  <MemoryRouter initialEntries={["/chats"]}>{children}</MemoryRouter>
);

function press(key: string, target?: HTMLElement) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  (target ?? window).dispatchEvent(event);
  return event;
}

describe("Справка по сочетаниям", () => {
  beforeEach(() => {
    useFocusBus.setState({ helpNonce: 0 });
    vi.restoreAllMocks();
  });

  it("«?» просит показать справку", () => {
    renderHook(() => useGlobalHotkeys(), { wrapper });

    press("?");

    expect(useFocusBus.getState().helpNonce).toBe(1);
  });

  it("из поля ввода «?» справку НЕ показывает — это обычный знак вопроса", () => {
    renderHook(() => useGlobalHotkeys(), { wrapper });
    const input = document.createElement("input");
    document.body.appendChild(input);

    press("?", input);

    expect(useFocusBus.getState().helpNonce).toBe(0);
    input.remove();
  });

  it("Ctrl+? — чужое сочетание, справку не открывает", () => {
    renderHook(() => useGlobalHotkeys(), { wrapper });

    const event = new KeyboardEvent("keydown", { key: "?", ctrlKey: true, bubbles: true });
    window.dispatchEvent(event);

    expect(useFocusBus.getState().helpNonce).toBe(0);
  });
});
