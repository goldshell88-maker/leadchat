// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в railChrome.test.ts и cssDeadClasses.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * У КАЖДОГО РАЗДЕЛА ОДИН ЗАГОЛОВОК, И ОН ОДНОГО РАЗМЕРА.
 *
 * НАЙДЕНО ОБХОДОМ 12 августа. Заголовок страницы рисовался тремя способами:
 * общим `PageHeader` в настройках (28px, токен `--lc-fz-page`), `Title fz={20}`
 * на «Статистике», `Title fz={24} fw={600}` на «Разборе диалогов» и в «Что
 * нового». Переходя между разделами, человек видел, как заголовок прыгает в
 * размере, — и это читается не как разные страницы, а как разное качество
 * сборки. У «Разбора» вдобавок жила `.dt__head` — построчная копия
 * `.page-header`, отличавшаяся ровно тем, чем не должна была.
 *
 * Отдельно: у «Чатов» заголовка первого уровня не было ВОВСЕ — колонка списка
 * называла себя `h2`. Для скринридера страница начиналась с подраздела
 * неизвестно чего.
 *
 * ПОЧЕМУ СТОРОЖ ЧИТАЕТ ИСХОДНИКИ. В vitest стоит `css: false` — в jsdom стилей
 * нет, кегль померить нечем. Здесь проверяется то, что проверяемо: разделы
 * пользуются общей шапкой, а своих кеглей у заголовков нет.
 */

/** Разделы верхнего уровня и файл, где живёт их заголовок. */
const SECTIONS: ReadonlyArray<readonly [string, string]> = [
  ["Статистика", "src/features/stats/StatsPage.tsx"],
  ["Разбор диалогов", "src/features/table/TablePage.tsx"],
  ["Что нового", "src/features/updates/UpdatesPage.tsx"],
  ["Уведомления", "src/features/notifications/NotificationsPage.tsx"],
  ["Профиль", "src/features/settings/profile/ProfilePage.tsx"],
  ["Каналы", "src/features/settings/accounts/AccountsPage.tsx"],
  ["Быстрые ответы", "src/features/settings/templates/TemplatesPage.tsx"],
  ["Команда", "src/features/settings/team/TeamPage.tsx"],
  ["Боты", "src/features/settings/bots/BotsPage.tsx"],
];

describe("Заголовок раздела — общий компонент, а не свой кегль", () => {
  for (const [name, path] of SECTIONS) {
    it(`${name}: пользуется PageHeader`, () => {
      const src = readFileSync(path, "utf-8") as string;
      expect(src, `${path} не импортирует общую шапку`).toMatch(/PageHeader/);
    });

    it(`${name}: не задаёт кегль заголовка руками`, () => {
      const src = readFileSync(path, "utf-8") as string;
      const code = src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\{\/\*[\s\S]*?\*\/\}/g, " ");
      // `<Title order={1} fz={...}>` — ровно то, чем расходились три раздела.
      const own = code.match(/<Title[^>]*order=\{1\}[^>]*fz=/);
      expect(own, `${path}: заголовок первого уровня со своим кеглем`).toBeNull();
    });
  }

  it("«Чаты»: заголовок первого уровня есть", () => {
    // Единственное название экрана: шапки над рабочим местом нет, а пункт в
    // левой панели — навигация, а не заголовок.
    const src = readFileSync(
      "src/features/chats/components/list/ChatListPane.tsx",
      "utf-8",
    ) as string;
    expect(src).toContain('<Text component="h1">Чаты</Text>');
  });

  it("копии раскладки шапки не завелись заново", () => {
    // `.dt__head` и `.updates__head` были копиями `.page-header`. Копия,
    // которая расходится с оригиналом в одном свойстве, — худший вид дубля:
    // выглядит как система, работает как исключение.
    for (const path of ["src/features/table/table.css", "src/features/updates/updates.css"]) {
      const css = (readFileSync(path, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, " ");
      expect(css, `${path}: своя шапка страницы вернулась`).not.toMatch(/\.\w+__head\s*\{/);
    }
  });
});
