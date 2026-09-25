// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в TeamRoster0409.test.ts и surfaceLadder.test.ts: часть сторожей ниже
// только читает файлы стилей — померить кегль в jsdom нечем (`css: false`).
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { AuditLogTab } from "@/features/settings/team/AuditLogTab";
import { TeamMembersTab } from "@/features/settings/team/TeamMembersTab";
import { TEAM_PAGE_SIZE } from "@/features/settings/team/api";
import { ROLE_HINTS, ROLE_ORDER } from "@/features/settings/team/roles";
import type { AuditLogPage, TeamUserDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * АДАПТИВНОСТЬ РАЗДЕЛА «КОМАНДА» — ЗАМЕРЫ 05.09 НА СТЕНДЕ ВЁРСТКИ.
 *
 * Проверялось на пяти ширинах (375, 768, 1024, 1440, 2560) в обеих темах;
 * стенд подключал настоящие файлы стилей раздела. Что нашлось и что здесь
 * стережётся:
 *
 * 1. СПРАВКА О РОЛЯХ СЪЕДАЛА СПИСОК. Она появилась 04.09 и ниже 1560 вставала
 *    второй строкой сетки, а вторая строка берёт высоту раньше первой. При
 *    1440×900 из тринадцати сотрудников оставалось видно ПЯТЬ, при 1024 — ДВА,
 *    при 375 — НИ ОДНОГО (список ужимался до двух пикселей, а подпись
 *    «13 сотрудников» ложилась поверх текста справки).
 *
 * 2. ОБЁРТКА ПРОКРУТКИ ПЕРЕЖИЛА СВОЙ ДОВОД. `.audit__scroll` берёт прокрутку
 *    на себя, чтобы не уезжала прилипшая шапка колонок. Ниже 901 строка
 *    становится карточкой, а `thead` спрятан — беречь нечего, но обёртка
 *    продолжала сжиматься в иллюминатор: 161 пиксель на карточки высотой 320.
 *    Тот самый класс дефекта, что описан в правиле проекта: медиазапрос
 *    остался от механизма, которого больше нет.
 *
 * 3. ПОЛ ШИРИНЫ ПОЧТЫ НЕ РАБОТАЛ. `min-width` стоял на `.team-members__email`
 *    с тех пор, когда почта была ЯЧЕЙКОЙ таблицы; 04.09 она стала строчным
 *    `<span>`, а к строчному элементу `min-width` не применяется вовсе.
 *
 * 4. ПОТОЛОК 1240 ОСТАЛСЯ ОТ ДЕВЯТИ КОЛОНОК. При 2560 справа простаивало 684
 *    пикселя, при том что колонка «Отдел» резала живое название бригады.
 *
 * 5. ЖУРНАЛ НЕ УМЕЛ УЗКИЙ ЭКРАН. При 375 вправо уезжало 324 пикселя — «Объект»
 *    и кнопка «детали» недостижимы без горизонтальной прокрутки, о которой
 *    ничто не сообщает.
 *
 * 6. МИШЕНИ МЕЛЬЧЕ НОРМЫ: кружок цвета 18×18, «детали» 21 по высоте.
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
    color: null,
    created_at: "2026-09-01T08:00:00Z",
    ...overrides,
  };
}

const TEAM: TeamUserDto[] = [
  member(1, { full_name: "Анна Кузнецова", role: "admin" }),
  member(2, { full_name: "Дмитрий Соколов", role: "head" }),
  member(3, { full_name: "Марина Егорова", department: "Бригада Андрея Владиславовича КП · Б6" }),
];

function auditPage(): AuditLogPage {
  return {
    items: [
      {
        id: 90211,
        user: { id: "u-1", full_name: "Анна Кузнецова" },
        action: "user.role_changed",
        description: "Смена роли: Вячеслав Константинопольский → Менеджер",
        entity: "user",
        entity_id: "7f3a19c2-1111-2222-3333-444455556666",
        details: { role: "manager" },
        created_at: "2026-09-05T11:32:00Z",
      },
    ],
    page: { limit: 50, offset: 0, total: 1 },
  };
}

function setupFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/audit-log")) return jsonResponse(200, auditPage());
      if (url.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
      if (url.pathname.endsWith("/users") && (init?.method ?? "GET") === "GET") {
        return jsonResponse(200, {
          items: TEAM,
          page: { limit: TEAM_PAGE_SIZE, offset: 0, total: TEAM.length },
        });
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    }),
  );
}

/**
 * Ширина СЕТКИ раздела, по которой справка выбирает вид.
 *
 * ⚠ РАНЬШЕ ЗДЕСЬ ПОДМЕНЯЛОСЬ ОКНО (`matchMedia`), И ЭТО БЫЛО ЧАСТЬЮ БЕДЫ
 * (08.09). Окно содержимому не достаётся целиком: слева рельса приложения, по
 * умолчанию развёрнутая на 218. Порог считает теперь ширину самой сетки —
 * `useInlineSize` в разметке, `@container` в стилях; разбор и числа замера — в
 * TeamAdaptive0809. jsdom раскладку не считает и отдаёт нули, поэтому ширину
 * подставляем руками; остальные поля прямоугольника — те же нули, что у jsdom.
 */
