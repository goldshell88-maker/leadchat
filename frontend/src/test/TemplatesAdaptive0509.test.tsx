// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts и breakpoints.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TemplatesManager } from "@/features/templates/TemplatesManager";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TemplateDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * БЫСТРЫЕ ОТВЕТЫ НА ЛЮБОМ ЭКРАНЕ (разбор со стендом 05.09).
 *
 * Всё, что здесь стережётся, найдено глазами в браузере на пяти ширинах — 375,
 * 768, 1024, 1440, 2560 — а не вычитано из кода. Числа в разборах ниже
 * померены там же.
 *
 * Дороже всего стоила первая находка. Общий рецепт «таблица → карточки»
 * (`src/app/lc-table-cards.css`) делает ячейку ФЛЕКС-КОНТЕЙНЕРОМ и писался под
 * ячейку с ОДНИМ простым значением. Позже текст быстрого ответа научили
 * подсвечивать подстановки, и в ячейке появилась разметка — а флексбокс
 * заворачивает каждый кусок текста между тегами в отдельный элемент. На 375
 * пикселях фраза раскладывалась в пять колонок: «Здравствуйте,  {имя}  ! По
 * адресу из  {объявлен…», хвост уходил за правый край и обрезался, карточка
 * росла до 466 пикселей. Читать нельзя, а это ровно тот текст, который человек
 * проверяет перед отправкой клиенту в Авито.
 *
 * ⚠ ПОЧЕМУ СТОРОЖ ЧИТАЕТ CSS, А НЕ МЕРЯЕТ ЭКРАН. В конфигурации тестов стоит
 * `css: false` (vite.config.ts): в jsdom стилей нет вовсе, любая проверка
 * ширины или высоты вернёт ноль и позеленеет на чём угодно. Поэтому раскладка
 * проверяется по тексту правил, а поведение — рендером. То же разделение
 * принято в surfaceLadder.test.ts и controlScale.test.ts.
 */

const ШАБЛОНЫ = "src/features/templates/templates.css";
const КАРТОЧКИ = "src/app/lc-table-cards.css";

/**
 * Комментарии заменяются пробелами (длина сохраняется, чтобы номера строк не
 * съезжали). Без этого сторож глохнет: в разборах у правил эти же селекторы и
 * значения названы словами, и наивный поиск засчитал бы объяснение за код.
 */
function безКомментариев(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, " "));
}

function читать(путь: string): string {
  return безКомментариев(readFileSync(путь, "utf-8") as string);
}

/** Тело правила по точному селектору. */
function правило(css: string, селектор: string): string {
  const at = css.indexOf(`${селектор} {`);
  expect(at, `правило ${селектор} пропало из стилей`).toBeGreaterThan(-1);
  return css.slice(at, css.indexOf("}", at));
}

/** Содержимое блока, начинающегося с `начало`, по парным скобкам. */
function блок(css: string, начало: string): string {
  const at = css.indexOf(начало);
  expect(at, `блок «${начало}» не найден`).toBeGreaterThan(-1);
  const открывающая = css.indexOf("{", at);
  let глубина = 0;
  let i = открывающая;
  for (; i < css.length; i++) {
    if (css[i] === "{") глубина++;
    else if (css[i] === "}") {
      глубина--;
      if (глубина === 0) break;
    }
  }
  return css.slice(открывающая + 1, i);
}

/** Все пороги `max-width` файла. */
function пороги(css: string): number[] {
  return [...css.matchAll(/@media\s*\(max-width:\s*(\d+)px\)/g)].map((m) => Number(m[1]));
}

