/**
 * Кнопка во флекс-строке не сжимается (27.08).
 *
 * ⚠ ЧЕТЫРЕ МЕСТА ОДНОГО ДЕФЕКТА, И ЭТО ПЯТОЕ ЗВЕНО — СТОРОЖ.
 *
 * Механизм, разобранный в client-card.css у `.card-merge__row .lc-btn`, ловил
 * проект уже четырежды: шапка ленты, панель действий, панель объединения и —
 * найдено 27.08 — кнопка «изменить» у имени клиента. Каждый раз одно и то же:
 *
 *   1) у Mantine-кнопки умолчание `flex: 0 1 auto` — она сжимаема;
 *   2) на её корне `overflow: hidden`, а это по спецификации снимает
 *      автоматический минимальный размер флекс-элемента: пол становится нулём;
 *   3) подпись внутри — `white-space: nowrap; overflow: hidden` БЕЗ
 *      `text-overflow`, поэтому лишнее рубится посреди буквы, без многоточия.
 *
 * Сосед с `min-width: 0` делает дележ пропорциональным — и кнопка проигрывает.
 *
 * Проверка статическая, по CSS: у каждой флекс-строки, где рядом с кнопкой
 * стоит текст с `min-width: 0`, кнопке обязан быть задан пол. Пятого места
 * этот сторож не предскажет, но за четырьмя известными следит — и не даст
 * снять правило «за ненадобностью», как уже случалось с комментарием у
 * `.card-name`, который годами утверждал обратное тому, что делал код.
 */
import { describe, expect, it } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]);
// ровно так же поступают соседние сторожа cardRecipe и AccountsWidth.
import { readFileSync } from "node:fs";

/** Файл стилей → селекторы, которым обязан быть задан пол флекса. */
const МЕСТА: ReadonlyArray<readonly [string, string]> = [
  ["src/features/chats/components/card/client-card.css", ".card-merge__row .lc-btn"],
  ["src/features/chats/components/card/client-card.css", ".card-name > button"],
  // Пятое место, найдено разбором дизайна 28.08: подвал пикера шаблонов.
  // Подсказка расползалась на три строки, а «Управлять шаблонами» срезалось.
  ["src/features/chats/components/composer/template-picker.css", ".tpl-popover__foot .lc-btn"],
];

function правило(path: string, selector: string): string {
  const css = (readFileSync(path, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, " ");
  const at = css.indexOf(`\n${selector} {`);
  expect(at, `${path}: правило ${selector} не найдено`).toBeGreaterThan(-1);
  return css.slice(at, css.indexOf("}", at));
}

describe("Кнопка во флекс-строке не сжимается", () => {
  for (const [path, selector] of МЕСТА) {
    it(`${selector} — пол задан`, () => {
      // `flex: none` или `flex-shrink: 0` — обе записи означают одно.
      expect(правило(path, selector)).toMatch(/flex:\s*none|flex-shrink:\s*0/);
    });
  }

  it("у имени клиента остался min-width: 0 — иначе оно выдавит кнопку", () => {
    // Оборотная сторона: снять `min-width` нельзя. Без него длинное имя встаёт
    // на min-content и выталкивает кнопку за край блока — другой дефект, не
    // менее заметный. Поэтому пол задаётся кнопке, а не отбирается у имени.
    expect(
      правило("src/features/chats/components/card/client-card.css", ".card-name .card-section__name"),
    ).toMatch(/min-width:\s*0/);
  });
});
