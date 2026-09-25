// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в railChrome.test.ts и controlScale.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * ОДИН РЕЦЕПТ КАРТОЧКИ.
 *
 * НАЙДЕНО ОБХОДОМ 12 августа: 129 объявлений `border-radius` в `.css` давали
 * ДЕВЯТНАДЦАТЬ рецептов поверхности на шести радиусах. Шесть отличий из них
 * осмысленны и сведению не подлежат — смысловые плашки (заметка, предупреждение,
 * ошибка), всплывающие поверхности с обязательной тенью, капсулы и чипы,
 * прозрачная строка списка диалогов с тремя признаками выбора. Остальное
 * расходилось без записанной причины.
 *
 * РАДИУС КАРТОЧКИ — 16, РАДИУС КОНТРОЛА — 12. Так объявляет тема (`Paper` →
 * `lg`), и так же читается на экране: пока карточка рисовалась двенадцатым,
 * форма переставала различать поверхность и элемент управления — карточка и
 * кнопка внутри неё имели одинаковые углы.
 *
 * Замер после правки (девять экранов в браузере): на живых страницах остался
 * ОДИН рецепт крупной поверхности — 16px + `--lc-bg-0` + рамка, — плюс две
 * названные выше осмысленные: янтарная плашка канала и прозрачная строка
 * списка.
 */

/** Карточки-поверхности: файл, селектор. */
const КАРТОЧКИ: ReadonlyArray<readonly [string, string]> = [
  ["src/features/stats/stats.css", ".stat-card"],
  ["src/features/stats/stats.css", ".stats-panel"],
  // ⚠ 27.08 КАНАЛЫ СТАЛИ СПИСКОМ, И ПОВЕРХНОСТЬ ПЕРЕЕХАЛА (решение владельца:
  // «аккаунты давай не плитками сделаем, а просто списком»). Крупная
  // поверхность здесь теперь одна — контейнер списка: у него рамка и радиус.
  // У строки внутри ни того, ни другого быть не должно, иначе рамки соседних
  // строк складываются в двойную линию, а радиус рисует ступеньки на стыках.
  // Правило рецепта не ослаблено — оно проверяется там, где поверхность.
  ["src/features/settings/accounts/accounts.css", ".accounts-page__grid"],
  ["src/features/settings/accounts/accounts.css", ".channel-operators"],
  ["src/features/table/table.css", ".dt__scroll"],
  ["src/features/settings/team/team.css", ".audit__scroll"],
  ["src/features/updates/updates.css", ".updates__release"],
];

