// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts: часть сторожей ниже только читает файлы стилей.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TeamMembersTab } from "@/features/settings/team/TeamMembersTab";
import { TEAM_PAGE_SIZE } from "@/features/settings/team/api";
import { ROLE_HINTS, ROLE_LABELS, ROLE_ORDER } from "@/features/settings/team/roles";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TeamUserDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * СТРОКА СОТРУДНИКА СОБРАНА ЗАНОВО (макет «Команда» 04.09, пункты 2, 3, 5, 8-10).
 *
 * Что было. Девять колонок на тринадцать человек: почта своей колонкой, цвет
 * своей, и обе за это платили. Почте доставалось около двухсот пикселей, и
 * адрес переносился посреди слова — «admin@leadpart / ner.ru» не прочитать и не
 * сверить глазом. Колонка «Цвет» отдавала 22 пикселя ячейке, где у большинства
 * стоял прочерк. Справа от таблицы (потолок 1240) на широком мониторе пустовала
 * треть экрана, а описания ролей — они в проекте есть — показывались только
 * при ПРИГЛАШЕНИИ, хотя роль меняют здесь и права применяются немедленно.
 *
 * Что стало. Почта — второй строкой под именем, цвет — кольцом вокруг аватара
 * (там же он и правится), справка «Что может роль» — в освободившейся трети.
 *
 * ⚠ ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ. Макет предлагал убрать колонку «Статус» вместе со
 * словом «работает». Это решение владельца от 11 августа, принятое ОБРАТНЫМ
 * ходом и закреплённое в TeamStatusPager.test.tsx: колонка, пустая у всех,
 * читается как поломка выдачи. Не трогаем.
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
    created_at: "2026-06-01T08:00:00Z",
    ...overrides,
  };
}

/** Боевой состав в миниатюре: администратор, руководитель, менеджеры, цвет. */
const TEAM: TeamUserDto[] = [
  member(1, { full_name: "Анна Кузнецова", role: "admin", is_online: true, color: "#3b82f6" }),
  member(2, { full_name: "Дмитрий Соколов", role: "head" }),
  member(3, { full_name: "Марина Егорова" }),
  member(4, { full_name: "Юлия Петрова", role: "observer" }),
];

let patched: { url: string; body: unknown } | null = null;

function setupFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/users") && (init?.method ?? "GET") === "GET") {
        return jsonResponse(200, {
          items: TEAM,
          page: { limit: TEAM_PAGE_SIZE, offset: 0, total: TEAM.length },
        });
      }
      if (init?.method === "PATCH") {
        patched = { url: url.pathname, body: JSON.parse(String(init.body)) };
        return jsonResponse(200, { user: TEAM[0] });
      }
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    }),
  );
}

/** Ячейка «Сотрудник» указанной строки. */
async function personCell(name: string): Promise<HTMLElement> {
  const table = await screen.findByRole("table");
  const cell = within(table).getByText(name).closest("td");
  expect(cell, `нет ячейки сотрудника ${name}`).not.toBeNull();
  return cell as HTMLElement;
}

