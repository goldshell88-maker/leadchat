import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { ChatsPage } from "@/features/chats/ChatsPage";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { NARROW_MAX, MOBILE_MAX } from "@/shared/lib/breakpoints";
import { RAIL_WIDTH_EXPANDED, useRailStore } from "@/shared/stores/railStore";
import { renderWithProviders } from "./render";

/**
 * ИЗ-ПОД ОВЕРЛЕЯ КАРТОЧКИ КЛИЕНТА ЕСТЬ ВЫХОД.
 *
 * ЧТО БЫЛО. Ниже 1360px карточка ложится `position: fixed` на правые 380
 * пикселей. Замеры со стенда (1280px, диалог из очереди): карточка занимает
 * x 900–1280, а под ней остаются «Принять диалог» (x 1009–1150) и «Отклонить»
 * (x 1158–1264) — `document.elementFromPoint` в этих точках отдаёт содержимое
 * карточки. Накрыты и все действия шапки, включая саму кнопку «Клиент»,
 * которой карточку открыли. Ни подложки, ни кнопки закрытия — единственным
 * выходом оставался Esc, о котором на экране не написано нигде. Диалог из
 * очереди становилось невозможно взять.
 *
 * ЧТО ПРОВЕРЯЕМ. Подложка есть, она закрывает карточку по клику и лежит В
 * РАЗМЕТКЕ ПЕРЕД карточкой — слои у них одинаковые, и порядок решает именно
 * разметка: встань подложка после, она накрыла бы карточку собой.
 */

vi.mock("@/features/chats/components/list/ChatListPane", () => ({
  ChatListPane: () => <div data-testid="list-pane" />,
}));
vi.mock("@/features/chats/components/thread/ChatThreadPane", () => ({
  ChatThreadPane: () => <div data-testid="thread-pane" />,
}));
vi.mock("@/features/chats/components/card/ClientCardPane", () => ({
  ClientCardPane: ({ overlay }: { overlay?: boolean }) => (
    <aside data-testid="client-card" data-overlay={overlay ? "true" : undefined} />
  ),
}));

/**
 * Окно заданной ширины.
 *
 * ⚠ ШИРИНА ОКНА — НЕ ШИРИНА РАБОЧЕГО МЕСТА, И ИМЕННО ЗА ЭТО СТОРОЖ ТЕПЕРЬ
 * ОТВЕЧАЕТ. Узкую раскладку решает замер `.chats-page`, а слева от неё стоит
 * рельса: развёрнутая забирает 218 пикселей. В jsdom раскладки нет вовсе —
 * `getBoundingClientRect` отдаёт нули, — и `useWorkspaceWidth` честно уходит
 * на оценку «окно минус рельса». То есть здесь проверяется ровно та
 * арифметика, из-за которой три колонки показывались на 398-пиксельной ленте;
 * САМУ раскладку jsdom не считает, её замеряли на стенде.
 */
function stubViewport(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
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

function renderChatsPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/chats/:id" element={<ChatsPage />} />
    </Routes>,
    { route: "/chats/conv-1" },
  );
}

/** Карточку открывают ПОСЛЕ монтирования: на смену диалога оверлей гасится. */
function openCard() {
  act(() => useChatUiStore.getState().setClientCardOpen(true));
}

describe("Оверлей карточки клиента", () => {
  beforeEach(() => {
    useChatUiStore.setState({ clientCardOpen: false });
    // Рельса развёрнута по умолчанию (railStore), и предыдущая проверка могла
    // её свернуть: сохранение живёт в localStorage и переживает cleanup.
    useRailStore.getState().set(true);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("на узком экране закрывается кликом мимо — подложка есть и работает", async () => {
    const user = userEvent.setup();
    stubViewport(NARROW_MAX); // 1359 — карточка уже оверлеем, экран ещё не телефон
    renderChatsPage();
    openCard();

    const backdrop = screen.getByRole("button", { name: "Закрыть карточку клиента" });
    await user.click(backdrop);

    expect(useChatUiStore.getState().clientCardOpen).toBe(false);
    expect(screen.queryByTestId("client-card")).toBeNull();
  });

  it("подложка лежит ПЕРЕД карточкой, иначе накрыла бы её собой", () => {
    stubViewport(1024);
    renderChatsPage();
    openCard();

    const backdrop = screen.getByRole("button", { name: "Закрыть карточку клиента" });
    const card = screen.getByTestId("client-card");
    expect(card).toHaveAttribute("data-overlay", "true");
    // DOCUMENT_POSITION_FOLLOWING (4): карточка идёт ПОСЛЕ подложки.
    expect(backdrop.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("на телефоне подложка тоже есть — там карточка накрывает вообще всё", async () => {
    const user = userEvent.setup();
    stubViewport(MOBILE_MAX);
    renderChatsPage();
    openCard();

    await user.click(screen.getByRole("button", { name: "Закрыть карточку клиента" }));
    expect(useChatUiStore.getState().clientCardOpen).toBe(false);
  });

  it("рельса вычитается: на окне 1440 карточка уже оверлеем, хотя окно шире порога", () => {
    /*
     * ⚠ ЭТО САМА БЕДА, А НЕ УГОЛ. 1440 — рабочий MacBook смены, и до правки
     * 08.09 медиазапрос по окну показывал здесь три колонки: 1440 > 1359.
     * Месту при развёрнутой рельсе достаётся 1222, ленте — 478 пикселей при
     * объявленном минимуме 616 (замер на стенде). Сними вычитание рельсы — и
     * проверка снова позеленеет на трёхколоночной раскладке.
     */
    stubViewport(1440);
    renderChatsPage();
    openCard();

    expect(screen.getByTestId("client-card")).toHaveAttribute("data-overlay", "true");
    expect(screen.getByRole("button", { name: "Закрыть карточку клиента" })).toBeTruthy();
  });

  it("свёрнутая рельса возвращает три колонки там, где развёрнутая их отняла", () => {
    /*
     * Обратная сторона той же правки: порог, просто сдвинутый с 1359 на 1577,
     * был бы так же неверен — на свёрнутой рельсе три колонки не появились бы
     * там, где на них есть место. Месту здесь достаётся 1440 − 72 = 1368.
     */
    act(() => useRailStore.getState().set(false));
    stubViewport(1440);
    renderChatsPage();
    openCard();

    expect(screen.queryByRole("button", { name: "Закрыть карточку клиента" })).toBeNull();
    expect(screen.getByTestId("client-card")).not.toHaveAttribute("data-overlay");
  });

  it("в трёхколоночной раскладке подложки нет: карточка стоит в потоке и ничего не накрывает", () => {
    // Три колонки требуют 1360 РАБОЧЕМУ МЕСТУ, то есть окна на рельсу шире.
    stubViewport(NARROW_MAX + 1 + RAIL_WIDTH_EXPANDED); // 1578
    renderChatsPage();
    openCard();

    expect(screen.queryByRole("button", { name: "Закрыть карточку клиента" })).toBeNull();
    expect(screen.getByTestId("client-card")).not.toHaveAttribute("data-overlay");
  });
});
