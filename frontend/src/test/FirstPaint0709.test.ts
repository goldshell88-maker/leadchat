// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в shellFrame0509.test.ts, railChrome.test.ts и CspInlineHash0109.test.ts:
// сторож только читает файлы.
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { предзагрузитьInter } from "@/build/предзагрузкаInter";
import { MOBILE_MAX } from "@/shared/lib/breakpoints";
import { RAIL_WIDTH_COLLAPSED, RAIL_WIDTH_EXPANDED } from "@/shared/stores/railStore";

/**
 * ПЕРВЫЙ КАДР: ЧТО ЧЕЛОВЕК ВИДИТ, ПОКА ЕДЕТ БАНДЛ (замер 06.09).
 *
 * ЧТО БЫЛО. `index.html` нёс один пустой `div#root`. Скелет рабочего места
 * (`.lc-boot*`) существовал только в React, то есть рисовался после разбора
 * 898 КБ JS: холодный экран пуст ≥1,08 с, и это КАЖДОЕ открытие вкладки, а
 * после «Вышло обновление» человек сам просит перезагрузку и получает пустоту
 * в награду.
 *
 * ЧТО ЗДЕСЬ. Тот же каркас статикой в разметке. Он рисуется сразу после
 * обязательного CSS и заменяется первым кадром React без промежуточного
 * пустого кадра. Всё, что делает эту подмену незаметной, — набор совпадений
 * между четырьмя файлами: разметка обязана повторять `AppChromeSkeleton`,
 * стили — базовые правила `app-layout.css`, числа рельсы — `railStore` и
 * `breakpoints`, адреса гейта — карту маршрутов. Каждое из этих совпадений
 * здесь и сторожится: расхождение видно на экране как рывок, а в тестах — нет.
 *
 * ⚠ ПОЧЕМУ ЧАСТЬ ПРОВЕРОК ЧИТАЕТ ИСХОДНИКИ. В vitest `css: false`
 * (vite.config.ts): стилей в jsdom нет, медиазапросы не применяются, координаты
 * нулевые. Геометрию машина может сверить только сличением правил.
 */

/** Пути относительные: vitest запускается из каталога frontend. */
const читать = (путь: string): string => readFileSync(путь, "utf-8") as string;

const HTML = читать("index.html");
const LAYOUT_CSS = читать("src/app/app-layout.css");
const LAYOUT = читать("src/app/AppLayout.tsx");
const ROUTER = читать("src/app/router.tsx");
const MAIN = читать("src/main.tsx");

const ВСТРОЕННЫЙ_СТИЛЬ = /<style>([\s\S]*?)<\/style>/.exec(HTML)?.[1] ?? "";
const ВСТРОЕННЫЙ_СКРИПТ = /<script>([\s\S]*?)<\/script>/.exec(HTML)?.[1] ?? "";

const разметка = new DOMParser().parseFromString(HTML, "text/html");

function безКомментариев(текст: string): string {
  return текст.replace(/\/\*[\s\S]*?\*\//g, " ");
}

/** `var(--имя, что угодно)` → `var(--имя)`: запасное значение сравнению не мешает. */
function безЗапасных(значение: string): string {
  let out = "";
  for (let i = 0; i < значение.length; ) {
    if (!значение.startsWith("var(", i)) {
      out += значение[i];
      i += 1;
      continue;
    }
    let глубина = 0;
    let запятая = -1;
    let j = i + 3;
    for (; j < значение.length; j += 1) {
      const c = значение[j];
      if (c === "(") глубина += 1;
      else if (c === ")") {
        глубина -= 1;
        if (глубина === 0) break;
      } else if (c === "," && глубина === 1 && запятая === -1) запятая = j;
    }
    out += `var(${значение.slice(i + 4, запятая === -1 ? j : запятая).trim()})`;
    i = j + 1;
  }
  return out;
}

/** Правила ВЕРХНЕГО уровня: содержимое @media и @keyframes выбрасывается целиком. */
function безВложенных(css: string): string {
  const куски: string[] = [];
  let i = 0;
  while (i < css.length) {
    const at = css.indexOf("@", i);
    if (at === -1) {
      куски.push(css.slice(i));
      break;
    }
    куски.push(css.slice(i, at));
    let j = css.indexOf("{", at);
    if (j === -1) break;
    let глубина = 0;
    for (; j < css.length; j += 1) {
      if (css[j] === "{") глубина += 1;
      else if (css[j] === "}") {
        глубина -= 1;
        if (глубина === 0) {
          j += 1;
          break;
        }
      }
    }
    i = j;
  }
  return куски.join("");
}

/** Тело at-правила (`@keyframes …`) — со скобками внутри. */
function телоAt(css: string, начало: string): string {
  const i = css.indexOf(начало);
  if (i === -1) return "";
  const открыв = css.indexOf("{", i);
  if (открыв === -1) return "";
  let глубина = 0;
  for (let j = открыв; j < css.length; j += 1) {
    if (css[j] === "{") глубина += 1;
    else if (css[j] === "}") {
      глубина -= 1;
      if (глубина === 0) return css.slice(открыв + 1, j);
    }
  }
  return "";
}

/** Объявления `.lc-preboot__stall` верхнего уровня — вне медиазапросов. */
function правилоСтроки(): string {
  const m = /\.lc-preboot__stall\s*\{([^}]*)\}/.exec(
    безВложенных(безКомментариев(ВСТРОЕННЫЙ_СТИЛЬ)),
  );
  return m?.[1] ?? "";
}