const роднойRect = Element.prototype.getBoundingClientRect;

function stubViewport(width: number) {
  Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0, width, height: 0 } as DOMRect;
  };
}

function asAdmin() {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ADMIN_PERMISSIONS,
    accessToken: "t",
    bootstrapped: true,
  });
}

describe("Команда — справка о ролях уступает списку на узком экране", () => {
  beforeEach(() => {
    queryClient.clear();
    setupFetch();
    asAdmin();
  });

  afterEach(() => {
    Element.prototype.getBoundingClientRect = роднойRect;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("ниже порога справка свёрнута в строку, а не стоит блоком", async () => {
    // 1075 — сетка при окне 1561 и развёрнутой рельсе: та самая ширина, на
    // которой справка появлялась вместе с прокруткой таблицы.
    stubViewport(1075);
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    const fold = screen.getByText("Что может роль").closest("details");
    expect(fold, "справка снова занимает высоту постоянным блоком").not.toBeNull();
    // Свёрнута ИМЕННО по умолчанию: раскрытая по умолчанию отнимает ту же
    // высоту, ради которой всё и делалось.
    expect((fold as HTMLDetailsElement).open).toBe(false);
    expect(
      screen.queryByRole("complementary", { name: "Что может каждая роль" }),
      "на 1440 справка снова стоит боковой карточкой — там для неё нет места",
    ).toBeNull();
  });

  it("содержимое справки от свёртывания не теряется", async () => {
    stubViewport(375);
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    const fold = screen.getByText("Что может роль").closest("details");
    for (const r of ROLE_ORDER) {
      // Дословно и внутри той же свёртки: по этой справке решают, кому что
      // открыть, и пересказ здесь хуже отсутствия.
      expect(within(fold as HTMLElement).getByText(ROLE_HINTS[r])).toBeInTheDocument();
    }
  });

  it("на широком экране справка остаётся раскрытой карточкой сбоку", async () => {
    // 1475 — сетка при окне 1961 и развёрнутой рельсе.
    stubViewport(1475);
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    // Там она стоит в пустой трети экрана и никому не мешает — ради этого её
    // 04.09 и завели.
    const aside = await screen.findByRole("complementary", { name: "Что может каждая роль" });
    expect(within(aside).getByText(ROLE_HINTS.admin)).toBeInTheDocument();
    expect(screen.queryByText("Что может роль")?.closest("details")).toBeFalsy();
  });
});

describe("Журнал аудита на телефоне", () => {
  beforeEach(() => {
    queryClient.clear();
    setupFetch();
    asAdmin();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("строка умеет становиться карточкой, и у каждой ячейки своя подпись", async () => {
    renderWithProviders(<AuditLogTab />, { route: "/settings/team" });
    const table = await screen.findByRole("table");

    expect(table.className, "журнал снова прячет колонки за прокруткой вбок").toContain(
      "lc-table--cards",
    );

    const cells = within(table).getAllByRole("cell");
    const labels = cells.map((td) => td.getAttribute("data-label"));
    // Подписи совпадают с заголовками колонок дословно: расхождение дало бы на
    // телефоне подпись «Роль» над отделом, и заметить это было бы нечем.
    expect(labels.slice(0, 4)).toEqual(["Время", "Сотрудник", "Действие", "Объект"]);
    // У ячейки с кнопкой подписи нет намеренно: «Детали» встало бы рядом со
    // словом «детали» на самой кнопке.
    expect(labels[4]).toBeNull();
  });
});

/* ─────────────────── правила, которые живут в стилях ──────────────────── */

const CSS_PATH = "src/features/settings/team/team.css";
const RAW_CSS = readFileSync(CSS_PATH, "utf-8") as string;
/** Комментарии срезаем: в них разобрано, КАК было, и наивный поиск краснел бы
    на самом объяснении (тот же приём в TeamRoster0409 и cssDeadClasses). */
const CSS = RAW_CSS.replace(/\/\*[\s\S]*?\*\//g, " ");
const TSX = readFileSync("src/features/settings/team/TeamMembersTab.tsx", "utf-8") as string;

/** Тело правила по селектору — с любым отступом (правила есть и внутри @media). */
function rule(selector: string): string {
  const at = CSS.indexOf(`${selector} {`);
  expect(at, `правило ${selector} не найдено`).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

/** Тело контейнерного запроса — тем же счётом скобок, что и у медиазапроса. */
function контейнер(query: string): string {
  return блок(`@container team-members ${query} {`);
}

/** Тело медиазапроса целиком — со счётом скобок, вложенные правила не рвутся. */
function media(query: string): string {
  return блок(`@media ${query} {`);
}

function блок(head: string): string {
  const at = CSS.indexOf(head);
  expect(at, `запрос ${head} не найден`).toBeGreaterThan(-1);
  let depth = 0;
  for (let i = at + head.length - 1; i < CSS.length; i += 1) {
    if (CSS[i] === "{") depth += 1;
    else if (CSS[i] === "}") {
      depth -= 1;
      if (depth === 0) return CSS.slice(at, i);
    }
  }
  throw new Error(`запрос ${head} не закрыт`);
}

describe("Команда — адаптивность в стилях", () => {
  it("порог справки один и тот же в разметке и в стилях", () => {
    // Разъезд здесь даёт состояние, которого не бывает: CSS уже поставил
    // справку в колонку сбоку, а разметка всё ещё рисует свёртку — и человек
    // видит свёрнутую строку в пустой трети экрана.
    //
    // ⚠ ПОРОГ СЧИТАЕТ ШИРИНУ СЕТКИ, А НЕ ОКНА (08.09) — разбор в
    // TeamAdaptive0809.
    const порог = /const ROLES_ASIDE_MIN_LAYOUT = (\d+);/.exec(TSX);
    expect(порог, "порог справки не объявлен числом").toBeTruthy();
    expect(CSS, "контейнерный запрос двух колонок разошёлся с разметкой").toContain(
      `@container team-members (min-width: ${порог![1]}px)`,
    );
  });

  it("в карточном режиме прокрутку берёт страница, а не обёртка таблицы", () => {
    const узко = media("(max-width: 900px)");
    // Обе таблицы раздела: у обеих в карточном режиме шапка спрятана, и
    // сжимать их в иллюминатор незачем.
    expect(узко).toMatch(/\.team-members \.audit__scroll,\s*\.audit > \.audit__scroll \{/);
    expect(узко, "обёртка снова держит свою прокрутку").toMatch(/overflow:\s*visible/);
    // Рамка и растушёвка «справа есть ещё» тоже уходят: карточки внутри
    // карточки — три рамки подряд, а крутить вбок в этом режиме нечего.
    expect(узко).toMatch(/background:\s*none/);
  });

  it("пол ширины стоит на блочном элементе, а не на строчном", () => {
    const широко = media("(min-width: 901px)");
    expect(широко, "пол ширины ушёл со строки имени и почты").toMatch(
      /\.team-members__table \.team-person__text \{\s*min-width:\s*240px/,
    );
    // ⚠ И ОБРАТНАЯ ПОЛОВИНА: мёртвое правило не должно вернуться. `min-width`
    // на строчном `<span>` не делает ничего — вернуть его значит вернуть
    // видимость защиты без защиты.
    expect(rule(".team-members__email"), "пол вернулся на строчный элемент").not.toMatch(
      /min-width/,
    );
  });

  it("на очень широком мониторе у таблицы и справки есть вторая ступень", () => {
    // Ступени считаются по ширине раздела: 1220 и 1620 (08.09).
    const шире = контейнер("(min-width: 1620px)");
    expect(шире).toMatch(/max-width:\s*1600px/);
    expect(шире).toMatch(/grid-template-columns:\s*minmax\(0,\s*1600px\)\s*340px/);
    // Первая ступень при этом жива: между 1220 и 1620 раскладка прежняя.
    expect(контейнер("(min-width: 1220px)")).toMatch(
      /grid-template-columns:\s*minmax\(0,\s*1240px\)\s*272px/,
    );
  });

  it("журнал не растягивается во всю ширину монитора", () => {
    expect(rule(".audit > .audit__scroll")).toMatch(/max-width:\s*1240px/);
  });

  it("мишени не мельче нормы 24", () => {
    const кружок = rule(".team-color-swatch");
    expect(кружок).toMatch(/width:\s*24px/);
    expect(кружок).toMatch(/height:\s*24px/);
    expect(rule(".audit__toggle")).toMatch(/min-height:\s*24px/);
  });
});

describe("Команда — иерархия и движение", () => {
  it("имя и почта разведены по кеглю, а не отличаются пикселем", () => {
    expect(rule(".team-person__name")).toMatch(/font-size:\s*var\(--lc-fz-body\)/);
    expect(rule(".team-members__email")).toMatch(/font-size:\s*var\(--lc-fz-micro\)/);
  });

  it("в журнале главное — действие, служебное тише", () => {
    expect(rule(".audit__action")).toMatch(/font-weight:\s*var\(--lc-fw-medium\)/);
    expect(rule(".audit__time"), "время снова весит как действие").toMatch(
      /font-size:\s*var\(--lc-fz-caption\)/,
    );
  });

  it("движение объясняет состояние и выключается по просьбе системы", () => {
    // Подсветка строки и поворот значка — токенами длительности, а не числами.
    expect(rule(".team-members__table tbody td")).toMatch(
      /transition:\s*background var\(--lc-dur-fast\)/,
    );
    expect(rule(".team-roles__chevron")).toMatch(/transition:\s*transform var\(--lc-dur-fast\)/);

    const тихо = CSS.match(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?\n\}/g) ?? [];
    const всё = тихо.join("\n");
    expect(всё, "подсветка строки не гасится при просьбе убрать движение").toMatch(
      /\.team-members__table tbody td \{\s*transition:\s*none/,
    );
    expect(всё, "поворот значка не гасится при просьбе убрать движение").toMatch(
      /\.team-roles__chevron \{\s*transition:\s*none/,
    );
  });
});
