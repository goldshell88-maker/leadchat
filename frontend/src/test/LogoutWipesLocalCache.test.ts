// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { fakeUser } from "./helpers";

/**
 * ВЫХОД СТИРАЕТ ЛОКАЛЬНЫЙ КЭШ И ОЧЕРЕДЬ ОТПРАВКИ.
 *
 * ⚠ ЧУЖОЕ СООБЩЕНИЕ УХОДИЛО ПОД ЧУЖИМ ИМЕНЕМ. Команда `cache_clear` в Rust была
 * написана, зарегистрирована в `lib.rs` и снабжена комментарием: «очередь
 * принадлежит сессии, и оставлять чужие неотправленные сообщения следующему
 * пользователю на этой машине нельзя». Вызывающих у неё не было НИ ОДНОГО.
 *
 * Выход чистил только токен в памяти процесса, база SQLite оставалась
 * нетронутой, а фоновый флаш раз в 30 секунд берёт ТЕКУЩИЙ токен. Диспетчер A
 * на общем компьютере смены пишет клиенту при оборванной сети — строка ложится
 * в очередь. A выходит и уходит домой. Диспетчер B входит на том же профиле — и
 * через полминуты сообщение A уезжает клиенту под именем B: в ленте, в журнале
 * и в правиле «кто ответил, тот и ведёт» это сообщение B, а он его не писал и
 * нигде не видит. Вместе с очередью на диске оставался и кэш диалогов A — чужая
 * переписка на чужом экране.
 *
 * ЧТО ЛОМАЛИ: убрали `wipeLocalCache()` из `clear()` — краснеет первый тест;
 * вернули заглушку вместо `invokeSafe("cache_clear")` в мосте — краснеет
 * проводка.
 */

describe("Выход из аккаунта", () => {
  beforeEach(() => {
    useSessionStore.setState({
      user: fakeUser,
      permissions: [],
      hotkeys: {},
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    leaveTauriRuntime();
    vi.restoreAllMocks();
  });

  it("чистит локальный кэш и очередь отправки", () => {
    const { bridge, calls } = makeFakeTauriBridge();
    enterTauriRuntime(bridge);

    useSessionStore.getState().clear();

    expect(calls.clearCache, "очередь предыдущего человека осталась на диске").toHaveBeenCalled();
  });

  it("то же и при принудительном отзыве сессии, а не только по кнопке", () => {
    /*
     * Точка одна — `clear()`. Она зовётся и из «Выход», и из `refresh()`, когда
     * сервер сессию не продлил: администратор отозвал доступ, а на машине
     * осталась чужая очередь. Проверяем по исходнику, что вызов стоит именно
     * там, а не в обработчике кнопки: иначе следующий путь выхода его потеряет.
     */
    const src = (readFileSync("src/shared/stores/sessionStore.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    const начало = src.indexOf("clear: () =>");
    expect(начало, "метод clear исчез").toBeGreaterThan(-1);
    const тело = src.slice(начало, начало + 320);
    expect(тело, "очистка кэша ушла из общей точки выхода").toContain("wipeLocalCache()");
  });

  it("в браузере чистить нечего — заглушка на месте", () => {
    const src = readFileSync("src/platform/web.ts", "utf-8") as string;
    expect(src, "веб-мост потерял метод clear и упадёт при выходе").toMatch(/async clear\(\)/);
  });

  it("ПРОВОДКА: десктопный мост зовёт именно cache_clear", () => {
    /*
     * Без этой проверки остальные зеленеют впустую: мост можно оставить пустым
     * методом, и все тесты выше по-прежнему пройдут — дефект вернётся целиком.
     */
    const src = (readFileSync("src/platform/tauri/offline.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    const начало = src.indexOf("async clear()");
    expect(начало, "метод clear исчез из десктопного моста").toBeGreaterThan(-1);
    expect(src.slice(начало, начало + 200)).toContain('"cache_clear"');
  });
});
