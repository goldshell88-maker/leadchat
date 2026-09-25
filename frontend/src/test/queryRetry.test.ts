import { describe, expect, it } from "vitest";
import { shouldRetryQuery } from "@/app/queryClient";
import { ApiError, TIMEOUT_ERROR } from "@/shared/api/http";

/**
 * СТОРОЖ ПРАВИЛА ПОВТОРОВ.
 *
 * Заведён 14 августа по замеру боя: открываем `/chats/not-a-uuid` и видим во
 * вкладке «Сеть» ТРИ одинаковых запроса с 400. Тройка стоила человеку тройного
 * ожидания перед сообщением об ошибке — и ни один повтор не мог помочь: «запрос
 * составлен не так» вторым разом составляется точно так же.
 *
 * Проверяется здесь именно ПРЕДИКАТ, а не экран: правило живёт одной строкой в
 * настройках TanStack Query, и его молчаливая потеря при следующей правке
 * настроек ничем бы себя не выдала — экран продолжил бы работать, просто снова
 * втрое дороже.
 */
const err = (status: number, code = "some_error") => new ApiError(status, code, "тест");

describe("Правило повторов запросов", () => {
  it("отказ «ты неправ» (4xx) не переспрашивается ни разу", () => {
    // 400 — тот самый кривой deep-link, с которого всё началось.
    expect(shouldRetryQuery(0, err(400, "validation_error"))).toBe(false);
    expect(shouldRetryQuery(0, err(401))).toBe(false);
    expect(shouldRetryQuery(0, err(403))).toBe(false);
    expect(shouldRetryQuery(0, err(404, "not_found"))).toBe(false);
    expect(shouldRetryQuery(0, err(410))).toBe(false);
  });

  it("лимит (429) тоже не долбим: у него свой срок в Retry-After", () => {
    // Сроки там не секундные — дневной лимит выгрузки статистики или окно
    // поддержки. Повтор через секунду это ровно то, от чего лимит и поставлен.
    expect(shouldRetryQuery(0, err(429, "rate_limited"))).toBe(false);
  });

  it("беда на стороне сервера (5xx) переспрашивается — но не бесконечно", () => {
    // Перезапуск контейнера на выкате — секунды: вторая попытка честно может
    // пройти там, где первая споткнулась.
    expect(shouldRetryQuery(0, err(500))).toBe(true);
    expect(shouldRetryQuery(1, err(503))).toBe(true);
    // Третья попытка — уже перебор: столько же, сколько было до правки.
    expect(shouldRetryQuery(2, err(503))).toBe(false);
  });

  it("молчащий сервер не переспрашивается (ACC-06) — правило не потеряно", () => {
    expect(shouldRetryQuery(0, new ApiError(0, TIMEOUT_ERROR, "таймаут"))).toBe(false);
  });

  it("обрыв сети — не ApiError вовсе, и он переспрашивается", () => {
    expect(shouldRetryQuery(0, new TypeError("Failed to fetch"))).toBe(true);
  });
});
