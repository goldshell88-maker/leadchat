import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { notifications } from "@mantine/notifications";
import { queryClient } from "@/app/queryClient";
import { __resetPlatform, initPlatform } from "@/platform";
import { __setBridge } from "@/platform/bridge";
import { __сброситьВыборы } from "@/platform/наЭкранеЛи";
import { toastForInbox } from "@/platform/toast";
import { createWebBridge } from "@/platform/web";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ВТОРАЯ КНОПКА В КАРТОЧКЕ ОЧЕРЕДИ — «Отклонить» (07.09, просьба владельца
 * дословно: «чтобы можно было принять его или отклонить там же в уведомлении»).
 *
 * ⚠ ЧТО ИМЕННО ЗДЕСЬ СТЕРЕЖЁТСЯ И ПОЧЕМУ ИМЕННО ЭТО. Кнопку легко нарисовать и
 * невозможно заметить, что она ничего не делает: карточка выглядит рабочей,
 * нажатие доезжает до вкладки и молча теряется — ровно та беда, которую 07.09
 * уже ловили у «Принять». Поэтому проверяется ПОСЛЕДСТВИЕ нажатия — какой
 * запрос ушёл на сервер, — а не разметка карточки.
 *
 * Отдельная забота — не перепутать два отказа. `POST /conversations/{id}/decline`
 * прячет диалог из моей очереди; `POST /conversations/{id}/transfer/decline`
 * возвращает передачу передавшему. Оба зовутся «Отклонить», оба живут в одном
 * `switch`, и ошибка выбора ветки была бы тихой: человек нажал одно, случилось
 * другое.
 */

const { navigateSpy } = vi.hoisted(() => ({ navigateSpy: vi.fn(async () => {}) }));
// Настоящая карта маршрутов в jsdom на переходе собирает Request, который
// undici бракует; здесь важен сам факт перехода, а не разбор дерева страниц.
vi.mock("@/app/router", () => ({ router: { navigate: navigateSpy } }));

type Слушатель = (e: MessageEvent) => void;

/** Браузер с сервис-воркером: помним слушателей сообщений от него. */
function поставитьВоркер() {
  const слушатели: Слушатель[] = [];
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      register: vi.fn(async () => ({ showNotification: vi.fn(async () => {}) })),
      addEventListener: (тип: string, cb: Слушатель) => {
        if (тип === "message") слушатели.push(cb);
      },
      removeEventListener: () => {},
    },
  });
  return { слушатели };
}

/** Нажатие кнопки в карточке приходит во вкладку сообщением от воркера. */
function нажали(слушатели: Слушатель[], action: string, conversationId: string) {
  for (const cb of слушатели) {
    cb({ data: { source: "leadchat-sw", action, conversationId } } as MessageEvent);
  }
}

const ОТКАЗ = {
  conversation_id: "c-8",
  declined: true,
  already_declined: false,
  reason: null,
  escalated_now: false,
  count: 3,
  escalated: 0,
};

describe("Карточка очереди: «Принять» и «Отклонить»", () => {
  let вМост: ReturnType<typeof makeFakeTauriBridge>["calls"]["notify"];

  beforeEach(() => {
    const fake = makeFakeTauriBridge();
    enterTauriRuntime(fake.bridge);
    useInboxStore.setState({ ids: {}, count: 2 } as never);
    вМост = fake.calls.notify;
  });

  afterEach(() => {
    leaveTauriRuntime();
    useInboxStore.getState().clear();
  });

  /**
   * ДИВЕРСИЯ (прогнана): в `toastForInbox` (platform/toast.ts) убрать из
   * `actions` вторую строку `{ action: "inbox-decline", title: "Отклонить" }`
   * — вернуть карточку с одной кнопкой, как было до 07.09. Тест краснеет.
   */
  it("карточка несёт обе кнопки, и «Отклонить» — это inbox-decline", () => {
    toastForInbox("c-8", 3);

    expect(вМост).toHaveBeenCalledTimes(1);
    expect(вМост.mock.calls[0][0].actions).toEqual([
      { action: "claim", title: "Принять" },
      // ⚠ Имя отдельное от `decline` (отказ от ПЕРЕДАЧИ) намеренно: это разные
      // ручки сервера, и общее имя однажды отказалось бы не от того.
      { action: "inbox-decline", title: "Отклонить" },
    ]);
  });
});

