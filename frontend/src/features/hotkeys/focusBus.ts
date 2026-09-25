import { create } from "zustand";

/**
 * Шина фокуса (10 §5.1/§5.2): хоткей знает, ЧТО сфокусировать, но не знает где
 * живёт поле. Компоненты подписываются на счётчик и фокусируют себя при его росте.
 * Чистое UI-состояние — в Query-кэше ему делать нечего.
 */
interface FocusState {
  searchNonce: number;
  composerNonce: number;
  /**
   * Счётчик запросов справки по сочетаниям. Тем же приёмом, что и фокус:
   * обработчик клавиш знает, ЧТО показать, но не знает, где живёт окно.
   */
  helpNonce: number;
  requestSearchFocus(): void;
  requestComposerFocus(): void;
  requestHotkeysHelp(): void;
}

export const useFocusBus = create<FocusState>()((set) => ({
  searchNonce: 0,
  composerNonce: 0,
  helpNonce: 0,
  requestSearchFocus: () => set((s) => ({ searchNonce: s.searchNonce + 1 })),
  requestComposerFocus: () => set((s) => ({ composerNonce: s.composerNonce + 1 })),
  requestHotkeysHelp: () => set((s) => ({ helpNonce: s.helpNonce + 1 })),
}));

/** Ctrl+K: фокус в поле поиска по диалогам (11 §8.4). */
export function requestSearchFocus(): void {
  useFocusBus.getState().requestSearchFocus();
}

/** Открыли диалог из списка — фокус сразу в поле ввода (10 §5.2). */
export function requestComposerFocus(): void {
  useFocusBus.getState().requestComposerFocus();
}

/** «?»: показать справку по сочетаниям поверх текущего экрана. */
export function requestHotkeysHelp(): void {
  useFocusBus.getState().requestHotkeysHelp();
}
