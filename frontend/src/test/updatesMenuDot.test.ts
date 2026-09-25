// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в cssDeadClasses.test.ts и hotkeysScroll.test.tsx: сторож только читает файлы.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * ТОЧКА «ЕСТЬ НЕПРОЧИТАННОЕ» ОБЯЗАНА БЫТЬ ВИДНА С ПЕРВОЙ СЕКУНДЫ (SHELL-05).
 *
 * ЧТО БЫЛО. `.lc-menu-dot` объявлялся в `features/updates/updates.css`, а тот
 * импортируется ровно из одного места — `UpdatesPage.tsx`, ленивый чанк
 * `/updates`. Стили чанка попадают в документ только после открытия страницы,
 * а открытие страницы тут же гасит признак (`markUpdatesSeen`). Признак
 * «появилось новое» не мог сработать ни разу.
 *
 * ЧЕГО ЭТОТ СТОРОЖ НЕ УМЕЕТ. Он не строит граф модулей и не знает про
 * `React.lazy`. Он держит ровно одно условие, зато прямое: файл со стилем
 * точки импортируется модулем, который шапка приложения тянет статически, —
 * значит правило едет в стартовом бандле. Если завтра стиль переедет обратно
 * в ленивый чанк, сторож упадёт.
 */

const CSS_ROOT = "src";
const CLASS = ".lc-menu-dot";
/**
 * Точку рисует левая панель — всё, что она импортирует статически, едет в
 * стартовом бандле. Была шапка (`AppLayout.tsx`), пока меню сотрудника жило
 * там; после переезда 12 августа сторож смотрел бы на файл, который точку
 * больше не рисует, и молчал бы всегда.
 */
const SHELL = "src/app/AppRail.tsx";

const SOURCES: Record<string, string> = import.meta.glob("/src/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function cssFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir) as string[]) {
    const full = `${dir}/${name}`;
    if ((statSync(full) as { isDirectory(): boolean }).isDirectory()) cssFiles(full, out);
    else if (name.endsWith(".css")) out.push(full);
  }
  return out;
}

describe("Стиль точки «что нового» едет в стартовом бандле (SHELL-05)", () => {
  const declaring = cssFiles(CSS_ROOT).filter((f) =>
    new RegExp(`\\${CLASS}\\s*\\{`).test(readFileSync(f, "utf-8") as string),
  );

  it("правило объявлено ровно один раз", () => {
    expect(declaring).toHaveLength(1);
  });

  it("файл со стилем импортирует модуль, который тянет сама шапка", () => {
    const cssName = declaring[0].split("/").pop() as string;
    // Кто импортирует этот css.
    const importers = Object.entries(SOURCES)
      .filter(([, text]) => new RegExp(`import\\s+["'][^"']*${cssName}["']`).test(text))
      .map(([path]) => path);
    expect(importers.length).toBeGreaterThan(0);

    const shell = readFileSync(SHELL, "utf-8") as string;
    const reachable = importers.some((path) => {
      // "/src/features/updates/seen.ts" → "features/updates/seen"
      const spec = path.replace(/^\/src\//, "").replace(/\.tsx?$/, "");
      return shell.includes(`@/${spec}`);
    });
    expect(reachable, `${cssName}: шапка не импортирует ни один из ${importers.join(", ")}`).toBe(
      true,
    );
  });
});