describe("Нажатие «Отклонить» в карточке очереди доводит дело до сервера", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let показанные: Parameters<typeof notifications.show>[0][];
  let declineStatus = 200;

  const posts = () =>
    fetchMock.mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "POST")
      .map((c) => decodeURIComponent(String(c[0])));

  beforeEach(() => {
    queryClient.clear();
    navigateSpy.mockClear();
    declineStatus = 200;
    показанные = [];
    useInboxStore.setState({ ids: {}, count: 2 } as never);
    vi.spyOn(notifications, "show").mockImplementation((opts) => {
      показанные.push(opts);
      return String(opts.id ?? "id");
    });
    vi.spyOn(notifications, "hide").mockImplementation((id) => String(id));
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/decline/undo")) {
        return jsonResponse(200, { ...ОТКАЗ, declined: false, count: 4 });
      }
      if (url.includes("/decline")) {
        return declineStatus === 200
          ? jsonResponse(200, ОТКАЗ)
          : jsonResponse(
              409,
              errorEnvelope("already_claimed", "Диалог уже принят: Пётр Ковалёв", {
                reason: "already_claimed",
                claimed_by: { id: "u-petr", full_name: "Пётр Ковалёв" },
                mine: false,
              }),
            );
      }
      if (url.includes("/claim")) {
        return jsonResponse(200, { conversation: { id: "c-8" }, count: 1, escalated: 0 });
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
    useInboxStore.getState().clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function поднятьВеб() {
    const { слушатели } = поставитьВоркер();
    __setBridge(createWebBridge());
    await initPlatform();
    expect(слушатели.length, "мост не подписался на сообщения воркера").toBeGreaterThan(0);
    return слушатели;
  }

  /**
   * ДИВЕРСИЯ (прогнана): в `исполнить` (platform/index.ts) убрать ветку
   * `case "inbox-decline"` — нажатие снова падает в `default` и просто
   * открывает диалог, как было до 07.09. Тест краснеет: POST /decline нет.
   */
  it("⚠ «Отклонить» отклоняет диалог очереди, а не просто открывает его", async () => {
    const слушатели = await поднятьВеб();

    нажали(слушатели, "inbox-decline", "c-8");

    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/conversations/c-8/decline"))).toBe(true),
    );
  });

  /**
   * Обратная сторона предыдущей проверки, и без неё та зеленела бы на любой
   * ветке, где отказ вообще куда-нибудь уходит.
   *
   * ДИВЕРСИЯ (прогнана): в `исполнить` дописать `case "inbox-decline":` к
   * ветке `"accept" | "decline"` — отказ от очереди поедет в шину передачи.
   * Тест краснеет.
   */
  it("это отказ от ОЧЕРЕДИ, а не от передачи и не приём", async () => {
    const слушатели = await поднятьВеб();

    нажали(слушатели, "inbox-decline", "c-8");

    await waitFor(() => expect(posts().length).toBeGreaterThan(0));
    expect(posts().some((u) => u.includes("/transfer/")), "ушёл отказ от передачи").toBe(false);
    expect(posts().some((u) => u.endsWith("/claim")), "отказ обернулся приёмом").toBe(false);
  });

  /**
   * ДИВЕРСИЯ (прогнана): в `исполнить` заменить в ветке `case "claim"` вызов
   * `claimFromQueue` на `declineFromQueue` — разбор перепутал бы ветки, а
   * карточка выглядела бы прежней. Тест краснеет.
   */
  it("«Принять» по-прежнему принимает, а не отклоняет", async () => {
    const слушатели = await поднятьВеб();

    нажали(слушатели, "claim", "c-8");

    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/conversations/c-8/claim"))).toBe(true),
    );
    expect(posts().some((u) => u.endsWith("/decline")), "приём обернулся отказом").toBe(false);
  });

  /**
   * ⚠ ПРОМАХ КНОПКОЙ В КАРТОЧКЕ НЕ СТРАШНЕЕ ПРОМАХА КЛАВИШЕЙ — но только пока
   * «Вернуть» есть на обоих путях. Кнопки в карточке мелкие и приходят внезапно
   * поверх чужого окна; отказ из уведомления без возврата был бы единственным
   * необратимым путём из трёх.
   *
   * Проверяем не наличие слова на экране, а РАБОТУ возврата: нажимаем «Вернуть»
   * и ждём `POST /decline/undo`.
   *
   * ДИВЕРСИЯ (прогнана): в `отклоненоУспешно` (useInbox.ts) заменить
   * `showUndoToast` на обычный `showToast` с тем же заголовком — то есть
   * завести отказу из уведомления свою обвязку без возврата. Тест краснеет.
   */
  it("⚠ отказ из уведомления оставляет «Вернуть», и оно работает", async () => {
    const user = userEvent.setup();
    const слушатели = await поднятьВеб();

    нажали(слушатели, "inbox-decline", "c-8");

    await waitFor(() => expect(показанные.some((o) => o.title === "Диалог отклонён")).toBe(true));
    const тост = показанные.find((o) => o.title === "Диалог отклонён");
    render(<>{тост?.message}</>);
    await user.click(screen.getByRole("button", { name: "Вернуть" }));

    await waitFor(() =>
      expect(posts().some((u) => u.endsWith("/conversations/c-8/decline/undo"))).toBe(true),
    );
  });

  /**
   * ⚠ РЕШЕНИЕ, ПРИНЯТОЕ ЯВНО (07.09): диалог, забранный коллегой за секунду до
   * нажатия, — обычный исход, а не поломка. Молчать нельзя (кнопка читалась бы
   * сломанной), красное «Не получилось отклонить» — тоже: человек хотел, чтобы
   * диалог ушёл из его очереди, и диалог ушёл. Говорим спокойно и тем же
   * текстом, каким встречают опоздавшего с «Принять».
   *
   * ДИВЕРСИЯ (прогнана): в `отказНеПрошёл` (useInbox.ts) убрать ветку
   * `claimedByFrom` — 409 снова показывался бы красным «Не получилось
   * отклонить». Тест краснеет.
   */
  it("диалог успел забрать коллега — спокойный тост с его именем, а не тишина", async () => {
    declineStatus = 409;
    const слушатели = await поднятьВеб();

    нажали(слушатели, "inbox-decline", "c-8");

    await waitFor(() => expect(показанные.length).toBeGreaterThan(0));
    expect(показанные[0]).toMatchObject({
      title: "Диалог уже занят",
      message: "Диалог принял Пётр Ковалёв",
    });
    // Красный тон здесь описывал бы поломку, которой не было.
    expect(показанные[0].color).not.toBe("red");
  });
});