function правилаКаркаса(css: string): Map<string, string[]> {
  const итог = new Map<string, string[]>();
  for (const m of безВложенных(безКомментариев(css)).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const объявления = m[2]
      .split(";")
      .map((пара) => {
        const двоеточие = пара.indexOf(":");
        if (двоеточие === -1) return "";
        const свойство = пара.slice(0, двоеточие).trim();
        const значение = безЗапасных(пара.slice(двоеточие + 1))
          .trim()
          .replace(/\s+/g, " ");
        return свойство && значение ? `${свойство}: ${значение}` : "";
      })
      .filter(Boolean);
    for (const селектор of m[1].split(",").map((s) => s.trim().replace(/\s+/g, " "))) {
      if (!селектор.startsWith(".lc-boot")) continue;
      итог.set(селектор, [...(итог.get(селектор) ?? []), ...объявления].sort());
    }
  }
  return итог;
}

/**
 * ═══ 1. КАРКАС ЛЕЖИТ ТАМ, ГДЕ ЕГО СНИМЕТ REACT ═══
 *
 * Подмена держится на одном свойстве `createRoot`: свой контейнер он очищает в
 * том же коммите, в котором вставляет первый кадр. Отсюда правило — каркас
 * лежит ВНУТРИ `#root`. Соседом он пережил бы монтирование и остался бы висеть
 * поверх работы, пока кто-нибудь не вспомнит убрать его руками из `main.tsx`.
 */
describe("Каркас до бандла лежит внутри #root", () => {
  it("разметка каркаса — потомок того самого контейнера", () => {
    expect(
      разметка.querySelector("#root > .lc-preboot"),
      "каркас уехал из #root — снимать его станет некому",
    ).toBeTruthy();
    expect(
      MAIN,
      "main.tsx монтируется не в #root — каркас останется на экране навсегда",
    ).toContain('document.getElementById("root")');
  });

  it("первый кадр React сносит каркас вместе с поддеревом", () => {
    /*
     * Не «проверка React», а проверка НАШЕЙ расстановки: в контейнер кладётся
     * тело настоящего index.html, и после монтирования во ВСЁМ документе не
     * должно остаться ни одного `.lc-preboot`. Вынеси каркас из `#root` — и
     * этот сторож покраснеет, потому что сосед переживёт очистку.
     */
    document.body.innerHTML = разметка.body.innerHTML;
    const контейнер = document.getElementById("root");
    expect(контейнер, "в index.html пропал #root").toBeTruthy();
    expect(document.querySelector(".lc-preboot")).toBeTruthy();

    render(createElement("main", null, "рабочее место"), { container: контейнер! });

    expect(
      document.querySelector(".lc-preboot"),
      "каркас пережил монтирование — он висит поверх рабочего места",
    ).toBeNull();
  });

  it("состав панелей тот же, что у AppChromeSkeleton", () => {
    /*
     * Разошёлся состав — разошлась и рамка: скелет React обещает одно, статика
     * рисует другое, и в кадре подмены колонки прыгают. Ровно это чинили 05.09,
     * когда у скелета не хватало третьей колонки.
     */
    const классыРазметки = [...разметка.querySelectorAll("#root [class]")]
      .map((узел) =>
        [...узел.classList].filter((имя) => имя.startsWith("lc-boot")).join(" "),
      )
      .filter(Boolean);

    const скелет = LAYOUT.slice(
      LAYOUT.indexOf("export function AppChromeSkeleton"),
      LAYOUT.indexOf("export function AppLayout"),
    );
    expect(скелет.length, "AppChromeSkeleton пропал из AppLayout.tsx").toBeGreaterThan(200);
    const классыСкелета = [...безКомментариев(скелет).matchAll(/className="([^"]+)"/g)]
      .map((m) => m[1].split(/\s+/).filter((имя) => имя.startsWith("lc-boot")).join(" "))
      .filter(Boolean);

    expect(классыСкелета.length, "перестали находиться классы скелета").toBeGreaterThan(3);
    expect(
      классыРазметки,
      "статический каркас и AppChromeSkeleton рисуют разные рамки — в кадре подмены она дёрнется",
    ).toEqual(классыСкелета);
    // Корень статики несёт ещё и свою метку: по ней его прячет гейт.
    expect(разметка.querySelector(".lc-boot.lc-preboot")).toBeTruthy();
  });
});

