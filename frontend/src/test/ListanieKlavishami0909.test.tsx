/**
 * ЛИСТАНИЕ ДИАЛОГОВ — КЛАВИШАМИ, И ЭТО ЕДИНСТВЕННЫЙ ЕГО СПОСОБ В ШАПКЕ ЛЕНТЫ.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «Убери эти стрелочки и сделай возможно
 * задавать вместо них клавиши, чтобы листать диалоги вниз и вверх по нажатию
 * клавиш».
 *
 * Файл заменил собой прежний `ThreadPager0509`, стороживший те самые шевроны
 * (просьба 05.09 «листать можно только во входящих, нужно везде»). Просьбы не
 * противоречат друг другу: 05.09 требовала листать НА ЛЮБОЙ ВКЛАДКЕ, 09.09 —
 * листать БЕЗ КНОПОК. Умение осталось, орган сменился, и здесь стережётся
 * ровно это.
 *
 * ⚠ ТРИ УМЕНИЯ ПЕРЕЕХАЛИ С КНОПКИ НА КЛАВИШУ, И КАЖДОЕ ПРОВЕРЯЕТСЯ ОТДЕЛЬНО:
 *   1. шаг по ВИДИМОМУ списку — тому же, что видит глаз;
 *   2. догрузка следующей страницы на границе загруженного (ниже 768px левой
 *      колонки нет вовсе, и подтянуть страницу прокруткой некому);
 *   3. честный край: идти некуда — нажатие НЕ съедается, а достаётся браузеру.
 *      Прежняя арифметика `Math.min(at + 1, rows.length - 1)` на последней
 *      строке «уводила» в уже открытый диалог и возвращала true.
 *
 * ⚠ ПРОВЕРЯЕМ РЕНДЕРОМ, А НЕ РАСЧЁТОМ. В этом проекте дважды случалось
 * «написано, но не подключено», поэтому монтируется настоящая шапка ленты и
 * нажимаются настоящие клавиши, а адрес читается из роутера. Там, где рендером
 * не отличить «шаг в себя» от «ничего не делали», проверка спускается на
 * уровень `runAction` — и это оговорено на месте.
 */
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";
import { runAction } from "@/features/hotkeys/dispatch";
import { useChatHotkeys } from "@/features/hotkeys/useChatHotkeys";

function строка(id: string): ConversationDto {
  return {
    id,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `cl-${id}`, name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: null,
    last_message: null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-05T10:00:00Z",
    in_inbox: false,
    offered_at: null,
  };
}

/** Выдача обычного списка: `total` больше загруженного — список не долистан. */
function список(ids: string[], total = ids.length) {
  queryClient.setQueryData(qk.conversations.list(useChatUiStore.getState().filters), {
    pages: [{ items: ids.map(строка), page: { limit: 50, offset: 0, total } }],
    pageParams: [0],
  });
}

/** Очередь: строки в кэше + счётчик в сторе, как после `GET /inbox`. */
function очередь(ids: string[]) {
  useChatUiStore.setState({ inboxOpen: true });
  useInboxStore.setState({
    count: ids.length,
    escalated: 0,
    ids: Object.fromEntries(ids.map((id) => [id, true])),
  });
  queryClient.setQueryData(qk.inbox.list, {
    pages: [
      {
        items: ids.map((id) => ({ ...строка(id), in_inbox: true, offered_at: "2026-09-05T09:00:00Z" })),
        page: { limit: 50, offset: 0, total: ids.length },
      },
    ],
    pageParams: [0],
  });
}

/*
 * Имя латинское: `react-hooks/rules-of-hooks` узнаёт компонент по заглавной
 * ЛАТИНСКОЙ букве — то же соглашение, что у `useDialogPin` в самом коде.
 */
function LocationProbe() {
  const { pathname } = useLocation();
  /*
   * ⚠ АКТИВНЫЙ ДИАЛОГ СЛЕДУЕТ ЗА АДРЕСОМ, А НЕ НАОБОРОТ. В приложении это
   * делает `ChatsPage.tsx:105` (`setActive(id ?? null)`), и источник истины —
   * URL. Без этой строки второй шаг подряд считался бы от ПЕРВОГО диалога:
   * адрес уехал, а стор остался, и Ctrl+↑ после Ctrl+↓ приводил не туда,
   * откуда пришли.
   */
  useEffect(() => {
    const id = pathname.startsWith("/chats/") ? pathname.slice("/chats/".length) : null;
    useChatUiStore.getState().setActive(id || null);
  }, [pathname]);
  return <div data-testid="адрес">{pathname}</div>;
}

