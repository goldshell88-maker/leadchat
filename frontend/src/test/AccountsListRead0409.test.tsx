/**
 * Список каналов ЧИТАЕТСЯ, а не разбирается (макет «Аккаунты Авито», 04.09).
 *
 * Десять правок макета про одно и то же: тридцать пять строк смотрят всю смену,
 * и каждая мелочь, которую приходится «искать глазами», умножается на тридцать
 * пять. Здесь сторожится то из них, что живёт поведением, а не вкусом:
 *
 *  · точка состояния стоит перед именем, то есть у всех строк на одном иксе;
 *  · id и пометки «источник не задан» — второй линией, а не в ряд с именем;
 *  · нажимают на САМ состав канала, а подписи «Назначить» на экране нет;
 *  · «Операторы: все» объясняется подсказкой, а не второй видимой строкой;
 *  · первая линия чисел недели не переносится — ряды одной высоты;
 *  · объяснение выключенного канала обрезается, а не растит свой ряд;
 *  · у колонок есть подписи, и набор колонок у подписей общий со строками;
 *  · доля колонки приёма событий вмещает самую длинную тревожную фразу;
 *  · полоса «требует переподключения» не двигает содержимое строки.
 *
 * ⚠ ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Макет предлагал собрать весь список в ОДНУ сетку
 * (ячейки — прямые дети контейнера). Это запрещено двумя действующими
 * сторожами: AccountsWidth требует, чтобы `.accounts-page__grid` оставался
 * колонкой без `grid-template-columns`, а AccountsRowGrid0409 — чтобы набор
 * колонок был объявлен у самой свёрнутой строки. Общий набор колонок сделан
 * иначе: одно правило на подписи и строку, и разъехаться им можно только
 * вместе с этой строчкой кода.
 */
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как у соседних сторожей AccountsWidth.test.ts и AccountsRowGrid0409.test.tsx.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const CSS: string = readFileSync("src/features/settings/accounts/accounts.css", "utf8");
const WEEK_CSS: string = readFileSync("src/features/settings/accounts/week-bars.css", "utf8");
const VARS: string = readFileSync("src/app/lc-vars.css", "utf8");

const ВСЕ_ОПЕРАТОРЫ = "Никто не назначен — обращения канала видят все операторы";
const DAY_MS = 86_400_000;

/** Тело правила по селектору: комментарии срезаны — в них те же слова. */
function правило(css: string, селектор: string): string {
  const текст = css.replace(/\/\*[\s\S]*?\*\//g, " ");
  const at = текст.indexOf(`\n${селектор} {`);
  expect(at, `правило ${селектор} не найдено`).toBeGreaterThan(-1);
  return текст.slice(at, текст.indexOf("}", at));
}

/** Значение токена из lc-vars.css — числа берём оттуда, а не из головы. */
function токен(имя: string): number {
  const m = VARS.match(new RegExp(`${имя}:\\s*(\\d+)px`));
  expect(m, `токен ${имя} не объявлен`).toBeTruthy();
  return Number(m![1]);
}

function account(overrides: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    id: "acc-1",
    title: "Валентина · МНЧ",
    avito_user_id: 100200300,
    status: "active",
    own_keys: false,
    lead_origin: null,
    lead_partner_number: null,
    token_expires_at: new Date(now + 30 * DAY_MS).toISOString(),
    created_at: new Date(now - 8 * DAY_MS).toISOString(),
    token: { state: "ok", message: "Токен активен", last_refresh_at: null, action: null },
    webhook: {
      status: "ok",
      url: "https://leadchat.example/hook",
      last_event_at: new Date(now - 60_000).toISOString(),
      state: "ok",
      message: "События приходят",
      action: null,
    },
    backfill: { status: "idle" },
    operators: { count: 2, preview: [{ id: "u1", full_name: "Анна Смирнова" }] },
    stats: { total: 512, missed: 61, daily: [10, 20, 30, 40, 50, 60, 78], stale_minutes: 60 },
    ...overrides,
  };
}

