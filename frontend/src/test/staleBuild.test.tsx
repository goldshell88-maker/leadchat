import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppLayout } from "@/app/AppLayout";
import { queryClient } from "@/app/queryClient";
import { UpdateBanner } from "@/platform/UpdateBanner";
import { useUpdateStore } from "@/platform/updateStore";
import type { Permission } from "@/shared/auth/usePermissions";
import { pageActions } from "@/shared/lib/pageActions";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ВЫКАТКА НЕ ЛОМАЕТ ОТКРЫТУЮ ВКЛАДКУ МОЛЧА (SHELL-02).
 *
 * После выкатки старые чанки исчезают с сервера (`immutable` + `try_files
 * $uri =404`), и вкладка, открытая до неё, падает на первом же переходе в
 * ленивый раздел. Экран отказа это ловит, но постфактум. Здесь проверяется
 * то, что должно случиться РАНЬШЕ поломки: вкладка сама замечает чужую версию
 * в `/api/health` и предлагает обновиться — строкой и кнопкой, а не
 * перезагрузкой исподтишка.
 */

const CLEAN_STORE = {
  available: null,
  checking: false,
  applying: false,
  error: null,
  dismissedVersion: null,
  dismissedAt: null,
  serverBuild: null,
};

/**
 * Свежий экземпляр модуля с подставленным `VITE_APP_VERSION`. Иначе никак:
 * vite инлайнит переменную на этапе сборки, а в тестовой сборке её нет вовсе
 * — без подмены проверялась бы только ветка «версии не знаем», и любая
 * проверка про расхождение версий была бы зелёной сама по себе.
 *
 * Стор берётся из ТОГО ЖЕ прогона импортов: `vi.resetModules` делает новый
 * экземпляр каждому модулю, и статически импортированный наверху файла
 * `useUpdateStore` — уже не тот, в который пишет свежий `buildVersion`.
 */
async function withBuildVersion(version: string) {
  vi.stubEnv("VITE_APP_VERSION", version);
  vi.resetModules();
  const [build, store] = await Promise.all([
    import("@/platform/buildVersion"),
    import("@/platform/updateStore"),
  ]);
  store.useUpdateStore.setState(CLEAN_STORE);
  return { ...build, store: store.useUpdateStore };
}

