/**
 * Хвост строки канала: «Включить», меню «…», операторы и неделя (замер 08.09).
 *
 * ⚠ ЭТОТ СТОРОЖ РАСКЛАДКИ НЕ ВИДИТ. jsdom не считает ни сетку, ни флекс:
 * `getBoundingClientRect` там всегда нули. Поэтому ниже проверяется ПРИЧИНА —
 * что именно объявлено в accounts.css и кто на самом деле приезжает в разметку,
 * — а сами числа померены руками в браузере на собранном CSS и настоящей
 * разметке (стенд `.dump/mera-accounts-*.html`, страница `.dump/mera-accounts.html`).
 *
 * ⚠ СТЕНД СТАВИТ РЕЛЬСУ 218, А НЕ 72. По умолчанию панель развёрнута
 * (railStore.ts: `expanded: true`, RAIL_WIDTH_EXPANDED = 218), запасное же
 * значение `--lc-rail-w` в lc-vars.css равно 72 — то есть замер «по умолчанию»
 * врёт на 146 px в сторону «всё влезает». Все числа ниже сняты при 218 и
 * перепроверены при 72.
 *
 * ЧТО БЫЛО ПОМЕРЕНО (client/scroll, рельса 218):
 *
 *  1. «Включить» — единственный способ вернуть канал в работу. Было: кнопка
 *     36 px, подпись 10 из 68 (на 1920+ — из 73), то есть от слова видно шестую
 *     часть. И так на ВСЕХ рабочих ширинах: 902, 1024, 1180, 1280, 1366, 1440,
 *     1512, 1600, 1920, 2560. Стало: кнопка 94 (99 на 1920+), подпись 68/68
 *     (73/73) — целиком, на всех тех же ширинах.
 *  2. Операторы. Было: 902 → 52/154, 1024 → 72/154, 1180 → 98/154, 1280 →
 *     114/154, 1366 → 128/154, 1440 → 140/154, 1512 → 152/154 — обрезка тихая,
 *     кружки резались посреди. Стало: client = scroll на всех ширинах от 1024;
 *     при 902 счёт обрезается ВИДИМО, многоточием («Операто…»), а лица целиком
 *     уходят на скрытую вторую линию.
 *  3. Неделя. Было: 902 → 53 при нужных 60 (7 столбиков × 6 px + 6 зазоров ×
 *     3 px) — правый столбик семидневки пропадал. Стало: 60/60.
 *  4. Меню «…» у всех строк на одной вертикали: замер 902/1024/1440/1920/2560 —
 *     правый край меню совпадает у всех пяти строк списка.
 */
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// так же поступают соседние сторожа, читающие исходники.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const CSS = readFileSync("src/features/settings/accounts/accounts.css", "utf8") as string;
const WEEK_CSS = readFileSync("src/features/settings/accounts/week-bars.css", "utf8") as string;
const PAGE = readFileSync("src/features/settings/accounts/AccountsPage.tsx", "utf8") as string;

/** Та же таблица стилей без комментариев: их текст не должен ловиться правилами. */
const ЧИСТЫЙ = CSS.replace(/\/\*.*?\*\//gs, "");

/**
 * Тело правила по ТОЧНОМУ селектору.
 *
 * ⚠ Не `indexOf`: `.account-card__operators` встречается и внутри более
 * длинного селектора `.account-card:not([data-open]) .account-card__operators`,
 * и первая редакция сторожа читала чужое тело (`grid-column: 3`) — то есть
 * проверяла не то правило, которое чинили.
 */
function правило(селектор: string): string {
  const rx = new RegExp(
    `(?:^|[};])\\s*${селектор.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`,
    "m",
  );
  const m = ЧИСТЫЙ.match(rx);
  expect(m, `правило «${селектор}» не найдено`).toBeTruthy();
  return m![1];
}

/** Набор колонок свёрнутой строки, без комментариев. */
function треки(): string[] {
  const m = ЧИСТЫЙ.match(
    /\.account-card:not\(\[data-open\]\)\s*\{[^}]*grid-template-columns:([^;]+);/s,
  );
  expect(m, "у свёрнутой строки нет grid-template-columns").toBeTruthy();
  return m![1]
    .split("\n")
    .map((s: string) => s.trim())
    .filter((s: string) => s.length > 0);
}

const DAY_MS = 86_400_000;

function account(overrides: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    id: "acc-1",
    title: "Никита КП",
    avito_user_id: 111222333,
    status: "active",
    own_keys: false,
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
    operators: { count: 4, preview: [{ id: "u1", full_name: "Анна Смирнова" }] },
    stats: null as unknown,
    ...overrides,
  };
}

function mount(acc: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/avito-accounts")) {
        return jsonResponse(200, { items: [acc], page: { limit: 50, offset: 0, total: 1 } });
      }
      return jsonResponse(200, {});
    }),
  );
  return renderWithProviders(<AccountsPage />);
}

