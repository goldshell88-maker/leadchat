// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а ставить @types/node ради одного сторожа несоразмерно. Так же сделано в
// railChrome.test.ts, breakpoints.test.ts и cssDeadClasses.test.ts.
import { readdirSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * СТОРОЖ ОДНОГО СОЕДИНЕНИЯ.
 *
 * ЧТО ОХРАНЯЕТСЯ. Живая лента обязана питаться тем же сокетом, что и всё
 * рабочее место, — через `applyWsEvent`. Соблазн завести ей своё соединение
 * велик и выглядит безобидно: экран отдельный, кадры нужны свои, подписка
 * своя. Цена — четыре вещи сразу: второй тикет на каждый реконнект, второй
 * heartbeat, вторая очередь backoff (пятьдесят вкладок ломятся в api уже
 * дважды) и, главное, ВТОРОЙ НАБОР КАДРОВ, который придётся заново
 * фильтровать по правам и по каналам.
 *
 * Последнее и есть настоящая причина сторожа. Сегодня лента чужой очереди не
 * покажет не потому, что мы её прячем, а потому, что кадров с ней у вкладки
 * нет: хаб сузил их по `eligible_operator_ids` (app/ws/hub.py, 7.2), не отдал
 * заметки ролям без `notes:read` и не отдал `account:needs_reauth` никому,
 * кроме администратора. Своё соединение потребовало бы повторить всю эту
 * матрицу здесь — второй копией, которая однажды разойдётся с первой и
 * покажет оператору «Парт - 7» чужую очередь.
 *
 * ПОЧЕМУ СТОРОЖ ЧИТАЕТ ИСХОДНИКИ. Проверить «сокет ровно один» в jsdom нечем:
 * WebSocket там не поднимается, а мок доказал бы только то, что мы его
 * позвали. Здесь проверяется единственное проверяемое машиной — что в коде
 * ленты нет ни своего сокета, ни клиента к нему, а точка съёма стоит там, где
 * ей и место.
 */

/** Пути относительные: vitest запускается из каталога frontend. */
const FEED_DIR = "src/features/feed";

function feedSources(): Array<{ name: string; text: string }> {
  return (readdirSync(FEED_DIR) as string[])
    .filter((f: string) => f.endsWith(".ts") || f.endsWith(".tsx"))
    .map((f: string) => ({ name: f, text: readFileSync(`${FEED_DIR}/${f}`, "utf-8") as string }));
}

describe("Живая лента слушает существующий сокет, а не заводит свой", () => {
  it("в коде ленты нет ни WebSocket, ни WsClient, ни тикета", () => {
    const files = feedSources();
    // Пустой список означал бы, что сторож ничего не читает и зелен всегда, —
    // худший из отказов, незаметный.
    expect(files.length, "исходники ленты не найдены").toBeGreaterThan(0);

    for (const { name, text } of files) {
      expect(text, `${name}: лента подняла собственный сокет`).not.toMatch(/new WebSocket/);
      expect(text, `${name}: лента завела свой WsClient`).not.toContain("WsClient");
      expect(text, `${name}: лента пошла за своим тикетом`).not.toContain("/ws/ticket");
    }
  });

  it("точка съёма стоит в общем диспетчере кадров", () => {
    const dispatcher = readFileSync("src/shared/realtime/applyWsEvent.ts", "utf-8") as string;
    expect(dispatcher).toContain("recordWsFrame(e)");
    /*
     * ДО `switch`, А НЕ В ЕГО ВЕТКАХ. Лента обязана видеть и те кадры, на
     * которые рабочее место не реагирует вовсе (`presence:online`): иначе
     * «что происходит в системе» молчало бы ровно про людей. Сравниваем места
     * в файле — это единственный способ поймать переезд вызова внутрь ветки.
     */
    expect(dispatcher.indexOf("recordWsFrame(e)")).toBeLessThan(dispatcher.indexOf("switch (e.type)"));
  });

  it("лента гаснет при выходе вместе с очередью и непрочитанным", () => {
    // На одном компьютере работают по очереди (11 §2.5), а в ленте лежат имена
    // клиентов и то, кто из сотрудников отошёл. Уборка обязана стоять там же,
    // где уборка остальных счётчиков, — иначе её однажды забудут перенести.
    const lifecycle = readFileSync("src/shared/realtime/realtime.ts", "utf-8") as string;
    const stop = lifecycle.slice(lifecycle.indexOf("export function stopRealtime"));
    expect(stop).toContain("clearFeed()");
  });

  /**
   * СЛЕЖЕНИЕ ЗА ОБРЫВАМИ ВКЛЮЧАЕТСЯ ВМЕСТЕ С СОКЕТОМ, А НЕ ВМЕСТЕ С ЭКРАНОМ.
   *
   * Экран открывают раз в день, обрывы случаются весь день. Заведи подписку в
   * компоненте — и лента честно рассказывала бы ровно про те обрывы, которые
   * человек и так видел своими глазами, стоя перед этим самым экраном; про
   * ночную потерю связи она молчала бы. Сторож исходниками, потому что поднять
   * `startRealtime` в jsdom нечем: WebSocket там не открывается.
   */
  it("обрывы связи слушаются с момента входа, а не с открытия экрана", () => {
    const lifecycle = readFileSync("src/shared/realtime/realtime.ts", "utf-8") as string;
    const start = lifecycle.slice(
      lifecycle.indexOf("export function startRealtime"),
      lifecycle.indexOf("export function stopRealtime"),
    );
    expect(start, "слежение за обрывами не включается при старте").toContain(
      "watchConnectionGaps()",
    );
    // И отпускается при выходе: подписка, пережившая logout, — это утечка,
    // которая к тому же пишет в ленту следующего человека.
    const stop = lifecycle.slice(lifecycle.indexOf("export function stopRealtime"));
    expect(stop).toContain("unwatchGaps");

    // В самом экране подписки быть не должно — иначе появится вторая.
    const page = readFileSync("src/features/feed/FeedPage.tsx", "utf-8") as string;
    expect(page).not.toContain("watchConnectionGaps");
  });
});