/**
 * ═══ 2. ГЕОМЕТРИЯ ВСТРОЕННОГО СТИЛЯ = БАЗОВЫЕ ПРАВИЛА app-layout.css ═══
 *
 * В бою геометрию каркасу задаёт обязательный CSS: он render-blocking, то есть
 * приходит РАНЬШЕ первой отрисовки, и статика с первым кадром React считаются
 * одними правилами. Встроенная копия — запас на случай, когда CSS не доехал
 * вовсе. Запас имеет право быть неполным (ступени медиазапросов остаются в
 * файле), но НЕ имеет права противоречить: разойдись значения — и в тот
 * единственный раз, когда копия работает, она нарисует не ту рамку.
 */
describe("Встроенный стиль повторяет базовые правила app-layout.css", () => {
  it("те же селекторы и те же значения", () => {
    const файл = правилаКаркаса(LAYOUT_CSS);
    const встроенный = правилаКаркаса(ВСТРОЕННЫЙ_СТИЛЬ);

    expect(файл.size, "разбор перестал находить базовые правила .lc-boot*").toBeGreaterThan(5);
    expect(
      [...встроенный.keys()].sort(),
      "набор правил во встроенном стиле разошёлся с app-layout.css",
    ).toEqual([...файл.keys()].sort());

    for (const [селектор, объявления] of файл) {
      expect(
        встроенный.get(селектор),
        `${селектор}: запасная копия обещает не ту геометрию, что рабочее место`,
      ).toEqual(объявления);
    }
  });

  it("запасные цвета не достаются скелету React и не спорят с токенами", () => {
    /*
     * Литералы живут отдельными именами (`--lc-fb-*`) и объявлены ТОЛЬКО на
     * `.lc-preboot`. Напиши их прямо в `.lc-boot` — и они пересилили бы токен
     * темы у скелета React: в пресете человек увидел бы вспышку базового
     * цвета ровно в кадре подмены.
     */
    const стиль = безКомментариев(ВСТРОЕННЫЙ_СТИЛЬ);
    for (const m of стиль.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const селектор = m[1].trim().replace(/\s+/g, " ");
      // Именно ОБЪЯВЛЕНИЕ, а не ссылка: `var(--lc-bg-1, var(--lc-fb-1, …))`
      // упоминает запасной цвет и в правилах `.lc-boot*`, и это норма.
      if (!/(^|;)\s*--lc-fb-[\w-]+\s*:/.test(m[2])) continue;
      expect(
        селектор.includes(".lc-preboot"),
        `запасные цвета объявлены на ${селектор} — они достанутся и скелету React`,
      ).toBe(true);
    }
    expect(стиль, "запасные цвета пропали — без CSS каркас будет прозрачным").toMatch(
      /--lc-fb-1:\s*#/,
    );
    expect(
      стиль,
      "у светлой темы не стало своих запасных цветов — вспышка тёмного экрана вернётся",
    ).toMatch(/\[data-mantine-color-scheme="light"\][^{]*\.lc-preboot\s*\{/);
  });

  it("гейт спрятан правилом сильнее, чем .lc-boot из app-layout.css", () => {
    /*
     * `.lc-boot { display: flex }` приезжает СЛЕДУЮЩИМ файлом. Спрячь каркас
     * правилом такой же силы — и на экране входа он появится обратно, потому
     * что при равной силе побеждает то, что позже.
     */
    const прячущее = [
      ...безВложенных(безКомментариев(ВСТРОЕННЫЙ_СТИЛЬ)).matchAll(/([^{}]+)\{([^{}]*)\}/g),
    ].find((m) => /display:\s*none/.test(m[2]));
    expect(прячущее, "правило, прячущее каркас, исчезло из встроенного стиля").toBeTruthy();

    const селектор = прячущее![1].trim().replace(/\s+/g, " ");
    expect(селектор, "гейт больше не смотрит на data-lc-boot").toContain("data-lc-boot");
    expect(
      селектор,
      "гейт стал не сильнее `.lc-boot` — на экране входа каркас появится обратно",
    ).toMatch(/^html[^\s]*\s+\.lc-preboot$/);
  });
});

/**
 * ═══ 3. КАРКАС НЕ ОСТАЁТСЯ НАВСЕГДА ═══
 *
 * Каркас честен, пока едет бандл. Не доехал — упал чанк после выкатки,
 * оборвалась связь — и человек смотрит на дышащие панели, которые никогда не
 * наполнятся. Сроком заведует CSS, а не таймер: подмена React сносит поддерево
 * вместе с незапущенной анимацией, поэтому мигнуть эта строка не может.
 */
describe("Через 20 секунд каркас признаётся, что бандл не доехал", () => {
  it("строка есть в разметке и появляется по времени", () => {
    expect(
      разметка.querySelector("#root .lc-preboot__stall")?.textContent?.trim(),
      "из каркаса убрали выход из тупика — вечно дышащие панели и ни слова человеку",
    ).toBeTruthy();

    const правило = /\.lc-preboot__stall\s*\{([^}]*)\}/.exec(
      безВложенных(безКомментариев(ВСТРОЕННЫЙ_СТИЛЬ)),
    );
    expect(
      правило,
      "правило строки пропало или уехало внутрь медиазапроса — под prefers-reduced-motion его прятать нельзя",
    ).toBeTruthy();
    expect(правило![1], "строка видна сразу — она обвинит бандл, который ещё едет").toMatch(
      /opacity:\s*0/,
    );
    expect(правило![1], "выдержка перестала быть 20 секунд").toMatch(/animation:[^;]*\b20s\b/);
  });

  it("двадцать секунд — это ЗАДЕРЖКА, и в конце строка правда видна", () => {
    /*
     * ⚠ ПРОВЕРКА ВЫШЕ ЗЕЛЕНЕЛА НА СЛОМАННОМ ВЫХОДЕ ИЗ ТУПИКА (диверсия 07.09).
     * Она смотрит на строку `animation:` целиком и на то, что там встречается
     * `20s`. Мимо неё проходят три поломки, и каждая оставляет человека перед
     * вечно дышащими панелями без единого слова:
     *
     *   `@keyframes … { to { opacity: 0 } }` — строка не появится никогда;
     *   `animation: … 20s ease 0.4s forwards` — 20 секунд стали ДЛИТЕЛЬНОСТЬЮ,
     *      и строка начинает проступать почти сразу, обвиняя бандл, который
     *      ещё едет;
     *   потерянный `forwards` — строка мигнёт 0,4 с и погаснет обратно.
     *
     * Поэтому здесь разбирается сама сокращённая запись: первое время в ней —
     * длительность, второе — задержка (так устроено `animation`), а конечный
     * кадр обязан оставлять строку видимой.
     */
    const объявление = /animation:\s*([^;]+)/.exec(правилоСтроки())?.[1] ?? "";
    expect(объявление, "у строки тупика не стало анимации — сроку неоткуда взяться").toBeTruthy();

    const имя = объявление.trim().split(/\s+/)[0];
    const времена = (объявление.match(/(?:\d+(?:\.\d+)?|\.\d+)m?s\b/g) ?? []).map((t) =>
      t.endsWith("ms") ? parseFloat(t) : parseFloat(t) * 1000,
    );
    expect(
      времена.length,
      "в записи анимации не два времени — какое из них задержка, стало непонятно",
    ).toBe(2);
    expect(
      времена[1],
      "20 секунд оказались длительностью, а не задержкой: строка проступает сразу и врёт",
    ).toBeGreaterThanOrEqual(15000);
    expect(
      времена[0],
      "проявление растянуто — человек несколько секунд не понимает, появилась строка или нет",
    ).toBeLessThanOrEqual(2000);
    expect(
      объявление,
      "пропал forwards — строка мигнёт и погаснет, тупик снова молчит",
    ).toMatch(/\bforwards\b/);

    const кадры = телоAt(безКомментариев(ВСТРОЕННЫЙ_СТИЛЬ), `@keyframes ${имя}`);
    expect(кадры, `@keyframes ${имя} не найден — анимация ссылается в пустоту`).toBeTruthy();
    const конечный = /(?:^|})\s*(?:to|100%)\s*\{([^}]*)\}/.exec(кадры);
    expect(конечный, "у анимации нет конечного кадра").toBeTruthy();
    const прозрачность = /opacity:\s*([\d.]+)/.exec(конечный![1]);
    expect(прозрачность, "конечный кадр перестал трогать прозрачность").toBeTruthy();
    expect(
      parseFloat(прозрачность![1]),
      "конечный кадр оставляет строку прозрачной — выход из тупика не появится никогда",
    ).toBeGreaterThan(0);
  });

  it("до срока строки нет и в дереве доступности", () => {
    /*
     * ⚠ `opacity: 0` ПРЯЧЕТ ОТ ГЛАЗ, НО НЕ ОТ СКРИНРИДЕРА (правило ARIA:
     * прячут `display`, `visibility` или `aria-hidden`). Строка лежит внутри
     * `role="status"`, так что человеку со скринридером «Рабочее место не
     * загрузилось» доставалось бы при КАЖДОЙ здоровой загрузке — за двадцать
     * секунд до того, как это стало бы правдой.
     *
     * Лечит `visibility: hidden`, и он же обязан вернуться кадром анимации
     * (проверено в браузере перемоткой на 20,5 с). `aria-hidden` не годится:
     * он отнял бы у этого человека выход из тупика насовсем, что хуже
     * исходной болтливости.
     */
    expect(
      правилоСтроки(),
      "строку прячет одна прозрачность — скринридер прочтёт её сразу, до всякого тупика",
    ).toMatch(/visibility:\s*hidden/);

    const объявление = /animation:\s*([^;]+)/.exec(правилоСтроки())?.[1] ?? "";
    const кадры = телоAt(безКомментариев(ВСТРОЕННЫЙ_СТИЛЬ), `@keyframes ${объявление.trim().split(/\s+/)[0]}`);
    const конечный = /(?:^|})\s*(?:to|100%)\s*\{([^}]*)\}/.exec(кадры);
    expect(
      конечный?.[1],
      "конечный кадр не возвращает видимость — со скринридером выхода из тупика не будет",
    ).toMatch(/visibility:\s*visible/);
  });
});

