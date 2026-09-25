/**
 * АККАУНТЫ АВИТО И «ЛЮДИ И КАНАЛЫ» ЖИВУТ НА ЛЮБОЙ ШИРИНЕ (замеры 05.09).
 *
 * Требование владельца: «должен быть адаптивным под любое устройство и любой
 * экран». Смотрено на пяти ширинах — 375, 768, 1024, 1440, 2560, — и все пять
 * находок здесь одного рода: правило писалось под раскладку, которой больше
 * нет, и продолжало действовать.
 *
 *  · полоса подписей колонок осталась шестиколоночной сеткой на телефоне;
 *  · неподвижные минимумы колонок вырезали меню «…» на окне 1024;
 *  · многоточия, заведённые ради одной строки в шесть колонок, продолжали
 *    резать текст в столбике, где места вдоволь;
 *  · `space-between` в шапке строки разбрасывал точку состояния и имя;
 *  · высоту решётки «люди × каналы» задавала формула от окна.
 *
 * ⚠ ПОЧЕМУ СТОРОЖ ЧИТАЕТ CSS, А НЕ МЕРЯЕТ ЭКРАН. В vitest стоит `css: false`
 * (vite.config.ts), в jsdom стилей нет вовсе — померить нечем. Здесь
 * проверяется то, что проверяемо: какой механизм объявлен, и сходится ли
 * арифметика ширин. Числа взяты из токенов, а не из головы.
 */
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как у соседних сторожей AccountsWidth.test.ts и AccountsListRead0409.test.tsx.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { OperatorsGridPage } from "@/features/settings/accounts/OperatorsGridPage";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const CSS: string = readFileSync("src/features/settings/accounts/accounts.css", "utf8");
const GRID_CSS: string = readFileSync(
  "src/features/settings/accounts/operators-grid.css",
  "utf8",
);
const WEEK_CSS: string = readFileSync("src/features/settings/accounts/week-bars.css", "utf8");
const VARS: string = readFileSync("src/app/lc-vars.css", "utf8");

/** Значение токена из lc-vars.css: числа берём оттуда, а не из головы. */
function токен(имя: string): number {
  const m = VARS.match(new RegExp(`${имя}:\\s*(\\d+)px`));
  expect(m, `токен ${имя} не объявлен`).toBeTruthy();
  return Number(m![1]);
}

