import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BUILD_VERSION } from "@/platform/buildVersion";
import { render, screen, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { __resetPlatform, initPlatform } from "@/platform";
import { __setBridge } from "@/platform/bridge";
import { createWebBridge } from "@/platform/web";
import { CACHE_DIALOGS, installCachePersist, warmupFromCache } from "@/platform/warmup";
import {
  МАКС_СТРОК,
  МАКС_ЗНАКОВ,
  подрезать,
  прочитатьСнимок,
  сохранитьСнимок,
  стеретьСнимок,
  type ЗаписьСнимка,
} from "@/platform/снимокСписка";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, TabCounts } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, resetSessionStore } from "./helpers";

/**
 * Снимок списка для ВЕБА (IndexedDB) — первый кадр без ожидания сервера.
 *
 * Проверяется не «модуль умеет писать в базу», а обещание целиком: строки и
 * числа вкладок видны ДО ответа сервера, снимок принадлежит человеку, стареет,
 * стирается на выходе и молчит там, где хранилища нет.
 */

/* -------------------------------------------------------------------------- *
 *  Макет IndexedDB: jsdom его не даёт, а тащить пакет ради теста нельзя.
 *  Поддержан ровно тот кусок API, которым пользуется `снимокСписка.ts`.
 * -------------------------------------------------------------------------- */

type РежимБазы = "ok" | "молчит" | "бросает" | "отказ";

function поставитьБазу(режим: РежимБазы = "ok"): Map<IDBValidKey, unknown> {
  const данные = new Map<IDBValidKey, unknown>();
  const имена = new Set<string>();

  const собратьБазу = () => {
    type Сделка = { oncomplete: (() => void) | null; objectStore: () => unknown };
    let tx: Сделка | null = null;
    const завершить = () => tx?.oncomplete?.();
    const store = {
      get(k: IDBValidKey) {
        const req: { onsuccess: (() => void) | null; onerror: (() => void) | null; result: unknown } = {
          onsuccess: null,
          onerror: null,
          result: undefined,
        };
        queueMicrotask(() => {
          req.result = данные.get(k);
          req.onsuccess?.();
          завершить();
        });
        return req;
      },
      put(v: unknown, k: IDBValidKey) {
        // Настоящая база кладёт КОПИЮ (structured clone) — макет тоже, иначе
        // тест «прочитали то, что положили» проходил бы на одной ссылке.
        данные.set(k, JSON.parse(JSON.stringify(v)));
        queueMicrotask(завершить);
      },
      delete(k: IDBValidKey) {
        данные.delete(k);
        queueMicrotask(завершить);
      },
    };
    return {
      objectStoreNames: { contains: (n: string) => имена.has(n) },
      createObjectStore: (n: string) => {
        имена.add(n);
        return store;
      },
      close: () => {},
      transaction: () => {
        tx = { oncomplete: null, objectStore: () => store };
        return tx as unknown as IDBTransaction;
      },
    };
  };

  const фейк = {
    open() {
      // Firefox в приватном окне бросает синхронно прямо из open().
      if (режим === "бросает") throw new Error("хранилище запрещено");
      const req: Record<string, unknown> = {
        onupgradeneeded: null,
        onsuccess: null,
        onerror: null,
        onblocked: null,
        result: null,
      };
      if (режим === "молчит") return req; // никто никогда не ответит
      queueMicrotask(() => {
        if (режим === "отказ") {
          (req.onerror as (() => void) | null)?.();
          return;
        }
        req.result = собратьБазу();
        if (имена.size === 0) (req.onupgradeneeded as (() => void) | null)?.();
        (req.onsuccess as (() => void) | null)?.();
      });
      return req;
    },
  };

  vi.stubGlobal("indexedDB", фейк as unknown as IDBFactory);
  return данные;
}

/* -------------------------------------------------------------------------- *
 *  Данные
 * -------------------------------------------------------------------------- */

const ЧУЖОЙ = "11111111-2222-3333-4444-555555555555";

function строка(id: string, overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: `Клиент ${id}`, phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: { body: "Здравствуйте", direction: "in", created_at: "2026-09-07T08:00:00Z" },
    unread_count: 2,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-07T08:00:00Z",
    ...overrides,
  };
}

function запись(overrides: Partial<ЗаписьСнимка> = {}): ЗаписьСнимка {
  return {
    ownerId: fakeUser.id,
    build: BUILD_VERSION,
    tab: "mine",
    dialogs: [строка("c1"), строка("c2")],
    counts: { mine: 41, mine_waiting: 3 },
    savedAt: new Date().toISOString(),
    ...overrides,
  };
}

