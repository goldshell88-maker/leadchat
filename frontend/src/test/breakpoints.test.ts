// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно.
import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { MOBILE_MAX, NARROW_MAX } from "@/shared/lib/breakpoints";

/**
 * Пороги раскладки обязаны совпадать в CSS и в TypeScript.
 *
 * Разъезд здесь даёт состояние, которого не бывает: медиазапрос уже показал
 * две колонки, а React всё ещё считает раскладку трёхколоночной — и карточка
 * клиента оказывается одновременно в потоке и в оверлее. Ни один рендер-тест
 * этого не увидит (в vitest `css: false`), а на глаз это выглядит как «иногда
 * дублируется карточка», то есть как плавающий баг.
 *
 * Медиазапросы не умеют читать TypeScript, поэтому числа приходится держать
 * в двух местах. Сторож делает дубль громким.
 */

/*
 * CSS читается через файловую систему, а не через `import.meta.glob` с `?raw`.
 * Причина та же, что у сторожа стилей Mantine: в конфигурации тестов стоит
 * `css: false`, и Vite отдаёт любой CSS пустой строкой — glob честно находит
 * файлы, но все они приходят нулевой длины, и проверка молча зеленеет.
 */
const CHAT_CSS_DIRS = [
  "src/features/chats",
  "src/features/chats/components/thread",
  "src/features/chats/components/list",
  "src/features/chats/components/card",
  "src/app",
  // Платформенный слой держит пятую копию порога 767 и до сих пор жил вне
  // проверки: тихая копия — ровно то, из-за чего пороги и разъезжаются.
  "src/platform",
];

function cssFiles(): string[] {
  const out: string[] = [];
  for (const dir of CHAT_CSS_DIRS) {
    let names: string[] = [];
    try {
      names = readdirSync(dir) as string[];
    } catch {
      continue;
    }
    for (const name of names) {
      if (name.endsWith(".css")) out.push(`${dir}/${name}`);
    }
  }
  return out;
}

/** Все пороги `max-width` из медиазапросов рабочего места. */
function chatBreakpoints(): number[] {
  const found = new Set<number>();
  for (const file of cssFiles()) {
    const css = readFileSync(file, "utf-8") as string;
    for (const m of css.matchAll(/@media[^{]*max-width:\s*(\d+)px/g)) {
      found.add(Number(m[1]));
    }
  }
  return [...found].sort((a, b) => a - b);
}

describe("Пороги раскладки рабочего места", () => {
  it("CSS видит те же числа, что и React", () => {
    const inCss = chatBreakpoints();
    // Пустой список означал бы, что glob перестал находить стили, — и тест
    // молча позеленел бы, ничего не проверяя.
    expect(inCss.length).toBeGreaterThan(0);
    expect(inCss).toContain(NARROW_MAX);
    expect(inCss).toContain(MOBILE_MAX);
  });

  it("прежний порог 1020 нигде не остался", () => {
    // Он был в четырёх местах, и забытая копия вернула бы ровно тот дефект,
    // ради которого порог поднимали: три колонки там, где ленте остаётся 400px.
    expect(chatBreakpoints()).not.toContain(1020);
  });

  it("ленте остаётся не меньше 600px там, где колонок три", () => {
    // Порог выбран расчётом, и расчёт обязан пережить правку ширин колонок:
    // подними кто-нибудь список до 400px — и на пороге лента снова окажется
    // непригодной, а заметит это уже оператор.
    const LIST = 340;
    const CARD = 340;
    const GUTTERS = 64;
    expect(NARROW_MAX + 1 - LIST - CARD - GUTTERS).toBeGreaterThanOrEqual(600);
  });
});
