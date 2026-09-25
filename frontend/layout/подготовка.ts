/**
 * ПОДГОТОВКА СТЕНДА. Запускается сама перед проверкой (`globalSetup`).
 *
 * Три шага, и каждый из них — грабли прошлых заходов:
 *
 *  1. СОБРАТЬ БОЕВОЙ CSS (`npm run build`). Мерить по исходникам нельзя: в бою
 *     работает собранный CSS, и порядок правил в нём другой.
 *     ⚠ ОДНОГО `index-*.css` МАЛО, И ПЕРВЫЙ ЗАХОД НА ЭТОМ ПОПАЛСЯ: ленивые
 *     разделы держат стили в СВОИХ чанках (AccountsPage-*.css, bots-*.css и
 *     т.д.). Стенд без них показывал таблицу команды без `min-width: 860` и
 *     врал, что порог не работает. Подключаем index первым, следом чанки —
 *     тем же порядком, что и бой.
 *
 *  2. СНЯТЬ РАЗМЕТКУ (`vitest --config vitest.dump.config.ts`). Разметку
 *     рисует настоящий React настоящими компонентами; jsdom не считает
 *     раскладку, но дерево узлов отдаёт верное.
 *     ⚠ ПАДЕНИЕ СНИМАЛКИ НЕ ОСТАНАВЛИВАЕТ ПОДГОТОВКУ, И ЭТО НАРОЧНО: на диске
 *     остаётся ВЧЕРАШНИЙ снимок, и стенд молча померил бы его. Поэтому
 *     запоминаем время начала прогона и пишем в манифест, какие снимки после
 *     него обновились. Протухший снимок валит проверку отдельным тестом с
 *     именем файла, который надо чинить.
 *
 *  3. СОБРАТЬ СТЕНДЫ: CSS + снимок + каркас приложения.
 *     ⚠ КАРКАС ОБЯЗАТЕЛЕН. Рабочее место обязано лежать ВНУТРИ `div.lc-main` —
 *     там объявлен `container-name: lc-workspace`, и без него контейнерные
 *     запросы не сработают вовсе, а их в проекте большинство. Слева обязана
 *     стоять рельса: она забирает 218px, и без неё содержимому достаётся на
 *     146px больше, чем в бою.
 */
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { ЭКРАНЫ, ключЭкрана, type Каркас } from "./экраны";

const ФРОНТ = resolve(new URL("..", import.meta.url).pathname);
const ДАМП = join(ФРОНТ, ".dump");

export interface Манифест {
  собрано: string;
  снималкаУпала: boolean;
  /** Имя снимка → что с ним. */
  снимки: Record<string, { есть: boolean; свежий: boolean; снимает: string }>;
}

export const ПУТЬ_МАНИФЕСТА = join(ДАМП, "pw-манифест.json");

/*
 * ⚠ ПОДГОТОВКА ПИШЕТ В ПОТОК ОТЧЁТА Playwright, каким бы он ни был. При
 * `--reporter=json` её строки окажутся в json-файле перед самим json: PW
 * перехватывает оба потока на время `globalSetup`. Для чтения глазами
 * (`list`, умолчание) это ровно то, что нужно; для машинного разбора отчёт
 * придётся начинать с первой фигурной скобки.
 */
function шаг(что: string): void {
  process.stderr.write(`[стенд] ${что}\n`);
}

/** Запускает команду; возвращает false вместо броска, если она упала. */
function запустить(команда: string, аргументы: string[]): boolean {
  try {
    execFileSync(команда, аргументы, { cwd: ФРОНТ, stdio: "inherit" });
    return true;
  } catch {
    return false;
  }
}

