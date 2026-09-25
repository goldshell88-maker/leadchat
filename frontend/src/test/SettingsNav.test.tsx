import { afterEach, describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { SettingsLayout } from "@/features/settings/SettingsLayout";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Вторая NAV-колонка `/settings/*` (11 §4): состав пунктов — строго по правам.
 * Пункт «Боты» отдельно: право `bots:manage` есть только у admin (01 §8), и без
 * пункта на экран ботов не попасть иначе как прямой ссылкой.
 */

function renderNav(role: Role, permissions: Permission[]) {
  resetSessionStore({ user: { ...fakeUser, role }, permissions, accessToken: "t", bootstrapped: true });
  return renderWithProviders(<SettingsLayout />, { route: "/settings/profile" });
}

afterEach(() => resetSessionStore());

describe("Меню настроек (11 §4)", () => {
  it("admin видит «Боты» и попадает на /settings/bots", () => {
    renderNav("admin", ["accounts:read", "templates:shared", "users:manage", "audit:read", "bots:manage"]);
    const link = screen.getByRole("link", { name: "Боты" });
    expect(link).toHaveAttribute("href", "/settings/bots");
  });

  it("порядок пунктов — как в макете: аккаунты, быстрые ответы, боты, лид-бот, команда, профиль", () => {
    renderNav("admin", ["accounts:read", "templates:shared", "users:manage", "audit:read", "bots:manage"]);
    const names = screen.getAllByRole("link").map((el) => el.textContent);
    // «Лид-бот» стоит СРАЗУ ПОСЛЕ «Ботов» и отдельной строкой: решение
    // владельца от 12 августа — это не один из ботов, а отдельная система
    // на своём сервере. Соседство читается как «оба про автоматику»,
    // отдельная строка — как «настраиваются они по-разному».
    expect(names).toEqual([
      "Аккаунты Авито",
      // «Люди и каналы» — сразу за аккаунтами (просьба владельца 04.09).
      // Соседство читается как «оба про каналы», отдельная строка — как «это
      // другой вопрос»: список отвечает «кто на этом канале», решётка — «куда
      // подключён этот человек».
      "Люди и каналы",
      "Быстрые ответы",
      "Боты",
      "Лид-бот",
      "Команда",
      "Профиль",
    ]);
  });

  it("без права bots:manage обоих пунктов нет — у head со всеми его правами тоже", () => {
    renderNav("head", ["accounts:read", "templates:shared", "users:manage", "audit:read"]);
    expect(screen.queryByRole("link", { name: "Боты" })).toBeNull();
    // И лид-бота тоже: право у них одно, и прячутся они вместе. Забудь
    // второй — руководитель получил бы экран, где включается автоответ
    // живым клиентам и меняется адрес чужого сервиса.
    expect(screen.queryByRole("link", { name: "Лид-бот" })).toBeNull();
    expect(screen.getByRole("link", { name: "Команда" })).toBeInTheDocument();
  });

  it("у менеджера в настройках остаётся только профиль", () => {
    renderNav("manager", []);
    expect(screen.getAllByRole("link").map((el) => el.textContent)).toEqual(["Профиль"]);
  });

  it("диспетчер видит «Быстрые ответы» — раздел открыт по templates:own", () => {
    /*
     * ⚠ РАЗБОР 03.09, И ЭТО ГЛАВНАЯ ПРАВКА РАЗДЕЛА.
     *
     * Пункт закрывался правом `templates:shared`, которого у роли `manager`
     * нет. То есть человек, у которого 17 % исходящих — дословно заготовки, не
     * мог открыть экран с их названием: личные лежали вкладкой в «Профиле», а
     * кнопка «Управлять шаблонами» из пикера высаживала его на «Учётную
     * запись». Цена была видна в данных: личные заготовки завели 2 человека
     * из 57.
     *
     * Условие пункта обязано совпадать с `RequirePermission` в router.tsx.
     * Разойдись они — пункт вёл бы на отказ, либо маршрут остался бы без входа.
     */
    renderNav("manager", ["templates:own", "stats:own"]);
    const link = screen.getByRole("link", { name: "Быстрые ответы" });
    expect(link).toHaveAttribute("href", "/settings/templates");
  });

  it("наблюдателю без своих заготовок пункта нет", () => {
    /* Обратная половина правила: открыли не всем подряд, а тем, у кого есть что
       настраивать. У `observer` нет ни `templates:own`, ни `templates:shared`. */
    renderNav("observer", ["conversations:read"]);
    expect(screen.queryByRole("link", { name: "Быстрые ответы" })).toBeNull();
  });
});
