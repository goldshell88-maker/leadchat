// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в railChrome.test.ts и cssDeadClasses.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * РАБОЧЕЕ МЕСТО НЕ УЕЗЖАЕТ ПОД НИЖНИЙ КРАЙ ОКНА.
 *
 * НАЙДЕНО ЗАМЕРОМ 12 августа. Полоса критичного (`CriticalBanners`) стоит в
 * потоке намеренно: под ней «Аккаунт Авито требует переподключения», и
 * закрытая кнопка «Переподключить» стоит дороже сдвинутого списка. Но экраны
 * считали высоту от ОКНА — `calc(100dvh - шапка)`, — то есть полоса
 * выталкивала их вниз ровно на свою высоту. Замер: документ 976 при окне 900,
 * поле ввода ответа под краем экрана, у всей страницы своя вертикальная
 * прокрутка. Полоса при этом висит не в редком случае — канал с истекающим
 * токеном показывает её постоянно, и на боевой системе она висела в тот самый
 * день.
 *
 * Теперь высоту раздаёт колонка `lc-main`: полоса берёт своё, экран — остаток.
 *
 * ПОЧЕМУ СТОРОЖ ЧИТАЕТ ИСХОДНИКИ. В vitest стоит `css: false` — в jsdom стилей
 * нет, высоты нулевые, и померить это тестом нельзя в принципе. Здесь
 * проверяется единственное, что проверяемо машиной: экраны не возвращаются к
 * счёту от окна, а колонка на месте.
 */

const SCREENS = [
  "src/features/chats/chats-page.css",
  "src/features/settings/settings.css",
  "src/features/table/table.css",
];

describe("Экраны берут остаток колонки, а не всю высоту окна", () => {
  it("колонка объявлена и тянется на всё окно", () => {
    const css = readFileSync("src/app/app-layout.css", "utf-8") as string;
    const at = css.indexOf(".lc-main {");
    expect(at, "правило .lc-main пропало — экранам не от чего считать остаток").toBeGreaterThan(-1);
    const rule = css.slice(at, css.indexOf("}", at));
    expect(rule).toMatch(/flex-direction:\s*column/);
    expect(rule).toMatch(/height:\s*100dvh/);
  });

  it("колонка надета на рабочую область", () => {
    const layout = readFileSync("src/app/AppLayout.tsx", "utf-8") as string;
    expect(layout).toContain('<AppShell.Main className="lc-main">');
  });

  for (const path of SCREENS) {
    it(`${path.split("/").pop()} не считает высоту от окна`, () => {
      const css = readFileSync(path, "utf-8") as string;
      // Комментарии не в счёт: в них эта запись объясняется как БЫВШАЯ.
      const code = css.replace(/\/\*[\s\S]*?\*\//g, " ");
      expect(code, "вернулся счёт от окна — полоса критичного снова вытолкнет экран").not.toMatch(
        /height:\s*calc\(100dvh/,
      );
      expect(code).toMatch(/flex:\s*1/);
    });
  }
});