function собранныйCSS(): string {
  const каталог = join(ФРОНТ, "dist/assets");
  if (!existsSync(каталог)) {
    throw new Error("нет dist/assets — сборка не отработала; запустите npm run build руками");
  }
  const все = readdirSync(каталог).filter((f) => f.endsWith(".css"));
  const индекс = все.filter((f) => f.startsWith("index-")).sort();
  const чанки = все.filter((f) => !f.startsWith("index-")).sort();
  if (индекс.length === 0) throw new Error("нет dist/assets/index-*.css — сборка не отработала");
  const куски = [...индекс.slice(-1), ...чанки].map((f) =>
    readFileSync(join(каталог, f), "utf8"),
  );
  // Пути к шрифтам в собранном CSS относительны корня сайта; стенд отдаётся
  // из `.dump`, куда шрифты скопированы шагом ниже.
  return куски.join("\n").replace(/url\(\/assets\//g, "url(./assets/");
}

/**
 * Шрифты рядом со стендом: без них метрики текста другие, и мерка соврёт.
 *
 * ⚠ ЯВНАЯ ПРОВЕРКА КАТАЛОГА, А НЕ ГОЛЫЙ `readdirSync`. В этом репозитории
 * работает не одна сессия сразу (см. заметку parallel-session-repo), а
 * `vite build` первым делом ОЧИЩАЕТ `dist`. Полный прогон 08.09 попал ровно в
 * это окно и упал сырым ENOENT из глубины Node — по такому следу причину не
 * найти. Теперь падение называет себя само.
 */
function перенестиШрифты(): void {
  const из = join(ФРОНТ, "dist/assets");
  if (!existsSync(из)) {
    throw new Error(
      "dist/assets исчез между сборкой и переносом шрифтов. Обычная причина — " +
        "параллельная сборка в этом же репозитории (`vite build` очищает dist). " +
        "Повторите `npm run layout`, когда соседний прогон закончится.",
    );
  }
  const в = join(ДАМП, "assets");
  mkdirSync(в, { recursive: true });
  for (const f of readdirSync(из)) {
    if (f.endsWith(".woff2") || f.endsWith(".woff")) copyFileSync(join(из, f), join(в, f));
  }
}

/**
 * Каркас вокруг снимка.
 *
 * `место` — рельса слева и колонка `.lc-main` справа, ровно как в AppLayout.
 * `страница` — снимок во всё окно (вход, «забыли пароль», загрузочный каркас:
 *              рельсу он рисует сам).
 * `рельса`  — снимок И ЕСТЬ рельса: `.lc-rail` объявлен `width: 100%`, значит
 *              ширину ему обязана задать обёртка.
 */
function обернуть(каркас: Каркас, разметка: string, полоса: string): string {
  if (каркас === "страница") return `<div id="стенд">${разметка}</div>`;
  if (каркас === "рельса") {
    return `<div id="стенд"><div class="стенд-рельса">${разметка}</div><div class="стенд-остаток"></div></div>`;
  }
  /*
   * ⚠ ПОЛОСА ИДЁТ ПЕРВЫМ РЕБЁНКОМ `.lc-main` — ровно там, где её ставит
   * `AppLayout`. Не «где-нибудь сверху»: место в дереве решает всё. Стояла бы
   * она снаружи колонки, стенд мерил бы оверлей поверх рабочего места, то
   * есть ровно ту беду, от которой уходим, — и показывал бы её как норму.
   */
  return `<div id="стенд"><div class="стенд-рельса"></div><div class="lc-main">${полоса}${разметка}</div></div>`;
}

const КАРКАСНЫЙ_CSS = `
  html, body { height: 100%; margin: 0; }
  /*
   * ⚠ ПЕРЕПОЛНЕНИЕ ЗДЕСЬ НЕ ПРЯЧУТ. Соблазн написать #стенд{overflow:hidden}
   * велик — картинка станет опрятной. Но тогда признак «полоса у страницы»
   * не сработает НИКОГДА: спрятанное переполнение до документа не доходит.
   * В бою у body тоже нет overflow: hidden.
   */
  #стенд { display: flex; min-height: 100vh; height: 100vh; }
  .стенд-рельса { flex: none; width: var(--lc-rail-w); height: 100%; }
  .стенд-остаток { flex: 1; min-width: 0; }
  #стенд > .lc-main { flex: 1; min-width: 0; min-height: 0; }
`;

function шаблон(
  имя: string,
  css: string,
  каркас: Каркас,
  разметка: string,
  полоса: string,
): string {
  return `<!doctype html><html lang="ru" data-mantine-color-scheme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Стенд адаптива — ${имя}</title>
<style>${css}</style>
<style>${КАРКАСНЫЙ_CSS}</style>
</head><body>${обернуть(каркас, разметка, полоса)}</body></html>
`;
}

export default async function подготовить(): Promise<void> {
  mkdirSync(ДАМП, { recursive: true });

  if (process.env.LAYOUT_SKIP_BUILD === "1") {
    шаг("сборка пропущена (LAYOUT_SKIP_BUILD=1) — CSS берём прежний");
  } else {
    шаг("собираю боевой CSS: npm run build");
    if (!запустить("npm", ["run", "build"])) {
      throw new Error("npm run build упал — мерить нечего, стенд без боевого CSS врёт");
    }
  }

  шаг("снимаю разметку: vitest --config vitest.dump.config.ts");
  /*
   * Секунда назад: mtime на файловых системах округляется, и снимок,
   * записанный в ту же миллисекунду, что и старт, иначе выглядел бы старым.
   */
  const начало = Date.now() - 1000;
  const снималкаЖива = запустить("npx", [
    "vitest",
    "run",
    "--config",
    "vitest.dump.config.ts",
  ]);
  if (!снималкаЖива) {
    шаг("⚠ снималка упала — часть снимков осталась вчерашней; отдельный тест назовёт какие");
  }

  перенестиШрифты();
  const css = собранныйCSS();

  /*
   * Стопка полос — свой снимок, общий для всех экранов «под полосой». Если
   * снималка полос упала, экраны с полосой пропускаются вместе с ней: пустая
   * стопка на стенде показала бы «накрывать нечем» и соврала бы в самую
   * удобную сторону.
   */
  const файлПолосы = join(ДАМП, "polosa.html");
  const полосаЕсть = existsSync(файлПолосы);
  const полосаСвежая = полосаЕсть && statSync(файлПолосы).mtimeMs >= начало;
  const полоса = полосаЕсть ? readFileSync(файлПолосы, "utf8") : "";

  const снимки: Манифест["снимки"] = {};
  for (const экран of ЭКРАНЫ) {
    const ключ = ключЭкрана(экран);
    const исходник = join(ДАМП, `${экран.имя}.html`);
    const есть = existsSync(исходник) && (!экран.полоса || полосаЕсть);
    const свежий =
      есть &&
      statSync(исходник).mtimeMs >= начало &&
      (!экран.полоса || полосаСвежая);
    снимки[ключ] = { есть, свежий, снимает: экран.снимает };
    if (!есть) continue;
    writeFileSync(
      join(ДАМП, `pw-${ключ}.html`),
      шаблон(
        ключ,
        css,
        экран.каркас,
        readFileSync(исходник, "utf8"),
        экран.полоса ? полоса : "",
      ),
      "utf8",
    );
  }

  const манифест: Манифест = {
    собрано: new Date().toISOString(),
    снималкаУпала: !снималкаЖива,
    снимки,
  };
  writeFileSync(ПУТЬ_МАНИФЕСТА, JSON.stringify(манифест, null, 1), "utf8");
  const свежих = Object.values(снимки).filter((с) => с.свежий).length;
  шаг(`стендов собрано: ${свежих} свежих из ${ЭКРАНЫ.length}`);
}
