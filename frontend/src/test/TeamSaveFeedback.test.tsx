import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TeamMembersTab } from "@/features/settings/team/TeamMembersTab";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TeamUserDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Отдел, тумблер «Ведёт диалоги» и текст ошибки (TEAM-02, TEAM-03, TEAM-07).
 *
 * Все три про одно: экран сохраняет молча и объясняет неправдой. Тумблер не
 * двигался до ответа сервера, поле отдела оставалось с новым значением после
 * неудачного сохранения, а любая ошибка 422 подписывалась «Нельзя оставить
 * систему без администратора» — при том что настоящий последний администратор
 * приходит с 409.
 */

const ADMIN_PERMISSIONS: Permission[] = ["users:manage", "audit:read"];

const PETR: TeamUserDto = {
  id: "u-1",
  email: "petr@partner-lead-centre.ru",
  full_name: "Пётр Ковалёв",
  role: "manager",
  is_active: true,
  is_online: true,
  invite_pending: false,
  handles_conversations: true,
  department: "СТАРШИЕ - ЧАТЫ",
  created_at: "2026-06-01T08:00:00Z",
};

type Handler = (url: URL, init?: RequestInit) => Response | Promise<Response> | null;

let patchHandler: Handler | null = null;
/** Что отдаёт сервер на список — тест меняет, когда сохранение должно «дойти». */
let serverUser: TeamUserDto = PETR;
let fetchMock: ReturnType<typeof vi.fn>;

function patchBodies() {
  return fetchMock.mock.calls
    .filter((c) => (c[1] as RequestInit | undefined)?.method === "PATCH")
    .map((c) => JSON.parse(String((c[1] as RequestInit).body)) as Record<string, unknown>);
}

function setupFetch() {
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (init?.method === "PATCH") {
      const custom = await patchHandler?.(url, init);
      if (custom) return custom;
      return jsonResponse(200, serverUser);
    }
    if (url.pathname.endsWith("/users")) {
      return jsonResponse(200, { items: [serverUser], page: { limit: 50, offset: 0, total: 1 } });
    }
    return jsonResponse(404, errorEnvelope("not_found", "нет"));
  });
  vi.stubGlobal("fetch", fetchMock);
}

