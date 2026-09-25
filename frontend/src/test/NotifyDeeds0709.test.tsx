import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import { requestTransferDecision, useActionBus, ЖИЗНЬ_ПРОСЬБЫ_МС } from "@/features/hotkeys/actionBus";
import { __resetPlatform, initPlatform } from "@/platform";
import { __setBridge } from "@/platform/bridge";
import { __сброситьВыборы } from "@/platform/наЭкранеЛи";
import { createWebBridge } from "@/platform/web";
import { listenSafe } from "@/platform/tauri/ipc";
import type { ConversationDetailDto } from "@/shared/api/types";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * КНОПКА В УВЕДОМЛЕНИИ ОБЯЗАНА ДЕЛАТЬ ТО, ЧТО НА НЕЙ НАПИСАНО.
 *
 * ⚠ ПОЧЕМУ ЭТОТ ФАЙЛ ПОЯВИЛСЯ ОТДЕЛЬНО ОТ ОСТАЛЬНЫХ ПРОВЕРОК УВЕДОМЛЕНИЙ.
 * Соседи (`NotifyEvents0709`, `ServiceWorker0709`) проверяют, что карточка
 * СОБРАНА правильно: верный тег, верные кнопки, верный текст. Всё это было
 * зелено и при этом бесполезно, потому что не хватало двух строк:
 *
 *  1. `включитьВоркер()` не звали НИ ОТКУДА. Регистрации нет — веб-мост всегда
 *     получает от `показатьЧерезВоркер` «не смог» и показывает карточку через
 *     `new Notification`, у которого поля `actions` не существует вовсе.
 *     Браузер молча его игнорирует: кнопок нет никогда, и никакой ошибки.
 *  2. Нажатие на кнопку доезжало до вкладки — и вело в диалог, как обычный
 *     клик по телу карточки. То есть «Принять» ничего не принимал. Это хуже
 *     ненарисованной кнопки: человек уходит, считая диалог своим, а диалог
 *     через минуту достаётся коллеге.
 *
 * Оба промаха — «написано, но не подключено»: каждый отдельный кусок верен,
 * а цепочка разорвана. Ни типы, ни линтер такого не видят, поэтому проверяем
 * ПОСЛЕДСТВИЕ — ушёл ли на сервер тот запрос, ради которого кнопку нажали.
 */

const { navigateSpy } = vi.hoisted(() => ({ navigateSpy: vi.fn(async () => {}) }));
// Настоящая карта маршрутов в jsdom на переходе собирает Request, который
// undici бракует; здесь важен сам факт перехода, а не разбор дерева страниц.
vi.mock("@/app/router", () => ({ router: { navigate: navigateSpy } }));

type Слушатель = (e: MessageEvent) => void;

/** Браузер с сервис-воркером: помним регистрацию и слушателей сообщений. */
function поставитьВоркер() {
  const register = vi.fn(async () => ({ showNotification: vi.fn(async () => {}) }));
  const слушатели: Слушатель[] = [];
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      register,
      addEventListener: (тип: string, cb: Слушатель) => {
        if (тип === "message") слушатели.push(cb);
      },
      removeEventListener: () => {},
    },
  });
  return { register, слушатели };
}

/** Нажатие кнопки в карточке приходит во вкладку сообщением от воркера. */
function нажали(слушатели: Слушатель[], action: string, conversationId: string) {
  for (const cb of слушатели) {
    cb({ data: { source: "leadchat-sw", action, conversationId } } as MessageEvent);
  }
}

const GIVER = { id: "u-giver", full_name: "Анна Отдающая" };
const TAKER = { id: "u-taker", full_name: "Борис Принимающий" };

function сПредложением(): ConversationDetailDto {
  return makeConversation({
    assignee: GIVER,
    transfer: { to: TAKER, by: GIVER, at: "2026-09-07T09:00:00Z", comment: null },
  });
}

describe("Нажатие в уведомлении доводит дело до сервера (07.09)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const posts = () =>
    fetchMock.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "POST")
      .map((c) => decodeURIComponent(String(c[0])));

  beforeEach(() => {
    queryClient.clear();
    navigateSpy.mockClear();
    useActionBus.setState({ transferDecisionNonce: 0, transferDecision: null });
    useInboxStore.setState({ ids: {} } as never);
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/claim")) {
        return jsonResponse(200, { conversation: { id: url.split("/").at(-2) }, inbox_count: 0 });
      }
      return jsonResponse(200, {});
    });
    vi.stubGlobal("fetch", fetchMock);
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
  });

  afterEach(() => {
    __resetPlatform();
    __сброситьВыборы();
    __setBridge(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("⚠ воркер ставится при старте веба — иначе кнопок нет никогда", async () => {
    /*
     * ДИВЕРСИЯ: убрать `void включитьВоркер();` из `initPlatform`.
     * Тест краснеет.
     *
     * Проверяем именно ВЫЗОВ РЕГИСТРАЦИИ, а не «есть ли файл sw.js» и не «умеет
     * ли модуль регистрировать» — и то и другое было верно, пока кнопок не
     * было. Отсутствовал ровно этот вызов.
     */
    const { register } = поставитьВоркер();
    __setBridge(createWebBridge());
    await initPlatform();

    await waitFor(() =>
      expect(register, "сервис-воркер не зарегистрирован при старте").toHaveBeenCalledWith("/sw.js"),
    );
  });

  it("⚠ «Принять» из карточки очереди принимает диалог, а не просто открывает его", async () => {
    /*
     * ДИВЕРСИЯ: вернуть в `initPlatform` прежний обработчик — «любое нажатие →
     * `openConversation`». Тест краснеет: POST /claim не уходит.
     *
     * Проверяем ЗАПРОС, а не переход: переход был и до правки, он-то и создавал
     * впечатление рабочей кнопки.
     */
    const { слушатели } = поставитьВоркер();
    __setBridge(createWebBridge());
    await initPlatform();
    expect(слушатели.length, "мост не подписался на сообщения воркера").toBeGreaterThan(0);

    нажали(слушатели, "claim", "c-7");

    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/conversations/c-7/claim"))).toBe(true),
    );
  });

  it("нажатие по телу карточки по-прежнему просто открывает диалог", async () => {
    // Обратная сторона предыдущей проверки: разбор действий не должен был
    // сломать обычный клик, ради которого подписка и заводилась.
    const { слушатели } = поставитьВоркер();
    __setBridge(createWebBridge());
    await initPlatform();

    нажали(слушатели, "open", "c-9");

    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith("/chats/c-9"));
    expect(posts(), "клик по телу карточки ушёл на сервер — этого он делать не должен").toEqual([]);
  });
});