const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");

/** Список рисуется ГЛОБАЛЬНЫМ клиентом: снимок ложится именно в него. */
function нарисоватьСписок() {
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/chats"]}>
          <ChatListPane />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

describe("Снимок списка в вебе (IndexedDB, 07.09)", () => {
  beforeEach(() => {
    queryClient.clear();
    useUnreadStore.getState().clear();
    useChatUiStore.setState({ filters: { tab: "all" } });
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    __setBridge(createWebBridge());
  });

  afterEach(() => {
    // Подписка на кэш живёт до явного снятия: оставь её — и следующий тест
    // писал бы снимок из чужих данных.
    __resetPlatform();
    __setBridge(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalOffsetHeight) {
      Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    }
    if (originalOffsetWidth) {
      Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
    }
  });

  /**
   * ДИВЕРСИЯ: в `сохранитьСнимок` ключ записи заменён на литерал "иной"
   * (`put({...}, "иной")`) — то есть кладём не туда, куда читаем.
   * РЕЗУЛЬТАТ: красный, «expected null not to be null» на первой же проверке.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("снимок кладётся и читается той же проводкой, что у десктопа", async () => {
    const данные = поставитьБазу();

    queryClient.setQueryData<TabCounts>(qk.conversations.counts, { mine: 41, mine_waiting: 3 });
    queryClient.setQueryData(qk.conversations.list({ tab: "all" }), {
      pages: [{ items: [строка("c1"), строка("c2")], page: { limit: 2, offset: 0, total: 2 } }],
      pageParams: [0],
    });

    // Та же подписка на кэш TanStack, что пишет SQLite в десктопе.
    const стоп = installCachePersist(0);
    queryClient.setQueryData(qk.conversations.list({ tab: "mine" }), {
      pages: [{ items: [строка("c3")], page: { limit: 1, offset: 0, total: 1 } }],
      pageParams: [0],
    });
    await waitFor(() => expect(данные.size).toBe(1));
    стоп();

    const прочитано = await прочитатьСнимок();
    expect(прочитано).not.toBeNull();
    expect(прочитано?.ownerId).toBe(fakeUser.id);
    expect(прочитано?.build).toBe(BUILD_VERSION);
    expect(прочитано?.tab).toBe("all"); // вкладка в момент записи
    expect(прочитано?.counts).toEqual({ mine: 41, mine_waiting: 3 });
    expect(прочитано?.dialogs.map((d) => d.id).sort()).toEqual(["c1", "c2", "c3"]);
  });

  /**
   * ДИВЕРСИЯ: в `warmupFromCache` набор вкладок сужен обратно до одной
   * (`new Set([filters.tab])`) — ровно то поведение, что было до правки.
   * РЕЗУЛЬТАТ: красный, «Unable to find an element with the text: Клиент c1»:
   * менеджер после смены роли смотрит на «Мои», а снимок лёг во «Все».
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("строки и числа вкладок видны в первом кадре, и тут же идёт перезапрос", async () => {
    поставитьБазу();
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 320 });

    await сохранитьСнимок(запись());

    // Сервер не отвечает НИКОГДА: всё, что окажется на экране, — из снимка.
    const fetchMock = vi.fn(
      (input: RequestInfo | URL) => new Promise<Response>(() => void String(input)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(warmupFromCache()).resolves.toBe(true);
    // `useRoleUiSync` переводит менеджера на «Мои» сразу после старта.
    useChatUiStore.setState({ filters: { tab: "mine" } });

    нарисоватьСписок();

    // Синхронно, без findBy: ждать нечего — ответа сервера не будет.
    expect(screen.getByText("Клиент c1")).toBeInTheDocument();
    expect(screen.getByText("Клиент c2")).toBeInTheDocument();
    expect(screen.getByText("41")).toBeInTheDocument(); // число над «Моими»

    // Снимок протух в момент подстановки — экран уходит за свежими данными.
    expect(queryClient.getQueryState(qk.conversations.list({ tab: "mine" }))?.dataUpdatedAt).toBe(0);
    await waitFor(() => {
      const адреса = fetchMock.mock.calls.map((c) => decodeURIComponent(String(c[0])));
      expect(адреса.some((u) => u.includes("/conversations?"))).toBe(true);
      expect(адреса.some((u) => u.includes("/conversations/counts"))).toBe(true);
    });

    /*
     * ⚠ И РОВНО ОДИН ЗАПРОС СПИСКА — ГЛАВНОЕ УСЛОВИЕ ВСЕЙ ЭТОЙ РАБОТЫ.
     * Засеяны ДВЕ вкладки, а смотрит человек в одну: ключ, который никто не
     * наблюдает, грузиться не должен. Тринадцать диспетчеров сидят за одним
     * офисным NAT, и предзагрузка, которая «на всякий случай» дёргает второй
     * срез, стоит им общего лимита. Проверка ловит и очевидную будущую правку —
     * `prefetchQuery`/`invalidateQueries` сразу после засева.
     */
    const списочные = fetchMock.mock.calls.filter((c) =>
      decodeURIComponent(String(c[0])).includes("/conversations?"),
    );
    expect(списочные).toHaveLength(1);
  });

  /**
   * ДИВЕРСИЯ: из `warmupFromCache` убрана строка
   * `if (snapshot.ownerId && snapshot.ownerId !== userId) return false;`.
   * РЕЗУЛЬТАТ: красный, `resolves.toBe(false)` получил true, а в кэше
   * оказались диалоги ушедшего сотрудника.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("чужой снимок не показывается: за одним столом работает смена", async () => {
    поставитьБазу();
    await сохранитьСнимок(запись({ ownerId: ЧУЖОЙ }));

    await expect(warmupFromCache()).resolves.toBe(false);
    expect(queryClient.getQueryData(qk.conversations.list({ tab: "all" }))).toBeUndefined();
    expect(queryClient.getQueryData(qk.conversations.list({ tab: "mine" }))).toBeUndefined();
    expect(queryClient.getQueryData(qk.conversations.counts)).toBeUndefined();
  });

  /**
   * ДИВЕРСИЯ: проверка срока в `прочитатьСнимок` ослаблена до
   * `if (!(возраст >= 0)) return null;` — то есть снимок не стареет никогда.
   * РЕЗУЛЬТАТ: красный на обеих проверках протухшего снимка.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("снимок старше двенадцати часов не показывается", async () => {
    поставитьБазу();
    const тринадцатьЧасовНазад = new Date(Date.now() - 13 * 60 * 60 * 1000).toISOString();
    await сохранитьСнимок(запись({ savedAt: тринадцатьЧасовНазад }));

    await expect(прочитатьСнимок()).resolves.toBeNull();
    await expect(warmupFromCache()).resolves.toBe(false);

    // Одиннадцать часов — ещё та же смена плюс ночь: снимок годен.
    await сохранитьСнимок(запись({ savedAt: new Date(Date.now() - 11 * 60 * 60 * 1000).toISOString() }));
    await expect(прочитатьСнимок()).resolves.not.toBeNull();

    /*
     * ⚠ ВОЗРАСТ ПРОВЕРЯЕТСЯ С ОБЕИХ СТОРОН, И ВТОРАЯ СТОРОНА НЕ УКРАШЕНИЕ.
     * `Date.parse` от мусора даёт NaN, а NaN не «старый» и не «свежий»: любое
     * сравнение с ним ложно, и проверка вида «возраст > срока» такую запись
     * ПРОПУСТИТ — снимок неизвестного возраста станет вечно свежим. Мусор в
     * хранилище берётся не из воздуха: прошлый выпуск фронта писал другое поле,
     * сеанс оборвался на середине записи. Дата из будущего — та же болезнь с
     * другой стороны: часы машины перевели назад (NTP, смена пояса, ручная
     * правка), и вчерашний список выглядит записанным «через час».
     */
    await сохранитьСнимок(запись({ savedAt: "вчера вечером" }));
    await expect(прочитатьСнимок()).resolves.toBeNull();

    await сохранитьСнимок(запись({ savedAt: new Date(Date.now() + 60 * 60 * 1000).toISOString() }));
    await expect(прочитатьСнимок()).resolves.toBeNull();
  });

  /**
   * ДИВЕРСИЯ: в `sessionStore.wipeLocalCache` возвращён заслон
   * `if (!isTauri()) return;`.
   * РЕЗУЛЬТАТ: красный — после выхода снимок остаётся в базе и читается.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("выход из аккаунта стирает снимок", async () => {
    const данные = поставитьБазу();
    await сохранитьСнимок(запись());
    expect(данные.size).toBe(1);

    useSessionStore.getState().clear();

    await waitFor(() => expect(данные.size).toBe(0));
    await expect(прочитатьСнимок()).resolves.toBeNull();
  });

  /**
   * ДИВЕРСИЯ: из `открыть()` убран `try/catch` вокруг `indexedDB.open`.
   * РЕЗУЛЬТАТ: красный — `прочитатьСнимок` не резолвится, а падает
   * («хранилище запрещено»), и вместе с ним падает старт приложения.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("недоступная IndexedDB не ломает ни чтение, ни запись, ни загрузку", async () => {
    поставитьБазу("бросает");

    await expect(прочитатьСнимок()).resolves.toBeNull();
    await expect(сохранитьСнимок(запись())).resolves.toBeUndefined();
    await expect(стеретьСнимок()).resolves.toBeUndefined();
    await expect(warmupFromCache()).resolves.toBe(false);

    // Базы нет вовсе (jsdom по умолчанию) — тот же исход, без исключения.
    vi.unstubAllGlobals();
    await expect(прочитатьСнимок()).resolves.toBeNull();
    await expect(warmupFromCache()).resolves.toBe(false);
  });

  /**
   * ДИВЕРСИЯ: `прочитатьСнимок` зовёт `достатьЗапись()` напрямую, без
   * `сДедлайном`.
   * РЕЗУЛЬТАТ: красный — тест не завершается и падает по своему таймауту
   * (15 с). Ровно это и случилось бы со стартом приложения.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("молчащая база не подвешивает старт", async () => {
    поставитьБазу("молчит");
    const начало = Date.now();
    await expect(прочитатьСнимок()).resolves.toBeNull();
    expect(Date.now() - начало).toBeLessThan(2000);
  });

  /**
   * ДИВЕРСИЯ: `подрезать` возвращает `dialogs` без обрезки.
   * РЕЗУЛЬТАТ: красный на обеих проверках потолка.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("снимок не растёт без предела: потолок по строкам и по размеру", () => {
    /*
     * ⚠ СНАЧАЛА САМ ПОТОЛОК, ПОТОМ ОБРЕЗКА ПО НЕМУ. `toHaveLength(МАКС_СТРОК)`
     * сравнивает константу с собой: подними её с 200 до 400 — и проверка ниже
     * останется зелёной, а веб начнёт писать вдвое больший снимок каждые две
     * секунды. Держим значение там, где оно заявлено, — на десктопном
     * `CACHE_DIALOGS` (04 §5.1), и расхождение двух потолков видно сразу.
     */
    expect(МАКС_СТРОК).toBe(CACHE_DIALOGS);

    const многоСтрок = Array.from({ length: 500 }, (_, i) => строка(`c${i}`));
    expect(подрезать(многоСтрок)).toHaveLength(МАКС_СТРОК);

    // Вырождение: один диалог с гигантским текстом последнего сообщения.
    const огромные = Array.from({ length: 40 }, (_, i) =>
      строка(`b${i}`, {
        last_message: { body: "я".repeat(50_000), direction: "in", created_at: "2026-09-07T08:00:00Z" },
      }),
    );
    expect(JSON.stringify(подрезать(огромные)).length).toBeLessThanOrEqual(МАКС_ЗНАКОВ);
  });

  /**
   * ⚠ ЭТОТ СТОРОЖ СТЕРЕЖЁТ ПРОВОДКУ, А НЕ МОДУЛЬ. Остальные тесты зовут
   * `installCachePersist` руками и потому одинаково зелены при ЛЮБОМ месте
   * вызова в коде — в том числе при прежнем «только для десктопа»
   * (`if (bridge.kind === "tauri")` в `initPlatform`). А это ровно то, чем
   * веб-снимок умирает молча: подписки нет, писать некому, база пуста, все
   * проверки проходят. Поэтому здесь поднимается настоящий `initPlatform` с
   * веб-мостом, и обещание проверяется целиком: ответ сервера лёг в кэш —
   * значит он оказался в IndexedDB.
   *
   * ДИВЕРСИЯ: в `initPlatform` возвращено `if (bridge.kind === "tauri")`
   * перед `teardown.push(installCachePersist())`.
   * РЕЗУЛЬТАТ: красный — в базе пусто.
   */
  it("подписку на кэш ставит сам initPlatform, и в вебе тоже", async () => {
    const данные = поставитьБазу();

    // Только таймеры: `Date` оставляем настоящей, иначе снимок оказался бы
    // записан «на две секунды вперёд» и проверка возраста его отбросила бы.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await initPlatform();
      queryClient.setQueryData(qk.conversations.list({ tab: "all" }), {
        pages: [{ items: [строка("c9")], page: { limit: 1, offset: 0, total: 1 } }],
        pageParams: [0],
      });
      // Пауза записи — та же, что в бою (2 с), и её тоже проходим насквозь.
      await vi.advanceTimersByTimeAsync(2000);
    } finally {
      vi.useRealTimers();
    }

    await waitFor(() => expect(данные.size).toBe(1));
    const прочитано = await прочитатьСнимок();
    expect(прочитано?.ownerId).toBe(fakeUser.id);
    expect(прочитано?.dialogs.map((d) => d.id)).toEqual(["c9"]);
  });

  /**
   * ⚠ ПОЗДНЯЯ ЗАПИСЬ ПОСЛЕ ВЫХОДА. Стирание снимка и отложенная запись живут в
   * одной вкладке и спорят за одну строку: `clear()` стирает базу сразу, а
   * подписка на кэш держит отложенный флаш до двух секунд — и строки ушедшего
   * всё ещё лежат в кэше запросов (его чистит `useRoleUiSync` только при входе
   * СЛЕДУЮЩЕГО человека). Проиграй этот спор — и запись, сделанная уже после
   * выхода, вернёт список ушедшего на диск, откуда его никто больше не сотрёт.
   *
   * ДИВЕРСИЯ: из веб-ветки `convCache.persist` убрана строка
   * `if (!s.ownerId || !s.role) return;`.
   * РЕЗУЛЬТАТ: красный — после выхода в базе снова лежит снимок.
   */
  it("выход не переигрывается поздней записью из очереди", async () => {
    const данные = поставитьБазу();
    await сохранитьСнимок(запись());
    expect(данные.size).toBe(1);

    const стоп = installCachePersist(0);
    try {
      queryClient.setQueryData(qk.conversations.list({ tab: "all" }), {
        pages: [{ items: [строка("c1")], page: { limit: 1, offset: 0, total: 1 } }],
        pageParams: [0],
      });
      // Выход/отзыв сессии ровно тем же путём, что и в бою.
      useSessionStore.getState().clear();

      await waitFor(() => expect(данные.size).toBe(0));
      // Даём отложенной записи отработать: она обязана промолчать.
      await new Promise((r) => setTimeout(r, 30));
      expect(данные.size).toBe(0);
    } finally {
      стоп();
    }
  });

  /**
   * ДИВЕРСИЯ: из веб-ветки `convCache.persist` убрана проверка
   * `document.visibilityState !== "visible"`.
   * РЕЗУЛЬТАТ: красный — скрытая вкладка перезаписала снимок.
   * Байт в байт восстановлено (sha256 сверен).
   */
  it("скрытая вкладка снимок не переписывает", async () => {
    const данные = поставитьБазу();
    await сохранитьСнимок(запись({ dialogs: [строка("исходный")] }));

    const мост = createWebBridge();
    const видимость = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await мост.convCache.persist({
      dialogs: [строка("из-скрытой")],
      savedAt: new Date().toISOString(),
      ownerId: fakeUser.id,
      role: fakeUser.role,
      tab: "all",
      counts: null,
    });
    видимость.mockRestore();

    expect(данные.size).toBe(1);
    const прочитано = await прочитатьСнимок();
    expect(прочитано?.dialogs.map((d) => d.id)).toEqual(["исходный"]);
  });

  /*
   * ⚠ СНИМОК НЕ ПЕРЕЖИВАЕТ ВЫКАТКУ (найдено ревью 07.09).
   *
   * Срок годности 12 часов длиннее промежутка между выкатками (их бывает 9–22
   * в сутки), а разбор проверяет у строки только `id`. Без сверки версии
   * сборки первый кадр на следующее утро после каждой выкатки, сменившей форму
   * строки списка, рисовался бы строками ПРЕДЫДУЩЕЙ сборки — у всех
   * тринадцати человек. Прод собирается с `--build-arg VITE_APP_VERSION=<коммит>`
   * (deploy/workstation/ship.sh), то есть версия там своя у каждой выкатки.
   *
   * ⚠ ДИВЕРСИЯ: убрать `if (запись.build !== BUILD_VERSION) return null;` в
   * `прочитатьСнимок` — проверка краснеет: чужая сборка возвращается как своя.
   */
  it("снимок чужой сборки не показывается", async () => {
    поставитьБазу();
    await сохранитьСнимок(запись({ build: "старая-сборка-abc1234" }));
    expect(await прочитатьСнимок()).toBeNull();

    await сохранитьСнимок(запись());
    expect(await прочитатьСнимок()).not.toBeNull();
  });
});