describe("Вкладка на старой сборке (SHELL-02)", () => {
  beforeEach(() => {
    useUpdateStore.setState(CLEAN_STORE);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.resetModules();
  });

  describe("сравнение версии бандла с версией из /api/health", () => {
    it("чужая версия на сервере — вкладка устарела, своя — нет", async () => {
      const { isStaleTab } = await withBuildVersion("a1b2c3d");
      expect(isStaleTab("9f8e7d6")).toBe(true);
      expect(isStaleTab("a1b2c3d")).toBe(false);
    });

    it("сборка без тега (локальный `npm run dev`) молчит", async () => {
      const { isStaleTab } = await withBuildVersion("");
      expect(isStaleTab("9f8e7d6")).toBe(false);
    });

    it("заглушка «dev» — не версия, ни с одной стороны", async () => {
      // Умолчание `ARG VITE_APP_VERSION="dev"` в Dockerfile.web и
      // `APP_VERSION=dev` в .env.example. Баннер от такой пары не гасится
      // перезагрузкой: после неё версия бандла та же самая.
      const { isStaleTab } = await withBuildVersion("dev");
      expect(isStaleTab("9f8e7d6")).toBe(false);

      const real = await withBuildVersion("a1b2c3d");
      expect(real.isStaleTab("dev")).toBe(false);
    });

    it("слово «prod» — не версия: откатный контейнер не вешает вечную полосу", async () => {
      /*
       * ⚠ БОЕВОЙ СЛУЧАЙ 30.08. Откат выкатки поднимал контейнеры без
       * APP_VERSION, /api/health отвечал словом «prod», и полоса «Вышло
       * обновление» горела у всех вечно: перезагрузка приносила то же слово.
       * Откат починен отдельно (ship.sh передаёт номер возвращаемой сборки),
       * но фронт обязан быть терпим к серверу, не знающему своей версии.
       */
      const { isStaleTab } = await withBuildVersion("a1b2c3d");
      expect(isStaleTab("prod"), "полоса снова навечно после отката").toBe(false);
    });

    it("нет ответа о версии — состояние не меняется", async () => {
      const { isStaleTab } = await withBuildVersion("a1b2c3d");
      expect(isStaleTab(null)).toBe(false);
      expect(isStaleTab(undefined)).toBe(false);
      expect(isStaleTab("  ")).toBe(false);
    });
  });

  describe("состояние полосы", () => {
    it("расхождение поднимает полосу", async () => {
      const { noticeServerVersion, store } = await withBuildVersion("a1b2c3d");

      noticeServerVersion("9f8e7d6");
      expect(store.getState().serverBuild).toBe("9f8e7d6");
    });

    it("возврат к версии этой вкладки — гасит", async () => {
      // Откат выкатки (deploy.sh умеет вернуть предыдущий тег): версии снова
      // сошлись — предлагать перезагрузку ради той же сборки нечестно, кнопка
      // после неё привела бы к той же самой вкладке и той же самой полосе.
      const { noticeServerVersion, store } = await withBuildVersion("a1b2c3d");
      noticeServerVersion("9f8e7d6");

      noticeServerVersion("a1b2c3d");
      expect(store.getState().serverBuild).toBeNull();
    });

    it("пропавший ответ о версии полосу не гасит", async () => {
      // Выкатка перезапускает api: `/api/health` несколько секунд отвечает
      // отказом. «Не знаем версию — снимем полосу» погасило бы её ровно тогда,
      // когда она только что стала правдой.
      const { noticeServerVersion, store } = await withBuildVersion("a1b2c3d");
      noticeServerVersion("9f8e7d6");

      noticeServerVersion(null);
      expect(store.getState().serverBuild).toBe("9f8e7d6");
    });
  });

  describe("полоса «Вышло обновление»", () => {
    it("на свежей вкладке полосы нет", () => {
      renderWithProviders(<UpdateBanner />);
      expect(screen.queryByRole("status", { name: "Вышло обновление" })).toBeNull();
    });

    it("показывает строку, обещание про черновики и кнопку «Обновить»", () => {
      useUpdateStore.getState().announceBuild("9f8e7d6");
      renderWithProviders(<UpdateBanner />);

      expect(screen.getByRole("status", { name: "Вышло обновление" })).toBeInTheDocument();
      expect(screen.getByText("Вышло обновление")).toBeInTheDocument();
      expect(screen.getByText("Набранные ответы сохранятся")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Обновить" })).toBeInTheDocument();
    });

    it("«Позже» здесь нет: откладывать нечего — вкладка уже на мёртвом коде", () => {
      useUpdateStore.getState().announceBuild("9f8e7d6");
      renderWithProviders(<UpdateBanner />);
      expect(screen.queryByRole("button", { name: "Позже" })).toBeNull();
    });

    it("перезагружает вкладку ТОЛЬКО по нажатию", async () => {
      const reload = vi.spyOn(pageActions, "reload").mockImplementation(() => {});
      useUpdateStore.getState().announceBuild("9f8e7d6");

      const user = userEvent.setup();
      renderWithProviders(<UpdateBanner />);

      // Сама по себе полоса вкладку не трогает: оператор может дописывать
      // ответ клиенту, и выдернуть у него страницу — ровно то, от чего лечимся.
      expect(reload).not.toHaveBeenCalled();

      await user.click(screen.getByRole("button", { name: "Обновить" }));
      expect(reload).toHaveBeenCalledTimes(1);
    });
  });

  describe("версия приезжает из уже существующего опроса", () => {
    beforeEach(() => {
      queryClient.clear();
      resetSessionStore({
        user: fakeUser,
        permissions: fakeMe.permissions as Permission[],
        accessToken: "t",
        bootstrapped: true,
      });
    });

    it("шапка отдаёт версию из /api/health в проверку сборки", async () => {
      // Второго таймера и второй ручки нет: `/api/health` шапка опрашивает
      // раз в 30 с ради точки состояния связи, версия едет в том же ответе.
      const seen: Array<string | null> = [];
      useUpdateStore.setState({ announceBuild: (v) => void seen.push(v) });

      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const url = String(input);
          if (url.includes("/api/health")) {
            return jsonResponse(200, { status: "ok", db: true, redis: true, version: "9f8e7d6" });
          }
          return jsonResponse(200, { items: [], page: { next_cursor: null } });
        }),
      );

      renderWithProviders(<AppLayout />);

      await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    });
  });
});
