// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как и у соседнего сторожа AccountsWidth.test.ts.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { OperatorsGridPage } from "@/features/settings/accounts/OperatorsGridPage";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * РЕШЁТКА «ЛЮДИ × КАНАЛЫ» — вместо тридцати пяти походов по карточкам.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 04.09: «продумай, как мне быстро можно было подключать
 * или отключать людей на всех аккаунтах — сейчас я вручную по 30 раз захожу и
 * тыкаю, неудобно». Замер боя: 35 каналов, 35 человек, в среднем один человек
 * назначен на 29,7 канала из 35 — то есть единица работы здесь ЧЕЛОВЕК, а
 * прежний экран построен вокруг канала.
 */
const CSS = readFileSync("src/features/settings/accounts/operators-grid.css", "utf8");

const ГРИД = {
  accounts: [
    // Название и источник — РАЗНЫЕ поля: подпись собирается из них
    // (`подписьКанала`), и фикстура с источником, вписанным в название, дала бы
    // «Парт-7 · В43 · В43» и проверяла бы то, чего в бою не бывает.
    { id: "a1", title: "Парт-7", lead_origin: "В43", status: "active", is_service: false },
    { id: "a2", title: "Парт-7", lead_origin: "В65", status: "active", is_service: false },
    { id: "srv", title: "SMOKE", lead_origin: null, status: "active", is_service: true },
  ],
  users: [
    {
      id: "u1",
      full_name: "Пётр Свой",
      role: "manager",
      can_be_operator: true,
      reason: null,
      // Два пробела — как на бою: поле свободное и правится руками.
      department: "Диспетчер  МНЧ",
    },
    {
      id: "u2",
      full_name: "Инна Наблюдатель",
      role: "observer",
      can_be_operator: false,
      reason: "Роль не отвечает клиентам",
      // Отдел заполнен НЕ У ВСЕХ (замер боя) — у этой строки скобок не будет.
      department: null,
    },
  ],
  assigned: [{ account_id: "a1", user_id: "u1" }],
};

