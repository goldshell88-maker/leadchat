// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts и cardRecipe.test.ts: сторож только читает файлы.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * ССЫЛКА НА ДОКУМЕНТ ВЕДЁТ В СУЩЕСТВУЮЩИЙ РАЗДЕЛ.
 *
 * НАЙДЕНО 12 августа. На `docs/16-DESIGN-SYSTEM-2026.md` ссылались 25 мест в
 * коде — комментарии вида «docs/16 §4.3», объясняющие, ПОЧЕМУ что-то сделано
 * именно так. Файла при этом не существовало вовсе: он был потерян или не
 * написан, а ссылки жили и указывали в пустоту.
 *
 * Это хуже отсутствия ссылок. Комментарий, который отсылает к источнику,
 * снимает вопрос у читателя — тот верит, что обоснование где-то записано, и не
 * перепроверяет. Двадцать пять таких обещаний были ложными.
 *
 * Сторож держит две вещи: документ на месте и КАЖДЫЙ параграф, на который
 * ссылается код, в нём объявлен. Второе важнее первого: файл легко вернуть
 * пустым.
 */

const ДОК = "../docs/16-DESIGN-SYSTEM-2026.md";
const КОРНИ = ["src", "../app"];

function файлы(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir) as string[]) {
    if (name === "node_modules" || name === "__pycache__" || name.startsWith(".")) continue;
    const full = `${dir}/${name}`;
    if ((statSync(full) as { isDirectory(): boolean }).isDirectory()) файлы(full, out);
    else if (/\.(ts|tsx|css|py)$/.test(name)) out.push(full);
  }
  return out;
}

describe("Ссылки на дизайн-систему ведут в существующие разделы", () => {
  const текст = readFileSync(ДОК, "utf-8") as string;

  const упомянуты = new Set<string>();
  for (const корень of КОРНИ) {
    for (const f of файлы(корень)) {
      const src = readFileSync(f, "utf-8") as string;
      for (const m of src.matchAll(/docs\/16[^)»,;\n]{0,60}?§([0-9]+(?:\.[0-9]+)?)/g)) {
        упомянуты.add(m[1]);
      }
    }
  }

  it("документ на месте и не пуст", () => {
    expect(текст.length, "docs/16 снова исчез или опустел").toBeGreaterThan(2000);
  });

  it("код вообще ссылается на него — иначе сторож проверяет пустоту", () => {
    expect(упомянуты.size).toBeGreaterThanOrEqual(8);
  });

  it("каждый упомянутый параграф в документе объявлен", () => {
    const пропавшие = [...упомянуты].sort().filter(
      (п) => !new RegExp(`^#{2,3} §${п.replace(".", "\\.")}[ .]`, "m").test(текст),
    );
    expect(
      пропавшие,
      `код ссылается на разделы, которых в документе нет: ${пропавшие.map((п) => "§" + п).join(", ")}`,
    ).toEqual([]);
  });
});