function mount(acc: Record<string, unknown> = account()) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/avito-accounts")) {
        return jsonResponse(200, { items: [acc], page: { limit: 50, offset: 0, total: 1 } });
      }
      return jsonResponse(200, {});
    }),
  );
  return renderWithProviders(<AccountsPage />, { route: "/settings/accounts" });
}

/** Свёрнутая строка канала. `article` — чтобы не поймать пустой скелетон. */
async function строка(): Promise<HTMLElement> {
  const card = await waitFor(() => {
    const el = document.querySelector("article.account-card");
    expect(el, "карточка канала не отрисовалась").toBeTruthy();
    return el as HTMLElement;
  });
  expect(card.hasAttribute("data-open"), "карточка приехала раскрытой").toBe(false);
  return card;
}

describe("Список каналов — что в нём видно с одного взгляда", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: [...fakeMe.permissions, "accounts:read", "accounts:manage"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  /*
   * Точка стояла ПОСЛЕ имени: имена разной длины, и тридцать пять состояний
   * приходилось искать по концам строк. Перед именем они встают на одну
   * вертикаль — «какой канал молчит» читается одним движением сверху вниз.
   */
  it("точка состояния стоит между треугольником и именем", async () => {
    mount();
    const шапка = (await строка()).querySelector(".account-card__header") as HTMLElement;
    const дети = Array.from(шапка.children);

    const треугольник = дети.findIndex((el) =>
      el.matches('button[aria-label^="Развернуть"], button[aria-label^="Свернуть"]'),
    );
    const точка = дети.findIndex((el) => el.matches('[role="img"][aria-label^="Статус:"]'));
    const имя = дети.findIndex((el) => el.matches(".account-row__name"));

    expect(треугольник, "кнопки раскрытия нет в шапке строки").toBeGreaterThanOrEqual(0);
    expect(точка, "точки состояния нет в шапке строки").toBeGreaterThanOrEqual(0);
    expect(имя, "имени канала нет в шапке строки").toBeGreaterThanOrEqual(0);
    expect(треугольник, "точка встала перед треугольником").toBeLessThan(точка);
    expect(точка, "точка снова стоит после имени — у каждой строки на своём иксе").toBeLessThan(
      имя,
    );
  });

  /*
   * «id 100200300 · источник не задан» стоял в одном ряду с названием и отбирал
   * у него половину колонки — при том что имя тут главное слово строки.
   */
  it("id и пометки занимают вторую линию целиком", async () => {
    mount();
    const карточка = await строка();
    const id = карточка.querySelector(".account-row__id") as HTMLElement;
    expect(id, "строки с id в шапке нет").toBeTruthy();
    expect(id.textContent).toContain("источник не задан");
    expect(
      id.closest(".account-row__name"),
      "id уехал внутрь имени — переименование заденет и его",
    ).toBeNull();

    const шапка = правило(CSS, ".account-card__header");
    expect(шапка, "шапка не переносит вторую линию").toMatch(/flex-wrap:\s*wrap/);
    expect(
      правило(CSS, ".account-row__id"),
      "id снова помещается в ряд с именем и отбирает у него полколонки",
    ).toMatch(/flex:\s*1\s+0\s+100%|flex-basis:\s*100%/);
  });

  /*
   * Подпись действия рядом со значением — ровно то, от чего отказались у
   * переименования. Целью нажатия служит сам состав; имя действия осталось в
   * доступном имени кнопки, но с экрана ушло.
   */
  it("состав канала сам служит кнопкой, а подписи «Назначить» на экране нет", async () => {
    mount();
    const карточка = await строка();

    const цель = within(карточка).getByRole("button", {
      name: /^Назначить операторов на аккаунт/,
    });
    expect(
      цель.textContent,
      "нажимают не на состав, а на подпись рядом с ним",
    ).toContain("Операторы:");
    expect(
      within(карточка).queryByText("Назначить"),
      "зелёная подпись действия вернулась в каждую строку списка",
    ).toBeNull();
  });

  /*
   * Пояснение занимало вторую линию в каждой открытой всем строке и объясняло
   * слово, которое само себя объясняет. Читалке с экрана фраза остаётся
   * текстом: `title` произносят не все и не всегда.
   */
  it("«Операторы: все» объясняется подсказкой, а не второй видимой строкой", async () => {
    mount(account({ operators: { count: 0, preview: [] } }));
    const карточка = await строка();

    const значение = within(карточка).getByText(/^Операторы:/);
    expect(значение).toHaveAttribute("title", ВСЕ_ОПЕРАТОРЫ);

    const пояснение = within(карточка).getByText(ВСЕ_ОПЕРАТОРЫ);
    expect(
      пояснение.hasAttribute("data-sr-note"),
      "пояснение снова стоит видимой строкой и растит высоту ряда",
    ).toBe(true);
  });

  /*
   * Три числа стояли одним переносящимся рядом: у канала с «512 обращений за
   * неделю · 61 без ответа» ряд ломался пополам и становился выше соседних.
   */
  it("первая линия чисел недели не переносится, «без ответа» — вне её", async () => {
    mount();
    const карточка = await строка();

    const линия = карточка.querySelector(".week-bars__line") as HTMLElement;
    expect(линия, "первой линии чисел нет вовсе").toBeTruthy();
    expect(линия.textContent).toContain("512");
    expect(линия.textContent).toContain("обращений за неделю");

    const пропуски = карточка.querySelector(".week-bars__missed") as HTMLElement;
    expect(пропуски, "числа без ответа пропали").toBeTruthy();
    expect(
      пропуски.closest(".week-bars__line"),
      "«без ответа» вернулось в первую линию — она снова переносится",
    ).toBeNull();

    const линияCss = правило(WEEK_CSS, ".week-bars__line");
    expect(линияCss, "первая линия снова переносится").toMatch(/white-space:\s*nowrap/);
    expect(линияCss, "не влезшее обрывается без признака обрезки").toMatch(
      /text-overflow:\s*ellipsis/,
    );
    expect(
      правило(WEEK_CSS, ".week-bars__numbers"),
      "числа снова уложены переносящимся рядом",
    ).toMatch(/flex-direction:\s*column/);
  });

  /*
   * Колонок шесть, и ни одна не была названа: «что это за число» выяснялось
   * наведением, а список читают всю смену. Шестая — обслуживание, у неё
   * подписи нет и на макете.
   */
  it("у каждой колонки, кроме служебной, есть подпись", async () => {
    mount();
    await строка();

    const подписи = document.querySelector(".accounts-page__head") as HTMLElement;
    expect(подписи, "строки подписей колонок нет").toBeTruthy();
    const слова = Array.from(подписи.children).map((el) => (el.textContent ?? "").trim());
    expect(слова.every((s) => s.length > 0), "подпись без слова").toBe(true);
    expect(слова.length, "подписей меньше, чем колонок").toBe(колонки().length - 1);

    expect(screen.getByText("Приём событий")).toBeInTheDocument();
  });

  it("подписи и строки берут КОЛОНКИ ИЗ ОДНОГО ПРАВИЛА", () => {
    // Два одинаковых набора колонок разъезжаются в тот день, когда правят один
    // из них. Общее правило разъехаться не может вовсе.
    const блок = CSS.split("}").find(
      (b) => /grid-template-columns:/.test(b) && /\.account-card:not\(\[data-open\]\)/.test(b),
    );
    expect(блок, "у свёрнутой строки нет набора колонок").toBeTruthy();
    expect(
      блок!.slice(0, блок!.indexOf("{")),
      "подписи колонок завели себе отдельный набор колонок",
    ).toContain(".accounts-page__head");
  });

  /*
   * ⚠ СЧЁТ, А НЕ ГЛАЗОМЕР. При прежних долях (1.3fr на приём событий) колонке
   * доставалось 355 px на мониторе 1920, а самая длинная тревожная строка —
   * «События нет заметно дольше обычного» с кнопкой «Обновить подписку» —
   * занимает 434 px по замеру на макете. То есть фраза, ради которой на
   * страницу и заходят, обрывалась ВСЕГДА.
   */
  it("колонка приёма событий вмещает тревожную фразу с кнопкой", () => {
    const доли = колонки();
    expect(доли.length, "колонок стало не шесть").toBe(6);

    const МЕНЮ = 215; // полоса разделов и меню настроек слева — как в AccountsWidth
    const потолок = Number(CSS.match(/\.accounts-page\s*\{[^}]*max-width:\s*(\d+)px/s)![1]);
    const страница = Math.min(1920 - МЕНЮ, потолок) - 2 * токен("--lc-space-5");
    // Внутри строки: поля карточки, пять зазоров и неподвижная колонка «…».
    const подолям =
      страница - 2 * токен("--lc-space-4") - 5 * токен("--lc-space-3") - фикс(доли);
    const сумма = доли.reduce((s, к) => s + (к.fr ?? 0), 0);
    const ширина = (i: number) => ((доли[i].fr ?? 0) / сумма) * подолям;

    expect(Math.round(ширина(4)), "тревожная фраза с кнопкой снова не помещается").toBeGreaterThanOrEqual(434);
    expect(Math.round(ширина(3)), "«Токен истёк» с кнопкой снова не помещается").toBeGreaterThanOrEqual(215);
  });

  /*
   * Единственная фраза списка, которой две колонки бывает мало: сто десять
   * знаков про выключенный канал. Она переносилась и растила ряд выше соседних
   * — ровный список портился ради одного канала, которым никто не пользуется.
   */
  it("объяснение выключенного канала обрезается, а не растит ряд", async () => {
    mount(account({ status: "disabled" }));
    const карточка = await строка();

    const заметка = карточка.querySelector(".account-card__disabled-note") as HTMLElement;
    expect(заметка, "объяснения выключенного канала нет").toBeTruthy();
    expect(
      заметка.getAttribute("title"),
      "обрезанную фразу больше негде прочесть целиком",
    ).toBe(заметка.textContent);

    expect(
      правило(CSS, ".account-card:not([data-open]) .account-card__disabled-note"),
      "фраза снова переносится и ряд выключенного канала становится выше соседних",
    ).toMatch(/white-space:\s*nowrap/);
  });

  /*
   * Рамка слева забирала три пикселя, и их возвращали уменьшенным отступом —
   * два числа, которые обязаны сходиться вручную. Внутренняя тень ширину
   * ячейки не трогает вовсе.
   */
  it("полоса «требует переподключения» не двигает содержимое строки", () => {
    const тело = правило(CSS, '.account-card[data-status="needs_reauth"]');
    expect(тело, "полоса снова рисуется рамкой и забирает ширину").not.toMatch(/border-left/);
    expect(тело, "ширину опять возвращают уменьшенным отступом").not.toMatch(/padding-left/);
    expect(тело, "полосы тревоги не осталось вовсе").toMatch(/box-shadow:\s*inset/);
  });
});

/** Колонки свёрнутой строки: доля (`fr`) либо неподвижная ширина в пикселях. */
function колонки(): Array<{ fr?: number; px?: number }> {
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
      if (fr) return { fr: Number(fr[1]) };
      return { px: Number(трек.match(/(\d+)px/)![1]) };
    });
}

/** Сколько ширины забрали неподвижные колонки. */
function фикс(доли: Array<{ fr?: number; px?: number }>): number {
  return доли.reduce((s, к) => s + (к.px ?? 0), 0);
}
