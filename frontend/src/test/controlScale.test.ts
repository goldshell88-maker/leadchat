// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в railChrome.test.ts и pageTitleCanon.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * ТРИ ВЫСОТЫ КОНТРОЛА, И ВСЕ ТРИ — ИЗ ОДНОГО МЕСТА.
 *
 * НАЙДЕНО ОБХОДОМ 12 августа. Явно заданный размер стоял у 179 контролов из
 * 282 и давал ШЕСТЬ высот; ещё 103 контрола размера не задавали вовсе и
 * добавляли две; самодельные кнопки на голом HTML — девятую. Тема при этом не
 * задавала размер по умолчанию НИ ДЛЯ ЧЕГО: у `Button` и `ActionIcon` в
 * `defaultProps` стоял один радиус, а записи для полей ввода не было вообще.
 * В одном ряду фильтров стояли поле 30, кнопка 22 и кнопка 36.
 *
 * Сведено переопределением собственных переменных Mantine — в двух правилах,
 * а не в 282 местах вызова. Сторож держит именно это: правила на месте, шкала
 * читается из токенов, и своих чисел в них нет.
 *
 * ПОЧЕМУ НЕ РЕНДЕР-ТЕСТ. В vitest стоит `css: false` — в jsdom стилей нет,
 * высоты нулевые. Фактические высоты померены в браузере: 32 у плотных рядов,
 * 40 у главных кнопок, у кнопок-заголовков таблиц — высота их ячейки.
 */

const VARS = readFileSync("src/app/lc-vars.css", "utf-8") as string;
const BASE = readFileSync("src/app/lc-base.css", "utf-8") as string;
const THEME = readFileSync("src/app/theme.tsx", "utf-8") as string;

function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, " ");
}

describe("Шкала контролов объявлена один раз", () => {
  it("три высоты и радиус лежат токенами", () => {
    const css = stripComments(VARS);
    expect(css).toMatch(/--lc-btn-h-sm:\s*32px/);
    expect(css).toMatch(/--lc-btn-h-md:\s*40px/);
    expect(css).toMatch(/--lc-btn-h-lg:\s*48px/);
    expect(css).toMatch(/--lc-btn-radius:/);
  });

  it("кнопки и значки-действия пересажены на неё", () => {
    const rule = stripComments(BASE);
    // Все шесть компактных и обычных размеров Mantine обязаны быть накрыты:
    // пропущенный вернёт на экран седьмую высоту, и заметить это глазами
    // трудно — разница бывает в два пикселя.
    for (const v of [
      "--button-height-compact-xs",
      "--button-height-compact-sm",
      "--button-height-compact-md",
      "--button-height-xs",
      "--button-height-sm",
      "--button-height-md",
      "--ai-size-sm",
      "--ai-size-md",
    ]) {
      expect(rule, `${v} не переопределён — вернётся высота Mantine`).toContain(v);
    }
    // Значок ВНУТРИ поля ввода трогать нельзя: его размер обязан совпадать с
    // высотой поля, а не с высотой кнопки.
    expect(rule).not.toContain("--ai-size-input");
  });

  it("поля ввода пересажены туда же", () => {
    expect(THEME, "тема не навешивает класс на Input").toContain('classNames: { wrapper: "lc-field" }');
    const rule = stripComments(BASE);
    for (const v of ["--input-height-xs", "--input-height-sm", "--input-height-md"]) {
      expect(rule, `${v} не переопределён`).toContain(v);
    }
  });

  it("своих чисел у пересадки нет — только токены", () => {
    const at = stripComments(BASE).indexOf(".lc-field {");
    const field = stripComments(BASE).slice(at, stripComments(BASE).indexOf("}", at));
    // Вертикальные поля — единственное, что задано числом: они зависят от
    // кегля текста, а не от высоты контрола.
    expect(field.match(/--input-height-[a-z]+:\s*\d/), "высота полем задана числом").toBeNull();
  });
});