/*
 * ⚠ СЛУШАТЕЛЬ КЛАВИШ ЖИВЁТ НЕ В ЛЕНТЕ. `useChatHotkeys()` подключён в
 * `ChatsPage` (выше ветки «телефон/десктоп»), а сама лента о нём не знает.
 * Смонтировав только `ChatThreadPane`, мы получили бы экран без единого
 * обработчика — и все проверки ниже зеленели бы или краснели по причине,
 * никак не связанной с листанием. Поднимаем хук отдельным узлом рядом, ровно
 * как это делает `hotkeys.test.tsx`.
 *
 * ⚠ ИМЯ ЛАТИНСКОЕ, И ЭТО НЕ ПРИХОТЬ — то же соглашение, что у `LocationProbe`
 * выше и у `useDialogPin` в самом коде: `react-hooks/rules-of-hooks` узнаёт
 * компонент по ЗАГЛАВНОЙ ЛАТИНСКОЙ букве, а для кириллического имени перестаёт
 * считать функцию компонентом — и вызов хука внутри читается как хук вне
 * компонента.
 */
function HotkeyHost() {
  useChatHotkeys();
  return null;
}

function открыть(convId: string) {
  queryClient.setQueryData(qk.messages.list(convId), {
    pages: [
      {
        items: [],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
  useChatUiStore.setState({ activeConversationId: convId });
  return renderWithProviders(
    <>
      <HotkeyHost />
      <ChatThreadPane convId={convId} />
      <LocationProbe />
    </>,
    { route: `/chats/${convId}` },
  );
}

/** Нажатие в документ — так же, как его получает слушатель на `window`. */
async function нажать(code: "ArrowDown" | "ArrowUp"): Promise<void> {
  await userEvent.keyboard(`{Control>}{${code}}{/Control}`);
}

describe("Листание диалогов клавишами", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, inboxOpen: false, drafts: {} });
    useInboxStore.setState({ count: 0, escalated: 0, ids: {} });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("шевронов «предыдущий/следующий» в шапке ленты больше нет", () => {
    список(["m-1", "m-2", "m-3"]);
    открыть("m-2");

    /*
     * ⚠ СНАЧАЛА ДОКАЗЫВАЕМ, ЧТО ШАПКА ВООБЩЕ ОТРИСОВАНА. Без этой строки
     * проверка зеленела бы и от того, что шапки на экране нет (она рисуется
     * только при `head` — детали в кэше или строке списка), то есть стерегла бы
     * не удаление кнопок, а собственную сломанную оснастку.
     */
    expect(screen.getByText("Ольга Никитина")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Следующий диалог" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Предыдущий диалог" })).toBeNull();
  });

  it("Ctrl+↓ и Ctrl+↑ ведут к соседям по видимому списку — без всякой настройки", async () => {
    /*
     * «Без всякой настройки» — это и есть половина просьбы: кнопок не стало,
     * значит клавиша обязана работать у того, кто её не включал. Диверсия:
     * вернуть `offByDefault: true` паре listNext/listPrev — обе проверки ниже
     * краснеют.
     */
    список(["m-1", "m-2", "m-3"]);
    открыть("m-2");

    await нажать("ArrowDown");
    await waitFor(() => expect(screen.getByTestId("адрес")).toHaveTextContent("/chats/m-3"));

    await нажать("ArrowUp");
    await waitFor(() => expect(screen.getByTestId("адрес")).toHaveTextContent("/chats/m-2"));
  });

  it("список не долистан — клавиша тянет страницу и идёт к первой новой строке", async () => {
    /*
     * ⚠ САМОЕ ДОРОГОЕ УМЕНИЕ КНОПКИ, И ОНО ОБЯЗАНО БЫЛО ПЕРЕЕХАТЬ. Ниже 768px
     * левой колонки на экране нет вовсе (`ChatsPage`: телефон — стек из двух
     * экранов), и подтянуть страницу прокруткой некому. Клавиша, вставшая на
     * пятидесятой строке, соврала бы «список кончился» при пятистах диалогах.
     *
     * ДИВЕРСИЯ: вернуть в `шагПоСписку` расчёт `Math.min(at + 1, rows.length - 1)`
     * — проверка краснеет на строке про offset=2.
     */
    список(["m-1", "m-2"], 4);
    const запросы: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        запросы.push(String(url));
        if (String(url).includes("/conversations?")) {
          return Promise.resolve(
            jsonResponse(200, {
              items: [строка("m-3"), строка("m-4")],
              page: { limit: 50, offset: 2, total: 4 },
            }),
          );
        }
        return new Promise(() => {});
      }),
    );
    открыть("m-2");

    await нажать("ArrowDown");

    await waitFor(() => expect(screen.getByTestId("адрес")).toHaveTextContent("/chats/m-3"));
    expect(запросы.some((u) => u.includes("offset=2"))).toBe(true);
  });

  it("список кончился — нажатие не съедено и в себя не уводит", () => {
    /*
     * ⚠ ПРОВЕРКА СПУСКАЕТСЯ НА `runAction`, И ЭТО НЕ ЛЕНЬ. Рендером «шаг в
     * себя» неотличим от «ничего не делали»: адрес в обоих случаях прежний.
     * Различает их ровно два признака — звали ли `navigate` и что вернул
     * разбор. Прежний код возвращал `true` (нажатие съедено) и звал navigate на
     * уже открытый диалог; теперь `false`, и клавиша достаётся браузеру.
     *
     * ДИВЕРСИЯ: вернуть `Math.min(at + 1, rows.length - 1)` — `сработало`
     * становится true, `куда` перестаёт быть пустым.
     */
    список(["m-1", "m-2"]); // total = 2, обе строки загружены
    useChatUiStore.setState({ activeConversationId: "m-2" });
    const куда: string[] = [];
    const сработало = runAction("listNext", {
      navigate: ((p: string) => куда.push(p)) as never,
      rows: () => [
        { id: "m-1", unread_count: 0 },
        { id: "m-2", unread_count: 0 },
      ],
      can: () => true,
    });

    expect(сработало, "нажатие на краю списка съедено впустую").toBe(false);
    expect(куда, "клавиша увела в уже открытый диалог").toEqual([]);
  });

  it("открытого диалога в видимом списке нет — никуда не уводим", async () => {
    /*
     * Так выглядит приход из «Разбора диалогов» и переход по прямой ссылке:
     * там свои фильтры, и открытый диалог в левую колонку не входит. Прыжок на
     * первую строку был бы переходом, которого человек не просил, — тот самый
     * «чат сам переключился на другого», что уже разбирали дважды.
     */
    список(["m-1", "m-2", "m-3"]);
    queryClient.setQueryData(qk.conversations.list({ tab: "mine" }), {
      pages: [{ items: [строка("из-разбора")], page: { limit: 50, offset: 0, total: 1 } }],
      pageParams: [0],
    });
    открыть("из-разбора");

    await нажать("ArrowDown");
    await нажать("ArrowUp");
    expect(screen.getByTestId("адрес")).toHaveTextContent("/chats/из-разбора");
  });

  it("во «Входящих» конвейер очереди остался и он один", async () => {
    очередь(["q-1", "q-2", "q-3"]);
    открыть("q-2");

    // Конвейер — с числом «столько осталось разобрать». Второй кнопки
    // «следующий» рядом нет: два разных ответа на один вопрос — это то, от
    // чего лечились.
    expect(screen.getAllByRole("button", { name: /Следующий/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Следующий (2)" })).toBeInTheDocument();

    // А назад по очереди ходит обычный шаг клавишей: возврат к тому, кого
    // только что пролистнули.
    await нажать("ArrowUp");
    await waitFor(() => expect(screen.getByTestId("адрес")).toHaveTextContent("/chats/q-1"));
  });

  it("с недописанным ответом клавиша диалог не меняет", async () => {
    /*
     * Замок «с недописанного ответа уводит только человек» был и раньше, но до
     * 09.09 его обходила кнопка: нажатие мышью считалось осознанным выбором.
     * Кнопки нет, обхода нет — и замок обязан остаться единственным правилом,
     * иначе набранный ответ теряется от случайного Ctrl+↓.
     */
    список(["m-1", "m-2", "m-3"]);
    useChatUiStore.setState({ drafts: { "m-2": { text: "уже написал половину ответа", isNote: false } } });
    открыть("m-2");

    await нажать("ArrowDown");
    expect(screen.getByTestId("адрес")).toHaveTextContent("/chats/m-2");
  });
});
