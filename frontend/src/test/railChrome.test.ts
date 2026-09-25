// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно. Так же сделано в
// breakpoints.test.ts, cssDeadClasses.test.ts и mantineStyles.test.ts.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * СТОРОЖ ЛЕВОЙ ПАНЕЛИ (макет владельца от 12 августа).
 *
 * ЧТО ЗДЕСЬ БЫЛО РАНЬШЕ. Этот файл назывался `headerNarrow.test.ts` и сторожил
 * шапку: при ширине 390 её правая половина переносилась на вторую строку — у
 * `Group` из Mantine перенос стоит умолчанием, — а высота шапки прибита к
 * 56px. Замер: группа высотой 84px с координатами y = −15…69; точка связи,
 * кнопка «?» и колокольчик уезжали за ВЕРХНИЙ край окна, а кнопка меню
 * пользователя вылезала на 14px ниже шапки, поверх содержимого.
 *
 * Той шапки больше нет: всё, что в ней теснилось, переехало в левый столбец, и
 * теснота ушла вместе с причиной. Но уроки остались, и они переписаны на новую
 * разметку — иначе сторож остался бы зелёным навсегда, сторожа пустое место.
 *
 * ПОЧЕМУ СТОРОЖ ЧИТАЕТ ИСХОДНИКИ, А НЕ РИСУЕТ ЭКРАН. В vitest стоит
 * `css: false` (vite.config.ts) — в jsdom стилей нет вовсе, медиазапросы не
 * применяются, координаты у всего нулевые. Рендер-тест такого наложения не
 * увидит НИКОГДА; его и нашли замером в браузере. Здесь проверяется то
 * единственное, что проверяемо машиной: правила на месте и классы надеты.
 */

/** Пути относительные: vitest запускается из каталога frontend. */
const CSS = readFileSync("src/app/app-rail.css", "utf-8") as string;
const LAYOUT_CSS = readFileSync("src/app/app-layout.css", "utf-8") as string;
const RAIL = readFileSync("src/app/AppRail.tsx", "utf-8") as string;
const LAYOUT = readFileSync("src/app/AppLayout.tsx", "utf-8") as string;

/**
 * Комментарии вырезаем ДО поиска: иначе объяснение «класс раньше не прятался»
 * засчиталось бы за правило, и сторож замолчал бы ровно там, где нужен.
 */
function stripCssComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, " ");
}

/**
 * Открывающие теги разметки, у которых в `className` есть `cls`.
 *
 * ПОЧЕМУ НЕ РЕГУЛЯРКОЙ ДО ПЕРВОГО `>`. Так и было написано сначала — и сторож
 * соврал на первом же прогоне: у кнопки звука в атрибутах стоит стрелочная
 * функция `onClick={() => …}`, её `>` обрывал тег ровно перед `aria-label`, и
 * сторож объявил названную строку безымянной. Поэтому конец тега ищется по
 * `>` на НУЛЕВОЙ глубине фигурных скобок.
 */
function openingTagsWithClass(cls: string): string[] {
  const out: string[] = [];
  for (const m of RAIL.matchAll(new RegExp(`className="${cls}`, "g"))) {
    const open = RAIL.lastIndexOf("<", m.index);
    let depth = 0;
    for (let i = open; i < RAIL.length; i += 1) {
      const ch = RAIL[i];
      if (ch === "{") depth += 1;
      else if (ch === "}") depth -= 1;
      else if (ch === ">" && depth === 0) {
        out.push(RAIL.slice(open, i + 1));
        break;
      }
    }
  }
  return out;
}

/** Объявления всех правил, в списке селекторов которых есть `selector`. */
function rulesFor(css: string, selector: string): string[] {
  const out: string[] = [];
  for (const m of stripCssComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = m[1].split(",").map((s) => s.trim().replace(/\s+/g, " "));
    if (selectors.includes(selector)) out.push(m[2]);
  }
  return out;
}

/** Всё, что панель прячет в свёрнутом виде. */
const HIDDEN_WHEN_COLLAPSED = [
  ".lc-rail__label",
  ".lc-rail__section",
  ".lc-rail__key",
  ".lc-rail__wordmark",
  ".lc-rail__collapse",
  ".lc-rail__who",
];