async function строка(): Promise<HTMLElement> {
  // `article`, а не `.account-card`: тот же класс носит скелетон загрузки, а у
  // него детей нет — сторож проверял бы пустоту (грабли AccountsRowGrid0409).
  return await waitFor(() => {
    const el = document.querySelector("article.account-card");
    expect(el, "строка канала не отрисовалась").toBeTruthy();
    return el as HTMLElement;
  });
}

describe("Хвост строки канала: «Включить» рядом с «…»", () => {
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

  it("у выключенного канала в блоке действий ДВОЕ, а не один значок", async () => {
    /*
     * Это и есть причина беды: колонку считали под меню обслуживания, а в неё
     * приезжает ещё и «Включить». Факт проверяется на настоящей разметке —
     * стоит кнопке уехать в другое место, правила ниже станут мёртвыми, и
     * сторож обязан об этом сказать.
     */
    mount(account({ status: "disabled" }));
    const блок = (await строка()).querySelector(".account-card__actions");
    expect(блок, "у выключенного канала нет блока действий").toBeTruthy();
    const дети = Array.from(блок!.children);
    expect(дети.length, "в блоке действий не двое").toBe(2);
    expect(блок!.textContent, "кнопки «Включить» в блоке действий нет").toContain("Включить");
  });

  it("у живого канала в том же блоке один жилец — меню обслуживания", async () => {
    // Отрицательная проверка: если «Включить» появится у всех, «двое у
    // выключенного» перестанет отличать выключенный канал от живого.
    mount(account());
    const блок = (await строка()).querySelector(".account-card__actions");
    expect(блок!.children.length).toBe(1);
    expect(блок!.textContent).not.toContain("Включить");
  });

  it("хвост выключенной строки шире одной колонки меню", () => {
    const тело = правило('.account-card[data-status="disabled"]:not([data-open]) .account-card__actions');
    expect(тело, "хвост выключенной строки снова занимает одну колонку").toMatch(
      /grid-column:\s*5\s*\/\s*span\s*2/,
    );
    // Перенос вырастил бы ряд выше соседних, сжатие — снова обрезало бы подпись.
    expect(тело).toMatch(/flex-wrap:\s*nowrap/);
    expect(
      правило('.account-card[data-status="disabled"]:not([data-open]) .account-card__actions > *'),
    ).toMatch(/flex:\s*none/);
  });

  it("меню «…» прижато к правому краю своей колонки у ВСЕХ строк", () => {
    /*
     * Иначе «Включить» с «…» встанут по своему краю, а тридцать четыре
     * одиноких значка — по своему: замер до этой строчки — 398 у выключенной
     * строки против 402 у соседних.
     */
    expect(правило(".account-card:not([data-open]) .account-card__actions")).toMatch(
      /justify-content:\s*flex-end/,
    );
  });

  it("починка не оплачена шириной соседних колонок", () => {
    /*
     * Расширить общий трек значило бы отнять 102 px у КАЖДОЙ из тридцати пяти
     * строк ради кнопки одного канала. Трек обязан остаться неподвижным и
     * узким — ровно под меню.
     */
    const список = треки();
    expect(список.length, "колонок стало не шесть").toBe(6);
    const последний = список[5];
    const px = последний.match(/^(\d+)px/);
    expect(px, `последняя колонка перестала быть неподвижной: ${последний}`).toBeTruthy();
    expect(Number(px![1]), "колонка меню разрослась — значит платят соседи").toBeLessThanOrEqual(48);
  });

  it("объяснение и тревога подписки не встают друг на друга", () => {
    /*
     * У выключенного канала строка приёма молчит всегда, кроме одной новости —
     * «подписку снять не удалось» (ChannelHealth.tsx). Хвост занят кнопками,
     * значит четвёртая колонка достаётся кому-то одному: тревоге, потому что
     * она про ЭТОТ канал, а объяснение одинаково у всех выключенных.
     */
    expect(правило(".account-card:not([data-open]) .account-card__disabled-note")).toMatch(
      /grid-column:\s*4\s*;/,
    );
    expect(
      правило(
        '.account-card[data-status="disabled"]:not([data-open]) .account-card__health[data-kind="webhook"]',
      ),
    ).toMatch(/grid-column:\s*4\s*;/);
    expect(
      CSS,
      "объяснение и тревога снова делят одну клетку — одно ляжет поверх другого",
    ).toMatch(
      /:has\(\.account-card__health\[data-kind="webhook"\]\)\s*\n?\s*\.account-card__disabled-note\s*\{[^}]*display:\s*none/s,
    );
  });

  it("в столбике (≤900) объяснение возвращается: там тесноты нет", () => {
    const m = CSS.match(/@media \(max-width:\s*900px\)\s*\{([\s\S]*?)\n\}/);
    expect(m, "нет правил для узких экранов").toBeTruthy();
    expect(
      m![1],
      "на телефоне объяснение выключенного канала осталось спрятанным",
    ).toMatch(/\.account-card__disabled-note\s*\{[^}]*display:\s*block/s);
  });

  it("«Включить» живёт в блоке действий — иначе правила выше ни о чём", () => {
    // Привязка правил к разметке: правило про хвост держится ровно до тех пор,
    // пока кнопка стоит там, где её ищет селектор.
    const блок = PAGE.slice(PAGE.indexOf('className="account-card__actions"'));
    expect(блок.slice(0, 1200), "кнопка «Включить» уехала из блока действий").toContain(
      "Включить",
    );
  });
});

