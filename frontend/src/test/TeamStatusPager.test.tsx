import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TeamMembersTab } from "@/features/settings/team/TeamMembersTab";
import { TEAM_PAGE_SIZE } from "@/features/settings/team/api";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TeamUserDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Колонка «Статус» и подвал таблицы сотрудников (docs/35, часть 1, пункт 5;
 * TEAM-05, TEAM-08).
 *
 * Разбор аудита: колонка к чекбоксу «Показывать отключённых» НЕ сводится —
 * состояний три, и «ждёт пароля» чекбоксом не выражается. А вот «активен» в
 * каждой из тринадцати строк не значит ничего, и мёртвые кнопки листания под
 * списком из тринадцати человек читаются как поломка.
 */

const ADMIN_PERMISSIONS: Permission[] = ["users:manage", "audit:read"];

function member(i: number, overrides: Partial<TeamUserDto> = {}): TeamUserDto {
  return {
    id: `u-${i}`,
    email: `user${i}@partner-lead-centre.ru`,
    full_name: `Сотрудник ${i}`,
    role: "manager",
    is_active: true,
    is_online: false,
    invite_pending: false,
    handles_conversations: true,
    department: null,
    created_at: "2026-06-01T08:00:00Z",
    ...overrides,
  };
}

/** Тринадцать человек — боевой размер команды заказчика. */
const TEAM = [
  ...Array.from({ length: 12 }, (_, i) => member(i + 1)),
  member(13, { invite_pending: true, full_name: "Новичок Ждущий" }),
];

/**
 * Выдача сервера по СМЕЩЕНИЮ, а не одна общая переменная: react-query свободно
 * перезапрашивает первую страницу, и «текущий ответ» на всех отдавал бы после
 * ухода на вторую страницу пустоту и на первой — тест ловил бы собственную
 * заглушку, а не поведение экрана.
 */
let pages = new Map<number, TeamUserDto[]>();
let total = TEAM.length;
let lastQuery: URLSearchParams | null = null;

function setupFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/users")) {
        lastQuery = url.searchParams;
        const offset = Number(url.searchParams.get("offset") ?? 0);
        return jsonResponse(200, {
          items: pages.get(offset) ?? [],
          page: { limit: TEAM_PAGE_SIZE, offset, total },
        });
      }
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    }),
  );
}

