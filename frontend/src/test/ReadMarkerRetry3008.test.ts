import { beforeEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { досдатьМаркеры, забытьМаркеры, ждутДосылки, отметитьПрочитанным } from "@/features/chats/readMarker";
import { queryClient } from "@/app/queryClient";
import { jsonResponse } from "./helpers";

/**
 * ОТМЕТКА «ПРОЧИТАНО» ДОСЫЛАЕТСЯ, ЕСЛИ НЕ ДОШЛА (аудит 30.08).
 *
 * ⚠ ЧТО БЫЛО. Эффект открытия диалога слал `POST /read` и глотал ошибку с
 * припиской «маркер доедет при следующем открытии». Не доезжал: ретраи
 * транспорта покрывают только 502/503/504 и сетевые сбои в окне около секунды,
 * а обрыв туннеля подольше, 429 от nginx или 500 проходят мимо.
 *
 * Локально бейдж уже погашен, но тихая сверка раз в две минуты перезапрашивает
 * списки, а `seedFromRows` объявлен «сервер — истина» и перезаписывает счётчики
 * ЦЕЛИКОМ. Диалог, в который оператор смотрит, снова становился жирным и снова
 * просил внимания — а человек, увидев непрочитанное там, где читает прямо
 * сейчас, перестаёт верить бейджам вообще.
 */

const CONV = "dddddddd-0000-0000-0000-000000000001";

describe("Досылка отметки прочтения", () => {
  beforeEach(() => {
    queryClient.clear();
    забытьМаркеры();
  });

  it("удачная отметка ничего не копит", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, {})));
    await отметитьПрочитанным(CONV);
    expect(ждутДосылки()).toBe(0);
  });

  it("упавшая отметка запоминается", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(500, { error: {} })));
    await отметитьПрочитанным(CONV);
    expect(
      ждутДосылки(),
      "недоставленный маркер забыт — сверка вернёт непрочитанное в открытый диалог",
    ).toBe(1);
  });

  it("досылка отправляет его повторно и очищает очередь", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(500, { error: {} })));
    await отметитьПрочитанным(CONV);

    const удачный = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", удачный);
    await досдатьМаркеры();

    expect(удачный, "досылка не сходила на сервер").toHaveBeenCalled();
    expect(ждутДосылки(), "маркер остался в очереди после удачной досылки").toBe(0);
  });

  it("вторая неудача оставляет маркер в очереди", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(500, { error: {} })));
    await отметитьПрочитанным(CONV);
    await досдатьМаркеры();
    expect(ждутДосылки(), "маркер выброшен после первой же неудачной досылки").toBe(1);
  });

  it("пустая очередь на сервер не ходит", async () => {
    const запрос = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", запрос);
    await досдатьМаркеры();
    expect(запрос, "лишний запрос на каждую сверку").not.toHaveBeenCalled();
  });

  it("досылка подключена к тихой сверке, и ДО перезапроса списков", () => {
    /*
     * ⚠ БЕЗ ЭТОГО ПРОВЕРКИ ВЫШЕ СТОРОЖИЛИ БЫ ФУНКЦИЮ, КОТОРУЮ НИКТО НЕ ЗОВЁТ.
     *
     * И порядок здесь — часть починки, а не стиль: сверка тянет счётчики с
     * сервера, и досылка ПОСЛЕ неё опоздала бы ровно на один цикл — жирная
     * строка успела бы вернуться.
     */
    const весь = readFileSync("src/shared/realtime/quietResync.ts", "utf-8") as string;
    // Ищем внутри тела функции: выше по файлу те же слова встречаются в шапке.
    const исходник = весь.slice(весь.indexOf("export function resyncNow"));
    const досылка = исходник.indexOf("досдатьМаркеры()");
    const списки = исходник.indexOf("invalidateQueries");
    expect(досылка, "досылка не подключена к тихой сверке").toBeGreaterThan(-1);
    expect(досылка, "досылка идёт после перезапроса — опоздает на цикл").toBeLessThan(списки);
  });

  it("лента зовёт отметку с досылкой, а не голый запрос", () => {
    const исходник = readFileSync(
      "src/features/chats/components/thread/ChatThreadPane.tsx",
      "utf-8",
    ) as string;
    expect(исходник, "лента снова глотает ошибку отметки").toMatch(/отметитьПрочитанным\(convId\)/);
    expect(исходник, "остался прежний путь без досылки").not.toMatch(/markConversationRead\(convId\)/);
  });
});
