import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, REFRESH_AHEAD_MS, refreshSession, запланироватьОбновление } from "@/shared/api/http";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ТОКЕН ОБНОВЛЯЕТСЯ ДО ИСТЕЧЕНИЯ, А НЕ ПО 401 (аудит 06.09).
 *
 * ЧТО БЫЛО. Токен доступа живёт 900 секунд и обновлялся ТОЛЬКО когда живой
 * запрос получал 401. `expires_in` при этом приходит в каждом ответе входа и
 * обновления (`types.ts:LoginResponse`), но его никто не читал. Замер: 1 440
 * ответов 401 в сутки — по одному на каждый истёкший токен каждой вкладки, —
 * и каждый стоил лишних два круга (0,27 с p50, до 1,4 с) ровно тому запросу,
 * на котором истёк: 0,4 % отправок и 0,74 % открытий диалога.
 *
 * ЧТО СТАЛО. После каждой выдачи токена ставится таймер на `expires_in − 60 с`;
 * срабатывает он только в видимой вкладке — у диспетчера их 11–13, и
 * обновлять токен в каждой значило бы тринадцать `POST /auth/refresh` каждые
 * четверть часа впустую на один офисный адрес. Путь по 401 остаётся запасным.
 *
 * ДИВЕРСИИ (все дали красный, всё восстановлено байт в байт):
 *  1. в `doRefresh` убрать `учестьВыдачуТокена(data)` — «за минуту до
 *     истечения» красный: второго обновления нет;
 *  2. в `запланироватьОбновление` снять проверку `visibilityState` — «скрытая
 *     вкладка» красный: обновление ушло из скрытой;
 *  3. в `request` убрать `if (options.auth === false) учестьВыдачуТокена(data)`
 *     — «после входа» красный: до первого 401 таймера нет.
 */

const ЖИЗНЬ_ТОКЕНА_МС = 900_000;

describe("Упреждающее обновление токена", () => {
  let обновлений: number;

  beforeEach(() => {
    vi.useFakeTimers();
    обновлений = 0;
    resetSessionStore({ accessToken: "old-token", bootstrapped: true });
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/refresh")) {
          обновлений += 1;
          return jsonResponse(200, {
            access_token: `token-${обновлений}`,
            token_type: "bearer",
            expires_in: 900,
            user: fakeUser,
          });
        }
        if (url.endsWith("/api/v1/auth/login")) {
          return jsonResponse(200, {
            access_token: "after-login",
            token_type: "bearer",
            expires_in: 300,
            user: fakeUser,
          });
        }
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => {
    запланироватьОбновление(null); // не оставлять таймер соседям
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("⚠ за минуту до истечения токен обновляется сам, без единого 401", async () => {
    expect(await refreshSession()).toBe("ok");
    expect(обновлений).toBe(1);

    await vi.advanceTimersByTimeAsync(ЖИЗНЬ_ТОКЕНА_МС - REFRESH_AHEAD_MS - 1);
    expect(обновлений, "обновились раньше срока — лишний запрос на офисный адрес").toBe(1);

    await vi.advanceTimersByTimeAsync(1);
    expect(обновлений, "срок подошёл, а обновления нет — следующий запрос получит 401").toBe(2);
    expect(useSessionStore.getState().accessToken).toBe("token-2");
  });

  it("каждое обновление ставит следующий таймер — цепочка не рвётся до конца смены", async () => {
    await refreshSession();
    await vi.advanceTimersByTimeAsync(ЖИЗНЬ_ТОКЕНА_МС - REFRESH_AHEAD_MS);
    await vi.advanceTimersByTimeAsync(ЖИЗНЬ_ТОКЕНА_МС - REFRESH_AHEAD_MS);
    expect(обновлений).toBe(3);
  });

  it("⚠ скрытая вкладка токен не обновляет: у диспетчера их тринадцать", async () => {
    await refreshSession();
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });

    await vi.advanceTimersByTimeAsync(ЖИЗНЬ_ТОКЕНА_МС);

    expect(обновлений, "скрытая вкладка сходила за токеном впустую").toBe(1);
  });

  it("после входа таймер стоит тоже — `expires_in` читается из ответа входа", async () => {
    await http.post("/auth/login", { email: "a@b", password: "x", remember: true }, { auth: false });
    useSessionStore.setState({ accessToken: "after-login" });

    await vi.advanceTimersByTimeAsync(300_000 - REFRESH_AHEAD_MS - 1);
    expect(обновлений).toBe(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(обновлений, "после входа первый токен доживает до 401, как раньше").toBe(1);
  });

  it("после выхода таймер молчит", async () => {
    await refreshSession();
    useSessionStore.getState().clear();

    await vi.advanceTimersByTimeAsync(ЖИЗНЬ_ТОКЕНА_МС);

    expect(обновлений, "вышедший человек продолжает обновлять токен").toBe(1);
  });

  it("без `expires_in` в ответе таймера нет — работает прежний путь по 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        обновлений += 1;
        return jsonResponse(200, { access_token: "t", token_type: "bearer", user: fakeUser });
      }),
    );
    await refreshSession();
    await vi.advanceTimersByTimeAsync(60 * 60_000);
    expect(обновлений).toBe(1);
  });

  /*
   * ДОБАВЛЕНО РЕВЬЮ 06.09 — диверсии, прошедшие мимо сторожей выше (обе дали
   * зелёный на сломанном коде, теперь красный):
   *  4. снять `if (через <= 0) return` — «короче минуты» красный: таймер с
   *     отрицательным сроком срабатывает сразу, и обновления идут подряд;
   *  5. не снимать прежний таймер при новой выдаче — «повторная выдача»
   *     красный: два таймера, два обновления.
   */

  it("токен короче минуты — таймера нет вовсе, а не обновление подряд", async () => {
    let выдач = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        обновлений += 1;
        выдач += 1;
        // Первый — тестовый токен на полминуты, дальше обычные: отрицательный
        // срок таймера означал бы обновление тут же и ещё раз следом.
        return jsonResponse(200, {
          access_token: `t-${выдач}`,
          token_type: "bearer",
          expires_in: выдач === 1 ? 30 : 900,
          user: fakeUser,
        });
      }),
    );
    await refreshSession();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(обновлений, "короткий токен обновился сам — таймер поставлен на отрицательный срок").toBe(1);
  });

  it("повторная выдача заменяет таймер, а не ставит второй рядом", async () => {
    await refreshSession(); // таймер на 840 с
    await vi.advanceTimersByTimeAsync(1_000);
    await refreshSession(); // путь по 401 обновил раньше срока: таймер теперь на 841 с
    expect(обновлений).toBe(2);

    await vi.advanceTimersByTimeAsync(ЖИЗНЬ_ТОКЕНА_МС - REFRESH_AHEAD_MS - 1_000);
    expect(обновлений, "прежний таймер пережил новую выдачу — лишний POST /auth/refresh").toBe(2);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(обновлений).toBe(3);
  });
});