describe("Состав канала: что не влезло — пропадает целиком", () => {
  it("у ячейки операторов есть чем показать обрезку", () => {
    /*
     * `overflow: hidden` без многоточия и без прокрутки — это тихая обрезка по
     * построению. Показывает её теперь пара «перенос + потолок высоты»: не
     * поместившийся кусок уходит на вторую линию и срезается ЦЕЛИКОМ, а не
     * пополам, — половина кружка это не часть сведения, а мусор.
     */
    const тело = правило(".account-card__operators");
    expect(тело, "ячейка операторов больше не обрезает — проверка ниже ни о чём").toMatch(
      /overflow:\s*hidden/,
    );
    expect(тело, "тихая обрезка вернулась: нет переноса").toMatch(/flex-wrap:\s*wrap/);
    expect(тело, "нет потолка высоты — вторая линия растит ряд").toMatch(/max-height:\s*24px/);
  });

  it("потолок высоты равен росту аватарки — иначе он режет первую линию", () => {
    // 24 берётся из разметки: <UserAvatar size={24} /> в AccountsPage.tsx.
    expect(PAGE, "рост аватарки в разметке разошёлся с потолком высоты").toMatch(
      /UserAvatar\s+name=\{[^}]*\}\s+size=\{24\}/,
    );
  });

  it("счёт уступает последним и обрезается видимо", () => {
    const тело = правило(".account-card__operators-label");
    expect(тело, "счёт снова режется без знака обрезки").toMatch(/text-overflow:\s*ellipsis/);
    expect(тело).toMatch(/overflow:\s*hidden/);
    expect(тело).toMatch(/white-space:\s*nowrap/);
    // `flex: none` держал бы счёт целым и резал бы его посреди слова.
    expect(тело, "счёт снова несжимаем — обрезка вернётся к «Операто» без многоточия").toMatch(
      /flex:\s*0\s+1\s+auto/,
    );
  });

  it("лица не сжимаются — сжатие и есть срезанный посреди кружок", () => {
    const тело = правило(".account-card__operators-faces");
    expect(тело).toMatch(/flex:\s*none/);
    expect(тело, "лица снова умеют сжиматься до половины кружка").not.toMatch(/min-width:\s*0/);
  });
});

describe("Неделя канала: диаграмма не режется", () => {
  it("у колонки недели минимум равен ширине семидневки", () => {
    const список = треки();
    const неделя = список[1];
    const min = неделя.match(/minmax\(\s*(\d+)px/);
    expect(min, `у колонки недели снова нулевой минимум: ${неделя}`).toBeTruthy();
    expect(Number(min![1])).toBe(60);
  });

  it("60 — это и есть ширина семидневки, а не круглое число", () => {
    /*
     * Считаем из самой диаграммы: столбик 6 px, зазор 3 px, дней семь. Сдвинь
     * любую из трёх величин — и минимум колонки перестанет совпадать с
     * рисунком, о чём эта проверка и скажет.
     */
    const столбик = WEEK_CSS.match(/\.week-bars__bar\s*\{[^}]*width:\s*(\d+)px/s);
    const зазор = WEEK_CSS.match(/\.week-bars__chart\s*\{[^}]*gap:\s*(\d+)px/s);
    expect(столбик, "у столбика недели нет ширины").toBeTruthy();
    expect(зазор, "у диаграммы недели нет зазора").toBeTruthy();
    const ширина = 7 * Number(столбик![1]) + 6 * Number(зазор![1]);
    expect(ширина, "рисунок семидневки разошёлся с минимумом колонки").toBe(60);
  });

  it("остальные колонки минимума по-прежнему не имеют", () => {
    /*
     * Отрицательная проверка. Минимумы на всех колонках сразу — это дефект
     * 05.09 (сумма 706 px резала меню обслуживания на 1024); исключение
     * оправдано только для рисунка постоянной ширины.
     */
    const сминимумом = треки().filter((т) => /minmax\(\s*[1-9]\d*px/.test(т));
    expect(сминимумом.length, `минимум завели ещё где-то: ${сминимумом.join(" | ")}`).toBe(1);
  });
});