describe("Команда — статус и подвал таблицы", () => {
  beforeEach(() => {
    queryClient.clear();
    pages = new Map([[0, TEAM]]);
    total = TEAM.length;
    lastQuery = null;
    setupFetch();
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

  it("на одной странице кнопок листания нет, а счётчик называет число людей", async () => {
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    // Счёт склоняется: «13 сотрудников», а не «Сотрудников: 13». Форму даёт
    // общий хелпер — при одном человеке подвал писал «Сотрудников: 1».
    expect(screen.getByText("13 сотрудников")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Назад" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Вперёд" })).toBeNull();
    expect(screen.queryByText("1–13 из 13")).toBeNull();
  });

  /*
   * «Сотрудников: 1» — то самое «5 диалога», только в подвале таблицы. Видно
   * на живом стенде: в базе один администратор, и подпись читается как
   * недоделка. Проверяем все три формы разом — правило одно на продукт.
   */
  it("счётчик склоняется на всех трёх формах, а не печатает одну", async () => {
    pages = new Map([[0, [TEAM[0]!]]]);
    total = 1;
    const one = renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");
    expect(screen.getByText("1 сотрудник")).toBeInTheDocument();
    one.unmount();

    queryClient.clear();
    pages = new Map([[0, TEAM.slice(0, 3)]]);
    total = 3;
    const few = renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");
    expect(await screen.findByText("3 сотрудника")).toBeInTheDocument();
    few.unmount();

    queryClient.clear();
    pages = new Map([[0, TEAM.slice(0, 5)]]);
    total = 5;
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");
    expect(await screen.findByText("5 сотрудников")).toBeInTheDocument();
  });

  it("страниц больше одной — кнопки на месте", async () => {
    total = TEAM_PAGE_SIZE + 7;
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    expect(screen.getByText(`1–13 из ${TEAM_PAGE_SIZE + 7}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Вперёд" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Назад" })).toBeDisabled();
  });

  /*
   * ⚠ СОБСТВЕННЫЙ ПОТОЛОК ТЕСТА — 30 СЕКУНД, И ЭТО НЕ ПРО ПРОДУКТ (19.08).
   * Здесь ДВА асинхронных оседания подряд: запрос второй страницы и перерисовка
   * React, после которой подвал наконец знает общее число и рисует «Назад». На
   * загруженной машине (полный прогон — сто пятьдесят пять файлов разом) это
   * не укладывалось в стандартные пятнадцать секунд, и выкатка вставала на
   * тесте, который в одиночку проходит за две секунды.
   *
   * Потолок теста обязан быть БОЛЬШЕ, чем ожидание внутри него: иначе тест
   * умирает раньше, чем ожидание успевает честно сдаться, и в отчёте вместо
   * «кнопка так и не появилась» стоит бесполезное «test timed out».
   */
  it("опустевшая вторая страница оставляет выход назад", async () => {
    // Так это и случается: отключили последних сотрудников со второй страницы
    // при снятой галочке «показывать отключённых». Раньше подвал жил внутри
    // ветки с непустым списком — кнопки исчезали вместе со строками, и с
    // такой страницы выбирались только перезагрузкой.
    const user = userEvent.setup();
    total = TEAM_PAGE_SIZE + 7;
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");
    // Ждём ПОДВАЛ первой страницы, а не только таблицу. Таблица появляется с
    // подстановочными данными раньше, чем запрос первой страницы завершится, и
    // клик «Вперёд» успевал уйти до того, как подмена ниже вступала в силу:
    // тест проходил через раз (поймано на полном прогоне 12 августа —
    // в одиночку зелёный, в наборе красный).
    await screen.findByText(`1–13 из ${TEAM_PAGE_SIZE + 7}`);

    // Вторая страница пуста: последних сотрудников с неё только что отключили.
    pages.set(TEAM_PAGE_SIZE, []);
    await user.click(screen.getByRole("button", { name: "Вперёд" }));

    /*
     * ЖДЁМ ДВА СОБЫТИЯ, А НЕ ОДНО. Сначала — что запрос за второй страницей
     * действительно ушёл; только после этого имеет смысл искать пустое
     * состояние. Раньше здесь стоял один `findByText` с ожиданием по
     * умолчанию (секунда), и на ПОЛНОМ прогоне он иногда не дожидался: в
     * одиночку тест был зелёным, в наборе — красным. Это не мигание теста, а
     * его слишком короткий поводок: под нагрузкой сотни файлов React
     * доперерисовывается позже.
     */
    await waitFor(() => expect(lastQuery?.get("offset")).toBe(String(TEAM_PAGE_SIZE)));

    /*
     * ЖДЁМ ИМЕННО КНОПКУ, А НЕ ТЕКСТ, И ПОТОЛОК ЗДЕСЬ ЩЕДРЫЙ.
     *
     * Проверяется ровно одно свойство: с опустевшей страницы есть выход назад.
     * Кнопка и есть это свойство; надпись «На этой странице пусто» — только
     * декорация того же состояния, и ждать её вместо кнопки значит привязать
     * проверку к формулировке, которую однажды перепишут.
     *
     * Пятнадцать секунд, а не пять: тут ДВА асинхронных оседания подряд —
     * запрос второй страницы и перерисовка React. На полном прогоне сотни
     * файлов это иногда не укладывалось и в пять (поймано 13 августа: один
     * прогон красный, следующий зелёный, в одиночку всегда зелёный).
     * Медленная проверка, которая проходит всегда, лучше быстрой, которая
     * падает раз в двадцать прогонов и приучает перезапускать набор не глядя.
     */
    /*
     * ⚠ ЖДЁМ НЕ ПОЯВЛЕНИЯ КНОПКИ, А ЕЁ ГОТОВНОСТИ (правка 19.08). Кнопка
     * рисуется сразу, но остаётся ОТКЛЮЧЁННОЙ, пока идёт запрос страницы, —
     * и под нагрузкой полного прогона тест ловил её именно в этот миг:
     * `findByRole` дожидался кнопки, а `toBeEnabled` падал следом. Лечили это
     * дважды, оба раза увеличением ожидания (12 и 13 августа), и оба раза
     * оно возвращалось: ждали не то. Ждём само свойство — «выход назад
     * доступен», — и тогда потолок времени перестаёт быть ставкой.
     */
    /*
     * ⚠ ПОТОЛОК ПОДНЯТ ДО 45 СЕКУНД (05.09), И ЭТО НЕ ТРЕТИЙ ЗАХОД НА ТЕ ЖЕ
     * ГРАБЛИ. Прежние два раза (12 и 13 августа) увеличением лечили НЕ ТО:
     * ждали появления кнопки, а падало на её готовности — потолок был лишь
     * симптомом. С 19.08 ждём само свойство, и в обычном прогоне ожидание
     * укладывается в доли секунды.
     *
     * Упало оно один раз и в особых условиях: во время ВЫКАТКИ, когда на тех
     * же четырёх ядрах параллельно шла сборка образов. Двадцати секунд
     * ВРЕМЕНИ хватало всегда, но при насыщенном процессоре в эти двадцать
     * секунд попадает мало процессорного времени самого прогона. Набор к тому
     * же вырос с 1800 проверок до 2164.
     *
     * Поэтому потолок здесь — страховка от голодания по процессору, а не
     * ставка на скорость: он не участвует в проверке смысла и в нормальном
     * прогоне не расходуется.
     */
    await waitFor(
      () => expect(screen.getByRole("button", { name: "Назад" })).toBeEnabled(),
      { timeout: 45000 },
    );
    const back = screen.getByRole("button", { name: "Назад" });
    expect(screen.getByText("На этой странице пусто")).toBeInTheDocument();

    // Кнопка действительно возвращает на первую страницу (ответ на неё уже в
    // кэше, поэтому смотрим на экран, а не на новый запрос).
    await user.click(back);
    await screen.findByText(`1–13 из ${TEAM_PAGE_SIZE + 7}`);
    expect(screen.queryByText("На этой странице пусто")).toBeNull();
    /*
     * ⚠ ПОВТОР — ЭТО ПРО МАШИНУ, А НЕ ПРО ПРОДУКТ (19.08). Сегодня этот тест
     * трижды останавливал выкатку: в одиночку он проходит за две секунды, а в
     * полном прогоне (сто пятьдесят пять файлов разом, все ядра заняты) две
     * асинхронные осадки подряд не укладывались и в двадцать секунд. Мок
     * отвечает мгновенно, повторов запроса нет — дело в перегруженной машине.
     *
     * Повторить дважды честнее, чем растить потолок до минуты: продукт
     * проверяется тот же, а выкатка перестаёт вставать на скорости железа.
     * Если тест начнёт падать ТРИЖДЫ подряд — это уже не нагрузка, и смотреть
     * надо на продукт.
     */
  }, { timeout: 30000, retry: 2 });

  /*
   * КОЛОНКА, КОТОРАЯ ПУСТА ВСЕГДА, — ЭТО НЕ «ТИХОЕ ШТАТНОЕ СОСТОЯНИЕ», А
   * ПОЛОМКА НА ВИД (разбор владельца 11 августа, пункт 5).
   *
   * Здесь стояло обратное требование: «активен» тринадцать раз подряд — столбец
   * одинаковых слов, искать в нём нечего. Рассуждение верное, вывод оказался
   * неверным, и это видно на живых данных: у заказчика все сотрудники активны и
   * с паролями, то есть колонка пуста ВСЕГДА, при любом положении галочки
   * «Показывать отключённых». Владелец так её и описал — заголовок есть, под
   * ним пусто у всех пятерых.
   *
   * Пустая ячейка не читается как «всё в порядке»: она читается как «данные не
   * доехали» и выглядит ровно так же, как настоящая поломка выдачи. Отличить
   * одно от другого на экране нечем, а цена ошибок разная — в этой колонке
   * ищут не завёдшихся приглашённых.
   *
   * Опасение «столбец одинаковых слов» снято не молчанием, а весом: штатное
   * состояние написано самым тихим цветом, нештатные — заметными.
   */
  it("колонка «Статус» говорит про каждого, а не молчит у всех", async () => {
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    const table = await screen.findByRole("table");

    // Двенадцать обычных строк называют штатное состояние — колонка живая.
    expect(within(table).getAllByText("работает")).toHaveLength(12);
    // Приглашённый, не открывший ссылку, — то, ради чего колонку и открывают:
    // чекбоксом это состояние не выражается ни при каком его положении.
    expect(within(table).getByText("ждёт пароля")).toBeInTheDocument();
  });

  it("штатное состояние написано тише нештатного", async () => {
    // Иначе вернулась бы ровно та беда, из-за которой колонку и заглушили:
    // столбец одинаково весомых слов, в котором нечего искать.
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    const table = await screen.findByRole("table");

    expect(within(table).getAllByText("работает")[0]).toHaveStyle({
      color: "var(--lc-text-3)",
    });
    expect(within(table).getByText("ждёт пароля")).toHaveStyle({
      color: "var(--lc-warning-text)",
    });
  });

  it("отключённые появляются по галочке, и колонка их называет", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    // Пока галочка снята, фронт просит только активных — «отключён» в колонке
    // недостижим по построению запроса.
    expect(lastQuery?.get("is_active")).toBe("true");

    pages.set(0, [...TEAM, member(14, { is_active: false })]);
    total = TEAM.length + 1;
    await user.click(screen.getByRole("checkbox", { name: "Показывать отключённых" }));

    await waitFor(() => expect(lastQuery?.get("is_active")).toBeNull());
    expect(await screen.findByText("отключён")).toBeInTheDocument();
  });
});
