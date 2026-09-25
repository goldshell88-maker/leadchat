import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ProfilePage } from "@/features/settings/profile/ProfilePage";
import type { Permission, Role } from "@/shared/auth/usePermissions";
import type { MyChannelDto } from "@/shared/api/types";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * Блок «Мои каналы» в профиле (план 7.2, п. 5).
 *
 * С назначением сотрудников на каналы очередь «Входящие» перестаёт показывать
 * всем всё, и у менеджера появляется вопрос, которого раньше не было: почему
 * коллега видит диалог, а я нет. Блок отвечает на него без администратора —
 * и отдельно объясняет каналы, попавшие в список по правилу совместимости
 * («на канал никто не назначен, поэтому он открыт всем»).
 *
 * Отдельная проверка — администратор: сервер отдаёт ему ВСЁ как «открыт всем»
 * (`sees_all_channels`), и объяснение про ненаписанные назначения для него
 * ложь в обе стороны (PROF-03).
 */

const MANAGER_PERMISSIONS: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "templates:own",
];

const CHANNELS: MyChannelDto[] = [
  { id: "acc-7", title: "! Парт - 7 / Ист - В43 МНЧ !", access: "assigned" },
  { id: "acc-723", title: "Парт - 723 БЕЛЫЙ", access: "open" },
];

let channels: MyChannelDto[];
let status = 200;

function setupFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/me/channels")) {
        return status === 200
          ? jsonResponse(200, { items: channels })
          : jsonResponse(500, { error: { code: "internal_error", message: "боль" } });
      }
      return jsonResponse(200, {});
    }),
  );
}

function renderProfile(role: Role = "manager", permissions: Permission[] = MANAGER_PERMISSIONS) {
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

describe("«Мои каналы» в профиле сотрудника (7.2)", () => {
  beforeEach(() => {
    channels = CHANNELS;
    status = 200;
    setupFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("показывает каналы сотрудника и помечает открытые всем", async () => {
    renderProfile();

    expect(await screen.findByText("Мои каналы")).toBeInTheDocument();
    expect(await screen.findByText("! Парт - 7 / Ист - В43 МНЧ !")).toBeInTheDocument();
    expect(screen.getByText("Парт - 723 БЕЛЫЙ")).toBeInTheDocument();
    expect(screen.getByText("назначен")).toBeInTheDocument();
    expect(screen.getByText("открыт всем")).toBeInTheDocument();
    expect(
      screen.getByText(/на канал не назначен ни один сотрудник, поэтому его диалоги видят все/i),
    ).toBeInTheDocument();
  });

  it("без единого канала предупреждает, что диалоги не придут", async () => {
    channels = [];
    renderProfile();

    expect(await screen.findByText("Каналы не назначены")).toBeInTheDocument();
    expect(screen.getByText(/Напишите администратору/)).toBeInTheDocument();
  });

  it("объяснение без пометки «открыт всем» не показывается", async () => {
    channels = [CHANNELS[0]];
    renderProfile();

    expect(await screen.findByText("назначен")).toBeInTheDocument();
    expect(screen.queryByText("открыт всем")).not.toBeInTheDocument();
    expect(screen.queryByText(/не назначен ни один сотрудник/i)).not.toBeInTheDocument();
  });

  it("ошибка загрузки не ломает профиль", async () => {
    status = 500;
    renderProfile();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Список каналов не загрузился. Нажмите «Повторить»",
    );
    // Остальной профиль на месте — блок падает сам, а не вместе со страницей.
    expect(screen.getByText("Мои каналы")).toBeInTheDocument();
    expect(screen.getByText(fakeUser.full_name)).toBeInTheDocument();
  });

  /*
   * PROF-06. Ошибка была тупиком: красная строка без кнопки, выход — только
   * перезагрузка всей страницы профиля. А прямо над этим блоком стоит форма
   * «Написать администратору», и перезагрузка стирает набранное в ней.
   */
  it("у ошибки есть кнопка «Повторить», и она правда перезапрашивает список", async () => {
    status = 500;
    const user = userEvent.setup();
    renderProfile();

    const retry = await screen.findByRole("button", { name: "Повторить" });
    status = 200;
    await user.click(retry);

    expect(await screen.findByText("! Парт - 7 / Ист - В43 МНЧ !")).toBeInTheDocument();
  });

  /*
   * PROF-03. Администратору сервер отдаёт КАЖДЫЙ канал как «открыт всем»
   * (`sees_all_channels`) — он видит очередь целиком по роли. Прежняя подпись
   * объясняла это единственной причиной на всех: «на канал не назначен ни один
   * сотрудник». Для администратора это ложь в обе стороны: назначения на
   * каналах есть, а видит он их всё равно. Прочитав буквально, он пойдёт
   * искать пропавшие назначения, которых никто не терял.
   */
  it("администратору не объясняют его доступ пустыми назначениями", async () => {
    channels = [
      { id: "acc-7", title: "! Парт - 7 / Ист - В43 МНЧ !", access: "open" },
      { id: "acc-723", title: "Парт - 723 БЕЛЫЙ", access: "open" },
    ];
    renderProfile("admin", [...MANAGER_PERMISSIONS, "users:manage", "accounts:manage"]);

    expect(await screen.findByText("! Парт - 7 / Ист - В43 МНЧ !")).toBeInTheDocument();
    expect(screen.queryByText(/не назначен ни один сотрудник/i)).toBeNull();
    // Вместо неё — правда про роль.
    expect(screen.getByText(/Вы администратор — вам приходят все каналы/)).toBeInTheDocument();
    // И тринадцати одинаковых серых плашек «открыт всем» тоже нет.
    expect(screen.queryByText("открыт всем")).toBeNull();
  });

  it("наблюдателю блок не показывают и запрос за каналами не шлют", async () => {
    renderProfile("observer", ["conversations:read"]);

    await waitFor(() => expect(screen.getByText("Профиль")).toBeInTheDocument());
    expect(screen.queryByText("Мои каналы")).not.toBeInTheDocument();
    const requested = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.some(
      (c) => String(c[0]).includes("/me/channels"),
    );
    expect(requested).toBe(false);
  });
});
