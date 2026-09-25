// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в TeamAdaptive0509 и surfaceLadder: часть сторожей читает файлы стилей.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { TeamMembersTab } from "@/features/settings/team/TeamMembersTab";
import { TEAM_PAGE_SIZE } from "@/features/settings/team/api";
import type { TeamUserDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { RAIL_WIDTH_COLLAPSED, RAIL_WIDTH_EXPANDED } from "@/shared/stores/railStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ПОРОГ СПРАВКИ О РОЛЯХ СЧИТАЛ ОКНО, А РЕШАЕТ ШИРИНА РАЗДЕЛА (08.09).
 *
 * ⚠ ЧЕСТНО О ГРАНИЦАХ ЭТОГО СТОРОЖА. jsdom раскладку не считает вовсе: у него
 * нет ни ширин, ни переносов, ни прокрутки. Проверить «таблица не уезжает за
 * край» здесь невозможно ни одним способом — именно поэтому беда и доехала до
 * боя мимо 300+ зелёных проверок. Что стережётся ниже: ПРИЧИНА — какой
 * величиной задан порог в стилях, совпадает ли она с числом в разметке и не
 * вернулась ли прежняя арифметика по окну. Результат мерян в браузере на
 * стенде (.dump/prov-team-*), числа замера — ниже.
 *
 * ЧТО БЫЛО (стенд, рельса развёрнута — 218, умолчание railStore):
 *   окно 1559 → сетка 1073, одна колонка, спрятано 0
 *   окно 1561 → сетка 1075, колонки 779 + 272, таблица упирается в min-width
 *               860 при 779 доступных → спрятано 83 (колонка «Действия» за
 *               краем), и одновременно появляется справка «Что может роль»
 *   окно 1600 → спрятано 44; 1640 → 4; 1680 → 0
 *   вторая ступень: 1959 → таблица 1175, 1961 → 1109 (справка выросла до 340),
 *               обещанных 1240 таблица достигала только около 2090
 *
 * ЧТО СТАЛО (там же):
 *   окно 1561 → сетка 1075, одна колонка, таблица 1073, спрятано 0
 *   окно 1706 → сетка 1220, колонки 924 + 272, спрятано 0 — справка приходит
 *               туда, где под неё есть место
 *   окно 2106 → сетка 1620, колонки 1256 + 340, таблица сразу шире прежних 1240
 *   спрятано 0 на всей матрице 1440…2560
 *
 * И ГЛАВНОЕ ДОКАЗАТЕЛЬСТВО, ЧТО ПОМЕНЯЛСЯ МОМЕНТ, А НЕ РАСКЛАДКА: при
 * СВЁРНУТОЙ рельсе (72) замеры до и после совпадают до пикселя на всех
 * пятнадцати ширинах — включая сами пороги (1560 и 1960). Прежние числа были
 * верны ровно для той рельсы, которую заложили в расчёт, и неверны для той,
 * что стоит у людей.
 */

const CSS = readFileSync("src/features/settings/team/team.css", "utf-8") as string;
/** Комментарии срезаем: в них разобрано, КАК было, и наивный поиск краснел бы
    на самом объяснении (тот же приём в TeamAdaptive0509 и cssDeadClasses). */
const СТИЛИ = CSS.replace(/\/\*[\s\S]*?\*\//g, " ");
const TSX = readFileSync("src/features/settings/team/TeamMembersTab.tsx", "utf-8") as string;
const РАЗМЕТКА = TSX.replace(/\/\*[\s\S]*?\*\//g, " ");

/** Обвязка раздела настроек без рельсы: список разделов 220 + поля 48. */
const ОБВЯЗКА_БЕЗ_РЕЛЬСЫ = 268;

function число(re: RegExp, где: string, что: string): number {
  const m = re.exec(где);
  expect(m, `не нашлось: ${что}`).toBeTruthy();
  return Number(m![1]);
}

describe("Команда — порог справки задан шириной раздела", () => {
  it("раздел объявлен контейнером, и обе ступени спрашивают его ширину", () => {
    const раздел = СТИЛИ.slice(СТИЛИ.indexOf(".team-members {"), СТИЛИ.indexOf("}", СТИЛИ.indexOf(".team-members {")));
    expect(раздел, "раздел перестал быть контейнером — @container ниже молчит").toMatch(
      /container-type:\s*inline-size/,
    );
    expect(раздел).toMatch(/container-name:\s*team-members/);

    expect(СТИЛИ, "первая ступень снова считает окно").toMatch(
      /@container team-members \(min-width: 1220px\)/,
    );
    expect(СТИЛИ, "вторая ступень снова считает окно").toMatch(
      /@container team-members \(min-width: 1620px\)/,
    );
  });

  it("прежние пороги по окну не вернулись", () => {
    // ⚠ ОБРАТНАЯ ПОЛОВИНА ПРОВЕРКИ. 1560 и 1960 — не «плохие числа сами по
    // себе»: они верны для свёрнутой рельсы и неверны для развёрнутой. Вернуть
    // их медиазапросом значит вернуть беду целиком, и заметить это будет нечем.
    expect(СТИЛИ, "вернулся медиазапрос @media (min-width: 1560px)").not.toMatch(
      /@media \(min-width: 1560px\)/,
    );
    expect(СТИЛИ, "вернулся медиазапрос @media (min-width: 1960px)").not.toMatch(
      /@media \(min-width: 1960px\)/,
    );
    // И в разметке: ширину окна она больше не спрашивает вовсе.
    expect(РАЗМЕТКА, "разметка снова решает раскладку по ширине окна").not.toMatch(
      /useMediaQuery/,
    );
  });

  it("страховка по окну посчитана от развёрнутой рельсы, а не от запасной", () => {
    const первая = число(/@media \(min-width: (\d+)px\) \{\s*\.team-members__layout/, СТИЛИ, "страховка первой ступени");
    const вторая = число(/@media \(min-width: (\d+)px\) \{\s*\.team-members__table/, СТИЛИ, "страховка второй ступени");

    // Обе страховки отстоят от своих контейнерных порогов ровно на обвязку
    // раздела при РАЗВЁРНУТОЙ рельсе. Ошибиться в эту сторону безопасно:
    // страховка включится позже, чем есть место, а не раньше.
    expect(первая - 1220).toBe(RAIL_WIDTH_EXPANDED + ОБВЯЗКА_БЕЗ_РЕЛЬСЫ);
    expect(вторая - 1620).toBe(RAIL_WIDTH_EXPANDED + ОБВЯЗКА_БЕЗ_РЕЛЬСЫ);

    // А это — сама беда, записанная числом: прежние 1560 и 1960 получаются из
    // тех же порогов, если считать рельсу СВЁРНУТОЙ. Значит расчёт был верен,
    // а величина в нём — нет.
    expect(1220 + RAIL_WIDTH_COLLAPSED + ОБВЯЗКА_БЕЗ_РЕЛЬСЫ).toBe(1560);
    expect(1620 + RAIL_WIDTH_COLLAPSED + ОБВЯЗКА_БЕЗ_РЕЛЬСЫ).toBe(1960);
  });

  it("разметка и стили спрашивают ОДНО число", () => {
    // Разъезд здесь даёт состояние, которого не бывает: CSS уже поставил
    // справку в колонку сбоку, а разметка всё ещё рисует свёртку — свёрнутая
    // строка в пустой трети экрана.
    const вРазметке = число(/const ROLES_ASIDE_MIN_LAYOUT = (\d+);/, РАЗМЕТКА, "порог в разметке");
    const вСтилях = число(/@container team-members \(min-width: (\d+)px\)/, СТИЛИ, "порог в стилях");
    expect(вРазметке).toBe(вСтилях);
  });
});

/* ─────────────── ветка разметки: справка выбирает тег по замеру ────────── */

const ADMIN_PERMISSIONS: Permission[] = ["users:manage", "audit:read"];

const КОМАНДА: TeamUserDto[] = [
  {
    id: "u-1",
    email: "anna@partner-lead-centre.ru",
    full_name: "Анна Кузнецова",
    role: "admin",
    is_active: true,
    is_online: false,
    invite_pending: false,
    handles_conversations: true,
    department: null,
    color: null,
    created_at: "2026-09-01T08:00:00Z",
  } as TeamUserDto,
];

const роднойRect = Element.prototype.getBoundingClientRect;

/**
 * Ширина ЛЮБОГО блока для замера.
 *
 * jsdom отдаёт нули на любой прямоугольник, а справка выбирает тег по ширине
 * своей сетки (`useInlineSize`). Подменяем ровно то, что решает дело;
 * остальные поля — те же нули, что и у jsdom.
 */
function ширинаСетки(width: number) {
  Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0, width, height: 0 } as DOMRect;
  };
}

describe("Команда — справка меняет вид по ширине сетки", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.endsWith("/users/assignable")) return jsonResponse(200, { items: [] });
        if (url.pathname.endsWith("/users")) {
          return jsonResponse(200, {
            items: КОМАНДА,
            page: { limit: TEAM_PAGE_SIZE, offset: 0, total: КОМАНДА.length },
          });
        }
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    Element.prototype.getBoundingClientRect = роднойRect;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("сетка 1219 — справка свёрнута в строку", async () => {
    // 1219 — сетка при окне 1559 и свёрнутой рельсе, то есть ровно под порогом.
    ширинаСетки(1219);
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    expect(screen.getByText("Что может роль").closest("details")).not.toBeNull();
    expect(
      screen.queryByRole("complementary", { name: "Что может каждая роль" }),
      "справка встала карточкой сбоку там, где для неё нет места",
    ).toBeNull();
  });

  it("сетка 1220 — справка стоит карточкой сбоку", async () => {
    ширинаСетки(1220);
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    await screen.findByRole("complementary", { name: "Что может каждая роль" });
    expect(screen.queryByText("Что может роль")?.closest("details")).toBeFalsy();
  });

  it("замера нет — остаётся прежний вид со справкой, а не свёртка", async () => {
    // Ноль от jsdom и любой среды без раскладки — это «неизвестно», а не
    // «узко»: свернуть справку по незнанию значило бы менять вид на ровном
    // месте. То же умолчание, что стояло у прежнего useMediaQuery.
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");
    await screen.findByRole("complementary", { name: "Что может каждая роль" });
  });
});