/**
 * ═══ 4. СКРИПТ: АДРЕС И ШИРИНА РЕЛЬСЫ ═══
 *
 * Здесь встроенный скрипт из `index.html` ВЫПОЛНЯЕТСЯ, а не читается глазами:
 * сверка по строкам пропустила бы и опечатку в условии, и порядок, при котором
 * гейт не ставится вовсе.
 */
describe("Встроенный скрипт решает про каркас", () => {
  const выполнить = (): void => {
    new Function(ВСТРОЕННЫЙ_СКРИПТ)();
  };

  const рельса = (): string =>
    document.documentElement.style.getPropertyValue("--lc-preboot-rail");

  beforeEach(() => {
    document.documentElement.removeAttribute("data-lc-boot");
    document.documentElement.removeAttribute("data-mantine-color-scheme");
    document.documentElement.removeAttribute("style");
    localStorage.clear();
    history.pushState({}, "", "/chats");
  });

  afterEach(() => {
    history.pushState({}, "", "/");
  });

  it("на рабочих адресах каркас разрешён", () => {
    for (const адрес of ["/", "/chats", "/settings/team", "/что-то-неизвестное"]) {
      document.documentElement.removeAttribute("data-lc-boot");
      history.pushState({}, "", адрес);
      выполнить();
      expect(
        document.documentElement.hasAttribute("data-lc-boot"),
        `на ${адрес} каркас не показан — человек снова смотрит в пустоту`,
      ).toBe(true);
    }
  });

  it("на входе и по приглашению каркаса нет", () => {
    // Хвостовая косая — тот же маршрут для react-router, значит и для гейта.
    for (const адрес of ["/login", "/login/", "/invite/abc123", "/invite/abc123/"]) {
      document.documentElement.removeAttribute("data-lc-boot");
      history.pushState({}, "", адрес);
      выполнить();
      expect(
        document.documentElement.hasAttribute("data-lc-boot"),
        `на ${адрес} обещан каркас рабочего места, которого там не будет`,
      ).toBe(false);
    }
  });

  it("пустое хранилище — панель развёрнута, как умолчание railStore", () => {
    выполнить();
    expect(рельса()).toBe(`${RAIL_WIDTH_EXPANDED}px`);
  });

  it("свёрнутую панель каркас помнит", () => {
    localStorage.setItem("leadchat-rail", JSON.stringify({ state: { expanded: false }, version: 0 }));
    выполнить();
    expect(рельса()).toBe(`${RAIL_WIDTH_COLLAPSED}px`);
  });

  it("испорченное хранилище не роняет ни тему, ни каркас", () => {
    /*
     * ⚠ Разбор хранилища стоит ПОСЛЕ темы и в своей ловушке. Слейся эти две
     * попытки в одну — испорченный `leadchat-rail` уводил бы светлую тему в
     * тёмную, то есть чинил бы одну вспышку, заводя другую.
     */
    localStorage.setItem("mantine-color-scheme-value", "light");
    localStorage.setItem("leadchat-rail", "{это не json");
    выполнить();
    expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe("light");
    expect(document.documentElement.hasAttribute("data-lc-boot")).toBe(true);
    expect(рельса()).toBe(`${RAIL_WIDTH_EXPANDED}px`);
  });

  it("на телефоне панель свёрнута, что бы ни лежало в хранилище", () => {
    const прежний = window.matchMedia;
    window.matchMedia = ((запрос: string) =>
      ({
        matches: запрос === `(max-width: ${MOBILE_MAX}px)`,
        media: запрос,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList) as typeof window.matchMedia;
    try {
      выполнить();
      expect(рельса()).toBe(`${RAIL_WIDTH_COLLAPSED}px`);
    } finally {
      window.matchMedia = прежний;
    }
  });

  it("ширину, которую поставил скрипт, читает именно каркас", () => {
    /*
     * ⚠ ПРОВЕРКИ ВЫШЕ ЗЕЛЕНЕЛИ С ПЕРЕРЕЗАННОЙ ПРОВОДКОЙ (диверсия 07.09).
     * Они смотрят только на то, ЧТО скрипт положил на корень. Убери из
     * `.lc-preboot` объявление `--lc-rail-w` — переменная по-прежнему
     * выставляется, все прогоны зелены, а каркас берёт запасные 72 из
     * lc-vars.css. При развёрнутой панели (умолчание) работа в первом кадре
     * стоит на 146 пикселей левее, чем через мгновение нарисует React, — тот
     * самый рывок, который чинили 05.09 внутри скелета.
     *
     * Имя переменной берётся не из литерала, а из того, что скрипт РЕАЛЬНО
     * записал: переименуют её в одном месте из двух — здесь станет красно.
     */
    выполнить();
    const инлайн = document.documentElement.getAttribute("style") ?? "";
    const свои = [...инлайн.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]);
    expect(свои, "скрипт перестал выставлять переменную ширины рельсы").toEqual([
      "--lc-preboot-rail",
    ]);

    const стиль = безКомментариев(ВСТРОЕННЫЙ_СТИЛЬ);
    const правило = [...стиль.matchAll(/([^{}]+)\{([^{}]*)\}/g)].find((m) =>
      new RegExp(`--lc-rail-w:\\s*var\\(\\s*${свои[0]}\\b`).test(m[2]),
    );
    expect(
      правило,
      `${свои[0]} никто не читает: каркас останется на запасной ширине из lc-vars.css`,
    ).toBeTruthy();
    expect(
      правило![1].trim(),
      "ширина рельсы объявлена не на каркасе — она достанется и скелету React",
    ).toContain(".lc-preboot");
  });

  it("числа рельсы взяты из railStore и breakpoints, а не придуманы", () => {
    /*
     * Прогон выше проверяет ПОВЕДЕНИЕ скрипта и не заметит, если 218 уедет в
     * railStore, а в разметке останется старое: разъедутся именно те два
     * места, совпадение которых и есть вся затея.
     */
    const скрипт = безКомментариев(ВСТРОЕННЫЙ_СКРИПТ);
    expect(скрипт, "ширина развёрнутой панели разошлась с RAIL_WIDTH_EXPANDED").toContain(
      `"${RAIL_WIDTH_EXPANDED}px"`,
    );
    expect(скрипт, "ширина свёрнутой панели разошлась с RAIL_WIDTH_COLLAPSED").toContain(
      `"${RAIL_WIDTH_COLLAPSED}px"`,
    );
    expect(скрипт, "порог телефона разошёлся с MOBILE_MAX").toContain(
      `(max-width: ${MOBILE_MAX}px)`,
    );
    expect(скрипт, "ключ хранилища разошёлся с railStore").toContain('"leadchat-rail"');
    expect(читать("src/shared/stores/railStore.ts"), "имя хранилища переименовали").toContain(
      '{ name: "leadchat-rail" }',
    );
  });

  it("гейт знает про ВСЕ адреса без рабочего места", () => {
    /*
     * Публичные маршруты — те, что объявлены до `RequireAuth`. Появится третий
     * (второй экран входа, страница ошибки) — здесь станет красно, а не на
     * экране у человека, которому каркас пообещает чужую рамку.
     */
    const карта = безКомментариев(ROUTER);
    const начало = карта.indexOf("createBrowserRouter([");
    const охрана = карта.indexOf("<RequireAuth />");
    expect(начало, "карта маршрутов не найдена").toBeGreaterThan(-1);
    expect(охрана, "RequireAuth пропал из карты маршрутов").toBeGreaterThan(начало);

    const публичные = [...карта.slice(начало, охрана).matchAll(/path:\s*"([^"]+)"/g)].map(
      (m) => m[1],
    );
    expect(публичные.length, "публичные маршруты перестали находиться").toBeGreaterThan(1);

    const скрипт = безКомментариев(ВСТРОЕННЫЙ_СКРИПТ);
    for (const адрес of публичные) {
      // `/invite/:token` в скрипте закрыт префиксом — сравнивать надо по нему.
      const образец = адрес.split("/:")[0];
      expect(
        скрипт,
        `маршрут ${адрес} рабочего места не имеет, а гейт про него не знает`,
      ).toContain(`"${образец}`);
    }
  });
});

