import { create } from "zustand";
// ⚠ ИЗ `version`, А НЕ ИЗ `changelog`: иначе стартовый чанк везёт все
// тексты выпусков ради одной строки (разбор 03.09).
import { LATEST_VERSION } from "./version";
// Стиль точки на пункте меню едет вместе с ответом «есть ли непрочитанное»:
// шапка приложения импортирует этот модуль, значит правило попадает в
// стартовый бандл, а не в ленивый чанк страницы (SHELL-05, см. menu-dot.css).
import "./menu-dot.css";

/**
 * Что человек уже прочитал в «Что нового».
 *
 * Хранится В БРАУЗЕРЕ, а не на сервере, и это осознанно: отметка машинная, а
 * не сотрудника. Заводить ради неё колонку в базе, ручку и миграцию — три
 * лишние сущности ради точки на пункте меню; а если человек сядет за другой
 * компьютер и увидит список ещё раз, он ничего не потеряет.
 *
 * Zustand, а не чтение localStorage по месту: точку показывает шапка, а гасит
 * страница обновлений — им нужно общее состояние, иначе точка не исчезнет,
 * пока не перезагрузишь вкладку.
 */

const KEY = "lc-updates-seen";

function read(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    // Приватный режим без хранилища: считаем, что не читал. Показать список
    // лишний раз безобиднее, чем спрятать новое.
    return null;
  }
}

interface UpdatesState {
  seenVersion: string | null;
  markSeen(): void;
}

export const useUpdatesSeen = create<UpdatesState>()((set) => ({
  seenVersion: read(),
  markSeen: () => {
    try {
      localStorage.setItem(KEY, LATEST_VERSION);
    } catch {
      /* без хранилища — просто не запомним */
    }
    set({ seenVersion: LATEST_VERSION });
  },
}));

export function markUpdatesSeen(): void {
  useUpdatesSeen.getState().markSeen();
}

/** Есть ли непрочитанное — точка на пункте меню. */
export function selectHasUnseenUpdates(s: UpdatesState): boolean {
  return s.seenVersion !== LATEST_VERSION;
}