describe("Команда — сотрудник одной ячейкой", () => {
  beforeEach(() => {
    queryClient.clear();
    patched = null;
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

  it("почта стоит под именем, а не отдельной колонкой", async () => {
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    const table = await screen.findByRole("table");

    // Адрес НА ЭКРАНЕ ЕСТЬ — вопрос только в том, где именно он стоит. Иначе
    // эта проверка зеленела бы и на экране, потерявшем почту вовсе.
    expect(within(table).getByText("user2@partner-lead-centre.ru")).toBeInTheDocument();
    const cell = await personCell("Дмитрий Соколов");
    expect(within(cell).getByText("user2@partner-lead-centre.ru")).toBeInTheDocument();

    const headers = within(table)
      .getAllByRole("columnheader")
      .map((th) => th.textContent?.trim());
    expect(headers).not.toContain("Email");
    expect(headers).not.toContain("Цвет");
    // Семь колонок вместо девяти: почта и цвет сложены в первую.
    expect(headers).toEqual(["Сотрудник", "Роль", "Отдел", "Ведёт диалоги", "Статус", "Онлайн", "Действия"]);
  });

  it("цвет правится нажатием на аватар, а не отдельной ячейкой", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    const cell = await personCell("Марина Егорова");

    // Кнопка выбора цвета живёт В ЯЧЕЙКЕ СОТРУДНИКА, а не в своей колонке.
    const target = within(cell).getByRole("button", { name: "Цвет: Марина Егорова" });

    await user.click(target);
    await user.click(await screen.findByRole("button", { name: "Цвет #3b82f6" }));

    expect(patched?.url).toMatch(/\/users\/u-3$/);
    expect(patched?.body).toEqual({ color: "#3b82f6" });
  });

  it("выбранный цвет виден кольцом аватара, а не пропадает вместе с колонкой", async () => {
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    const withColor = within(await personCell("Анна Кузнецова")).getByRole("button", {
      name: "Цвет: Анна Кузнецова",
    });
    const without = within(await personCell("Марина Егорова")).getByRole("button", {
      name: "Цвет: Марина Егорова",
    });

    expect(withColor).toHaveStyle({ borderColor: "#3b82f6" });
    // У невыбравшего кольцо задаёт CSS (прозрачное, проступает по наведению),
    // а не разметка: иначе цвет «по умолчанию» перебивал бы подсветку строки.
    expect(without.style.borderColor).toBe("");
  });

  it("справка «Что может роль» называет все четыре роли словами из roles.ts", async () => {
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    const aside = await screen.findByRole("complementary", { name: "Что может каждая роль" });

    for (const r of ROLE_ORDER) {
      expect(within(aside).getByText(ROLE_LABELS[r])).toBeInTheDocument();
      // Дословно, а не пересказом: разъехавшаяся справка о правах хуже её
      // отсутствия — по ней принимают решение, кому что открыть.
      expect(within(aside).getByText(ROLE_HINTS[r])).toBeInTheDocument();
    }
  });
});

/* ─────────────────────── правила, которые живут в CSS ─────────────────────── */

const CSS = (readFileSync("src/features/settings/team/team.css", "utf-8") as string).replace(
  /\/\*[\s\S]*?\*\//g,
  " ",
);
const VARS = readFileSync("src/app/lc-vars.css", "utf-8") as string;

/** Тело правила по селектору — с любым отступом (правила есть и внутри @media). */
function rule(selector: string): string {
  const at = CSS.indexOf(`${selector} {`);
  expect(at, `правило ${selector} не найдено`).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

/** Значение токена тёмной темы (первое объявление — базовое). */
function token(name: string): string {
  const m = new RegExp(`${name}:\\s*([^;]+);`).exec(VARS);
  expect(m, `токен ${name} не объявлен`).toBeTruthy();
  const v = m![1].trim();
  const ссылка = /^var\((--lc-[a-z0-9-]+)\)$/.exec(v);
  return ссылка ? token(ссылка[1]) : v;
}

/** Относительная яркость WCAG — нужна ровно для «светлее/темнее». */
function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => {
    const x = parseInt(h.slice(i, i + 2), 16) / 255;
    return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

describe("Команда — правила строки в стилях", () => {
  it("наведение осветляет строку, а не топит её", () => {
    /*
     * Довод здесь не «так в макете», а арифметика темы: общий
     * `--lc-surface-hover` темнее поверхности карточки, на которой лежит
     * таблица. Строка под курсором проваливалась бы вглубь — движение вниз
     * читается как «выключено», а не «здесь курсор».
     */
    expect(luminance(token("--lc-surface-hover"))).toBeLessThan(luminance(token("--lc-bg-raise")));
    expect(luminance(token("--lc-selected"))).toBeGreaterThan(luminance(token("--lc-bg-raise")));

    const тело = rule(".team-members__table tbody tr:hover td");
    expect(тело).toMatch(/background:\s*var\(--lc-selected\)/);
    expect(тело, "строка под курсором снова тонет").not.toMatch(/var\(--lc-surface-hover\)/);
  });

  it("залиты только роли с правами, менеджер тих", () => {
    // Менеджеров тридцать из тридцати пяти: самая частая роль обязана быть
    // самой тихой, иначе заливка перестаёт что-либо выделять.
    const тихий = rule(".team-role--manager");
    expect(тихий).toMatch(/background:\s*var\(--lc-status-closed-bg\)/);
    expect(тихий, "менеджер выкрашен акцентом").not.toMatch(/--lc-(primary|info|warn|danger)/);

    expect(rule(".team-role--admin")).toMatch(/background:\s*var\(--lc-primary-subtle\)/);
    expect(rule(".team-role--head")).toMatch(/background:\s*var\(--lc-info-subtle\)/);
    // «Только чтение» — это отсутствие заливки, а не ещё один цвет.
    expect(rule(".team-role--observer"), "у наблюдателя появилась заливка").not.toMatch(
      /background:/,
    );
  });

  it("справка занимает освободившуюся треть, а не ложится поверх таблицы", () => {
    // Потолок таблицы (1240) и ширина карточки (272) — одно решение: без
    // второй колонки в сетке справка оказалась бы под таблицей на любом экране.
    //
    // ⚠ ПОРОГ ПЕРЕЕХАЛ С ОКНА НА ШИРИНУ РАЗДЕЛА (08.09): окно содержимому не
    // достаётся целиком — слева рельса, по умолчанию развёрнутая. Прежние 1560
    // были верны только для свёрнутой; разбор и числа замера — в
    // TeamAdaptive0809.
    expect(rule(".team-members__table")).toMatch(/max-width:\s*1240px/);
    const шире = CSS.match(
      /@container team-members \(min-width:\s*1220px\)\s*\{([\s\S]*?)\n\}/,
    );
    expect(шире, "широкой раскладки нет").toBeTruthy();
    expect(шире![1]).toMatch(/grid-template-columns:\s*minmax\(0,\s*1240px\)\s*272px/);
  });
});

describe("Команда — цифры подвала", () => {
  beforeEach(() => {
    queryClient.clear();
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

  it("счётчик набран моноширинными цифрами", async () => {
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    // Подпись на месте — проверяем ИМЕННО набор, а не наличие подвала.
    const счётчик = await screen.findByText("4 сотрудника");
    expect(счётчик).toHaveClass("team-pager__count");
    expect(rule(".team-pager__count")).toMatch(/font-variant-numeric:\s*tabular-nums/);
  });
});