/**
 * ═══ 5. ВСТРОЕННЫЙ СКРИПТ РОВНО ОДИН ═══
 *
 * CSP пускает его по хешу содержимого. Второй встроенный скрипт браузер
 * заблокирует молча: страница не упадёт, просто перестанет делать то, ради чего
 * его добавили, — и заметить это можно будет только по консоли у людей.
 * (Сам хеш стережёт `CspInlineHash0109`.)
 */
describe("Встроенных скриптов в index.html ровно один", () => {
  it("второго нет", () => {
    const встроенные = [...разметка.querySelectorAll("script")].filter(
      (узел) => !узел.getAttribute("src"),
    );
    expect(
      встроенные.length,
      "встроенных скриптов стало больше одного — лишний будет молча заблокирован CSP",
    ).toBe(1);
  });
});

/**
 * ═══ 6. ПРЕДЗАГРУЗКА ДВУХ ПОДМНОЖЕСТВ INTER ═══
 *
 * Имена файлов шрифта содержат хеш и меняются каждой сборкой — вписать их в
 * `index.html` руками нельзя. Подставляет их хук `lc-preload-inter`; здесь он и
 * вызывается, с поддельным составом сборки.
 */
describe("Хук предзагрузки шрифта", () => {
  type Хук = (
    html: string,
    ctx: { path: string; filename: string; bundle?: Record<string, unknown> },
  ) => { html: string; tags: { tag: string; attrs: Record<string, string>; injectTo: string }[] };

  const СБОРКА = Object.fromEntries(
    [
      "assets/index-BFGmBGBF.js",
      "assets/index-BUySbU0z.css",
      "assets/inter-cyrillic-ext-wght-normal-BOeWTOD4.woff2",
      "assets/inter-cyrillic-wght-normal-DqGufNeO.woff2",
      "assets/inter-greek-wght-normal-CkhJZR-_.woff2",
      "assets/inter-latin-ext-wght-normal-DO1Apj_S.woff2",
      "assets/inter-latin-wght-normal-Dx4kXJAl.woff2",
      "assets/inter-vietnamese-wght-normal-CBcvBZtf.woff2",
    ].map((имя) => [имя, {}]),
  );

  /*
   * Плагин берётся из своего модуля, а не из `vite.config.ts`: импорт конфига
   * тащит в прогон esbuild, который в jsdom не заводится. Поэтому отдельной
   * строкой проверяется и то, что конфиг его подключает, — иначе сторож
   * зеленел бы на плагине, выключенном в сборке.
   */
  function хук(): Хук {
    const плагин = предзагрузитьInter();
    expect(плагин.name).toBe("lc-preload-inter");
    const преобразование = плагин.transformIndexHtml;
    expect(
      typeof преобразование === "object" && преобразование !== null && "handler" in преобразование,
      "хук перестал быть объектом с handler",
    ).toBe(true);
    return (преобразование as unknown as { handler: Хук }).handler;
  }

  it("подключён в сборке", () => {
    expect(
      безКомментариев(читать("vite.config.ts")).replace(/\s+/g, " "),
      "плагин выключен в vite.config.ts — шрифт снова ждёт разбора CSS",
    ).toContain("предзагрузитьInter()");
  });

  it("подставляет ровно латиницу и кириллицу, кириллицу первой", () => {
    const итог = хук()("<html></html>", {
      path: "/index.html",
      filename: "index.html",
      bundle: СБОРКА,
    });

    expect(
      итог.tags.map((t) => t.attrs.href),
      "предзагружаются не те подмножества: расширенных в интерфейсе нет, а за ними 111 КБ",
    ).toEqual([
      "/assets/inter-cyrillic-wght-normal-DqGufNeO.woff2",
      "/assets/inter-latin-wght-normal-Dx4kXJAl.woff2",
    ]);

    for (const тег of итог.tags) {
      expect(тег.tag).toBe("link");
      expect(тег.attrs.rel).toBe("preload");
      expect(тег.attrs.as).toBe("font");
      expect(тег.attrs.type).toBe("font/woff2");
      // Без crossorigin браузер скачает файл дважды: ключи кэша не совпадут.
      expect(тег.attrs, "пропал crossorigin — шрифт поедет по сети дважды").toHaveProperty(
        "crossorigin",
      );
      /*
       * ⚠ ПРОВЕРКА АТРИБУТОВ ЗЕЛЕНЕЛА С ПРЕДЗАГРУЗКОЙ В КОНЦЕ ТЕЛА
       * (диверсия 07.09). Смысл этой правки — назвать шрифт РАНЬШЕ, чем о нём
       * узнает CSS; уехав в конец документа, ссылка становится честной, но
       * бесполезной, и все прогоны остаются зелёными.
       */
      expect(
        тег.injectTo,
        "предзагрузка уехала из <head> — браузер узнает о шрифте после всего документа",
      ).toBe("head");
    }
  });

  it("пропавший файл роняет сборку, а не тихо исчезает из разметки", () => {
    /*
     * Смени шрифт или убери его импорт — и предзагрузка перестала бы работать
     * молча, оставив в разметке правдоподобную пустоту. Пусть падает сборка.
     */
    const урезанная = { ...СБОРКА };
    delete урезанная["assets/inter-latin-wght-normal-Dx4kXJAl.woff2"];
    const обработчик = хук();
    expect(() =>
      обработчик("<html></html>", {
        path: "/index.html",
        filename: "index.html",
        bundle: урезанная,
      }),
    ).toThrow(/latin/);
  });

  it("в dev, где имён с хешем нет, хук не выдумывает ссылок", () => {
    const итог = хук()("<html></html>", {
      path: "/index.html",
      filename: "index.html",
    }) as unknown;
    expect(итог, "хук что-то подставил без сборки").toBe("<html></html>");
  });
});
