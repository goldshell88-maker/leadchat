import { включитьВсеСочетания } from "./helpers";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useActionBus } from "@/features/hotkeys/actionBus";
import { useChatHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { CONV_ID } from "./render";

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => vi.fn() };
});

/**
 * Три беды клавиатуры в одном месте (#31).
 *
 * Все три названы в описи готовности, и каждая по-своему учит человека не
 * доверять интерфейсу: сочетание из шпаргалки не работает, привычное движение
 * при наборе делает не то, а перебор списка уводит подсветку за край экрана.
 */
describe("Клавиатура: исправления #31", () => {
  beforeEach(() => {
    // Сочетания включены явно: с 02.09 они выключены по умолчанию, а этот набор
    // проверяет САМО ДЕЙСТВИЕ, а не то, включено ли оно из коробки.
    включитьВсеСочетания();
    useChatUiStore.setState({ activeConversationId: CONV_ID });
    useActionBus.setState({ declineNonce: 0, transferNonce: 0 });
  });

  function press(init: KeyboardEventInit, target?: EventTarget) {
    const event = new KeyboardEvent("keydown", { ...init, bubbles: true, cancelable: true });
    (target ?? window).dispatchEvent(event);
    return event;
  }

  it("Ctrl+Backspace из поля ввода НЕ отклоняет диалог", () => {
    /*
     * ГЛАВНОЕ ИЗ ТРЁХ. В Windows и Linux это «удалить слово» — одно из самых
     * частых движений при наборе. Проверка «человек печатает» стояла ниже по
     * коду, и сочетание перехватывалось раньше неё: диспетчер, стиравший слово
     * в ответе клиенту, отклонял диалог. Тот уходил другим операторам, а
     * человек оставался с недописанным текстом и без объяснения.
     */
    renderHook(() => useChatHotkeys());

    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const before = useActionBus.getState().declineNonce;

    press({ key: "Backspace", ctrlKey: true }, textarea);

    expect(useActionBus.getState().declineNonce).toBe(before);
    textarea.remove();
  });

  it("Ctrl+Backspace вне поля ввода по-прежнему отклоняет", () => {
    /* Граница: сочетание не убрано, оно лишь перестало мешать набору. */
    renderHook(() => useChatHotkeys());
    const before = useActionBus.getState().declineNonce;

    press({ key: "Backspace", ctrlKey: true });

    expect(useActionBus.getState().declineNonce).toBe(before + 1);
  });

  it("Ctrl+T просит передачу — и эту просьбу теперь есть кому услышать", () => {
    /*
     * Счётчик поднимался и раньше; беда была в том, что на него никто не
     * подписан — `transferNonce` не читал ни один файл во всём интерфейсе.
     * Здесь проверяется половина в шине; вторая половина (лента открывает окно
     * передачи) живёт в ChatThreadPane и проверяется через него.
     */
    renderHook(() => useChatHotkeys());
    const before = useActionBus.getState().transferNonce;

    press({ key: "t", ctrlKey: true });

    expect(useActionBus.getState().transferNonce).toBe(before + 1);
  });
});