/** Тело правила по селектору; комментарии срезаны — в них те же слова. */
function правило(css: string, селектор: string): string {
  const текст = css.replace(/\/\*[\s\S]*?\*\//g, " ");
  const at = текст.indexOf(`\n${селектор} {`);
  expect(at, `правило ${селектор} не найдено`).toBeGreaterThan(-1);
  return текст.slice(at, текст.indexOf("}", at));
}

/** Тело медиазапроса узких экранов из указанного файла. */
function узкий(css: string): string {
  const m = css.match(/@media \(max-width:\s*900px\)\s*\{([\s\S]*?)\n\}/);
  expect(m, "нет правил для узких экранов").toBeTruthy();
  return m![1];
}

/** Колонки свёрнутой строки: неподвижный минимум и доля каждой. */
function колонки(): Array<{ min: number; fr: number | null }> {
  const шаблон = CSS.match(
    /\.account-card:not\(\[data-open\]\)\s*\{[^}]*grid-template-columns:([^;]+);/s,
  );
  expect(шаблон, "у свёрнутой строки нет grid-template-columns").toBeTruthy();
  return шаблон![1]
    .replace(/\/\*.*?\*\//gs, "")
    .split("\n")
    .map((s: string) => s.trim())
    .filter((s: string) => s.length > 0)
    .map((трек: string) => {
      const fr = трек.match(/([\d.]+)fr/);
      // `minmax(0, 1.35fr)` — минимум ноль; `36px` — колонка целиком неподвижна.
      const min = трек.match(/minmax\(\s*(\d+)px/) ?? трек.match(/^(\d+)px/);
      return { min: min ? Number(min[1]) : 0, fr: fr ? Number(fr[1]) : null };
    });
}

describe("Список каналов на любом экране", () => {
  /*
   * ⚠ ГЛАВНАЯ ЛОВУШКА ЭТОГО ЭКРАНА, И ОНА СРАБОТАЛА ДВАЖДЫ.
   *
   * Набор колонок объявлен ОДНИМ правилом на строку канала и на полосу
   * подписей — так задумано, чтобы подписи не разъехались со своими колонками.
   * Правило-исключение для узких экранов 05.09 сняло сетку со СТРОКИ и не
   * тронуло полосу: замер на 375 показал полосу шириной 734 px в ячейке на
   * 325, лишнее срезал `overflow: hidden` контейнера. На экране оставались
   * «Канал» и «Неделя» — подписи к колонкам, которых на этой ширине уже нет.
   */
  it("полоса подписей колонок гаснет там, где колонок больше нет", () => {
    expect(
      узкий(CSS),
      "подписи колонок остались сеткой на ширине, где строка уже столбик",
    ).toMatch(/\.accounts-page__head\s*\{[^}]*display:\s*none/s);
  });

  it("на широком экране полоса подписей — та же сетка, что и строка", () => {
    /*
     * Отрицательная проверка обязана падать по своей причине: не будь у
     * подписей общего со строкой набора колонок, «гаснет там, где колонок
     * нет» стало бы утверждением ни о чём.
     */
    const блок = CSS.split("}").find(
      (b: string) =>
        /grid-template-columns:/.test(b) && /\.account-card:not\(\[data-open\]\)/.test(b),
    );
    expect(блок, "у свёрнутой строки нет набора колонок").toBeTruthy();
    expect(
      блок!.slice(0, блок!.indexOf("{")),
      "подписи колонок больше не делят набор со строкой",
    ).toContain(".accounts-page__head");
  });

  /*
   * ⚠ СЧЁТ, А НЕ ГЛАЗОМЕР. Минимумы колонок складывались в 706 px; вместе с
   * пятью зазорами и полями карточки строка требовала 798 px и помещалась
   * только от окна 1067. Замер при 1024: строка просит 782 в ячейке на 754, и
   * вылезшие 28 px срезает `overflow: hidden` — под нож попадает последняя
   * колонка, то есть меню «…». А это единственный вход в «Отключить и
   * стереть» и «Удалить»: на ноутбуке 1024 канал нельзя было ни выключить, ни
   * убрать, и экран об этом молчал.
   */
  it("на 1024 строка канала помещается целиком, вместе с меню «…»", () => {
    const МЕНЮ = 221; // полоса разделов 220 + линия
    const потолок = Number(CSS.match(/\.accounts-page\s*\{[^}]*max-width:\s*(\d+)px/s)![1]);
    const страница = Math.min(1024 - МЕНЮ, потолок) - 2 * токен("--lc-space-5");
    const доступно = страница - 2 * токен("--lc-space-4") - 5 * токен("--lc-space-3");

    const треки = колонки();
    expect(треки.length, "колонок стало не шесть").toBe(6);
    const неподвижно = треки.reduce((s, к) => s + к.min, 0);

    expect(
      неподвижно,
      "неподвижные ширины колонок снова шире строки — меню обслуживания уедет под обрез",
    ).toBeLessThanOrEqual(доступно);
  });

  it("колонки по-прежнему заданы долями, а не содержимым", () => {
    // Иначе первая проверка лечится тем самым дефектом, ради которого доли и
    // вводили: `auto`/`min-content` дают каждой из тридцати пяти строк свою
    // ширину колонок, и список «едет».
    const треки = колонки();
    expect(треки.filter((к) => к.fr !== null).length, "доли исчезли из набора колонок").toBe(5);
    expect(CSS).not.toMatch(
      /\.account-card:not\(\[data-open\]\)\s*\{[^}]*grid-template-columns:[^;]*\b(auto|min-content|max-content)\b/s,
    );
  });

  /*
   * Многоточие в списке — плата за ОДНУ строку в шесть колонок. Ниже 900
   * строка складывается в столбик, у каждого блока появляется вся ширина
   * карточки, и платить больше не за что. Правила же оставались в силе: замер
   * на 375 — «Бригада Андрея Владиславовича …», «… · партнё…», «Токен активен.
   * Обновится автоматическ…».
   */
  it("на узком экране обрезка снимается со всех четырёх мест сразу", () => {
    const тело = узкий(CSS);
    for (const [что, селектор] of [
      ["строки состояния канала", "\\.account-card__health-text"],
      ["объяснения выключенного канала", "\\.account-card__disabled-note"],
      ["названия канала", "\\.account-row__name \\[data-truncate\\]"],
      ["второй линии с id", "\\.account-row__id"],
    ] as const) {
      expect(тело, `${что} на телефоне всё ещё режется многоточием`).toMatch(
        new RegExp(`${селектор}[^{}]*\\{[^}]*white-space:\\s*normal`, "s"),
      );
    }
    expect(
      узкий(WEEK_CSS),
      "числа недели на телефоне всё ещё режутся многоточием",
    ).toMatch(/\.week-bars__line\s*\{[^}]*white-space:\s*normal/s);
  });

  it("на широком экране те же места действительно обрезаются", () => {
    // Отрицательная проверка: не будь обрезки в базовых правилах, снятие её на
    // узком экране ничего не значило бы.
    expect(правило(CSS, ".account-card:not([data-open]) .account-card__health-text")).toMatch(
      /white-space:\s*nowrap/,
    );
    expect(правило(CSS, ".account-row__id")).toMatch(/white-space:\s*nowrap/);
    expect(правило(WEEK_CSS, ".week-bars__line")).toMatch(/white-space:\s*nowrap/);
  });

  /*
   * Точка состояния стоит ПЕРЕД именем с 04.09 — «перед именем все точки
   * встают на одну вертикаль». А `justify-content: space-between` пережил свой
   * повод (точку, стоявшую ПОСЛЕ имени) и разбрасывал их обратно по правому
   * краю: замер при 1440 — треугольник на x=261, точка на x=432. Вторая
   * половина той же беды — `flex: 1 1 auto` у имени: с основой по содержимому
   * длинное имя не сжималось, а переносилось, и ряды выходили разной высоты
   * (70 / 95 / 103 px у соседей).
   */
  it("точка состояния стоит у треугольника, а имя сжимается на месте", () => {
    expect(
      правило(CSS, ".account-card__header"),
      "точка состояния снова уезжает к правому краю колонки",
    ).not.toMatch(/justify-content:\s*space-between/);
    expect(
      правило(CSS, ".account-row__name"),
      "имя снова переносится на свою линию вместо того, чтобы сжаться",
    ).toMatch(/flex:\s*1\s+1\s+0\b/);
  });

  it("высота ряда не зависит от того, что в него попало", () => {
    // Пустая неделя давала ряд 139 px против 70 у соседей, «61 без ответа» на
    // окне 1024 — 88 против 67. Список шёл волной ровно там, где показывать
    // нечего.
    const тело = правило(
      CSS,
      ".account-card:not([data-open]) .week-bars__empty > *,\n.account-card:not([data-open]) .week-bars__missed",
    );
    expect(тело, "числа недели снова переносятся и растят ряд").toMatch(
      /white-space:\s*nowrap/,
    );
  });
});

describe("«Люди и каналы» на любом экране", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: [...(fakeMe.permissions as string[]), "accounts:read", "accounts:manage"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/operators/grid")) {
          return jsonResponse(200, {
            accounts: [
              { id: "a1", title: "Парт-7", lead_origin: "В43", status: "active", is_service: false },
            ],
            users: [
              { id: "u1", full_name: "Пётр Свой", role: "manager", can_be_operator: true, reason: null },
            ],
            assigned: [],
          });
        }
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  /*
   * Высоту решётки задавала `calc(100vh - 230px)`. Число 230 — сумма чего-то,
   * чего на экране давно нет: замер при 2560×1440 показал 641 px пустоты под
   * решёткой (45 % высоты монитора) при том, что прокрутить нужно ещё сотни
   * строк. Остаток колонки считает браузер, и он не устаревает.
   */
  it("высоту решётке даёт остаток колонки, а не формула от окна", () => {
    const тело = правило(GRID_CSS, ".op-grid__scroll");
    expect(тело, "высота решётки снова считается от окна магическим числом").not.toMatch(/100vh/);
    expect(тело, "решётка перестала занимать остаток колонки").toMatch(/flex:\s*1/);
    expect(тело, "без `min-height: 0` флекс-элемент не сожмётся ниже содержимого").toMatch(
      /min-height:\s*0/,
    );
    expect(правило(GRID_CSS, ".op-grid"), "разделу нечего растягивать").toMatch(/flex:\s*1/);
  });

  it("раздел отбит от краёв так же, как соседние разделы настроек", () => {
    // Экран лежит прямо в `.settings-content`, у которой отступов нет: замер
    // при 1440 — левый край поиска вплотную к линии колонки разделов.
    expect(правило(GRID_CSS, ".op-grid"), "раздел снова приклеен к краям окна").toMatch(
      /padding:\s*var\(--lc-space-4\)\s+var\(--lc-space-5\)/,
    );
  });

  /*
   * Полоса подсветки колонки высотой 300vh — абсолютная, и всё, что свисает
   * вниз, попадает в прокручиваемую область. Замер: таблица 729 px внутри
   * рамки, которая считала себя высотой 2489, то есть 1760 px прокрутки в
   * пустоту. Строки кончились, а полоса прокрутки обещает ещё две трети —
   * читается как «люди не загрузились».
   */
  it("прокрутка решётки кончается вместе со строками", () => {
    expect(
      правило(GRID_CSS, ".op-grid__table"),
      "полоса подсветки колонки снова растит прокрутку на два экрана вниз",
    ).toMatch(/contain:\s*paint/);
  });

  it("подсветка колонки при этом осталась во всю таблицу", () => {
    // Отрицательная проверка: срежь кто-нибудь саму полосу вместо того, чтобы
    // ограничить отрисовку, — прокрутка тоже стала бы честной, но перекрестье
    // исчезло бы.
    expect(правило(GRID_CSS, ".op-grid__hit::after"), "полосы подсветки колонки не осталось").toMatch(
      /height:\s*300vh/,
    );
  });

  it("«везде/нигде» достаётся пальцем, а не только курсором", () => {
    // «Проступает под курсором» — верный довод там, где курсор есть. На
    // планшете наведения не бывает, и половина смысла экрана (назначить
    // человека сразу на все показанные каналы) была недостижима.
    const m = GRID_CSS.match(/@media \(hover: none\)\s*\{([\s\S]*?)\n\}/);
    expect(m, "на устройствах без курсора кнопка так и не появляется").toBeTruthy();
    expect(m![1]).toMatch(/\.op-grid__row-btn\s*\{[^}]*opacity:\s*1/s);
    // Цель нажатия не меньше 24 px: при `padding: 2px` кнопка выходила 23.
    expect(правило(GRID_CSS, ".op-grid__row-btn")).toMatch(/min-height:\s*24px/);
  });

  it("на узком экране колонка имён не закрывает собой всю решётку", () => {
    // Замер при 375: липкая колонка имён 435 px — ШИРЕ экрана. Ни одной
    // галочки достать было нельзя.
    expect(узкий(GRID_CSS)).toMatch(
      /\.op-grid__corner,\s*\n\s*\.op-grid__name\s*\{[^}]*max-width:\s*50vw/s,
    );
  });

  it("у экрана есть заголовок первого уровня", async () => {
    // Раздел открывался полем поиска: ни заголовка, ни строки о том, что здесь
    // делают. Для читалки с экрана страница начиналась с безымянного ввода.
    renderWithProviders(<OperatorsGridPage />);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1, name: "Люди и каналы" })).toBeInTheDocument(),
    );
  });

  it("размер работы набран крупнее своей подписи", async () => {
    // «14 × 34» отвечает «насколько велика правка, уходящая одним
    // „Сохранить“», и стояло кеглем подписи между двумя полями поиска.
    renderWithProviders(<OperatorsGridPage />);
    await waitFor(() => expect(screen.getByText("сотрудников × каналов")).toBeInTheDocument());
    expect(правило(GRID_CSS, ".op-grid__scale-num")).toMatch(
      /font-size:\s*var\(--lc-fz-metric\)/,
    );
    expect(правило(GRID_CSS, ".op-grid__scale-cap")).toMatch(
      /font-size:\s*var\(--lc-fz-micro\)/,
    );
  });
});
