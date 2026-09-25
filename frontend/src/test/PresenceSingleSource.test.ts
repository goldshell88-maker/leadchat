/**
 * Состояние «на месте / отошёл» — одна точка правды (27.08).
 *
 * ⚠ ЧТО БЫЛО. Путей к одному состоянию было два, и ни один не доводил дело до
 * конца:
 *
 *   • переключатель в интерфейсе делал PUT и писал в хранилище — но десктоп об
 *     этом не узнавал: тосты продолжали звучать, галочка в меню трея оставалась
 *     на прежнем пункте;
 *   • выбор в трее вызывал ровно `setAwayMode` — тосты замолкали, а на сервер
 *     не уходило ничего: коллеги видели человека на месте, и автораздача
 *     продолжала слать ему обращения.
 *
 * Замысел записан в tray.rs («фронт делает PUT и возвращает подтверждённое
 * значение командой set_presence, Rust сам в API не ходит») — не хватало общей
 * функции и подписки на хранилище.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { pushPresence, usePresenceStore } from "@/features/presence/usePresence";
import { resetSessionStore } from "./helpers";

describe("Состояние присутствия", () => {
  beforeEach(() => {
    usePresenceStore.getState().set("online");
    resetSessionStore({ user: null, permissions: [], accessToken: "t", bootstrapped: true });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("смена статуса уходит на сервер и попадает в хранилище", async () => {
    // Тип аргумента объявлен через дженерик: без него `vi.fn` выводит кортеж
    // нулевой длины, и обращение к `c[0]` ниже становится ошибкой типов. Через
    // параметр функции не выходит — линтер справедливо ругается на неиспользуемый.
    const fetchMock = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(
      async () => new Response(JSON.stringify({ status: "away" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const итог = await pushPresence("away");

    expect(итог).toBe("away");
    expect(usePresenceStore.getState().status).toBe("away");
    const адреса = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(адреса.some((u) => u.includes("/presence"))).toBe(true);
  });

  it("хранилище принимает подтверждённое сервером значение, а не запрошенное", async () => {
    // Сервер вправе ответить иначе — например, вернуть «online», если «away»
    // не разрешён. Верим ответу, а не своей просьбе.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ status: "online" }), { status: 200 })),
    );

    const итог = await pushPresence("away");

    expect(итог).toBe("online");
    expect(usePresenceStore.getState().status).toBe("online");
  });

  it("отказ сервера хранилище не трогает", async () => {
    // Оставить «отошёл» при неудачном запросе нельзя ни в коем случае: человек
    // будет уверен, что обращений ему не дают, а они пойдут.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ error: { code: "server_error" } }), { status: 500 })),
    );

    await expect(pushPresence("away")).rejects.toBeTruthy();
    expect(usePresenceStore.getState().status).toBe("online");
  });

  it("ПРОВОДКА: отказ возвращает галочку трея на подтверждённое", () => {
    /*
     * ⚠ ЗДЕСЬ БЫЛО ПУСТОЕ `catch` С НЕИСПОЛНИМЫМ ОБЕЩАНИЕМ.
     *
     * Rust переставляет галку ОПТИМИСТИЧНО, до похода в сеть
     * (`tray.rs::request_presence`), а комментарий в catch обещал: «подписка
     * ниже вернёт галочку на подтверждённое значение». Она не вернёт:
     * `pushPresence` при отказе бросает ДО записи в хранилище, значение не
     * меняется, подписка не срабатывает — и `set_presence`, единственная
     * команда, переставляющая галку обратно, не вызывается никогда.
     *
     * Диспетчер уходит на обед, выбирает «Отошёл», сеть отвалилась: в трее
     * «Отошёл», на сервере «на месте», автораздача продолжает давать обращения.
     *
     * Проверяем ТЕЛО обработчика трея, а не файл целиком: `set_presence` стоит
     * и ниже, в помощнике `применить`, — по файлу проверка была бы зелёной
     * всегда. Первая редакция этой проверки на этом и попалась: срез до
     * `usePresenceStore.subscribe` захватывал определение `применить`, и
     * вырезанный откат её не ронял.
     */
    const src = (readFileSync("src/platform/index.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    const начало = src.indexOf("PRESENCE_EVENT");
    expect(начало, "обработчик события трея исчез").toBeGreaterThan(-1);
    const конец = src.indexOf("const применить", начало);
    expect(конец, "помощник `применить` исчез — граница среза потерялась").toBeGreaterThan(начало);
    const тело = src.slice(начало, конец);
    expect(тело, "при отказе галочка трея остаётся переставленной").toMatch(
      /invokeSafe\(\s*"set_presence"/,
    );
  });

  it("ПРОВОДКА: своё состояние спрашивается у сервера при входе", () => {
    /*
     * Хранилище стартует со значения «на месте» — «состояние только что
     * открытого приложения». Но статус переживает перезагрузку страницы: он
     * лежит в том же ключе, что и присутствие, тот живёт минутами, а
     * переподключение сокета текущее значение сохраняет. После F5 весь
     * интерфейс говорил «На месте», а сервер держал «Отошёл» и обращений не
     * давал: человек ждал работы, которой ему не дадут.
     */
    const src = (readFileSync("src/shared/stores/sessionStore.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    expect(src, "состояние присутствия больше не спрашивается при входе").toContain('"/presence"');
    expect(src).toContain("usePresenceStore");
  });

  it("ПРОВОДКА: и при ПЕРЕЗАГРУЗКЕ страницы — это другая дорога", () => {
    /*
     * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 28.08: «слетает отошёл при перезагрузке сайта».
     *
     * Чтение статуса стояло ТОЛЬКО во `applySession`, то есть срабатывало при
     * ВХОДЕ. Перезагрузка идёт другой дорогой — `bootstrap`: тихий refresh по
     * куке плюс `/auth/me`, — и на ней статус не спрашивал никто. Проверка
     * выше при этом зеленела: строка `"/presence"` в файле есть.
     *
     * Поэтому проверяем не наличие строки в файле, а вызов ИЗ ОБЕИХ функций.
     */
    const src = (readFileSync("src/shared/stores/sessionStore.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");

    const тело = (имя: string): string => {
      const i = src.indexOf(`async ${имя}(`);
      expect(i, `функция ${имя} исчезла — проверка охраняет пустоту`).toBeGreaterThan(-1);
      const j = src.indexOf("\n  },", i);
      return src.slice(i, j === -1 ? undefined : j);
    };

    for (const имя of ["applySession", "bootstrap"]) {
      expect(
        тело(имя),
        `${имя} не спрашивает своё состояние — на этой дороге «отошёл» будет теряться`,
      ).toContain("подтянутьСвоёСостояние()");
    }
  });

  it("ПРОВОДКА: трей шлёт статус на сервер, а не только глушит тосты", () => {
    const src = readFileSync("src/platform/index.ts", "utf-8") as string;
    expect(src).toContain("pushPresence");
  });

  it("ПРОВОДКА: хранилище доводит статус до тостов и до галочки в трее", () => {
    /*
     * Обе связи заводятся подпиской на хранилище — она и есть недостающее звено.
     *
     * ⚠ ПЕРВАЯ РЕДАКЦИЯ ЭТОЙ ПРОВЕРКИ БЫЛА СЛЕПОЙ. Она искала СЛОВА рядом с
     * подпиской, а слово `set_presence` стоит и в комментарии над ней — поэтому
     * вырезанный вызов проверку не ронял. Ловим вызовы, а не упоминания:
     * комментарии из исходника вырезаем до поиска.
     */
    const src = (readFileSync("src/platform/index.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    expect(src, "подписка на хранилище присутствия исчезла").toContain("usePresenceStore.subscribe");
    expect(src, "«отошёл» больше не глушит тосты").toMatch(/setAwayMode\(/);
    expect(src, "галочку в трее никто не переставляет").toMatch(/invokeSafe\(\s*"set_presence"/);
  });

  it("ПРОВОДКА: переключатель и трей делят один запрос", () => {
    // Два места, шлющих PUT /presence, однажды разойдутся: одно вернёт
    // подтверждение в хранилище, другое забудет.
    const src = readFileSync("src/features/presence/usePresence.ts", "utf-8") as string;
    expect(src.match(/http\.put<\{ status: PresenceStatus \}>\("\/presence"/g)).toHaveLength(1);
  });
});