describe("Люди и каналы", () => {
  let отправлено: unknown = null;

  beforeEach(() => {
    отправлено = null;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      // Экран правит только администратор: у остальных решётка читающая.
      permissions: [...(fakeMe.permissions as string[]), "accounts:read", "accounts:manage"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/operators/bulk")) {
          отправлено = JSON.parse(String(init?.body));
          return jsonResponse(200, { applied: 2, accounts_touched: 2, opened_to_all: [] });
        }
        if (url.includes("/operators/grid")) return jsonResponse(200, ГРИД);
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it("показывает, на скольких каналах человек, и служебную заглушку не считает", async () => {
    renderWithProviders(<OperatorsGridPage />);
    /*
     * Служебная заглушка проверочного набора — не канал Авито: обращения через
     * неё не идут и идти не могут. Колонка под неё была бы обещанием, которого
     * не выполнить, а знаменатель «из 3» — враньём.
     */
    expect(await screen.findByText("1 из 2")).toBeInTheDocument();
    expect(screen.queryByText("SMOKE")).toBeNull();
  });

  it("«везде» подключает человека ко ВСЕМ каналам одним запросом", async () => {
    const user = userEvent.setup();
    renderWithProviders(<OperatorsGridPage />);
    await screen.findByText("1 из 2");

    // Подпись кнопки — «везде», но зовётся она полной фразой: тридцать пять
    // одинаковых «везде» подряд читалка с экрана не различает.
    await user.click(screen.getByRole("button", { name: /Назначить Пётр Свой \(Диспетчер МНЧ\)/ }));
    await user.click(screen.getByRole("button", { name: /Сохранить/ }));

    await waitFor(() => expect(отправлено).not.toBeNull());
    // Ровно одно изменение: на первом канале человек уже стоял, и повторно его
    // не шлём — иначе журнал обрастал бы строками ни о чём.
    expect(отправлено).toEqual({
      changes: [{ account_id: "a2", user_id: "u1", assigned: true }],
    });
  });

  it("вернул как было — правка исчезает, «Сохранить» гаснет", async () => {
    /*
     * Иначе кнопка оставалась бы живой после того, как человек передумал, и
     * пустая пачка уезжала бы на сервер.
     */
    const user = userEvent.setup();
    renderWithProviders(<OperatorsGridPage />);
    await screen.findByText("1 из 2");

    const ячейка = screen.getByRole("checkbox", { name: "Пётр Свой (Диспетчер МНЧ) на канале Парт-7 · В65" });
    await user.click(ячейка);
    expect(screen.getByRole("button", { name: "Сохранить (1)" })).toBeEnabled();

    await user.click(ячейка);
    expect(screen.getByRole("button", { name: "Сохранить (0)" })).toBeDisabled();
  });

  it("негодного нельзя поставить, но можно снять", async () => {
    /*
     * То же одностороннее правило, что и на поканальном экране: иначе набор
     * канала однажды становится несохраняемым — ровно тот боевой тупик, из-за
     * которого 03.09 заперло 14 каналов из 14.
     */
    renderWithProviders(<OperatorsGridPage />);
    await screen.findByText("1 из 2");

    expect(
      screen.getByRole("checkbox", { name: "Инна Наблюдатель на канале Парт-7 · В43" }),
    ).toBeDisabled();
    expect(screen.getByText("Роль не отвечает клиентам")).toBeInTheDocument();
  });

  /* ─────────────────── перерисовка 04.09 («всё криво, мелко и некрасиво») ── */

  it("колонка канала — фиксированной ширины, а не по подписи", () => {
    /*
     * ⚠ ЭТО И БЫЛА «КРИВИЗНА». Ширину колонки задавала подпись в шапке, а в
     * ячейке под ней стояла галочка: у канала с длинным именем колонка шире, и
     * кружки переставали стоять в столбик. Поэтому подпись развёрнута
     * вертикально, а ширина задана числом и от содержимого не зависит.
     */
    const блок = CSS.match(/\.op-grid__col\s*\{([^}]*)\}/s);
    expect(блок, "нет правила .op-grid__col").toBeTruthy();
    const ширина = блок![1].match(/(?:^|[\s;])width:\s*(\d+)px/);
    const минимум = блок![1].match(/min-width:\s*(\d+)px/);
    const максимум = блок![1].match(/max-width:\s*(\d+)px/);
    expect(ширина, "у колонки нет числовой ширины").toBeTruthy();
    expect(минимум?.[1], "минимум расходится с шириной").toBe(ширина![1]);
    expect(максимум?.[1], "максимум расходится с шириной").toBe(ширина![1]);
  });

  it("подпись канала читается прямо, а не боком", () => {
    /*
     * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 05.09: «чтобы не пришлось читать боком». Подпись была
     * развёрнута вертикально ради равной ширины колонок — ширину это держало,
     * но на подписи смотрят столько же раз, сколько ставят галочек, и каждый
     * раз это поворот головы.
     *
     * Тот же результат даёт КОРОТКАЯ подпись: код источника влезает
     * горизонтально в колонку 44 пикселя, а полное имя канала живёт в
     * подсказке и в доступном имени кнопки.
     */
    // Якорь на начало строки: без него первым совпадает
    // `.op-grid__col-btn:hover .op-grid__col-text`, где ничего этого нет.
    const блок = CSS.match(/^\.op-grid__col-text\s*\{([^}]*)\}/ms);
    expect(блок, "нет правила .op-grid__col-text").toBeTruthy();
    expect(блок![1], "подпись снова развёрнута боком").not.toMatch(/writing-mode/);
    expect(блок![1], "подпись снова развёрнута боком").not.toMatch(/rotate\(/);
    // И она обязана обрезаться, а не распирать колонку — иначе вернётся «криво».
    expect(блок![1]).toMatch(/text-overflow:\s*ellipsis/);
    expect(блок![1]).toMatch(/white-space:\s*nowrap/);
  });

  it("шапка и колонка имён прилипают, а прокрутка живёт внутри рамки", () => {
    // Тридцать пять строк без липкой шапки означают, что к середине списка
    // человек уже не знает, над каким каналом стоит галочка.
    expect(CSS).toMatch(/\.op-grid__table thead th\s*\{[^}]*position:\s*sticky/s);
    expect(CSS).toMatch(/\.op-grid__name\s*\{[^}]*position:\s*sticky/s);
    // Липкое держится за ближайшего прокручиваемого предка: прокручивайся
    // страница — прилипать было бы не к чему.
    expect(CSS).toMatch(/\.op-grid__scroll\s*\{[^}]*overflow:\s*auto/s);
  });

  it("нажимается вся ячейка, а не одна галочка", () => {
    // Промах по соседней ячейке стоит чужого доступа к каналу.
    const блок = CSS.match(/\.op-grid__hit\s*\{([^}]*)\}/s);
    expect(блок, "нет правила .op-grid__hit").toBeTruthy();
    expect(блок![1]).toMatch(/height:\s*\d+px/);
    expect(блок![1]).toMatch(/display:\s*flex/);
  });

  /*
   * ОТДЕЛ В СКОБКАХ У ИМЕНИ (просьба владельца 04.09).
   *
   * ⚠ ЗАЧЕМ ИМЕННО ЗДЕСЬ. Решётку собирают ОТДЕЛАМИ — «все чатеры на этот
   * канал». Строки шли одними именами, и чтобы понять, кто из них чатер,
   * приходилось держать вкладку «Команда» в соседнем окне.
   *
   * ⚠ ПОЛНАЯ ПОДПИСЬ — В `title`. Ячейка имени режется многоточием с конца
   * (`op-grid__person`), отдел стоит в хвосте намеренно: при узкой колонке
   * уходит он, а имя остаётся целым.
   */
  it("имя подписано отделом, а у кого его нет — скобок нет", async () => {
    const { container } = renderWithProviders(<OperatorsGridPage />);
    await screen.findByText("1 из 2");

    const свой = container.querySelector<HTMLElement>(".op-grid__person[title='Пётр Свой (Диспетчер МНЧ)']");
    expect(свой, "у имени нет подписи отделом").not.toBeNull();
    // Два пробела из боевого поля схлопнуты: один отдел не должен выглядеть как два.
    expect(свой!.textContent).toBe("Пётр Свой (Диспетчер МНЧ)");

    const без = container.querySelector<HTMLElement>(".op-grid__person[title='Инна Наблюдатель']");
    expect(без, "человек без отдела получил скобки").not.toBeNull();
    expect(без!.textContent).not.toContain("(");
  });

  it("«везде» стоит у имени, а не за краем экрана", async () => {
    /*
     * Кнопка стояла ПОСЛЕ тридцати четырёх колонок: чтобы нажать её для
     * человека, которого видишь, надо было уехать вбок и потерять его строку.
     */
    renderWithProviders(<OperatorsGridPage />);
    const кнопка = await screen.findByRole("button", { name: /Назначить Пётр Свой \(Диспетчер МНЧ\)/ });
    const имя = кнопка.closest("th");
    expect(имя, "кнопка вне ячейки с именем").toBeTruthy();
    expect(имя!.className).toContain("op-grid__name");
  });

  it("подпись канала в шапке назначает на него всех разом", async () => {
    /*
     * ⚠ ВТОРАЯ ПОЛОВИНА РАБОТЫ. Строка отвечает «куда подключён человек»,
     * колонка — «кто на этом канале»; до сих пор второе делалось только на
     * поканальном экране, ради ухода с которого решётку и заводили.
     *
     * Негодного колонка не ставит: одностороннее правило то же, что у ячейки.
     */
    const user = userEvent.setup();
    renderWithProviders(<OperatorsGridPage />);

    await user.click(await screen.findByRole("button", { name: /Парт-7 · В65: назначить всех/ }));
    await user.click(screen.getByRole("button", { name: "Сохранить (1)" }));

    await waitFor(() => expect(отправлено).not.toBeNull());
    expect((отправлено as { changes: unknown[] }).changes).toEqual([
      { account_id: "a2", user_id: "u1", assigned: true },
    ]);
  });
});
