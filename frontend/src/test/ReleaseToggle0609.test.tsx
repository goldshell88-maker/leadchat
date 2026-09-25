import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { DistributionTab } from "@/features/settings/distribution/DistributionTab";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Выключатель сторожа «освободить диалоги того, кого нет за столом» (06.09).
 *
 * Просьба владельца дословно: «у нас есть функция, которая передаёт диалоги,
 * когда сотрудник не активен, нужно сделать её выключаемой». Тумблер живёт на
 * вкладке «Распределение» и едет той же ручкой `/settings/distribution`, той же
 * кнопкой «Сохранить распределение».
 *
 * Проверяется то, что ломается тише всего: что тумблер вообще нарисован и
 * показывает сохранённое, что выключение доезжает до сервера ИМЕННО ЭТИМ полем
 * и по кнопке, а не по касанию, что тумблер не гаснет вместе с раздачей, и что
 * ответ старого бэкенда без поля не превращается в «правку».
 *
 * ⚠ ДИВЕРСИИ: убрать `<Switch label="Освобождать диалоги…">` из DistributionTab
 * — краснеют все тесты на первом `findByRole`; убрать `release_unavailable` из
 * тела `save.mutate` — краснеет «выключение уезжает тем же PATCH».
 */

const LABEL = /Освобождать диалоги сотрудника/;
const SAVE = "Сохранить распределение";

interface State {
  enabled: boolean;
  max_active: number | null;
  release_unavailable?: boolean;
}

describe("Выключатель освобождения диалогов недоступного", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let state: State = { enabled: false, max_active: 5, release_unavailable: true };
  let hours = { start_hour: 8, end_hour: 22 };

  const patches = () =>
    fetchMock.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "PATCH")
      .map((c) => JSON.parse(String((c[1] as RequestInit).body)));

  beforeEach(() => {
    queryClient.clear();
    state = { enabled: false, max_active: 5, release_unavailable: true };
    hours = { start_hour: 8, end_hour: 22 };
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["settings:manage"],
      accessToken: "t",
      bootstrapped: true,
    });

    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/settings/distribution")) {
        if (init?.method === "PATCH") {
          const body = JSON.parse(String(init.body));
          if (body.enabled !== undefined) state.enabled = body.enabled;
          if (body.release_unavailable !== undefined) {
            state.release_unavailable = body.release_unavailable;
          }
          if (body.max_active_unlimited) state.max_active = null;
          else if (body.max_active !== undefined) state.max_active = body.max_active;
          return jsonResponse(200, state);
        }
        return jsonResponse(200, state);
      }
      if (url.pathname.endsWith("/settings/work-hours")) {
        if (init?.method === "PATCH") {
          hours = JSON.parse(String(init.body));
          return jsonResponse(200, hours);
        }
        return jsonResponse(200, hours);
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const render = () => renderWithProviders(<DistributionTab />, { route: "/settings/distribution" });

  it("тумблер нарисован, показывает сохранённое и не гаснет вместе с раздачей", async () => {
    render();
    const toggle = await screen.findByRole("switch", { name: LABEL });
    expect(toggle).toBeChecked();
    // Раздача выключена (`state.enabled === false`), а сторож от неё не
    // зависит: диалоги, взятые руками, он освобождает так же.
    expect(toggle).toBeEnabled();
    // Ничего не меняли — сохранять нечего.
    expect(screen.getByRole("button", { name: SAVE })).toBeDisabled();
  });

  it("сохранённое «выключено» показано выключенным", async () => {
    state.release_unavailable = false;
    render();
    expect(await screen.findByRole("switch", { name: LABEL })).not.toBeChecked();
    expect(screen.getByRole("button", { name: SAVE })).toBeDisabled();
  });

  it("выключение уезжает тем же PATCH, по кнопке, а не по касанию", async () => {
    render();
    const toggle = await screen.findByRole("switch", { name: LABEL });
    await userEvent.click(toggle);

    // Сторож меняет судьбу диалогов всей смены; «случайно задел» стоит дороже
    // лишнего клика — тумблер сам ничего не шлёт.
    expect(patches()).toHaveLength(0);
    expect(screen.getByText(/освобождение выключается/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: SAVE }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0].release_unavailable).toBe(false);
    // Сосед не тронут: раздача как была выключена, так и осталась.
    expect(patches()[0].enabled).toBe(false);

    // Ответ сервера прижился: тумблер выключен, сохранять снова нечего.
    await waitFor(() => expect(screen.getByRole("button", { name: SAVE })).toBeDisabled());
    expect(screen.getByRole("switch", { name: LABEL })).not.toBeChecked();
  });

  it("подпись объясняет, что именно выключается, и следует за состоянием", async () => {
    render();
    const toggle = await screen.findByRole("switch", { name: LABEL });
    expect(screen.getByText(/вернётся во «Входящие»/)).toBeInTheDocument();

    await userEvent.click(toggle);
    // Статичная подпись после переключения утверждала бы обратное, а читают
    // её как состояние системы.
    expect(screen.queryByText(/вернётся во «Входящие»/)).toBeNull();
    expect(screen.getByText(/остаются за сотрудником/)).toBeInTheDocument();
  });

  it("ответ без поля читается как «включено», а не как правка", async () => {
    // Бэкенд старше выключателя поля не шлёт; до выключателя сторож работал
    // всегда, и тумблер обязан показывать это, не зажигая кнопку.
    delete state.release_unavailable;
    render();
    expect(await screen.findByRole("switch", { name: LABEL })).toBeChecked();
    expect(screen.getByRole("button", { name: SAVE })).toBeDisabled();
  });
});
