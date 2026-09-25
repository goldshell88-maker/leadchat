import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { TablePage } from "@/features/table/TablePage";
import {
  ApiError,
  NETWORK_ERROR,
  REQUEST_TIMEOUT_MS,
  TIMEOUT_ERROR,
  TRANSFER_TIMEOUT_MS,
  http,
  requestForm,
} from "@/shared/api/http";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ЗАВИСШИЙ СЕРВЕР ОБЯЗАН КОНЧАТЬСЯ СЛОВАМИ, А НЕ ВЕЧНОЙ ЗАГРУЗКОЙ
 * (ACC-06, DIALOGS-13).
 *
 * Оборванную сеть браузер отбивает сам. А случай «соединение приняли и
 * замолчали» он не отбивает НИКОГДА: промис `fetch` просто не выполняется.
 * Пока у запросов не было потолка ожидания, это давало вечный спиннер на
 * «Разборе диалогов», вечные скелетоны в настройках и — хуже всего — вечный
 * скелет оболочки на старте вкладки, потому что первым запросом идёт
 * `POST /auth/refresh`, а до его ответа `bootstrapped` остаётся false.
 *
 * Отличить это от «медленно грузится» человеку нечем, поэтому он ждёт. На
 * боевой системе оба повода штатные: выкат перезапускает контейнер `api`, а
 * таблица разбора считает метрики по 454 тысячам диалогов.
 *
 * ВРЕМЯ ЗДЕСЬ ПОДДЕЛЬНОЕ. Ждать двадцать секунд по-настоящему в прогоне
 * нельзя, а трогать сам потолок ради теста — значит проверять не то число,
 * которое поедет на прод. Поэтому таймеры фальшивые, а константы берутся из
 * модуля, а не переписаны цифрами.
 */

/** `fetch`, который принял запрос и молчит, пока его не отменят. */
function hangingFetch() {
  return vi.fn(
    (_url: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      }),
  );
}

describe("Потолок ожидания у запроса", () => {
  beforeEach(() => {
    resetSessionStore({ accessToken: "t", bootstrapped: true });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("молчащий сервер кончается отказом, а не бесконечным ожиданием", async () => {
    vi.stubGlobal("fetch", hangingFetch());

    const pending = http.get("/conversations/table").catch((e: unknown) => e);

    // За секунду до потолка ответа всё ещё нет — потолок не срабатывает раньше
    // времени и не режет честные медленные ответы.
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS - 1000);
    let settled = false;
    void pending.then(() => (settled = true));
    await Promise.resolve();
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(1000);
    const err = await pending;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe(TIMEOUT_ERROR);
  });

  it("про молчащий сервер говорится не то же, что про оборванный интернет", async () => {
    vi.stubGlobal("fetch", hangingFetch());

    const pending = http.get("/whatever").catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    const err = (await pending) as ApiError;

    // Код отдельный: «Проверьте соединение» отправило бы человека чинить
    // интернет, с которым всё в порядке, — и не починило бы ничего.
    expect(err.code).not.toBe(NETWORK_ERROR);
    expect(err.message).toMatch(/Сервер не ответил/);
    expect(err.message).not.toMatch(/соединени|интернет/i);
  });

  it("ответ вовремя проходит целым, а таймер за собой убирается", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { ok: true })),
    );

    await expect(http.get<{ ok: boolean }>("/fast")).resolves.toEqual({ ok: true });
    // Забытый таймер держал бы вкладку живой на каждый запрос: сорок открытий
    // диалогов за смену — сорок висящих таймеров по двадцать секунд.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("отмена вызывающего не выдаётся за отказ сервера", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const ctl = new AbortController();

    const pending = http.get("/leaving", { signal: ctl.signal }).catch((e: unknown) => e);
    ctl.abort();
    const err = await pending;

    // Человек ушёл с экрана — ответ ему больше не нужен, и говорить не о чем.
    // Спутать это с таймаутом значит показывать красную плашку на каждый уход.
    expect(err).toBeInstanceOf(DOMException);
    expect((err as DOMException).name).toBe("AbortError");
  });

  it("у файлов потолок свой — иначе фото клиенту рвалось бы на середине", async () => {
    vi.stubGlobal("fetch", hangingFetch());

    const pending = requestForm("/media", new FormData()).catch((e: unknown) => e);

    // Общий потолок здесь не годится: снимок с телефона диспетчера по
    // мобильному каналу честно идёт дольше двадцати секунд.
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1000);
    let settled = false;
    void pending.then(() => (settled = true));
    await Promise.resolve();
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(TRANSFER_TIMEOUT_MS - REQUEST_TIMEOUT_MS);
    expect(((await pending) as ApiError).code).toBe(TIMEOUT_ERROR);
  });
});

describe("Таймаут не переспрашивают", () => {
  it("три попытки по двадцать секунд — это минута скелетона вместо вечности", () => {
    const retry = queryClient.getDefaultOptions().queries?.retry;
    expect(typeof retry).toBe("function");
    const decide = retry as (n: number, e: unknown) => boolean;

    expect(decide(0, new ApiError(0, TIMEOUT_ERROR, "Сервер не ответил за 20 с"))).toBe(false);
    // Остальные отказы переспрашиваются как раньше: правило сужает поведение
    // ровно в одной точке, а не отменяет повторы вообще.
    expect(decide(0, new ApiError(500, "internal_error", "Ошибка"))).toBe(true);
    expect(decide(1, new ApiError(500, "internal_error", "Ошибка"))).toBe(true);
    expect(decide(2, new ApiError(500, "internal_error", "Ошибка"))).toBe(false);
  });
});

describe("Разбор диалогов на молчащем сервере", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: { ...fakeUser, role: "head" },
      permissions: ["conversations:read", "stats:all", "accounts:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("вместо вечного спиннера — слова и кнопка «Повторить»", async () => {
    vi.stubGlobal("fetch", hangingFetch());

    renderWithProviders(<TablePage />, { route: "/dialogs" });
    // Ровно то, что видел человек до правки, и видел бы до скончания смены.
    expect(document.querySelector(".dt__state")).toBeTruthy();

    /*
     * Время двигаем внутри `act`, а не ждём через `findByText`: ожидания
     * RTL сами крутят таймеры, и с подделанным временем это кончается
     * взаимной блокировкой — тест висит до собственного потолка в пять
     * секунд. Здесь же шаг ровно один: доехали до потолка, дали React
     * доперерисоваться, смотрим.
     */
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1000);
    });

    expect(screen.getByText("Не получилось загрузить")).toBeInTheDocument();
    expect(screen.getByText(/Сервер не ответил/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });
});