describe("Команда — сохранение видно, ошибка названа честно", () => {
  beforeEach(() => {
    queryClient.clear();
    patchHandler = null;
    serverUser = PETR;
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

  it("тумблер «Ведёт диалоги» двигается от щелчка, не дожидаясь сервера", async () => {
    const user = userEvent.setup();
    // Сервер держит ответ: ровно неспешная сеть, на которой беда и видна.
    let release: () => void = () => {};
    const held = new Promise<void>((r) => (release = r));
    patchHandler = async () => {
      await held;
      return jsonResponse(200, { ...PETR, handles_conversations: false });
    };

    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    const toggle = screen.getByLabelText("Ведёт диалоги: Пётр Ковалёв");
    expect(toggle).toBeChecked();

    await user.click(toggle);

    // Ответа ещё нет, а тумблер уже в новом положении: иначе человек решает,
    // что щелчок не сработал, и щёлкает второй раз — обратно.
    expect(toggle).not.toBeChecked();
    // И второго щелчка вдогонку не примет, пока летит первый запрос.
    expect(toggle).toBeDisabled();

    serverUser = { ...PETR, handles_conversations: false };
    release();
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();
    expect(patchBodies()).toEqual([{ handles_conversations: false }]);
  });

  it("не сохранился отдел — ячейка возвращается к серверному значению, а не врёт", async () => {
    const user = userEvent.setup();
    patchHandler = () =>
      jsonResponse(
        409,
        errorEnvelope("conflict", "Сотрудник изменён в другом окне", { reason: "stale" }),
      );

    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    const dept = screen.getByLabelText("Отдел: Пётр Ковалёв");
    await user.clear(dept);
    await user.type(dept, "ВЫЕЗД");
    await user.tab();

    expect(await screen.findByText("Сотрудник изменён в другом окне")).toBeInTheDocument();
    // Главное: на экране снова то, что на сервере. Раньше в ячейке оставался
    // «ВЫЕЗД», которого на сервере нет, и правка исчезала при перезагрузке.
    await waitFor(() => expect(dept).toHaveValue("СТАРШИЕ - ЧАТЫ"));
  });

  it("422 «свой пароль меняют в профиле» доходит до экрана дословно", async () => {
    const user = userEvent.setup();
    // Настоящий ответ сервера, app/services/users.py:487-491. Раньше экран
    // печатал вместо него «Нельзя оставить систему без администратора».
    const serverSays = "Свой пароль меняют в профиле — с вводом текущего";
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/set-password")) {
        return jsonResponse(422, errorEnvelope("unprocessable", serverSays, { reason: "use_own_profile" }));
      }
      if (url.pathname.endsWith("/users")) {
        return jsonResponse(200, { items: [serverUser], page: { limit: 50, offset: 0, total: 1 } });
      }
      return jsonResponse(404, errorEnvelope("not_found", "нет"));
    });

    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Задать пароль" }));
    await user.type(await screen.findByLabelText("Новый пароль"), "ЧайникРемонт7!");
    await user.click(screen.getByRole("button", { name: "Задать пароль" }));

    expect(await screen.findByText(serverSays)).toBeInTheDocument();
    expect(screen.queryByText("Нельзя оставить систему без администратора")).toBeNull();
    // И ровно один раз: у окна есть своё место под ошибку, страница под ним
    // молчит — иначе один отказ читается как два.
    expect(screen.getAllByText(serverSays)).toHaveLength(1);
  });

  it("400 validation_error — слова сервера и имя поля", async () => {
    const user = userEvent.setup();
    // Сервер переводит правила pydantic сам (`app/core/errors.py`): одно поле —
    // его текст и в `message`, и в разборе полей.
    patchHandler = () =>
      jsonResponse(
        400,
        errorEnvelope("validation_error", "Слишком длинно: не больше 100 символов", {
          fields: [
            {
              field: "department",
              rule: "string_too_long",
              message: "Слишком длинно: не больше 100 символов",
            },
          ],
        }),
      );

    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    const dept = screen.getByLabelText("Отдел: Пётр Ковалёв");
    await user.clear(dept);
    await user.type(dept, "ВЫЕЗД");
    await user.tab();

    expect(
      await screen.findByText("Не сохранилось: «Отдел» — Слишком длинно: не больше 100 символов"),
    ).toBeInTheDocument();
  });

  it("отказ правки почты — словами сервера и один раз, в окне (проверка 24.09)", async () => {
    const user = userEvent.setup();
    patchHandler = () =>
      jsonResponse(
        400,
        errorEnvelope("validation_error", "Некорректный адрес почты", {
          fields: [{ field: "", rule: "value_error", message: "Некорректный адрес почты" }],
        }),
      );

    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");
    await user.click(screen.getByLabelText("Действия: Пётр Ковалёв"));
    await user.click(await screen.findByRole("menuitem", { name: "Изменить имя и почту" }));
    const email = await screen.findByLabelText(/Почта/);
    await user.clear(email);
    await user.type(email, "petr.partner-lead-centre.ru");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent("Не сохранилось: Некорректный адрес почты");
  });

  it("поле «Отдел» не даёт набрать больше серверного предела", async () => {
    renderWithProviders(<TeamMembersTab />, { route: "/settings/team" });
    await screen.findByRole("table");

    // 100 — MAX_DEPARTMENT из app/schemas/users.py:33. Без ограничения на вводе
    // сто первый знак уезжал на сервер и возвращался общим отказом.
    expect(screen.getByLabelText("Отдел: Пётр Ковалёв")).toHaveAttribute("maxlength", "100");
  });
});
