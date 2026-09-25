import { включитьВсеСочетания } from "./helpers";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import { useChatHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";

const navigate = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

/**
 * КЛАВИШИ НЕ УНОСЯТ ИЗ ДИАЛОГА, КОТОРОГО НЕТ В ЛЕВОМ СПИСКЕ.
 *
 * ОТКУДА БЕРЁТСЯ ТАКОЙ ДИАЛОГ. Из «Разбора диалогов»: там свой период, свои фильтры
 * и своя выдача, и открытый оттуда диалог в левую колонку часто не входит вовсе —
 * закрытые, например, в обычный список не попадают по правилу сервера.
 *
 * ЧТО БЫЛО. `findIndex` возвращал −1, а правило `at === -1 ? 0` читало это как
 * «начни сначала» и уводило в ПЕРВУЮ строку выдачи, которую человек не читал.
 * Молча, одним нажатием J, посреди разбора чужой переписки — и обратно уже не
 * вернуться, потому что что было открыто, нигде не записано.
 *
 * ⚠ ОБРАТНАЯ СТОРОНА ВАЖНЕЕ САМОЙ ПРАВКИ: прыжок на первую строку законен, когда
 * не открыто НИЧЕГО. Запрети и его — и клавиатура перестанет открывать список
 * вовсе, то есть лечение окажется хуже болезни.
 */

const СПИСОК = {
  pages: [
    {
      items: [
        { id: "conv-1", unread_count: 1 },
        { id: "conv-2", unread_count: 2 },
      ],
    },
  ],
  pageParams: [0],
};

describe("Горячие клавиши: открытый диалог вне списка", () => {
  beforeEach(() => {
    // Сочетания включены явно: с 02.09 они выключены по умолчанию, а этот набор
    // проверяет САМО ДЕЙСТВИЕ, а не то, включено ли оно из коробки.
    включитьВсеСочетания();
    navigate.mockClear();
    queryClient.clear();
    useChatUiStore.setState({ filters: { tab: "all" }, inboxOpen: false });
    queryClient.setQueryData(qk.conversations.list({ tab: "all" }), СПИСОК);
  });

  function press(init: KeyboardEventInit) {
    renderHook(() => useChatHotkeys());
    window.dispatchEvent(new KeyboardEvent("keydown", { ...init, bubbles: true, cancelable: true }));
  }

  /*
   * ⚠ ЖМЁМ ДЕЙСТВУЮЩЕЕ СОЧЕТАНИЕ, А НЕ ЛЮБОЕ ПОХОЖЕЕ (правка 09.09). Здесь
   * стояло голое «j», и оно перестало быть умолчанием: листание сузили до
   * `Ctrl + ↓ / Ctrl + ↑`, потому что голая стрелка — это клавиша прокрутки, а
   * `j` — привычка из vim, которой у тринадцати диспетчеров нет. Помощник
   * `включитьВсеСочетания` раздаёт действиям их УМОЛЧАНИЯ, поэтому проверка,
   * жмущая снятое сочетание, перестала бы что-либо проверять: `navigate` не
   * звали бы просто потому, что клавиша ничья.
   */
  it("Ctrl+↓ не уводит из чужого диалога", () => {
    useChatUiStore.setState({ activeConversationId: "из-разбора" });
    press({ key: "ArrowDown", ctrlKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("Ctrl+↑ не уводит из чужого диалога", () => {
    useChatUiStore.setState({ activeConversationId: "из-разбора" });
    press({ key: "ArrowUp", ctrlKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("Alt+↓ не уводит из чужого диалога", () => {
    useChatUiStore.setState({ activeConversationId: "из-разбора" });
    press({ key: "ArrowDown", altKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("но когда не открыто ничего — Ctrl+↓ по-прежнему открывает первый", () => {
    useChatUiStore.setState({ activeConversationId: null });
    press({ key: "ArrowDown", ctrlKey: true });
    expect(navigate).toHaveBeenCalledWith("/chats/conv-1");
  });

  it("и внутри своего списка ходит как ходил", () => {
    useChatUiStore.setState({ activeConversationId: "conv-1" });
    press({ key: "ArrowDown", ctrlKey: true });
    expect(navigate).toHaveBeenCalledWith("/chats/conv-2");
  });
});
