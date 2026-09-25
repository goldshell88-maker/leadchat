/**
 * Строка канала не «едет»: у каждого блока своя колонка (жалоба владельца 04.09).
 *
 * ⚠ ДОСЛОВНО: «вкладка Аккаунты Авито всё так же едет, сделай всё максимально
 * ровно, функционально и компактно».
 *
 * ЧТО БЫЛО. Свёрнутая карточка — сетка, и колонок в ней было объявлено пять, а
 * жильцов приезжало шесть. Лишний — блок действий — уезжал на ВТОРУЮ строку, и
 * ряд разваливался. Хуже того, часть жильцов необязательна (нет статистики,
 * выключенный канал), и без закреплённых колонок соседи сдвигались каждый на
 * своё место: у тридцати пяти каналов получалось тридцать пять раскладок.
 *
 * ⚠ ПОЧЕМУ СТОРОЖ, А НЕ ОДНА ПРАВКА CSS. Правка держится ровно до следующего
 * блока, который кто-нибудь добавит в карточку. Сторож ловит именно это: он
 * читает из accounts.css, кому колонка НАЗНАЧЕНА, и сверяет со списком тех,
 * кто в строке действительно оказался.
 */
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как и у соседнего сторожа AccountsWidth.test.ts.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { AccountsPage } from "@/features/settings/accounts/AccountsPage";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

const CSS = readFileSync("src/features/settings/accounts/accounts.css", "utf8");

/** Кому в свёрнутой строке НАЗНАЧЕНА колонка — читаем из самой таблицы стилей. */
const ЗАКРЕПЛЕНЫ: string[] = (() => {
  const найдено: string[] = [];
  const rx = /\.account-card:not\(\[data-open\]\)\s+([^{,]+?)\s*(?:,|\{)/g;
  let m: RegExpExecArray | null;
  const блоки = CSS.split("}");
  for (const блок of блоки) {
    if (!/grid-column\s*:/.test(блок)) continue;
    const голова = блок.slice(0, блок.indexOf("{"));
    rx.lastIndex = 0;
    while ((m = rx.exec(голова + ","))) найдено.push(m[1].trim());
  }
  return найдено;
})();

const DAY_MS = 86_400_000;

function account(overrides: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    id: "acc-1",
    title: "Fake Avito Account",
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
    operators: { count: 2, preview: [{ id: "u1", full_name: "Анна Смирнова" }] },
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
  /*
   * ⚠ `article`, А НЕ ПРОСТО `.account-card`. Пока список грузится, тот же
   * класс носит ПУСТОЙ скелетон (`div.account-card--skeleton`), и первая
   * редакция сторожа находила именно его. Детей у скелетона нет, значит и
   * «блоков без колонки» нет — проверка зеленела при любой раскладке и
   * пропустила диверсию, снявшую колонку у недели.
   */
  const card = await waitFor(() => {
    const el = document.querySelector("article.account-card");
    expect(el, "карточка канала не отрисовалась").toBeTruthy();
    return el as HTMLElement;
  });
  expect(card.hasAttribute("data-open"), "карточка приехала раскрытой").toBe(false);
  // Пустая карточка не проверяет ничего: сторож обязан смотреть на жильцов.
  expect(card.children.length, "в строке канала нет ни одного блока").toBeGreaterThanOrEqual(3);
  return card;
}

function безколонки(card: HTMLElement): string[] {
  return Array.from(card.children)
    .filter((el) => !ЗАКРЕПЛЕНЫ.some((sel) => el.matches(sel)))
    .map((el) => el.tagName.toLowerCase() + "." + (el.className || "без класса"));
}

describe("свёрнутая строка канала", () => {
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

  it("таблица стилей вообще раздаёт колонки — иначе сторож проверял бы пустоту", () => {
    expect(ЗАКРЕПЛЕНЫ.length).toBeGreaterThanOrEqual(6);
    expect(ЗАКРЕПЛЕНЫ).toContain(".account-card__actions");
  });

  it("у живого канала со статистикой каждый блок стоит в своей колонке", async () => {
    mount(
      account({
        stats: { total: 288, missed: 1, daily: [10, 20, 30, 40, 50, 60, 78], stale_seconds: 60 },
      }),
    );
    expect(безколонки(await строка())).toEqual([]);
  });

  it("у канала без статистики — тоже", async () => {
    mount(account());
    expect(безколонки(await строка())).toEqual([]);
  });

  it("у выключенного канала — тоже", async () => {
    mount(account({ status: "disabled" }));
    expect(безколонки(await строка())).toEqual([]);
  });

  it("ни одна колонка не меряется содержимым — иначе строки снова разъедутся", () => {
    /*
     * ⚠ ЭТО И БЫЛА ВТОРАЯ ЖАЛОБА («всё так же едет»). Каждая карточка — СВОЯ
     * сетка: тридцать пять строк, тридцать пять `display: grid`. Колонки у них
     * совпадают ровно настолько, насколько ширины не зависят от содержимого.
     * Стоило одной колонке стать `auto`, и канал с «292 обращения за неделю»
     * сдвигал вправо всё, что стоит после недели, а канал с «37 обращений» —
     * не сдвигал.
     */
    const шаблон = CSS.match(
      /\.account-card:not\(\[data-open\]\)\s*\{[^}]*grid-template-columns:([^;]+);/s,
    );
    expect(шаблон, "у свёрнутой строки нет grid-template-columns").toBeTruthy();
    const треки = шаблон![1].replace(/\/\*.*?\*\//gs, "");
    for (const слово of ["auto", "max-content", "min-content", "fit-content"]) {
      expect(треки, `колонка меряется содержимым: ${слово}`).not.toContain(слово);
    }
  });

  it("колонок объявлено не меньше, чем занятых номеров", () => {
    const шаблон = CSS.match(
      /\.account-card:not\(\[data-open\]\)\s*\{[^}]*grid-template-columns:([^;]+);/s,
    );
    expect(шаблон, "у свёрнутой строки нет grid-template-columns").toBeTruthy();
    // Колонки перечислены по одной на строку с комментарием — считаем их.
    const колонок = шаблон![1].split("\n")
      .filter((строка: string) => /\S/.test(строка.replace(/\/\*.*?\*\//g, ""))).length;
    const максимум = Math.max(
      ...[...CSS.matchAll(/grid-column:\s*(\d+)/g)].map((m) => Number(m[1])),
    );
    expect(колонок).toBeGreaterThanOrEqual(максимум);
  });
});
