// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно. Так же сделано в
// breakpoints.test.ts, cssDeadClasses.test.ts и headerNarrow.test.ts.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { HotkeysModal } from "@/features/hotkeys/HotkeysModal";
import { hasMoreBelow } from "@/features/hotkeys/scrollHint";
import { hotkeysFor } from "@/features/hotkeys/catalog";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ОКНО СПРАВКИ ОБРЕЗАЛО ПОСЛЕДНИЕ СТРОКИ БЕЗ ВИДИМОГО ПРИЗНАКА.
 *
 * ЧТО БЫЛО. При окне 1440×600 замер такой: `.mantine-Modal-content`
 * clientHeight 538 при scrollHeight 623. Строки «Esc» (y=565) и «?» (y=597)
 * оказывались ниже нижней границы окна (bottom=570). Прокрутка формально
 * работала — `overflow-y: auto`, — но полоса прокрутки в macOS наложенная:
 * замер ширины отведённого ей места дал 0 пикселей. Список выглядел
 * законченным, то есть человек, искавший «чем закрыть», уходил ни с чем.
 *
 * ЧЕГО ЭТОТ ФАЙЛ НЕ ПРОВЕРЯЕТ И ПОЧЕМУ. Самого наложения — в jsdom размеров
 * нет (`scrollHeight` всегда 0) и стили не применяются (`css: false`), так
 * что «две строки за краем» тут не воспроизвести ничем. Дефект и нашли
 * замером в браузере. Здесь держатся три вещи, которые машине проверяемы:
 * арифметика «есть ли ниже ещё», разметка с отдельным прокручиваемым списком
 * и правила, которые эту прокрутку задают. Живая проверка после правки:
 * окно перестало прокручиваться целиком (clientHeight 538 = scrollHeight
 * 538), прокручивается список (379 из 463), затемнение горит при `data-more`
 * и гаснет в конце, последняя строка «?» ложится ровно в нижнюю границу
 * списка.
 */

/** Путь относительный: vitest запускается из каталога frontend. */
const CSS = readFileSync("src/features/hotkeys/hotkeys-modal.css", "utf-8") as string;

describe("Есть ли что-то ниже видимой части списка", () => {
  it("список выше окна — да, ниже ещё есть", () => {
    expect(hasMoreBelow({ scrollTop: 0, clientHeight: 379, scrollHeight: 463 })).toBe(true);
  });

  it("прокручен до конца — нет", () => {
    expect(hasMoreBelow({ scrollTop: 84, clientHeight: 379, scrollHeight: 463 })).toBe(false);
  });

  it("список целиком помещается — нет", () => {
    expect(hasMoreBelow({ scrollTop: 0, clientHeight: 600, scrollHeight: 463 })).toBe(false);
  });

  it("дробный хвост в полпикселя за «ещё есть» не считается", () => {
    // Экран с масштабом 125% (обычный ноутбук) даёт дробные высоты. Без
    // допуска затемнение горело бы на самом конце списка — подсказка «ниже
    // ещё есть» там, где ниже уже ничего нет.
    expect(hasMoreBelow({ scrollTop: 83.5, clientHeight: 379.2, scrollHeight: 463 })).toBe(false);
  });

  it("списка ещё нет (окно закрыто) — нет", () => {
    expect(hasMoreBelow(null)).toBe(false);
  });
});

/** Менеджер: полный набор прав рабочего места — видит все строки справки. */
const MANAGER: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "notes:read",
  "notes:write",
  "templates:own",
  "stats:own",
];

describe("Разметка окна справки", () => {
  it("список лежит в отдельном прокручиваемом блоке, а не в теле окна", () => {
    resetSessionStore({ user: fakeUser, permissions: MANAGER, bootstrapped: true });
    const { container } = renderWithProviders(<HotkeysModal opened onClose={() => {}} />);
    const box = container.ownerDocument.querySelector(".hotkeys-modal__scroll");
    expect(box).not.toBeNull();
    // Все сочетания, доступные этому человеку, — внутри него: половина списка
    // снаружи означала бы, что прокручивается опять окно целиком.
    expect(box?.querySelectorAll("tr").length).toBe(hotkeysFor((p) => MANAGER.includes(p)).length);
    // Последняя строка каталога — та самая, что уезжала за нижний край.
    expect(screen.getByText("Эта справка — из любого места")).toBeTruthy();
  });

  it("вводная фраза остаётся на месте, прокручивается только список", () => {
    const { container } = renderWithProviders(<HotkeysModal opened onClose={() => {}} />);
    const box = container.ownerDocument.querySelector(".hotkeys-modal__scroll");
    expect(box?.textContent).not.toContain("переучиваться не нужно");
  });
});

describe("Стили окна справки", () => {
  it("прокрутку ведёт список, а не окно", () => {
    expect(CSS).toMatch(/\.hotkeys-modal__content\s*\{[^}]*overflow:\s*hidden/);
    expect(CSS).toMatch(/\.hotkeys-modal__scroll\s*\{[^}]*overflow-y:\s*auto/);
  });

  it("флекс-детям разрешено сжиматься ниже содержимого", () => {
    // Без `min-height: 0` флекс-ребёнок не станет ниже своего содержимого
    // (умолчание `min-height: auto`) — список не сожмётся, и окно снова
    // полезет за край экрана. Тот же капкан, что у шапки в app-layout.css.
    expect(CSS).toMatch(/\.hotkeys-modal__body\s*\{[^}]*min-height:\s*0/);
    expect(CSS).toMatch(/\.hotkeys-modal__list\s*\{[^}]*min-height:\s*0/);
  });

  it("затемнение показывается только когда ниже правда что-то есть", () => {
    // Вечная полоска над последней строкой врала бы в обратную сторону:
    // «ниже ещё есть» там, где список кончился.
    expect(CSS).toMatch(/\.hotkeys-modal__list\[data-more\]::after/);
    expect(CSS).not.toMatch(/\.hotkeys-modal__list::after/);
  });
});
