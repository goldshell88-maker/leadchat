import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ProfilePage } from "@/features/settings/profile/ProfilePage";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ПОДСКАЗКА ПРО ПАРОЛЬ ЗНАЕТ, КТО СМОТРИТ.
 *
 * ЧТО БЫЛО. Один текст на всех: «Пароль меняет администратор — напишите ему
 * формой ниже или в поддержку Telegram @unlukyguy». Администратор открывал
 * свой профиль и читал прямо под собственной ролью «Администратор»
 * предложение написать администратору — то есть самому себе, формой, которая
 * кладёт обращение в его же центр уведомлений. Круг замкнутый: человек,
 * который единственный и может задать пароль, отправлен просить об этом себя.
 *
 * ПОЧЕМУ ПРАВО, А НЕ РОЛЬ. Набор прав приходит с сервера (`GET /auth/me`), и
 * матрицу «роль → права» фронт не повторяет намеренно
 * (shared/auth/usePermissions.ts). Пароли сотрудникам задаёт тот, у кого
 * `users:manage`; появись завтра вторая роль с этим правом — текст ей
 * достанется правильный сам собой.
 */

const MANAGER_PERMISSIONS: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "templates:own",
];

const ADMIN_PERMISSIONS: Permission[] = [...MANAGER_PERMISSIONS, "users:manage", "accounts:manage"];

function renderProfile(role: Role, permissions: Permission[]) {
  resetSessionStore({
    user: { ...fakeUser, role },
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/settings/profile"]}>
          <ProfilePage />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

describe("Профиль: подсказка о смене пароля", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [] })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("администратору не предлагают написать администратору", () => {
    renderProfile("admin", ADMIN_PERMISSIONS);

    expect(screen.queryByText(/Пароль меняет администратор/)).toBeNull();
    expect(screen.queryByText(/напишите ему формой ниже/)).toBeNull();
  });

  it("администратору сказано, где он задаёт ЧУЖИЕ пароли", () => {
    renderProfile("admin", ADMIN_PERMISSIONS);

    // Раздел «Команда» — то самое место, где живут «Задать пароль» и
    // «Сбросить пароль» (features/settings/team/TeamMembersTab.tsx).
    expect(screen.getByText(/в разделе «Команда»/)).toBeTruthy();
  });

  /*
   * ⚠ ТРИ ПРОВЕРКИ НИЖЕ ПЕРЕПИСАНЫ 05.09, И ПРИЧИНА НЕ В НИХ. Они утверждали
   * «свой пароль меняет кто-то другой» — и были верны ровно до этого дня, пока
   * формы не существовало: ручка `POST /auth/change-password` жила на сервере
   * без единого потребителя (дефект аудита FUNC-82). Форма появилась, значит
   * прежние подсказки стали враньём, а сторож, стерегущий враньё, хуже
   * отсутствующего.
   *
   * Что осталось стеречь: разницу между СВОИМ и ЧУЖИМ паролем. Свой человек
   * меняет сам — у всех ролей одинаково; чужие сбрасывает администратор.
   */
  it("свой пароль меняется на месте — у всех ролей", () => {
    renderProfile("manager", MANAGER_PERMISSIONS);
    expect(screen.getByLabelText("Текущий пароль")).toBeTruthy();
    expect(screen.getByLabelText("Новый пароль")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Сменить пароль" })).toBeTruthy();
  });

  it("никому больше не сказано, что пароль меняет администратор", () => {
    renderProfile("manager", MANAGER_PERMISSIONS);
    expect(screen.queryByText(/Пароль меняет администратор/)).toBeNull();
    expect(screen.queryByText(/в разделе «Команда»/)).toBeNull();
  });

  it("наблюдателю — то же самое: право сменить свой пароль есть у всех", () => {
    renderProfile("observer", ["conversations:read"]);
    expect(screen.getByRole("button", { name: "Сменить пароль" })).toBeTruthy();
  });
});

/**
 * ВКЛАДКИ ПРОФИЛЯ — ДОСТУПНОСТЬ.
 *
 * Роли `tablist`/`tab` стояли, а остального набора не было: панель под ними не
 * имела `role="tabpanel"` и ни на кого не ссылалась, `aria-controls` не было, и
 * все вкладки собирали фокус по Tab подряд. Для программы чтения с экрана это
 * разъехавшееся обещание: объявлено «вкладка 2 из 3», а содержимое — просто
 * следующий кусок страницы, никак с ней не связанный.
 */
describe("Вкладки профиля — клавиатура и роли", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [] })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("стрелка переключает вкладку и уводит за собой фокус", async () => {
    const user = userEvent.setup();
    renderProfile("admin", ADMIN_PERMISSIONS);

    const account = screen.getByRole("tab", { name: "Учётная запись" });
    expect(account).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "Интерфейс" })).toHaveAttribute("tabindex", "-1");

    account.focus();
    await user.keyboard("{ArrowRight}");

    const iface = screen.getByRole("tab", { name: "Интерфейс" });
    expect(iface).toHaveAttribute("aria-selected", "true");
    expect(iface).toHaveFocus();
  });

  it("панель подписана активной вкладкой", () => {
    renderProfile("admin", ADMIN_PERMISSIONS);

    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", "profile-tab-account");
    expect(screen.getByRole("tab", { name: "Учётная запись" })).toHaveAttribute(
      "aria-controls",
      panel.id,
    );
  });
});