describe("Свёрнутая панель прячет подписи — правилами, а не обещаниями", () => {
  it("правило свёрнутого вида вообще есть", () => {
    // Пустой результат означал бы, что сторож ничего не читает и зелен
    // всегда, — худший из отказов, незаметный.
    const collapsed = stripCssComments(CSS).match(/\.lc-rail:not\(\[data-expanded\]\)/g) ?? [];
    expect(collapsed.length).toBeGreaterThan(0);
  });

  for (const cls of HIDDEN_WHEN_COLLAPSED) {
    it(`${cls} прячется в свёрнутом виде`, () => {
      const rules = rulesFor(CSS, `.lc-rail:not([data-expanded]) ${cls}`);
      expect(rules.length, `нет правила для ${cls}`).toBeGreaterThan(0);
      expect(rules.join(" ")).toMatch(/display:\s*none/);
    });

    it(`${cls} действительно надет на разметку`, () => {
      // Правило без разметки — мёртвый CSS: он врёт следующему читателю о том,
      // что в продукте есть.
      const name = cls.slice(1);
      const used = RAIL.includes(`"${name}"`) || RAIL.includes(`${name}"`);
      expect(used, `${cls} не встречается в AppRail.tsx`).toBe(true);
    });
  }
});

/**
 * ОДНА ВЕЛИЧИНА — ОДИН ИСТОЧНИК.
 *
 * Ширина столбца задаётся в разметке (`AppShell navbar={{ width }}`), а
 * подписи прячет CSS. Если ширину решает разметка, а «сворачивать ли на
 * телефоне» — медиазапрос, то на первом же изменении брейкпоинта подписи
 * спрячутся в столбце шириной 218 пикселей: половина панели станет пустой
 * полосой. Поэтому решение ровно одно и живёт в `useRailExpanded`.
 */