describe("Раскладка быстрых ответов держит любую ширину", () => {
  it("порог карточного режима — тот же, что у общего рецепта", () => {
    /*
     * Правки для карточек лежат в двух файлах: превращение таблицы в карточки —
     * в общем рецепте, поведение прозы внутри карточки — здесь. CSS не умеет
     * читать чужой медиазапрос, поэтому число приходится копировать, а копия
     * обязана быть громкой.
     *
     * Разъедься они на пиксель — появится полоса ширин, где таблица уже
     * карточки, а текст ответа ещё разрезан на колонки (или наоборот: таблица
     * ещё таблица, а ячейка уже блок и ломает колонки).
     */
    const общий = пороги(читать(КАРТОЧКИ));
    const свой = пороги(читать(ШАБЛОНЫ));
    expect(общий, "в общем рецепте не стало порога карточек").toContain(900);
    expect(свой, "в стилях быстрых ответов нет правил карточного режима").not.toEqual([]);
    for (const п of свой) {
      expect(общий, `порог ${п}px разъехался с общим рецептом карточек`).toContain(п);
    }
  });

  it("в карточке текст ответа — абзац, а не флекс-строка", () => {
    /*
     * `display: block` здесь не косметика: он снимает флекс-контейнер, из-за
     * которого подсвеченные подстановки растаскивали предложение по колонкам.
     * `text-align: left` — вторая половина: проза, прижатая вправо рваным
     * краем, читается заметно медленнее.
     */
    const карточный = блок(читать(ШАБЛОНЫ), "@media (max-width: 900px)");
    const проза = правило(карточный, ".tpl-table tr td.tpl-table__body");
    expect(проза, "ячейка текста снова флекс — предложение разрежет на колонки").toMatch(
      /display:\s*block/,
    );
    expect(проза, "проза прижата вправо").toMatch(/text-align:\s*left/);

    // Вес селектора считан: перебиваем `.lc-table--cards tr td` — (0,1,2).
    // Меньшего веса не хватит при любом порядке приезда файлов.
    expect(карточный, "подпись ячейки осталась в строку со значением").toMatch(
      /td\.tpl-table__body::before[\s\S]{0,120}display:\s*block/,
    );
  });

  it("ширины колонок заданы долей, а не пикселями и не clamp", () => {
    /*
     * Пиксельные 200 и 140 были постоянными при любой ширине окна: на 1024
     * служебные колонки съедали 64 % таблицы и текст обрезался во всех
     * строках, на 2560 название оставалось теми же 200 и всё равно обрезалось.
     *
     * ⚠ И ОТДЕЛЬНО ПРО `clamp()`. Он выглядит очевидным решением и НЕ
     * РАБОТАЕТ: в режиме `table-layout: fixed` браузер вычислительные функции
     * в ширине `<col>` выбрасывает молча. Померено на стенде: с `clamp`
     * название, текст и папка получили по 342 пикселя вместо 277 / 586 / 162.
     * Правило выглядит написанным, а его нет — поэтому запрет явный.
     */
    const css = читать(ШАБЛОНЫ);
    for (const колонка of [".tpl-col--title", ".tpl-col--folder"]) {
      const тело = правило(css, колонка);
      expect(тело, `${колонка}: ширина перестала быть долей таблицы`).toMatch(/width:\s*\d+%/);
      expect(тело, `${колонка}: вычислительную функцию в ширине <col> браузер выбросит`).not.toMatch(
        /width:\s*(clamp|min|max|calc)\(/,
      );
    }
  });

  it("высота вкладки берётся из токена, а не складывается из отступов", () => {
    /*
     * Замер: `padding: 4px 12px` давало ровно 24 пикселя — нижнюю границу
     * нормы мишени, при 32 у полосы папок СТРОКОЙ НИЖЕ. И вторая половина:
     * шаг шкалы `@media (min-width: 1800px)` поднимает `--lc-btn-h-sm` до 34,
     * а мимо вкладок проходил, потому что их высоту задавал не токен. На 2560
     * весь ряд был 34, вкладки — 24.
     */
    const тело = правило(читать(ШАБЛОНЫ), ".tpl-manager__tabs button");
    expect(тело, "высота вкладки снова не задана — вернутся 24 пикселя").toMatch(
      /height:\s*var\(--lc-btn-h-sm\)/,
    );
  });

  it("имя папки не переносится и не растит строку", () => {
    /*
     * Замер на 1440: высоты строк были 72 / 45 / 45 / 45 — первая в полтора
     * раза выше остальных только потому, что «Бригада Андрея Владиславовича
     * КП · Б6» раскладывалась в три строки. Ячейки выровнены по верху, и
     * соседние значения в такой строке отъезжают вниз.
     */
    const тело = правило(читать(ШАБЛОНЫ), ".tpl-table__folder");
    expect(тело, "папка снова переносится — строка списка поедет").toMatch(
      /white-space:\s*nowrap/,
    );
    expect(тело, "обрезка без многоточия не читается как обрезка").toMatch(
      /text-overflow:\s*ellipsis/,
    );
  });

  it("акцентной заливки на экране не осталось ни одной", () => {
    /*
     * Померено формулой WCAG по токенам продукта: `--lc-primary-subtle` против
     * фона ряда `--lc-bg-2` даёт 1.008 в тёмной теме и 1.054 в светлой, то
     * есть «того же цвета». Три места экрана — вкладка, фильтр по папке и
     * половина «Кто видит» — были залиты ею и не выделялись ничем, а работу
     * делала акцентная рамка.
     *
     * Заменено на поверхность выбранного (1.116 / 1.136) плюс тонкая линия
     * акцента. Заливка акцентом осталась ровно одна на экране — кнопка
     * «Создать быстрый ответ», ради которой сюда и заходят.
     */
    const css = читать(ШАБЛОНЫ);
    expect(css, "акцентная заливка вернулась — она невидима и отбирает единственный акцент").not.toMatch(
      /background:\s*var\(--lc-primary-subtle\)/,
    );
    expect(правило(css, '.tpl-manager__tabs button[data-active="true"]')).toMatch(
      /box-shadow:\s*inset 0 -2px 0 var\(--lc-primary\)/,
    );
    expect(
      правило(css, '.tpl-manager__folders button[data-active="true"]'),
      "выбранная папка потеряла акцентную обводку — фильтр снова станет невидимым",
    ).toMatch(/border-color:\s*var\(--lc-primary\)/);
  });

  it("каждое движение выключается по просьбе системы", () => {
    /*
     * Отрицательная проверка обязана падать по своей причине, поэтому она не
     * «блок существует», а «в блоке названы ВСЕ, кто объявил переход».
     * Добавили переход и забыли про блок — сторож назовёт селектор.
     */
    const css = читать(ШАБЛОНЫ);
    const тише = блок(css, "@media (prefers-reduced-motion: reduce)");
    const снаружи = css.replace(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?\n\}/g, " ");

    const сПереходом = [...снаружи.matchAll(/([^{}]+)\{[^{}]*transition:[^{}]*\}/g)].flatMap((m) =>
      m[1]!
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    );
    // Пустой список означал бы, что переходов нет вовсе, — тогда и проверять
    // нечего, а сторож бы молча позеленел.
    expect(сПереходом.length, "в стилях не осталось ни одного перехода").toBeGreaterThan(0);
    for (const селектор of сПереходом) {
      expect(тише, `${селектор} объявил переход и не выключает его`).toContain(селектор);
    }
  });
});

