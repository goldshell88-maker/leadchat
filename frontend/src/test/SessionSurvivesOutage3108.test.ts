import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, NETWORK_ERROR, http } from "@/shared/api/http";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ОБРЫВ СВЯЗИ НЕ ВЫГОНЯЕТ ЧЕЛОВЕКА ИЗ РАБОТЫ.
 *
 * ⚠ НАХОДКА ОБХОДА ЭКРАНОВ 31.08. Обновление токена доступа возвращало
 * `boolean`, и все пять мест читали `false` одинаково: «сессия мертва — гасим и
 * отправляем на вход». Но `false` возвращался ЛЮБОЙ бедой: оборванным туннелем
 * (а туннель здесь рвётся регулярно), потолком ожидания, 502 от nginx во время
 * нашей же выкатки.
 *
 * То есть моргнувшая на секунду сеть выбрасывала диспетчера из работающего
 * приложения ВМЕСТЕ С НАБРАННЫМ ОТВЕТОМ КЛИЕНТУ, а экран входа объяснял это
 * «долгим перерывом или отключением учётной записи» — ни того ни другого не
 * было. Для человека, которого просили «никогда не обновлять страницу», это
 * хуже F5: там он хотя бы остаётся внутри.
 *
 * Разница между «нам отказали» и «мы не дозвонились» есть на проводе: отзыв
 * refresh-cookie сервер сообщает кодом 401/403, всё остальное — молчание,
 * таймаут или пятисотка. Первое необратимо, второе проходит само.
 */
describe("Сессия переживает обрыв связи", () => {
  beforeEach(() => {
    resetSessionStore({ accessToken: "old-token", bootstrapped: true });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** 401 на рабочем запросе, а на обновлении токена — беда, заданная снаружи. */
  function серверСБедойНаОбновлении(беда: () => Promise<Response>) {
    const вызовов = { обновлений: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/refresh")) {
          вызовов.обновлений += 1;
          return беда();
        }
        return jsonResponse(401, errorEnvelope("unauthorized", "Токен просрочен"));
      }),
    );
    return вызовов;
  }

  it("сеть отвалилась — сессия ОСТАЁТСЯ, человек продолжает работать", async () => {
    const вызовов = серверСБедойНаОбновлении(() => Promise.reject(new TypeError("Failed to fetch")));

    const err = await http.get("/conversations").catch((e: unknown) => e);

    expect(вызовов.обновлений).toBeGreaterThan(0);
    expect(
      useSessionStore.getState().accessToken,
      "сессию погасили из-за обрыва связи — диспетчера выкинуло из работы вместе с набранным ответом",
    ).toBe("old-token");
    expect(err).toBeInstanceOf(ApiError);
    expect(
      (err as ApiError).code,
      "беда связи выдана как ошибка входа — человек ищет причину не там",
    ).toBe(NETWORK_ERROR);
  });

  it("сервер перезапускается (502) — сессия ОСТАЁТСЯ", async () => {
    /*
     * Это ровно то, что происходит на каждой нашей выкатке: nginx на несколько
     * секунд отвечает 502. Выгонять из-за этого всю смену — значит наказывать
     * людей за наш же перезапуск.
     */
    серверСБедойНаОбновлении(async () =>
      jsonResponse(502, errorEnvelope("bad_gateway", "Сервер перезапускается")),
    );

    await http.get("/conversations").catch(() => undefined);

    expect(
      useSessionStore.getState().accessToken,
      "перезапуск сервера погасил сессию — выкатка выкидывает всю смену",
    ).toBe("old-token");
  });

  it("сервер ОТКАЗАЛ (401) — сессию гасим, это настоящий отзыв", async () => {
    /*
     * Обратная сторона: смягчив разбор, легко проглядеть настоящий отзыв и
     * оставить работать того, кого администратор только что отключил.
     */
    серверСБедойНаОбновлении(async () =>
      jsonResponse(401, errorEnvelope("unauthorized", "Refresh отозван")),
    );

    await http.get("/conversations").catch(() => undefined);

    expect(
      useSessionStore.getState().accessToken,
      "настоящий отзыв не погасил сессию — отключённый сотрудник продолжает работать",
    ).toBeNull();
  });

  it("вход при мёртвой связи не выдаёт человека за вошедшего", async () => {
    /*
     * ⚠ ЭТА ЛОВУШКА СРАБОТАЛА ПРИ ПРАВКЕ. В `bootstrap` стояло `if (ok)`, где
     * `ok` стал строкой, — а строка истинна всегда, включая «нет-связи» и
     * «отозван». Типы такую подмену пропускают молча.
     */
    const запросы: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        запросы.push(String(input));
        return Promise.reject(new TypeError("Failed to fetch"));
      }),
    );
    resetSessionStore({ accessToken: null, bootstrapped: false });

    await useSessionStore.getState().bootstrap();

    /*
     * ⚠ ПРИЗНАК ВЫБРАН НЕ ПЕРВЫЙ ПОПАВШИЙСЯ. Сначала здесь проверялось, что
     * `user` пуст, — и диверсия (`if (итог)` вместо сравнения) эту проверку
     * НЕ уронила: профиль всё равно не загрузится, потому что связи нет, и
     * `user` остаётся пустым в обоих случаях. Проверка зеленела по неверной
     * причине.
     *
     * Отличает эти два мира ровно одно: пошли ли мы за профилем вообще. Вход в
     * ветку означает «человек считается вошедшим» — и вот это видно.
     */
    expect(
      запросы.some((u) => u.includes("/auth/me")),
      "при мёртвом обновлении токена приложение всё равно пошло за профилем — человек считается вошедшим",
    ).toBe(false);
    expect(
      useSessionStore.getState().bootstrapped,
      "загрузка не завершилась — на экране навсегда останется скелет",
    ).toBe(true);
  });

  it("обновление удалось — запрос повторяется с новым токеном", async () => {
    /*
     * Проверка «а не сломали ли основное»: три исхода вместо двух легко
     * развернуть так, что удача перестанет считаться удачей.
     *
     * ⚠ ТОКЕНЫ ЗДЕСЬ ЛАТИНИЦЕЙ НАМЕРЕННО. Кириллица в значении заголовка
     * роняет `new Headers()` прямо в подменённом `fetch`, и падение приходит
     * в код как «сервер недоступен» — то есть тест краснеет, обвиняя правку,
     * которая ни при чём. Полчаса на это уже потрачено.
     */
    let повторов = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/refresh")) {
          return jsonResponse(200, {
            access_token: "new-token",
            token_type: "bearer",
            expires_in: 900,
            user: fakeUser,
          });
        }
        повторов += 1;
        if (new Headers(init?.headers).get("Authorization") === "Bearer new-token") {
          return jsonResponse(200, { items: [], page: 1 });
        }
        return jsonResponse(401, errorEnvelope("unauthorized", "Токен просрочен"));
      }),
    );

    await expect(http.get("/conversations")).resolves.toEqual({ items: [], page: 1 });
    expect(повторов).toBe(2);
    expect(useSessionStore.getState().accessToken).toBe("new-token");
  });
});
