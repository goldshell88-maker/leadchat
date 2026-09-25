import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import { resyncNow, startQuietResync } from "@/shared/realtime/quietResync";

vi.mock("@/features/chats/inbox/api", () => ({ refreshInboxCount: vi.fn() }));

/**
 * ТИХАЯ СВЕРКА ВМЕСТО F5.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 28.08: «иногда приходится обновлять страницу, так как
 * что-то не проставилось, не прошло и т.д. Сделай бесшовное обновление
 * страницы, чтобы это было незаметно».
 *
 * Догон после ОБРЫВА написан давно, но держится на допущении «сокет жив,
 * значит кэш верен». Кадр теряется и без обрыва: публикация в Pub/Sub без
 * подтверждения доставки, кадр без нужного поля (за один сегодняшний день
 * таких нашлось три), придержанные таймеры скрытой вкладки. Человеку в этих
 * случаях остаётся F5 — и он его жмёт, теряя черновик и место в ленте.
 *
 * С 31.08 скрытая вкладка сверяется наравне с открытой (см. ниже).
 */
describe("Тихая сверка", () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let invalidate: any;

  beforeEach(() => {
    vi.useFakeTimers();
    invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  /** Аргументы всех вызовов сверки — нужны, когда ключа у вызова нет вовсе. */
  const вызовы = () =>
    invalidate.mock.calls.map((c: unknown[]) => (c[0] ?? {}) as Record<string, unknown>);

  it("сверяет ключи чатов, а не всё, что на экране", () => {
    /*
     * ⚠ ПРАВИЛО ПЕРЕВЁРНУТО ДВАЖДЫ, И ЭТОТ ТЕСТ ПЕРЕПИСАН ВМЕСТЕ С НИМ.
     *
     * 31.08 здесь стоял сплошной обход «всё, у чего есть наблюдатель» — ради
     * жалобы про F5, все три беды которой были про кадры ЧАТОВ. Аудит 06.09
     * измерил цену: на экране чатов 10–17 активных запросов, включая шаблоны
     * со `staleTime` пять минут, раз в две минуты с каждой из 11–13 вкладок
     * за одним офисным адресом — часть потока, давшего 344 ответа 429 в сутки.
     *
     * Сверяем то, что живёт на кадрах сокета и без них стареет: список,
     * числа, очередь, открытая лента. Шаблоны и настройки правят на своих
     * экранах, где мутация сбрасывает ключ сама, а при следующем открытии
     * экрана их обновит собственный `staleTime`.
     */
    resyncNow(true);
    const сплошной = вызовы().some((v: Record<string, unknown>) => !("queryKey" in v));
    expect(
      сплошной,
      "сплошная сверка вернулась — 10–17 запросов на вкладку раз в две минуты",
    ).toBe(false);
    const ключи = вызовы().map((v: Record<string, unknown>) => JSON.stringify(v.queryKey));
    expect(ключи).toContain(JSON.stringify(CONVERSATIONS_LIST_KEY));
    expect(ключи).toContain(JSON.stringify(qk.conversations.counts));
    expect(ключи).toContain(JSON.stringify(qk.inbox.list));
  });

  it("НЕ СНИМАЕТ данные с экрана: только активные ключи", () => {
    /*
     * Здесь вся «бесшовность». `refetchType: "active"` оставляет прежние данные
     * на месте, пока идёт запрос, и подменяет их готовыми: ни скелета, ни
     * мигания, ни прыжка прокрутки — в отличие от F5, который всё это делает и
     * вдобавок теряет набранное.
     */
    resyncNow(true);
    for (const вызов of invalidate.mock.calls as unknown[][]) {
      expect((вызов[0] as { refetchType?: string }).refetchType).toBe("active");
    }
  });

  it("не чаще раза в двадцать секунд", () => {
    /*
     * Возврат во вкладку бывает и раз в секунду: человек переключается между
     * окнами. Без порога тринадцать вкладок устроили бы поток запросов.
     */
    resyncNow(true);
    const было = invalidate.mock.calls.length;
    resyncNow();
    resyncNow();
    expect(invalidate.mock.calls.length).toBe(было);
  });

  it("возврат во вкладку сверяет", () => {
    const стоп = startQuietResync();
    invalidate.mockClear();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(invalidate.mock.calls.length).toBeGreaterThan(0);
    стоп();
  });

  it("скрытую вкладку сверяем ТОЖЕ", () => {
    /*
     * ⚠ ПРАВИЛО ПЕРЕВЁРНУТО 31.08 ПО ПРОСЬБЕ ВЛАДЕЛЬЦА: «обновляться должно
     * всё, даже если вкладка свёрнута».
     *
     * Здесь проверялось обратное — «смотреть на неё некому, вернётся, тогда и
     * сверим». Довод неверен дважды. У свёрнутой вкладки видно главное:
     * счётчик в заголовке браузера, бейдж очереди и звук нового обращения. И
     * кадр, потерянный при свёрнутой вкладке, не чинился НИКОГДА: сверка по
     * возврату случается, только если человек вернётся, а диспетчер может
     * полсмены работать в соседнем окне Авито.
     *
     * Что свёрнутое окно и правда ходит на сервер, а не только метит кэш
     * устаревшим, проверено отдельно — `HiddenTabResync3108`.
     */
    const стоп = startQuietResync();
    invalidate.mockClear();
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(5 * 60_000);
    expect(
      invalidate.mock.calls.length,
      "свёрнутая вкладка не сверяется — данные в ней устареют молча и до возврата",
    ).toBeGreaterThan(0);
    стоп();
  });

  it("в работе сверяет сама, без участия человека", () => {
    /*
     * Пол сверки: пропущенный кадр живёт на экране не дольше двух минут. Без
     * этого расхождение держалось бы до следующего переключения окна — то есть
     * до вечера у того, кто из вкладки не выходит.
     */
    const стоп = startQuietResync();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    invalidate.mockClear();
    /*
     * ⚠ ПЛЮС РАЗБРОС (03.09). У периодической сверки появилась случайная
     * задержка до восьми секунд: после выкатки все тринадцать вкладок
     * перезапускаются разом и дальше идут в такт, складывая свои сверки в один
     * залп — а предел nginx считается на АДРЕС, и офис за одним NAT. Ждём
     * период плюс полный разброс, иначе проверка ловит не сверку, а жребий.
     */
    vi.advanceTimersByTime(2 * 60_000 + 9_000);
    expect(invalidate.mock.calls.length).toBeGreaterThan(0);
    стоп();
  });

  it("выключение снимает и слушатель, и таймер", () => {
    const стоп = startQuietResync();
    стоп();
    invalidate.mockClear();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(10 * 60_000);
    expect(invalidate.mock.calls.length).toBe(0);
  });

  it("ПРОВОДКА: сверка включается вместе с реальным временем и гаснет с ним", async () => {
    /*
     * Без этой проверки всё остальное зеленеет впустую: механизм можно
     * написать, покрыть тестами и не позвать ни разу. Этот проект уже
     * несколько раз попадался на такой дыре — последний раз сегодня, когда
     * гашение тревог было позвано из трёх мест из четырёх.
     */
    // @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
    const { readFileSync } = await import("node:fs");
    const src = (readFileSync("src/shared/realtime/realtime.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    expect(src, "тихая сверка не включается — она мертва").toContain("startQuietResync()");
    expect(src, "сверка переживает выход — таймер останется висеть").toMatch(
      /unwatchResync\?\.\(\)/,
    );
  });
});

describe("Пульс: возврат во вкладку освежает числа сразу и дёшево", () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let invalidate: any;

  beforeEach(() => {
    vi.useFakeTimers();
    invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  /**
   * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 03.09: «очень долго обновляется вкладка/поля/индикаторы,
   * нужно максимально быстро и сделай, чтобы весь сайт обновлялся в фоне».
   *
   * Возврат во вкладку звал ПОЛНУЮ сверку — 12–17 запросов. Диспетчер работает
   * в двух окнах, LeadChat и само Авито, и щёлкает между ними постоянно;
   * тринадцать человек сидят за ОДНИМ офисным адресом, а nginx считает предел
   * на адрес. Залп из тринадцати вкладок по пятнадцать запросов — почти двести
   * штук разом при пропуске шестьдесят: лишнее получало 429, а на 429 клиент не
   * переспрашивает. Частые возвраты не ускоряли обновление, а теряли его.
   *
   * Теперь на возврат идут ДВА запроса — числа, на которые человек и смотрит.
   */
  it("на возврате спрашивает числа, а не всё подряд", async () => {
    const стоп = startQuietResync();
    invalidate.mockClear();

    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));

    const ключи = (invalidate.mock.calls as unknown[][]).map((c) =>
      JSON.stringify((c[0] as { queryKey?: readonly unknown[] } | undefined)?.queryKey),
    );
    expect(ключи, "числа над вкладкой не освежились").toContain(
      JSON.stringify(qk.conversations.counts),
    );

    /*
     * ⚠ А ВОТ ПОВТОРНЫЙ ВОЗВРАТ ПОЛНУЮ СВЕРКУ УЖЕ НЕ ЗОВЁТ. Раньше она шла на
     * КАЖДОЕ переключение окна в обход общего пола: пятнадцать запросов на
     * щелчок, тринадцать вкладок за одним офисным адресом. Теперь возврат
     * обходит пол только для двух дешёвых чисел, а тяжёлая сверка идёт по
     * своему сроку.
     */
    invalidate.mockClear();
    document.dispatchEvent(new Event("visibilitychange"));
    const полные = (invalidate.mock.calls as unknown[][]).filter(
      (c) => (c[0] as { queryKey?: readonly unknown[] } | undefined)?.queryKey === undefined,
    ).length;
    expect(полные, "щелчок по окну снова зовёт полную сверку").toBe(0);
    стоп();
  });

  it("щёлканье между окнами не устраивает очередь запросов", () => {
    /* Пол пульса: пять секунд. Иначе перещёлкивание раз в секунду даёт поток. */
    const стоп = startQuietResync();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    invalidate.mockClear();

    for (let i = 0; i < 10; i++) document.dispatchEvent(new Event("visibilitychange"));

    const счёт = (invalidate.mock.calls as unknown[][]).filter(
      (c) =>
        JSON.stringify((c[0] as { queryKey?: readonly unknown[] } | undefined)?.queryKey) ===
        JSON.stringify(qk.conversations.counts),
    ).length;
    expect(счёт, "каждое переключение окна бьёт по серверу").toBe(0);
    стоп();
  });
});

describe("Фон: пульс по фокусу окна и при молчании сокета", () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let invalidate: any;

  beforeEach(() => {
    vi.useFakeTimers();
    invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    useConnectionStore.setState({ status: "open", lastEventAt: null, reconnects: [], lastFrameAt: 0 });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function числаОсвежили(): number {
    return (invalidate.mock.calls as unknown[][]).filter(
      (c) =>
        JSON.stringify((c[0] as { queryKey?: readonly unknown[] } | undefined)?.queryKey) ===
        JSON.stringify(qk.conversations.counts),
    ).length;
  }

  it("окно видно, но не в фокусе: возврат мышью освежает числа", () => {
    /*
     * ⚠ У ДИСПЕТЧЕРА ДВА МОНИТОРА. Окно LeadChat видно постоянно, значит
     * `visibilitychange` не приходит НИКОГДА, и возврат к нему мышью не
     * освежал ничего — данные старели до следующей периодической сверки.
     */
    const стоп = startQuietResync();
    invalidate.mockClear();

    window.dispatchEvent(new Event("focus"));

    expect(числаОсвежили(), "фокус окна ничего не освежил").toBeGreaterThan(0);
    стоп();
  });

  it("сокет открыт, но молчит дольше порога — спрашиваем сами", () => {
    /*
     * Сокет бывает жив по всем признакам и при этом не приносит ничего:
     * подписка на сервере потерялась, кадр не дошёл. Экран тихо стареет, и
     * человеку остаётся F5 — ровно то, ради отмены чего написана вся сверка.
     */
    const стоп = startQuietResync();
    useConnectionStore.setState({ lastFrameAt: Date.now() });
    invalidate.mockClear();

    vi.advanceTimersByTime(95_000);

    expect(числаОсвежили(), "молчащий сокет никто не проверил").toBeGreaterThan(0);
    стоп();
  });

  it("а живой поток кадров сторожа не будит", () => {
    /* Обратная половина: пока кадры идут, спрашивать не о чем. */
    const стоп = startQuietResync();
    invalidate.mockClear();

    for (let прошло = 0; прошло < 95_000; прошло += 10_000) {
      useConnectionStore.setState({ lastFrameAt: Date.now() });
      vi.advanceTimersByTime(10_000);
    }

    expect(числаОсвежили(), "сторож стреляет при живом потоке").toBe(0);
    стоп();
  });
});