describe("Ширину панели решает одно место", () => {
  it("узкий экран учитывается в коде, а не отдельным медиазапросом", () => {
    expect(RAIL).toContain("MOBILE_QUERY");
    expect(RAIL).toContain("useRailExpanded");
    // В стилях панели медиазапросов по ширине быть не должно вовсе.
    expect(stripCssComments(CSS)).not.toMatch(/@media[^{]*width/);
  });

  it("каркас берёт ширину из тех же двух констант", () => {
    expect(LAYOUT).toContain("RAIL_WIDTH_EXPANDED");
    expect(LAYOUT).toContain("RAIL_WIDTH_COLLAPSED");
    expect(LAYOUT).toContain("useRailExpanded");
  });

  /*
   * И САМ ОТВЕТ ЖИВЁТ В ОБЫЧНОМ `.ts`, А НЕ РЯДОМ С КОМПОНЕНТАМИ.
   *
   * Пока `useRailExpanded` был объявлен в `AppRail.tsx`, горячая перезагрузка
   * этого файла давала каркасу и панели РАЗНЫЕ копии одной функции: Vite
   * заменяет `.tsx` целиком, а импортёр остаётся со старой. Два места,
   * которым положено отвечать одинаково, начинали отвечать по-разному — и
   * ровно такую вторую копию модуля мы уже ловили сегодня у хранилища
   * Zustand. Поэтому проверяется не только «решение одно», но и то, что оно
   * объявлено там, где раздвоиться не может.
   */
  it("хук объявлен в хранилище, а не в файле с компонентами", () => {
    const store = readFileSync("src/shared/stores/railStore.ts", "utf-8") as string;
    expect(store).toMatch(/export function useRailExpanded/);
    expect(RAIL, "хук вернулся в AppRail.tsx — там он раздваивается при HMR").not.toMatch(
      /export function useRailExpanded/,
    );
    expect(LAYOUT).not.toMatch(/export function useRailExpanded/);
  });
});

/**
 * ПРЯТАТЬ МОЖНО ТОЛЬКО ТО, ЧТО ЧЕЛОВЕК НАЙДЁТ В ДРУГОМ МЕСТЕ.
 *
 * В свёрнутой панели от сотрудника остаётся один аватар. Значит имя, роль и
 * почта обязаны быть в окне под ним: иначе на узком экране узнать, под кем ты
 * сидишь и с какими правами, будет негде. Ровно этот дефект и ловил прежний
 * сторож — тогда в меню стояла одна почта.
 */
describe("Кто я и с какими правами — находится всегда", () => {
  it("имя, роль и почта стоят в окне под именем", () => {
    const pop = RAIL.slice(RAIL.indexOf("<Popover.Dropdown"), RAIL.indexOf("</Popover.Dropdown>"));
    expect(pop).toContain("user.full_name");
    expect(pop).toContain("ROLE_LABELS[user.role]");
    expect(pop).toContain("user.email");
  });

  it("роль не повторяется на самой кнопке", () => {
    // На боевом стенде шапка читалась как «Администратор Lead Partner /
    // Администратор»: одно слово дважды в двенадцати пикселях друг от друга.
    // Имена сотрудников заводит администратор, и в них уже сказано, кто это.
    const target = RAIL.slice(RAIL.indexOf("<Popover.Target>"), RAIL.indexOf("</Popover.Target>"));
    expect(target).toContain("user.full_name");
    expect(target).not.toContain("ROLE_LABELS");
  });

  it("подписи «by Lead Partner» нет ни в разметке, ни в стилях", () => {
    expect(RAIL).not.toContain("by Lead Partner");
    expect(LAYOUT).not.toContain("by Lead Partner");
    expect(stripCssComments(LAYOUT_CSS)).not.toContain("lc-header__tagline");
  });
});

/**
 * СВЁРНУТАЯ ПАНЕЛЬ — ДЕВЯТЬ БЕЗЫМЯННЫХ ЗНАЧКОВ, и для того, кто ходит табом,
 * они безымянны ровно так же, как для того, кто водит мышью.
 */
describe("Подсказки свёрнутого вида", () => {
  it("рисуются и по наведению, и по клавиатурному фокусу", () => {
    const css = stripCssComments(CSS);
    expect(css).toContain(":hover::after");
    expect(css).toContain(":focus-visible::after");
    expect(css).toContain("content: attr(data-tip)");
  });

  it("каждая строка панели несёт свою подпись", () => {
    // `data-tip` без значения — подсказка-пустышка: рамка без текста.
    const tips = RAIL.match(/data-tip=/g) ?? [];
    expect(tips.length).toBeGreaterThanOrEqual(6);
    expect(RAIL).not.toMatch(/data-tip=""/);
  });
});

/**
 * У СВЁРНУТОЙ СТРОКИ ОСТАЁТСЯ ИМЯ.
 *
 * НАЙДЕНО НА СТЕНДЕ 12 августа деревом доступности. Подпись строки прячется
 * `display: none`, а спрятанный так текст НЕ УЧАСТВУЕТ в вычислении доступного
 * имени. В свёрнутом виде «Разбор диалогов», «Статистика» и «Настройки» были
 * ссылками без названия вообще: скринридер читал «ссылка», и всё. Заметить это
 * глазами нельзя — на экране значки как значки.
 *
 * Поэтому у каждой строки есть собственный `aria-label`, не зависящий от того,
 * видна подпись или нет. Сторож считает не текст, а число строк с именем:
 * следующая добавленная строка без `aria-label` уронит его.
 */
describe("Строка панели названа независимо от подписи", () => {
  it("у каждой строки-ссылки и каждой строки-кнопки есть aria-label", () => {
    /*
     * Считается не общее число `aria-label` в файле, а НАЛИЧИЕ ИМЕНИ У КАЖДОГО
     * открывающего тега со строкой. Счётчик «имён не меньше, чем строк» уже
     * оказался ложно зелёным на проверке ломанием: у одной строки имя убрали,
     * и его недостачу закрыли имена соседей.
     */
    const items = openingTagsWithClass("lc-rail__item");
    // Число точное, а не «не меньше»: добавили строку — придите сюда и
    // убедитесь, что у неё есть имя. Это и есть цена одной цифры.
    //
    // 12 августа стало восемь: добавилась «Живая лента» — экран событий
    // системы в реальном времени, право `stats:all`, как у статистики.
    //
    // 29 августа стало девять: добавились «Диалоги бота» — надзор за работой
    // бота (docs/45), право `bots:manage`, то есть только администратор.
    expect(items.length, "число строк в панели изменилось — проверьте имя новой").toBe(9);

    const anonymous = items.filter((t) => !t.includes("aria-label"));
    expect(anonymous, `строка без имени: ${anonymous.join(" | ")}`).toEqual([]);
  });

  it("подпись, спрятанная в свёрнутом виде, не единственный источник имени", () => {
    // Обратная сторона того же дефекта: если бы имя брали из `.lc-rail__label`,
    // сторож выше был бы зелёным, а имя всё равно пропадало.
    const bell = readFileSync("src/features/notifications/NotificationBell.tsx", "utf-8") as string;
    expect(bell).toContain("aria-label={label}");
  });
  /*
   * ПОДСКАЗКИ СВЁРНУТОГО РЕЛЬСА НЕ ЛОЖАТСЯ НА КОНТЕНТ (разбор интерфейса 13.08).
   *
   * ЧТО БЫЛО. Отступ подписи задавался жёстким `left: 56px` — числом, отмеренным
   * от 44-пиксельного значка, тогда как сам рельс шире (72px), а значок вдвинут
   * внутрь двумя границами. Левый край подписи приходился на 69px, то есть на три
   * пикселя ВНУТРИ рельса: подпись начиналась поверх него и уезжала на контент —
   * в «Чатах» накрывала поле поиска и пустое состояние, в «Разборе» — первый фильтр.
   *
   * Здесь сторожатся четыре свойства правки, и каждое закрывает свою половину беды:
   * отсчёт от ЦЕНТРА якоря (жёсткое число не может обслужить два якоря разного
   * размера — значки 44px и логотип 30px), ПОТОЛОК ширины с многоточием (сдвиг
   * вправо перекрытия не снимает: колонка контента начинается там же, где кончается
   * рельс), ЗАДЕРЖКА (курсор пересекает узкий рельс десятки раз за смену, и без
   * задержки каждый пролёт даёт вспышку) и ТРЕБОВАНИЕ `[data-tip]` у всех трёх
   * якорей (без него логотип без подписи показывал пустую рамку).
   */
  it("подсказка отсчитывается от центра якоря, а не жёстким числом", () => {
    // ⚠ Сравниваем по ОБЪЯВЛЕНИЯМ, а не по тексту файла: в комментарии рядом
    // разобрано, почему прежнее `left: 56px` было неверным, и наивный поиск по
    // всему файлу краснел на самом объяснении.
    const правила = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    expect(правила).not.toMatch(/\bleft:\s*56px/);
    expect(правила).toMatch(/left:\s*calc\(50% \+ var\(--lc-rail-w\) \/ 2/);
  });

  it("у подсказки есть потолок ширины и многоточие", () => {
    expect(CSS).toMatch(/max-width:\s*240px/);
    expect(CSS).toMatch(/text-overflow:\s*ellipsis/);
  });

  it("подсказка появляется с задержкой", () => {
    expect(CSS).toMatch(/transition:\s*opacity 0s linear 400ms/);
  });

  it("подсказку рисуют только якоря с data-tip", () => {
    // Логотип на телефоне идёт без подписи; без этого условия у него всплывала
    // пустая рамка — content пустой, а padding, граница и тень рисуются.
    for (const якорь of ["lc-rail__logo", "lc-rail__me"]) {
      const без = new RegExp(`\\.${якорь}:(hover|focus-visible)::after`);
      expect(CSS, `${якорь} рисует подсказку без data-tip`).not.toMatch(без);
    }
  });

  it("запасная ширина рельса совпадает с настоящей", () => {
    // Читатели переменной вне корня AppShell берут именно это значение. Здесь
    // годами стояло 56 — остаток от рельса прошлой ширины, — и подсказка,
    // посчитанная от него, снова уехала бы внутрь рельса.
    const vars = readFileSync("src/app/lc-vars.css", "utf-8") as string;
    expect(vars).toMatch(/--lc-rail-w:\s*72px/);
    const store = readFileSync("src/shared/stores/railStore.ts", "utf-8") as string;
    expect(store).toMatch(/RAIL_WIDTH_COLLAPSED = 72/);
  });
});
