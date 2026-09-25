// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно: он читает стили и
// больше ничем в Node не пользуется. Так же сделано в breakpoints.test.ts и
// mantineStyles.test.ts.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * СТОРОЖ: В CSS НЕТ КЛАССОВ, КОТОРЫХ НИКТО НЕ НАДЕВАЕТ.
 *
 * ЗАЧЕМ. К 11 августа в стилях лежало пятнадцать правил без единого
 * потребителя: пилюля статуса и её точка, заглушка поля ввода, кнопки в шапке
 * диалога, время в карточке списка, три остатка многошагового мастера, две
 * строки подвала на экране каналов, пустое состояние журнала, строка под
 * кнопку «Разгрузить…» (саму разгрузку вырезали коммитом 7d8724e) и подвал
 * мастера с раскладкой по краям. Все они пережили свою разметку и остались
 * лежать. Беда не в килобайтах: следующий человек читает «.thread-status» и
 * решает, что статусная пилюля в продукте есть, — а её нет уже полгода.
 * Мёртвый CSS врёт о том, как выглядит система, и врёт убедительно.
 *
 * ЧТО ДЕЛАТЬ, ЕСЛИ СТОРОЖ УПАЛ. Он называет файл, строку и класс. Выхода два,
 * и оба честные: либо класс правда лишний — удалить правило; либо разметку ещё
 * не написали — тогда её и надо написать, а не заводить стиль впрок. Третьего
 * («допишу в исключения») быть не должно: список исключений ниже закрыт и
 * объясняет свой единственный пункт причиной, а не фамилией.
 *
 * ЧЕГО СТОРОЖ НЕ УМЕЕТ (сказано вслух, чтобы на него не полагались сверх
 * меры). Он ищет ИМЯ КЛАССА в тексте исходников целым словом, а не разбирает
 * JSX. Значит класс, чьё имя совпадает с обычным словом из кода, будет сочтён
 * использованным, даже если разметки нет. Ложной ТРЕВОГИ это не даёт — только
 * молчание, и размен выбран сознательно: сторож, который врёт, хуже
 * отсутствующего, потому что после второго ложного падения его отключат
 * вместе со всей пользой.
 */

/*
 * СТИЛИ ЧИТАЮТСЯ ЧЕРЕЗ `node:fs`, А НЕ ЧЕРЕЗ `import.meta.glob` С `?raw`.
 *
 * Не вкусовщина, а грабли, на которые этот файл уже наступил. В конфигурации
 * тестов стоит `css: false` (vite.config.ts), и плагин `vitest:css-disable`
 * подменяет содержимое ЛЮБОГО файла, чей путь совпал с `\.css(?:$|\?)`, пустой
 * строкой — запрос `?raw` от этого не спасает, он попадает под то же правило.
 * Первая версия сторожа так и была написана: glob честно находил все 32 файла,
 * все они приходили нулевой длины, ни одного класса не объявлялось — и тест
 * зеленел, даже когда мёртвый класс возвращали в CSS руками. Замер: 32 файла,
 * 0 символов.
 *
 * Тот же капкан описан в mantineStyles.test.ts и breakpoints.test.ts — там на
 * нём уже спотыкались до нас.
 *
 * Пути относительные: vitest запускается из каталога frontend.
 */
const CSS_ROOT = "src";

/** Разметка приходит через glob: `.ts`/`.tsx`/`.html` подмене не подлежат. */
const MARKUP: Record<string, string> = {
  ...(import.meta.glob("/src/**/*.{ts,tsx}", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
  // Стенд вёрстки — тоже потребитель: класс, который надевает только он, живой.
  ...(import.meta.glob("/dev/**/*.{ts,tsx}", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
  ...(import.meta.glob("/*.html", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
};

/**
 * Исключения. Список закрыт, и на сегодня в нём один пункт — чужие классы.
 *
 * `mantine-*` (`mantine-Modal-header` и родня) вешает сама библиотека. В нашей
 * разметке их не пишут и писать не должны: мы из CSS только прицеливаемся в
 * них, чтобы переопределить чужие отступы. Искать их среди наших компонентов
 * бессмысленно — их там не будет никогда.
 */
const EXCEPTIONS: ((cls: string) => boolean)[] = [(cls) => cls.startsWith("mantine-")];

function cssFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir) as string[]) {
    const full = `${dir}/${name}`;
    if ((statSync(full) as { isDirectory(): boolean }).isDirectory()) cssFiles(full, out);
    else if (name.endsWith(".css")) out.push(full);
  }
  return out;
}

/**
 * Комментарии выбрасываем ДО поиска имён. Иначе сторож глохнет ровно там, где
 * он нужнее всего: удаляя класс, человек пишет рядом «.thread-status больше
 * нет» — и это упоминание засчиталось бы за использование. Проверено сломом:
 * с включённым вырезанием возвращённый в CSS `.thread-status` ловится; стоит
 * отключить — и он проскакивает из-за одной строки комментария в соседнем
 * файле тестов.
 *
 * Строчные комментарии режем только когда перед `//` не двоеточие и не буква:
 * так `https://…` внутри строки остаётся целым. Промах в эту сторону даёт
 * ЛОЖНУЮ ТРЕВОГУ (видно сразу), а не молчание.
 */
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/(^|[^:\w])\/\/[^\n]*/gm, "$1");
}

/** Комментарии CSS — иначе объяснение «класс удалён» читается как объявление. */
function stripCssComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, " "));
}

