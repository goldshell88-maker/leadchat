import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { CLIENT_FALLBACK } from "@/features/templates/vars";
import { MOBILE_MAX } from "@/shared/lib/breakpoints";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread } from "./render";

/**
 * ПРАВАЯ КОЛОНКА — КАРТОЧКА АТРИБУТОВ, А НЕ ПУЛЬТ (правка 10 от 12 августа).
 *
 * ЧТО БЫЛО. Владелец разобрал живой экран и насчитал ОДНО И ТО ЖЕ действие в
 * ЧЕТЫРЁХ местах сразу: иконки в шапке ленты, кнопки в правой карточке, нижняя
 * плашка и ховер-иконка в строке списка слева. Причём одно действие называлось
 * двумя именами — «Принять диалог» внизу и «Взять в работу» в карточке — при
 * том что это один и тот же `POST /claim`. На широком экране (от 1360px)
 * карточка стоит третьей колонкой постоянно, то есть её кнопки и иконки шапки
 * человек видел ОДНОВРЕМЕННО, в трёхстах пикселях друг от друга.
 *
 * ЦЕНА. Тринадцать диспетчеров, пришедших из Jivo, каждый раз выбирают, каким
 * из четырёх способов сделать одно и то же, — и половина этих способов ведёт
 * себя по-разному в мелочах (одна кнопка спрашивает результат, другая нет).
 *
 * ЧТО ЗАКРЕПЛЕНО ЗДЕСЬ. Правая колонка ничего не ДЕЛАЕТ: она отвечает на
 * вопросы «кто это», «о чём заявка», «что с диалогом». Статус и ответственный
 * остаются ПОЛЯМИ — значение правится на месте, это не кнопка-дубль.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел поимённо —
 * список в ответе воркера):
 *  - вернули в карточку кнопки «Взять в работу» / «Передать» / «Позвать» —
 *    падает «ни одной кнопки-действия»;
 *  - вернули «snoozed» в STATUS_ORDER — падает «отложки в карточке нет вовсе»;
 *  - вернули `<Text className="card-section__name">` — падает «имени нет на
 *    широком экране»;
 *  - убрали `isMobile` из условия имени — падает «на телефоне имя есть»;
 *  - вернули «Ничей» в StatusContext — падает «ответственный назван один раз»;
 *  - вернули строку «Первое обращение клиента» — падает «первый раз — чипом»;
 *  - вернули заметкам собственную `section.card-section` — падает «заметки
 *    внутри „Хода диалога“»;
 *  - вернули заголовки «Объявление» и «Диалог» — падает «блоки названы по
 *    смыслу».
 */

/** Диалог без ответственного и без часов — на нём кнопок было больше всего. */
function freeConversation(over: Partial<ConversationDetailDto> = {}): ConversationDetailDto {
  return makeConversation({
    status: "new",
    assignee: null,
    in_inbox: true,
    offered_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    ...over,
  });
}

/**
 * Ширина окна для `useMediaQuery`. Разбираем строку запроса так же, как
 * clientCardOverlay.test.tsx: jsdom медиазапросы не считает, а карточка решает
 * судьбу имени именно по ним.
 */
function stubViewport(width: number) {
  vi.stubGlobal("matchMedia", (query: string) => {
    const max = Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? Infinity);
    return {
      matches: width <= max,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  });
}

