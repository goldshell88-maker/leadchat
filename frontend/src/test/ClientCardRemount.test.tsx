/**
 * КАРТОЧКА КЛИЕНТА ПЕРЕСОЗДАЁТСЯ ВМЕСТЕ С ДИАЛОГОМ.
 *
 * ЧТО БЫЛО. На широком экране карточка висит в потоке постоянно и при смене
 * диалога НЕ пересоздавалась — у неё не было ключа, в отличие от ленты
 * (`ChatThreadPane` держит `key={convId}` с самого начала).
 *
 * Начатая правка имени живёт локальным состоянием карточки. Оператор нажал
 * «изменить», набрал имя, не сохранил и щёлкнул другой диалог. Деталь второго
 * диалога уже в кэше (gcTime 30 минут), поэтому карточка даже не мигнёт
 * скелетоном: поле правки останется открытым с набранным текстом, а `clientId`
 * внутри будет УЖЕ ДРУГОЙ. Нажатие «Сохранить» переписывает имя ЧУЖОМУ
 * клиенту — тихо, без единого признака на экране.
 *
 * ЧТО ПРОВЕРЯЕМ. Смена `:id` пересоздаёт поддерево карточки — то есть любое её
 * внутреннее состояние (правка имени, телефона, набранный тег) начинается с
 * чистого листа. Проверяем именно пересоздание, а не конкретное поле: полей у
 * карточки несколько, и следующее добавленное обязано получить ту же защиту.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { useEffect, useRef } from "react";
import userEvent from "@testing-library/user-event";
import { Route, Routes, useNavigate } from "react-router-dom";
import { ChatsPage } from "@/features/chats/ChatsPage";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { renderWithProviders } from "./render";

const монтирований: string[] = [];

vi.mock("@/features/chats/components/list/ChatListPane", () => ({
  ChatListPane: () => <div data-testid="list-pane" />,
}));
vi.mock("@/features/chats/components/thread/ChatThreadPane", () => ({
  ChatThreadPane: () => <div data-testid="thread-pane" />,
}));
vi.mock("@/features/chats/components/card/ClientCardPane", () => ({
  ClientCardPane: ({ convId }: { convId: string | null }) => {
    // Пустые зависимости НАМЕРЕННО: считаем МОНТИРОВАНИЯ, а не перерисовки.
    // Без ключа React переиспользует тот же узел и правка переезжает в чужую
    // карточку — именно это и должно быть видно счётчику.
    const первый = useRef(convId);
    useEffect(() => {
      монтирований.push(первый.current ?? "none");
    }, []);
    return <aside data-testid="client-card" data-conv={convId ?? "none"} />;
  },
}));

/**
 * Широкий экран: карточка стоит в потоке третьей колонкой, а не оверлеем.
 *
 * ⚠ ОКНО, А НЕ МЕСТО. Узкую раскладку решает ширина `.chats-page` — окно за
 * вычетом рельсы (`features/chats/ширинаМеста.ts`), — поэтому окно обязано
 * быть шире порога на ширину рельсы. В jsdom раскладки нет, и замер уходит на
 * ту же оценку «окно минус рельса».
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

/** Переход между диалогами БЕЗ перезагрузки экрана — как по клику в списке. */
function NavHarness() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate("/chats/conv-B")}>
        перейти
      </button>
      <Routes>
        <Route path="/chats/:id" element={<ChatsPage />} />
      </Routes>
    </>
  );
}

function render(route: string) {
  return renderWithProviders(<NavHarness />, { route });
}

describe("Карточка клиента при смене диалога", () => {
  beforeEach(() => {
    монтирований.length = 0;
    useChatUiStore.setState({ clientCardOpen: false });
    stubViewport(1920);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("переход в соседний диалог пересоздаёт поддерево карточки", async () => {
    const user = userEvent.setup();
    render("/chats/conv-A");
    expect(монтирований).toEqual(["conv-A"]);

    await user.click(screen.getByRole("button", { name: "перейти" }));

    // Ключ у карточки — идентификатор диалога, поэтому React не переиспользует
    // прежний узел, а собирает новый: локальное состояние правки не переезжает.
    expect(монтирований).toEqual(["conv-A", "conv-B"]);
  });

  it("карточка знает тот же диалог, что и адрес", () => {
    render("/chats/conv-B");
    expect(document.querySelector('[data-testid="client-card"]')?.getAttribute("data-conv")).toBe(
      "conv-B",
    );
  });
});