/**
 * Содержимое `url(…)` — иначе `www.w3.org` в картинке-маске шеврона читается
 * как классы `.w3` и `.org` (24.09). Ложная тревога на адресе хуже молчания
 * только тем, что приучает её не читать.
 */
function stripCssUrls(text: string): string {
  return text.replace(/url\((?:"[^"]*"|'[^']*'|[^()"'])*\)/g, (u) => u.replace(/[^\n]/g, " "));
}

/**
 * Приставки для модификаторов, которые собираются на лету: `conv-card__chip--`
 * из `` `conv-card__chip--${chip.tone}` `` или `msg--` из `"msg--" + kind`.
 * Имени `conv-card__chip--danger` в исходниках нет и быть не может, поэтому
 * классы, начинающиеся с такой приставки, считаем живыми.
 *
 * ПРИСТАВКОЙ СЧИТАЕТСЯ ТОЛЬКО ТО, ЧТО КОНЧАЕТСЯ РАЗДЕЛИТЕЛЕМ (`-` или `_`).
 * Без этого условия `` `card-section${modifier ? …}` `` объявляло бы живым всё
 * семейство `card-section*`, и сторож замолчал бы на целом блоке разом.
 *
 * Правило не косметическое: со снятыми приставками сторож обвиняет восемь
 * живых классов (`msg--bot`, `msg--note`, `conv-card__chip--wait`, три
 * `lc-notify-severity--*` и родню) — то есть ровно превращается в того самого
 * врущего сторожа, которого лучше бы не было.
 */
function collectPrefixes(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(/([A-Za-z][A-Za-z0-9_-]*[-_])\$\{/g)) out.push(m[1]);
  for (const m of text.matchAll(/([A-Za-z][A-Za-z0-9_-]*[-_])["'`]\s*\+/g)) out.push(m[1]);
  return out;
}

describe("CSS без мёртвых классов", () => {
  it("каждый класс из frontend/src/**.css надет хотя бы одним компонентом", () => {
    const files = cssFiles(CSS_ROOT);
    const styles = files.map((f) => [f, readFileSync(f, "utf-8") as string] as const);
    const totalCss = styles.reduce((sum, [, text]) => sum + text.length, 0);

    // Сторож, который ничего не прочитал, зелен всегда — а это худший из
    // отказов, незаметный. Именно так и вела себя первая версия (см. шапку).
    expect(files.length).toBeGreaterThan(20);
    expect(totalCss).toBeGreaterThan(50_000);
    expect(Object.keys(MARKUP).length).toBeGreaterThan(50);

    // Где объявлен каждый класс.
    const declared = new Map<string, string>();
    for (const [file, text] of styles) {
      stripCssUrls(stripCssComments(text))
        .split("\n")
        .forEach((line, i) => {
          for (const m of line.matchAll(/\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g)) {
            if (!declared.has(m[1])) declared.set(m[1], `frontend/${file}:${i + 1}`);
          }
        });
    }
    expect(declared.size).toBeGreaterThan(200);

    // Весь код одной простынёй + приставки динамических модификаторов.
    let markup = "";
    const prefixes: string[] = [];
    for (const text of Object.values(MARKUP)) {
      const clean = stripComments(text);
      markup += `\n${clean}`;
      prefixes.push(...collectPrefixes(clean));
    }

    const isUsed = (cls: string) => {
      // Целым словом: `.msg` не должен засчитываться из-за `msg--out` рядом.
      if (new RegExp(`(?<![A-Za-z0-9_-])${cls}(?![A-Za-z0-9_-])`).test(markup)) return true;
      return prefixes.some((p) => cls.startsWith(p) && cls.length > p.length);
    };

    const dead: string[] = [];
    for (const [cls, where] of declared) {
      if (isUsed(cls)) continue;
      if (EXCEPTIONS.some((excused) => excused(cls))) continue;
      dead.push(`  .${cls} — ${where}`);
    }
    dead.sort();

    expect(
      dead,
      "Эти классы объявлены в CSS, но их не надевает ни один компонент.\n" +
        "Либо удалите правило, либо напишите разметку — но не заводите стиль впрок:\n" +
        "следующий человек прочитает его как описание того, что в продукте есть.\n" +
        dead.join("\n"),
    ).toEqual([]);
  });
});