function rule(path: string, selector: string): string {
  const css = (readFileSync(path, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, " ");
  const at = css.indexOf(`\n${selector} {`);
  expect(at, `${path}: правило ${selector} не найдено`).toBeGreaterThan(-1);
  return css.slice(at, css.indexOf("}", at));
}

describe("Крупная поверхность рисуется одним рецептом", () => {
  for (const [path, selector] of КАРТОЧКИ) {
    it(`${selector} — радиус карточки, а не контрола`, () => {
      expect(rule(path, selector), `${selector}: радиус контрола на карточке`).toContain(
        "--lc-radius-lg",
      );
    });

    it(`${selector} — рамка есть`, () => {
      // Без рамки карточка на фоне того же цвета исчезает: ровно так и жила
      // обёртка таблицы в настройках — прокручивалась, но поверхности не имела.
      expect(rule(path, selector)).toMatch(/border(-top)?:\s*1px solid var\(--lc-border\)/);
    });
  }

  it("соседи одного ранга в песочнице залиты одинаково", () => {
    // В тёмной теме #080a09 и #0b0e0d почти неразличимы, поэтому расхождение
    // жило годами; в светлой это белое против серо-голубого.
    const chat = rule("src/features/settings/bots/bots.css", ".sandbox-chat");
    const state = rule("src/features/settings/bots/bots.css", ".sandbox-state");
    const фон = (r: string) => r.match(/background:\s*var\((--lc-[a-z0-9-]+)\)/)?.[1];
    expect(фон(state), "панели одного ранга залиты разными токенами").toBe(фон(chat));
  });

  it("предупреждение везде нарисовано одним токеном", () => {
    // `--lc-warn-soft` — полупрозрачная смесь: она берёт оттенок у того, что
    // под ней, и одна и та же плашка выглядела по-разному на разных фонах.
    const css = (readFileSync("src/features/settings/accounts/accounts.css", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ");
    expect(css).not.toContain("--lc-warn-soft");
  });

  it("витрина показывает те же радиусы, что продукт", () => {
    const uikit = (readFileSync("src/features/uikit/uikit.css", "utf-8") as string).replace(
      /\/\*[\s\S]*?\*\//g,
      " ",
    );
    for (const s of [".uikit__list", ".uikit__thread"]) {
      const at = uikit.indexOf(`\n${s} {`);
      expect(uikit.slice(at, uikit.indexOf("}", at)), `${s} расходится с продуктом`).toContain(
        "--lc-radius-lg",
      );
    }
  });
  /*
   * КНОПКИ НЕ УСЫХАЮТ ДО НЕЧИТАЕМОГО — ТРЕТЬЕ МЕСТО ОДНОГО ДЕФЕКТА.
   *
   * У Mantine-кнопки нет своего `flex`, и `.lc-btn` его не задаёт: действует
   * умолчание `flex: 0 1 auto`. А `overflow: hidden` на корне кнопки снимает
   * автоматический минимальный размер флекс-элемента — пол становится нулём, и
   * кнопка сжимается сколько угодно. Подпись внутри при этом `nowrap` без
   * многоточия, поэтому рубится посреди буквы.
   *
   * Шапка ленты и панель действий это себе уже запретили; в панели объединения
   * карточек правило просто не доехало, и «Разъединить» приезжало обрубком.
   * Сторож держит все три места разом: следующая флекс-строка с кнопкой обязана
   * прийти сюда, а не завести четвёртый экземпляр той же болезни.
   */
  it("кнопки во флекс-строках не сжимаются", () => {
    const места: Array<[string, string]> = [
      ["src/features/chats/components/card/client-card.css", ".card-merge__row .lc-btn"],
      ["src/features/chats/components/thread/chat-thread.css", ".thread-header__next"],
    ];
    for (const [файл, селектор] of места) {
      const css = readFileSync(файл, "utf-8") as string;
      const at = css.indexOf(`\n${селектор} {`);
      expect(at, `правило ${селектор} не найдено в ${файл}`).toBeGreaterThan(-1);
      expect(css.slice(at, css.indexOf("}", at)), `${селектор} снова сжимаем`).toContain(
        "flex: none",
      );
    }
  });
  /*
   * МЕТКА СТРОКИ ВИДНА ЦЕЛИКОМ, УСТУПАЕТ ПРЕВЬЮ (разбор 13.08, замер в браузере).
   *
   * Было наоборот: у превью стоял пол `min-width: 96px`, у чипа `flex: none`. При
   * ширине колонки на окне 1440 телу строки достаётся 172px, а нужно 96 + 8 + 98 =
   * 202 — лишнее не обрезалось и не переносилось, а ВЫЛЕЗАЛО наружу и уходило под
   * шкалу ожидания. Пряталась при этом «не отправлено», то есть «ответ до клиента
   * не дошёл» — самая дорогая метка списка.
   *
   * Дать уступить чипу пробовал: он сжимается до «не о…» и перестаёт что-либо
   * значить. Разница в том, КАК эти двое деградируют: превью теряет хвост и
   * остаётся узнаваемым, короткий ярлык от обрезки погибает целиком.
   */
  it("чип строки не сжимается, а превью сжимается", () => {
    const css = readFileSync("src/features/chats/components/list/chat-list.css", "utf-8") as string;
    const тело = (селектор: string) => {
      const at = css.indexOf(`\n${селектор} {`);
      expect(at, `правило ${селектор} не найдено`).toBeGreaterThan(-1);
      return css.slice(at, css.indexOf("}", at)).replace(/\/\*[\s\S]*?\*\//g, "");
    };
    expect(тело(".conv-card__chip"), "чип снова сжимаем — он обрежется до бессмыслицы").toContain(
      "flex: none",
    );
    const превью = тело(".conv-card__preview");
    expect(превью, "у превью вернулся пол — чип вылезет под шкалу").not.toMatch(
      /min-width:\s*[1-9]/,
    );
    expect(превью, "превью обязано уметь сжиматься").toContain("min-width: 0");
  });
});
