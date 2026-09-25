import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { PermissionsBanner } from "@/app/PermissionsBanner";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { actionFor } from "@/features/hotkeys/dispatch";
import { HotkeysModal } from "@/features/hotkeys/HotkeysModal";
import { HotkeysEditor } from "@/features/settings/profile/HotkeysEditor";
import type { Permission } from "@/shared/auth/usePermissions";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ГОРЯЧИЕ КЛАВИШИ — ПРОВЕРКА 24.09.
 *
 *  1. «?» не открывал справку ни в одной раскладке: разбор нажатия писал
 *     «Shift+?», а в реестре стоит «?».
 *  2. В редакторе две строки назывались «—», а тост и экранный диктор — «listPrev».
 *  3. «Включить» возвращал сочетание, уже отданное другому действию: две строки
 *     с одной клавишей, срабатывала верхняя.
 *  4. Справка давала строкам ключ по подписи — одиннадцать одинаковых «выключено».
 *  5. Полоса «Восстанавливаем ваши права» не возвращала личные сочетания.
 */

const PERMISSIONS: Permission[] = ["conversations:read", "messages:send", "conversations:manage", "notes:write"];

function press(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", init);
}

function row(label: string | RegExp): HTMLElement {
  const tr = screen.getByText(label).closest("tr");
  if (!tr) throw new Error("строка не найдена");
  return tr;
}

describe("Клавиша «?»", () => {
  it.each([
    ["английская раскладка", "Slash"],
    ["русская раскладка", "Digit7"],
  ])("настоящее нажатие открывает справку — %s", (_layout, code) => {
    expect(actionFor(press({ code, key: "?", shiftKey: true }), false)?.id).toBe("help");
  });

  it("при наборе текста «?» остаётся знаком", () => {
    expect(actionFor(press({ code: "Slash", key: "?", shiftKey: true }), true)).toBeNull();
  });
});

describe("Редактор клавиш", () => {
  beforeEach(() => {
    resetSessionStore({ user: fakeUser, permissions: PERMISSIONS, accessToken: "t", bootstrapped: true });
    useSessionStore.setState({ hotkeys: {} });
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { hotkeys: {} })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("у каждой строки своё имя — без «—» и без кодов действий", () => {
    renderWithProviders(<HotkeysEditor />);

    expect(screen.queryByText("—")).toBeNull();
    expect(row("Предыдущий диалог").textContent).toContain("Ctrl + ↑");
    expect(
      within(row("Предыдущий диалог")).getByRole("button", { name: "Изменить сочетание: Предыдущий диалог" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /listPrev|unreadPrev/ })).toBeNull();
  });

  it("«включить» забирает своё сочетание у действия, которому его отдали", () => {
    renderWithProviders(<HotkeysEditor />);

    // Ctrl+R отдан «Закрыть диалог» — «Принять» остался без клавиши.
    const close = row(/^Закрыть диалог/);
    const edit = within(close).getByRole("button", { name: /Изменить сочетание/ });
    fireEvent.click(edit);
    fireEvent.keyDown(edit, { code: "KeyR", key: "r", ctrlKey: true });
    expect(row(/^Принять диалог/).textContent).toContain("выключено");

    fireEvent.click(within(row(/^Принять диалог/)).getByRole("switch"));

    expect(row(/^Принять диалог/).textContent).toContain("Ctrl + R");
    expect(row(/^Закрыть диалог/).textContent).not.toContain("Ctrl + R");
  });
});

describe("Справка по клавишам", () => {
  it("строки не делят ключ по подписи «выключено»", () => {
    resetSessionStore({ user: fakeUser, permissions: PERMISSIONS, accessToken: "t", bootstrapped: true });
    useSessionStore.setState({ hotkeys: {} });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});

    renderWithProviders(<HotkeysModal opened onClose={() => {}} />);

    expect(screen.getAllByText("выключено").length).toBeGreaterThan(1);
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key/);
    errors.mockRestore();
  });
});

describe("Восстановление прав полосой", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("возвращает и личные сочетания клавиш", async () => {
    const hotkeys = { close: [] as string[], claim: ["Mod+KeyY"] };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { ...fakeUser, permissions: PERMISSIONS, hotkeys })),
    );
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    useSessionStore.setState({ hotkeys: {} });

    render(
      <QueryClientProvider client={queryClient}>
        <MantineProvider theme={theme} defaultColorScheme="light">
          <PermissionsBanner />
        </MantineProvider>
      </QueryClientProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });

    expect(useSessionStore.getState().permissions).toEqual(PERMISSIONS);
    expect(useSessionStore.getState().hotkeys).toEqual(hotkeys);
  });
});