/* ─────────────────────── поведение строки и пустоты ─────────────────────── */

const ADMIN_PERMISSIONS: Permission[] = ["templates:shared", "templates:own"];

const SHARED: TemplateDto[] = [
  {
    id: "t-1",
    owner_id: null,
    title: "Территориально не подходит",
    body: "Здравствуйте, {имя}! По адресу из {объявление} мы не выезжаем",
    // Имя длиннее колонки — ровно тот случай, ради которого нужна подсказка.
    folder: "Бригада Андрея Владиславовича КП · Б6",
    used_count: 1435,
  },
  {
    id: "t-2",
    owner_id: null,
    title: "Без папки",
    body: "Гарантия 12 месяцев",
    folder: null,
    used_count: 0,
  },
  {
    /** Сервер постарше: поля `used_count` в ответе нет вовсе. */
    id: "t-3",
    owner_id: null,
    title: "Ждём запчасть",
    body: "Запчасть заказана",
    folder: "Сроки",
  },
];

function ответы(items: TemplateDto[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/templates") && (init?.method ?? "GET") === "GET") {
        return jsonResponse(200, {
          items,
          page: { limit: 200, offset: 0, total: items.length },
        });
      }
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    }),
  );
}

function ячейка(название: string, подпись: string): HTMLElement | null {
  const ряд = screen.getByText(название).closest("tr") as HTMLElement;
  return ряд.querySelector<HTMLElement>(`[data-label='${подпись}']`);
}

describe("Строка списка договаривает обрезанное", () => {
  beforeEach(() => {
    queryClient.clear();
    ответы(SHARED);
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("папку режет многоточием — значит подсказка называет её целиком", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    expect(ячейка("Территориально не подходит", "Папка")).toHaveAttribute(
      "title",
      "Бригада Андрея Владиславовича КП · Б6",
    );
  });

  it("у прочерка подсказки нет: договаривать там нечего", async () => {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    /*
     * ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ПАДАЕТ ПО СВОЕЙ ПРИЧИНЕ: ячейка на экране есть
     * и прочерк в ней виден — проверяем именно отсутствие подсказки, а не
     * отсутствие ячейки, которое зеленело бы и на пустой таблице.
     */
    const пустая = ячейка("Без папки", "Папка");
    expect(пустая).toHaveTextContent("—");
    expect(пустая).not.toHaveAttribute("title");
  });

  it("«не знаем» помечено и не выдаёт себя за величину", async () => {
    /*
     * Столбец вставок набран крупнее и жирнее остальной строки — это
     * единственный показатель экрана. Прочерк в том же весе читался бы как
     * ещё одно значение, хотя значения у нас нет: поля просто не было в
     * ответе сервера постарше.
     */
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    expect(ячейка("Ждём запчасть", "Вставок")).toHaveAttribute("data-unknown", "true");
    // Ноль — это ответ, а не пустота, и метки «не знаем» на нём быть не может.
    expect(ячейка("Без папки", "Вставок")).toHaveTextContent("0");
    expect(ячейка("Без папки", "Вставок")).not.toHaveAttribute("data-unknown");
    expect(ячейка("Территориально не подходит", "Вставок")).not.toHaveAttribute("data-unknown");
  });
});

describe("Пустой раздел предлагает единственное действие", () => {
  beforeEach(() => {
    queryClient.clear();
    ответы([]);
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("из пустоты редактор открывается на месте, а не поиском кнопки наверху", async () => {
    /*
     * Из трёх пустых состояний экрана два уже отвечали кнопкой («Сбросить
     * фильтры», «Повторить»), а самое частое — «ещё ни одного не создали» —
     * не отвечало ничем и отправляло искать кнопку глазами в правый верхний
     * угол панели. Личные заготовки за всё время завели двое из пятидесяти
     * семи.
     */
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });

    const пусто = await screen.findByText("Общих быстрых ответов нет");
    const блокПустоты = пусто.closest(".lc-empty") as HTMLElement;
    const кнопка = блокПустоты.querySelector<HTMLElement>("button");
    expect(кнопка, "у пустого состояния нет действия").not.toBeNull();
    expect(кнопка).toHaveTextContent("Создать быстрый ответ");

    await user.click(кнопка!);
    expect(await screen.findByText("Новый быстрый ответ")).toBeInTheDocument();
  });
});