describe("Карточка клиента — ни одного дубля (правка 10)", () => {
  /** Что отдаёт `GET /client-history` — тесты его подменяют. */
  let history: { client: Record<string, unknown>; items: unknown[] };

  beforeEach(() => {
    history = { client: { id: "client-1", name: "Иван Петров" }, items: [] };
    queryClient.clear();
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("client-history")) return jsonResponse(200, history);
        if (url.includes("/users/assignable")) return jsonResponse(200, { items: [] });
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function renderCard(
    conversation: ConversationDetailDto,
    permissions: Permission[] = fakeMe.permissions as Permission[],
    role: "admin" | "head" | "manager" | "observer" = "manager",
  ) {
    resetSessionStore({
      user: { ...fakeUser, role },
      permissions,
      accessToken: "t",
      bootstrapped: true,
    });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conversation);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  /**
   * Видимое поле селекта «Статус». Рядом с ним Mantine держит скрытый input с
   * тем же `aria-label`, поэтому поиск по ярлыку находит два узла.
   */
  function statusInput(): HTMLInputElement {
    const input = document.querySelector<HTMLInputElement>(
      'input[aria-label="Статус диалога"]:not([type="hidden"])',
    );
    if (!input) throw new Error("поле «Статус» не найдено");
    return input;
  }

  /** Блок карточки по его заголовку. */
  function section(title: string): HTMLElement {
    const found = screen
      .getByText(title, { selector: ".card-section__title" })
      .closest("section");
    if (!found) throw new Error(`блок «${title}» не найден`);
    return found;
  }

  it("ни одной кнопки-действия над диалогом: они живут в шапке и нижней плашке", () => {
    /*
     * Берём худший случай — свободный новый диалог у менеджера с полными
     * правами: раньше здесь показывались разом «Взять в работу», «Передать» и
     * «Позвать», а у своего диалога добавлялась «Отложить до…» (её не стало
     * вместе со всей отложкой 12 августа, но регулярка оставлена — вернуться
     * кнопка может и без статуса).
     */
    renderCard(freeConversation());

    for (const name of [/Взять в работу/, /^Передать$/, /^Позвать$/, /Отложить до/]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }

    // А ПОЛЯ НА МЕСТЕ: карточка перестала быть пультом, но не перестала быть
    // рабочей. Без этой половины теста «уберите всё» прошло бы как правка.
    // Ищем видимый ввод селекта: Mantine кладёт рядом ещё и скрытый с тем же
    // ярлыком, и запрос по ярлыку находит оба.
    expect(statusInput()).not.toBeNull();
    expect(screen.getByText("Ответственный")).toBeInTheDocument();
  });

  it("отложки в карточке нет ВОВСЕ: ни пункта в «Статусе», ни окна срока", async () => {
    /*
     * ПЕРЕВЁРНУТЫЙ ТЕСТ. Здесь проверялось обратное — что «Отложить до…» не
     * потерялась при уборке кнопок (правка 10) и по-прежнему открывается через
     * пункт «Отложен» в селекте. 12 августа владелец снял отложку целиком, и
     * теперь охранять надо ровно противоположное.
     *
     * Проверяется ИМЕННО ВЫПАДАЮЩИЙ СПИСОК, а не словарь: словарь сторожит
     * `statusWaiting.test.ts`, а здесь важно, что пункты селекта собираются из
     * него, а не выписаны рядом своей копией. Такая копия — самый вероятный
     * способ вернуть отложку случайно.
     */
    const user = userEvent.setup();
    renderCard(makeConversation({ status: "in_progress" }));

    await user.click(statusInput());
    // Дожидаемся раскрытия по заведомо живому пункту — иначе пустой список
    // прошёл бы проверку ниже просто потому, что не успел отрисоваться.
    expect(await screen.findByRole("option", { name: "Закрыт" })).toBeInTheDocument();

    expect(screen.queryByRole("option", { name: "Отложен" })).toBeNull();
    expect(screen.queryByText("Отложить диалог")).toBeNull();
  });

  it("имя клиента дублируется в карточке (решение владельца 17.08)", async () => {
    stubViewport(1440);
    renderCard(makeConversation());

    await waitFor(() => expect(screen.getByText("Клиент")).toBeInTheDocument());
    expect(screen.queryAllByText("Иван Петров").length).toBeGreaterThan(0);
  });

  it("на телефоне имя есть: там карточка накрывает шапку собой", async () => {
    /*
     * Ниже 768px `.client-card--overlay` ложится поверх всего, кроме левой
     * рельсы, — шапка ленты вместе с именем оказывается под карточкой. Убери мы
     * имя и здесь, диспетчер на телефоне читал бы карточку человека, чьё имя на
     * экране не написано нигде.
     */
    stubViewport(MOBILE_MAX);
    renderCard(makeConversation());

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
  });

  it("на телефоне у безымянного клиента заглушка стоит на месте имени", () => {
    stubViewport(MOBILE_MAX);
    renderCard(
      makeConversation({
        client: { id: "client-1", name: null, phone: null, avito_rating: null },
      }),
    );

    const name = document.querySelector(".card-section__name");
    expect(name).not.toBeNull();
    expect(name).toHaveTextContent(CLIENT_FALLBACK);
  });

  it("ответственный назван один раз: «Ничей» из строки статуса убран", async () => {
    /*
     * Было три строки подряд об одном: «Статус: Новый», под ним «Ничей», под
     * ним поле «Ответственный» со словом «Без ответственного». Одно значение,
     * показанное дважды разными словами, заставляет искать между ними разницу,
     * которой нет.
     */
    renderCard(freeConversation(), fakeMe.permissions as Permission[], "admin");

    expect(await screen.findByText(/В очереди 5 мин/)).toBeInTheDocument();
    expect(screen.queryByText(/Ничей/)).toBeNull();
  });

  it("строка под статусом даёт длительность, а не повтор статуса и ответственного", () => {
    renderCard(
      makeConversation({
        status: "in_progress",
        status_since: new Date(Date.now() - 12 * 60_000).toISOString(),
      }),
    );

    expect(screen.getByText("Уже 12 мин")).toBeInTheDocument();
    // Ни «В работе» (это слово стоит в поле «Статус» строкой выше), ни имени
    // держателя (оно в поле «Ответственный» строкой ниже).
    expect(screen.queryByText(/В работе у/)).toBeNull();
  });

  it("первое обращение — чипом, а не строкой без данных", async () => {
    renderCard(makeConversation({ client_conversations_count: 1 }));

    expect(await screen.findByText("Впервые у нас")).toBeInTheDocument();
    expect(screen.queryByText("Первое обращение клиента")).toBeNull();
  });

  it("чипа «Впервые у нас» нет, пока история не пришла и когда обращений больше одного", async () => {
    /*
     * Дефект SCEN-15: история отдаётся БЕЗ текущего диалога, и одной её пустоты
     * мало — вернувшийся в тот же чат клиент объявлялся новичком. Второй
     * признак — счётчик обращений — этот случай ловит.
     */
    renderCard(makeConversation({ client_conversations_count: 3 }));

    // Ждём именно ОТВЕТА сервера, а не просто отрисовки: пока история в пути,
    // чипа нет по любой причине, и проверять в этот момент нечего. Признак
    // ответа — исчезнувший блок истории: он рисуется, пока крутится загрузка,
    // и пропадает, когда пришёл пустой список.
    await waitFor(() => expect(document.querySelector(".card-client-history")).toBeNull());
    expect(screen.queryByText("Впервые у нас")).toBeNull();
  });

  it("заметки — строкой внутри «Хода диалога», а не четвёртой карточкой", async () => {
    renderCard(makeConversation());

    const toggle = await screen.findByRole("button", { name: /Заметки \(0\)/ });
    // Внутри блока «Ход диалога» — то есть у заметок больше нет своей карточки
    // с рамкой, тенью и полями ради нуля.
    expect(within(section("Ход диалога")).getByRole("button", { name: /Заметки \(0\)/ })).toBe(toggle);
    expect(document.querySelectorAll("section.card-section")).toHaveLength(3);
  });

  it("блоки названы по смыслу: «Клиент» / «Заявка» / «Ход диалога»", () => {
    renderCard(makeConversation());

    const titles = Array.from(document.querySelectorAll(".card-section__title")).map(
      (n) => n.textContent,
    );
    expect(titles).toEqual(["Клиент", "Заявка", "Ход диалога"]);
  });
});
