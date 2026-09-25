import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { PermissionsBanner } from "@/app/PermissionsBanner";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ПУСТЫЕ ПРАВА ЧИТАЮТСЯ КАК ОТНЯТЫЙ ДОСТУП (SHELL-04, SCEN-01).
 *
 * Вход и старт вкладки кончаются запросом «кто я». Не ответил — в `catch`
 * ставится `permissions: []`, человек остаётся залогиненным, и оболочка
 * рисуется как у роли без доступа: из рельсы пропадают «Разбор диалогов» и
 * «Статистика», из настроек — вкладки, из диалога — кнопки. Сообщения нет
 * никакого, повторной попытки не было нигде.
 *
 * Руководитель объясняет это себе единственным доступным способом —
 * «администратор что-то со мной сделал», — и идёт разбираться. Починка была
 * одна: перезагрузить вкладку, о чём надо догадаться.
 *
 * Признак беды надёжен: ролей без прав сервер не знает — у самой урезанной,
 * `observer`, есть `conversations:read` (app/core/rbac.py). Значит «человек
 * есть, прав ноль» означает ровно одно.
 *
 * ВЫДЕРЖКА ПЕРЕД ПОЛОСОЙ проверяется отдельно и не для красоты: обычный вход
 * проходит через то же состояние (`applySession` кладёт пользователя раньше,
 * чем приходит ответ «кто я»), и без выдержки полоса мигала бы при каждом
 * входе — то есть учила бы на себя не смотреть.
 */

const ME = {
  ...fakeUser,
  permissions: ["conversations:read", "messages:send", "stats:own"],
};

function renderBanner() {
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <PermissionsBanner />
      </MantineProvider>
    </QueryClientProvider>,
  );
}

/** Ровно то, что оставляет за собой неудавшийся `GET /auth/me`. */
function sessionWithoutPermissions() {
  resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
}

/** Пережить выдержку, дав React доперерисоваться. */
async function waitOutGrace(extraMs = 100) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500 + extraMs);
  });
}

describe("Права не загрузились", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("сами добираем «кто я» и возвращаем права на место", async () => {
    // Адреса копим сами, а не читаем из `mock.calls`: без объявленного
    // параметра у мока пустой кортеж аргументов, и `c[0]` не типизируется —
    // а объявленный и неиспользованный параметр запрещает линтер.
    const urls: string[] = [];
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      urls.push(String(url));
      return jsonResponse(200, ME);
    });
    vi.stubGlobal("fetch", fetchMock);
    sessionWithoutPermissions();
    renderBanner();

    await waitOutGrace();

    // Главное — не текст полосы, а то, что интерфейс перестал врать про роль.
    expect(useSessionStore.getState().permissions).toEqual(ME.permissions);
    expect(urls.some((u) => u.endsWith("/auth/me"))).toBe(true);
    // Починились — полосе на экране делать нечего.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("пока не починились — говорим словами, а не показываем урезанный экран молча", async () => {
    // Запрос принят и молчит: полоса обязана быть видна уже во время попытки.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_res, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    sessionWithoutPermissions();
    renderBanner();

    await waitOutGrace();

    expect(screen.getByRole("alert")).toHaveTextContent(/Восстанавливаем ваши права/);
  });

  it("попытки кончились — прямо сказано, что доступ не отбирали, и есть «Повторить»", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(502, errorEnvelope("internal_error", "Плохой шлюз"))),
    );
    sessionWithoutPermissions();
    renderBanner();

    await waitOutGrace();
    // Повторы `queryClient` идут с нарастающими паузами — доводим их до конца.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    const banner = screen.getByRole("alert");
    // Первое объяснение, которое приходит в голову человеку с исчезнувшими
    // разделами, — «мне урезали доступ». Пока оно не опровергнуто прямо, всё
    // остальное читается как подтверждение.
    expect(banner).toHaveTextContent(/доступ у вас не отбирали/i);
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });

  it("обычный вход полосой не мигает: у неё есть выдержка", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, ME)),
    );
    sessionWithoutPermissions();
    renderBanner();

    // Столько живёт промежуток между «положили пользователя» и «пришли права»
    // при обычном входе. Полосы здесь быть не должно — иначе она появлялась бы
    // на каждом входе и её перестали бы читать.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    // И запроса тоже быть не должно: лишний «кто я» на каждый вход.
    expect(
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length,
      "лишний запрос «кто я» на каждом обычном входе",
    ).toBe(0);
  });

  it("у роли с правами полосы нет вовсе", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, ME)),
    );
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    renderBanner();

    await waitOutGrace();

    expect(screen.queryByRole("alert")).toBeNull();
  });
});
