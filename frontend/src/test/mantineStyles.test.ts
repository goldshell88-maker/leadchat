// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно: он читает два файла
// библиотеки и больше ничем в Node не пользуется.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * Сторож поштучных стилей Mantine (src/app/mantine-styles.ts).
 *
 * Раз мы не грузим `@mantine/core/styles.css` целиком, компонент без своего
 * CSS-файла в списке приедет на экран голым — и ни один рендер-тест этого не
 * покажет (в vitest `css: false`). Поэтому сверяем список с исходниками:
 * каждый компонент, который где-то импортируется из "@mantine/core" и имеет
 * собственный файл стилей, обязан быть в списке.
 */

/** Исходники — как в webBundlePurity.test.ts, через glob, без node:fs. */
const sources = import.meta.glob("/src/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Ленивый glob: нужны только имена файлов, содержимое CSS в тест не тянем. */
const styleFiles = Object.keys(import.meta.glob("/node_modules/@mantine/core/styles/*.css"));

const AVAILABLE = new Set(
  styleFiles
    .map((p) => p.slice(p.lastIndexOf("/") + 1).replace(/\.css$/, ""))
    .filter((name) => !name.endsWith(".layer") && /^[A-Z]/.test(name)),
);

const IMPORTED = new Set(
  [...(sources["/src/app/mantine-styles.ts"] ?? "").matchAll(/styles\/([A-Za-z-]+)\.css/g)].map((m) => m[1]),
);

/** Именованные импорты из "@mantine/core" по всему src (без тестов). */
function componentsUsedInSources(): Map<string, string[]> {
  const used = new Map<string, string[]>();
  for (const [rel, code] of Object.entries(sources)) {
    if (rel.startsWith("/src/test/")) continue;
    for (const m of code.matchAll(/import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+"@mantine\/core"/g)) {
      for (const raw of m[1].split(",")) {
        const name = raw.trim().split(/\s+as\s+/)[0].trim();
        if (!/^[A-Z]/.test(name)) continue;
        used.set(name, [...(used.get(name) ?? []), rel]);
      }
    }
  }
  return used;
}

/**
 * Содержимое CSS библиотеки читается через `node:fs`, а не через glob с
 * `?raw`, как остальные исходники в этом файле.
 *
 * Причина техническая и неочевидная: в конфигурации тестов стоит `css: false`,
 * поэтому Vite отдаёт любой CSS пустой строкой — glob честно находит все 182
 * файла, и все они приходят нулевой длины. Проверка порядка на пустых файлах
 * зелёная всегда: у каждого «нет ни одного правила», значит и переставить
 * ничего нельзя. Ровно так первая версия этого теста молча пропустила
 * поломку, ради которой писалась.
 */
const MANTINE_STYLES_DIR = "node_modules/@mantine/core/styles";

/** Путь относительный: vitest запускается из каталога frontend. */
function readCss(relative: string): string {
  try {
    return readFileSync(relative, "utf-8") as string;
  } catch {
    return "";
  }
}

function canonicalCss(): string {
  return readCss("node_modules/@mantine/core/styles.css");
}

function componentCss(name: string): string {
  return readCss(`${MANTINE_STYLES_DIR}/${name}.css`);
}

describe("Поштучные стили Mantine (src/app/mantine-styles.ts)", () => {
  it("glob находит файлы стилей библиотеки", () => {
    expect(AVAILABLE.size).toBeGreaterThan(50);
    expect(AVAILABLE.has("Button")).toBe(true);
  });

  it("styles.css целиком больше нигде не импортируется", () => {
    const offenders = Object.entries(sources)
      .filter(([, code]) => code.includes('"@mantine/core/styles.css"'))
      .map(([rel]) => rel);
    expect(offenders).toEqual([]);
  });

  it("у каждого используемого компонента подключены его стили", () => {
    const missing: string[] = [];
    for (const [name, files] of componentsUsedInSources()) {
      if (AVAILABLE.has(name) && !IMPORTED.has(name)) missing.push(`${name} (${files[0]})`);
    }
    expect(missing).toEqual([]);
  });

  /**
   * Порядок импортов обязан совпадать с порядком внутри
   * `@mantine/core/styles.css`.
   *
   * Это не педантизм. Селекторы Mantine намеренно держат специфичность в один
   * класс, поэтому между правилами равного веса решает каскад — то есть
   * порядок подключения. `UnstyledButton.css` (основа кнопок) ставит
   * `background-color: transparent`; однажды он уже стоял ПОСЛЕ `Button.css`,
   * выигрывал по каскаду и обнулял фон — во всём приложении ни одна кнопка не
   * имела заливки, все выглядели простым текстом. Ни один рендер-тест этого не
   * видит (в vitest `css: false`), а на глаз «кнопка без фона» легко принять
   * за задуманный минимализм.
   *
   * Порядок восстанавливается по позиции первого хэш-класса файла в
   * каноническом `styles.css` — то есть по самой библиотеке, а не по нашему
   * представлению о правильном.
   */
  it("порядок импортов совпадает с каноническим порядком библиотеки", () => {
    const canon = canonicalCss();
    const positionInCanon = new Map<string, number>();
    for (const m of canon.matchAll(/\.(m_[0-9a-f]{8})\b/g)) {
      if (!positionInCanon.has(m[1])) positionInCanon.set(m[1], m.index ?? 0);
    }

    /** Позиция файла компонента в каноническом styles.css. */
    const rank = (component: string): number => {
      const css = componentCss(component);
      let best = Number.MAX_SAFE_INTEGER;
      for (const m of css.matchAll(/\.(m_[0-9a-f]{8})\b/g)) {
        const pos = positionInCanon.get(m[1]);
        if (pos !== undefined && pos < best) best = pos;
      }
      return best;
    };

    // Базовые файлы (сброс, переменные, глобальные правила) хэш-классов не
    // содержат и всегда идут первыми — их из проверки исключаем.
    const BASE = ["baseline", "default-css-variables", "global"];
    const listed = [...IMPORTED].filter((n) => !BASE.includes(n) && AVAILABLE.has(n));
    const ranks = listed.map(rank);

    const outOfOrder: string[] = [];
    for (let i = 1; i < listed.length; i += 1) {
      if (ranks[i] < ranks[i - 1]) outOfOrder.push(`${listed[i]} стоит после ${listed[i - 1]}`);
    }
    expect(outOfOrder).toEqual([]);
  });

  it("в списке нет опечаток — каждому импорту соответствует файл библиотеки", () => {
    const unknown = [...IMPORTED].filter(
      (name) => !AVAILABLE.has(name) && !["baseline", "default-css-variables", "global"].includes(name),
    );
    expect(unknown).toEqual([]);
  });
});
