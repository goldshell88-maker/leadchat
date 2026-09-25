import { включитьВсеСочетания } from "./helpers";
/**
 * ОДИН ESC — ОДИН СЛОЙ.
 *
 * ЧТО БЫЛО. В `dispatch.луковица()` написано «меню и модальные окна гасят
 * событие раньше, до нас оно не доходит». Это неправда: окно закрывает себя из
 * своего обработчика и распространение НЕ останавливает, а наши слушатели
 * висят на `window`. Значит один Esc над открытым окном срабатывал ДВАЖДЫ —
 * закрывал окно и следом снимал слой под ним.
 *
 * Как это выглядело: оператор держит открытой карточку клиента, жмёт Ctrl+T,
 * открывается «Передать диалог», передумывает и жмёт Esc — закрывается окно И
 * схлопывается карточка. Хуже с режимом заметки: композер молча переключается
 * обратно на «Сообщение», и следующая мысль, набранная «для себя», уходит
 * клиенту.
 *
 * ЧТО СТАЛО. Пока сверху лежит чужой слой (`role="dialog"`), экранные
 * сочетания молчат ВСЕ, а не только Esc: Ctrl+D поверх открытого окна закрывал
 * диалог, до которого человек в этот момент даже не может дотянуться мышью.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useChatHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { requestClose } from "@/features/hotkeys/actionBus";

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock("@/features/hotkeys/actionBus", async () => {
  const actual = await vi.importActual<typeof import("@/features/hotkeys/actionBus")>(
    "@/features/hotkeys/actionBus",
  );
  return { ...actual, requestClose: vi.fn() };
});

/** Слой поверх экрана — так его помечают и Mantine, и наш поповер шаблонов. */
function открытьОкно(): HTMLElement {
  const el = document.createElement("div");
  el.setAttribute("role", "dialog");
  document.body.appendChild(el);
  return el;
}

function нажать(init: KeyboardEventInit) {
  renderHook(() => useChatHotkeys());
  window.dispatchEvent(new KeyboardEvent("keydown", { ...init, bubbles: true, cancelable: true }));
}

describe("Сочетания под открытым окном", () => {
  beforeEach(() => {
    // Сочетания включены явно: с 02.09 они выключены по умолчанию, а этот набор
    // проверяет САМО ДЕЙСТВИЕ, а не то, включено ли оно из коробки.
    включитьВсеСочетания();
    document.body.innerHTML = "";
    vi.mocked(requestClose).mockClear();
    useChatUiStore.setState({
      clientCardOpen: true,
      activeConversationId: "conv-1",
      drafts: {},
      filters: { tab: "all" },
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("Esc над открытым окном не закрывает карточку клиента под ним", () => {
    открытьОкно();
    нажать({ key: "Escape" });
    expect(useChatUiStore.getState().clientCardOpen).toBe(true);
  });

  it("без окна Esc по-прежнему снимает верхний слой", () => {
    нажать({ key: "Escape" });
    expect(useChatUiStore.getState().clientCardOpen).toBe(false);
  });

  it("Esc над окном не снимает и режим заметки", () => {
    useChatUiStore.setState({ clientCardOpen: false, drafts: { "conv-1": { isNote: true, text: "для себя" } } });
    открытьОкно();
    нажать({ key: "Escape" });
    expect(useChatUiStore.getState().drafts["conv-1"]?.isNote).toBe(true);
  });

  /*
   * Не только Esc: любое действие над диалогом, до которого человек в этот
   * момент не может дотянуться мышью, — это действие вслепую.
   */
  it("Ctrl+D над открытым окном не закрывает диалог за ним", () => {
    открытьОкно();
    нажать({ key: "d", code: "KeyD", ctrlKey: true });
    expect(requestClose).not.toHaveBeenCalled();
  });

  it("без окна Ctrl+D работает как обещано в шпаргалке", () => {
    нажать({ key: "d", code: "KeyD", ctrlKey: true });
    expect(requestClose).toHaveBeenCalled();
  });
});