describe("Решение по передаче из уведомления исполняет та же панель", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const posts = () =>
    fetchMock.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "POST")
      .map((c) => String(c[0]));

  beforeEach(() => {
    queryClient.clear();
    useActionBus.setState({ transferDecisionNonce: 0, transferDecision: null });
    fetchMock = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    resetSessionStore({
      user: { ...fakeUser, id: TAKER.id, full_name: TAKER.full_name },
      permissions: ["messages:send", "conversations:read"] as never,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("⚠ просьба, сделанная ДО появления панели, дожидается её", async () => {
    /*
     * ДИВЕРСИЯ: убрать `<TransferDecisionFromNotify .../>` из `TransferBar`.
     * Тест краснеет.
     *
     * Порядок в тесте тот же, что в бою, и в этом весь смысл: нажатие в
     * уведомлении случается, когда диалога на экране ещё нет — панель родится
     * только после перехода. Просьба обязана ЖДАТЬ исполнителя, а не пропасть.
     */
    requestTransferDecision(CONV_ID, "accept");
    renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={сПредложением()} />);

    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/transfer/accept"))).toBe(true),
    );
  });

  it("«Отклонить» отказывается от передачи, а не принимает её", async () => {
    // Без этой проверки предыдущая зеленела бы и на панели, которая на любую
    // просьбу отвечает принятием: разницы между кнопками не было бы видно.
    requestTransferDecision(CONV_ID, "decline");
    renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={сПредложением()} />);

    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/transfer/decline"))).toBe(true),
    );
    expect(posts().some((u) => u.endsWith("/transfer/accept"))).toBe(false);
  });

  it("⚠ протухшая просьба не выстреливает по следующей передаче", async () => {
    /*
     * ДИВЕРСИЯ: убрать проверку срока (`Date.now() - просьба.в > ЖИЗНЬ_ПРОСЬБЫ_МС`).
     * Тест краснеет.
     *
     * БОЕВОЙ СЛУЧАЙ, КОТОРЫЙ ЭТИМ ЗАКРЫТ. Человек нажал «Принять» в карточке,
     * которая висела с утра, — передачу к тому времени уже отменили, панели
     * нет, исполнить просьбу некому, и она остаётся в шине. Через час тот же
     * диалог предлагают ему снова: панель рождается и принимает передачу САМА,
     * без человека и без его ведома.
     */
    useActionBus.setState({
      transferDecisionNonce: 1,
      transferDecision: { convId: CONV_ID, kind: "accept", в: Date.now() - ЖИЗНЬ_ПРОСЬБЫ_МС - 1_000 },
    });
    renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={сПредложением()} />);

    // Ждать здесь нечего — ждём отсутствие. Даём событийному циклу прокрутиться
    // столько же, сколько занял бы настоящий запрос.
    await new Promise((r) => setTimeout(r, 30));
    expect(posts(), "просьба из прошлой жизни исполнилась сама").toEqual([]);
  });

  it("просьба по ЧУЖОМУ диалогу не трогает открытый", async () => {
    requestTransferDecision("другой-диалог", "accept");
    renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={сПредложением()} />);

    await new Promise((r) => setTimeout(r, 30));
    expect(posts(), "решение уехало не в тот диалог").toEqual([]);
  });
});

describe("Подписка на события Rust не роняет остальную обвязку", () => {
  it("⚠ listenSafe не бросает без ядра Tauri", () => {
    /*
     * ДИВЕРСИЯ: убрать try/catch вокруг `listen` в `listenSafe`.
     * Тест краснеет.
     *
     * `listen` проверяет `window.__TAURI_INTERNALS__` ДО первого `await`, то
     * есть бросает синхронно — мимо `.catch`, который ловит только отказ уже
     * созданного обещания. Подпись функции при этом обещала «не бросает».
     *
     * Цена: обе подписки `wireDesktop` стоят в одной цепочке с
     * `emitSafe(APP_READY_EVENT)`, и первый бросок уносит вместе с ними
     * «слушатели навешаны» — отложенный переход по клику на тост при
     * выключенном приложении остаётся в очереди Rust навсегда.
     */
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    expect(() => listenSafe("session:refresh-needed", () => {})).not.toThrow();
  });
});
