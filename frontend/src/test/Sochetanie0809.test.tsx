/**
 * ОБЕЩАННАЯ КЛАВИША ОБЯЗАНА БЫТЬ ПРИВЯЗАНА (аудит 08.09, H-06).
 *
 * ЧТО БЫЛО. Приложение обещало «Ctrl+K» в ПЯТИ местах: подсказка значка поиска
 * в рельсе, его `aria-label`, значок-клавиша в самой рельсе, значок в поле
 * поиска и совет на пустом экране ленты. При этом действие `search` объявлено
 * `offByDefault: true` — по умолчанию оно не привязано ни к чему и не
 * срабатывает. Человек читал подпись, нажимал и делал единственный доступный
 * вывод: сломалось.
 *
 * ВТОРАЯ ПОЛОВИНА БЕДЫ. Сочетания переназначаются в профиле. Зашитая строка
 * врала и тому, кто перевёл поиск на другую клавишу.
 *
 * Поэтому стерегутся ТРИ состояния, а не одно: выключено (умолчание), включено
 * штатной клавишей, переназначено своей. Проверка только третьего прошла бы и
 * на зашитой строке.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { AppRail } from "@/app/AppRail";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const ПРАВА = ["conversations:read", "messages:send", "stats:own", "templates:own"];

function рельса(hotkeys: Record<string, string[]>) {
  queryClient.clear();
  resetSessionStore({
    user: fakeUser,
    permissions: ПРАВА as never,
    accessToken: "t",
    bootstrapped: true,
    hotkeys,
  });
  return renderWithProviders(<AppRail />, { route: "/chats" });
}

/** Кнопка поиска — ищем по началу доступного имени, а не по всей строке. */
function кнопкаПоиска(): HTMLElement {
  const кнопка = screen
    .getAllByRole("button")
    .find((el) => (el.getAttribute("aria-label") ?? "").startsWith("Поиск по диалогам"));
  expect(кнопка, "кнопки поиска в рельсе нет").toBeTruthy();
  return кнопка!;
}

describe("Подпись сочетания идёт за привязкой, а не за строкой в коде", () => {
  beforeEach(() => {
    queryClient.clear();
  });

  it("по умолчанию поиск не обещает никакой клавиши", () => {
    рельса({});
    const кнопка = кнопкаПоиска();
    expect(
      кнопка.getAttribute("aria-label"),
      "рельса обещает клавишу, которая по умолчанию не привязана",
    ).toBe("Поиск по диалогам");
    expect(кнопка.getAttribute("data-tip")).toBe("Поиск по диалогам");
    expect(
      кнопка.querySelector(".lc-rail__key"),
      "значок клавиши нарисован, хотя клавиши нет",
    ).toBeNull();
  });

  it("со штатной привязкой показывает её", () => {
    рельса({ search: ["Mod+KeyK"] });
    const кнопка = кнопкаПоиска();
    expect(кнопка.getAttribute("aria-label")).toBe("Поиск по диалогам — Ctrl + K");
    expect(кнопка.querySelector(".lc-rail__key")?.textContent).toBe("Ctrl + K");
  });

  it("с переназначенной привязкой показывает ЕЁ, а не зашитую Ctrl+K", () => {
    /*
     * ⚠ ЭТА ПРОВЕРКА — ГЛАВНАЯ. Первые две пройдут и у кода, который просто
     * прячет зашитую строку при пустых настройках. Здесь видно, что подпись
     * СПРАШИВАЕТСЯ у того же источника, у которого спрашивает разбор нажатия.
     */
    рельса({ search: ["Alt+KeyF"] });
    const кнопка = кнопкаПоиска();
    expect(кнопка.getAttribute("aria-label")).toBe("Поиск по диалогам — Alt + F");
    expect(кнопка.querySelector(".lc-rail__key")?.textContent).toBe("Alt + F");
    expect(document.body.textContent).not.toMatch(/Ctrl\s*\+?\s*K/);
  });

  it("выключенное самим человеком сочетание подписи не даёт", () => {
    /*
     * Пустой список — это «выключил сам» (см. `действующие` в catalog.ts), и
     * он обязан читаться так же, как умолчание: клавиши нет — обещания нет.
     */
    рельса({ search: [] });
    expect(кнопкаПоиска().getAttribute("aria-label")).toBe("Поиск по диалогам");
  });
});
