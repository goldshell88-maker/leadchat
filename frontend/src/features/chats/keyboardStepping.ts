import { useEffect, useState } from "react";
import { create } from "zustand";

/**
 * «ЧЕЛОВЕК ЛИСТАЕТ СТРЕЛКАМИ» — И ПОКА ОН ЛИСТАЕТ, ДИАЛОГУ ХВАТАЕТ ГЛАВНОГО.
 *
 * ЧТО БЫЛО (проверка 24.09). Каждый шаг Ctrl+↓ открывал диалог целиком: деталь,
 * ленту, отметку прочтения, историю клиента, подсказки объединения — около
 * девяти запросов. Зажатая стрелка даёт до тридцати шагов в секунду, и личная
 * планка nginx (30 запросов в секунду, запас 60) кончалась за одну-три секунды:
 * в архиве журналов 08.09–24.09 — 5 583 отказа 429, на диалоге, где человек
 * остановился, «Диалог недоступен», а «Принять» в эти секунды падало тостом.
 *
 * ЧТО ЗДЕСЬ. Отметка времени последнего шага с клавиатуры. Пока с неё не прошло
 * `STEP_SETTLE_MS`, открытый диалог считается пролистанным: деталь и лента
 * грузятся как обычно (без них нечего показать), а отметка прочтения, история
 * клиента и подсказки объединения ждут, пока человек остановится. Пролистанный
 * за долю секунды диалог и не прочитан — отметить его прочитанным было бы
 * неправдой.
 *
 * Мышь сюда не пишет: щелчок — это выбор, после него человек смотрит в диалог.
 */

/** Сколько стоять на диалоге, чтобы он считался открытым, а не пролистанным. */
export const STEP_SETTLE_MS = 300;

const useSteppingStore = create<{ lastStepAt: number }>(() => ({ lastStepAt: 0 }));

/** Шаг по списку с клавиатуры. */
export function markKeyboardStep(): void {
  useSteppingStore.setState({ lastStepAt: Date.now() });
}

/** true, пока человек листает; false, когда он простоял на диалоге `STEP_SETTLE_MS`. */
export function useKeyboardStepping(): boolean {
  const lastStepAt = useSteppingStore((s) => s.lastStepAt);
  const [, recheck] = useState(0);
  useEffect(() => {
    const left = lastStepAt + STEP_SETTLE_MS - Date.now();
    if (left <= 0) return;
    const timer = window.setTimeout(() => recheck((n) => n + 1), left);
    return () => window.clearTimeout(timer);
  }, [lastStepAt]);
  return Date.now() - lastStepAt < STEP_SETTLE_MS;
}
