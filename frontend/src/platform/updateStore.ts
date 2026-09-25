import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { UpdateInfo } from "./bridge";

/**
 * Состояние автообновления (04 §6.3). Живёт вне Tauri-чанка, поэтому баннер
 * можно смонтировать в общий layout — в вебе `available` просто всегда null.
 *
 * Правило UX: обновление НИКОГДА не ставится без клика; отложенный баннер
 * возвращается через сутки (у оператора мог быть недописанный ответ).
 */

export const DISMISS_TTL_MS = 24 * 60 * 60 * 1000;

interface UpdateState {
  available: UpdateInfo | null;
  checking: boolean;
  applying: boolean;
  error: string | null;
  dismissedVersion: string | null;
  dismissedAt: number | null;
  /**
   * Версия сборки, которая лежит на сервере, когда она РАСХОДИТСЯ с версией
   * этой вкладки (веб, SHELL-02); `null` — вкладка свежая. Отдельное поле, а не
   * `available`: там десктопный `UpdateInfo` из мостa (версия, заметки, дата
   * публикации), и ставится он совсем другим действием — установкой пакета,
   * а не перезагрузкой страницы. Смешать их значило бы получить на десктопе
   * кнопку «Обновить», которая перезагружает WebView и ничего не обновляет.
   *
   * Взаимоисключающи по построению: `buildVersion.isStaleTab` в десктопной
   * сборке всегда возвращает `false`, так что двух полос одновременно не бывает.
   */
  serverBuild: string | null;

  announce(u: UpdateInfo | null): void;
  announceBuild(version: string | null): void;
  setChecking(v: boolean): void;
  setApplying(v: boolean): void;
  setError(e: string | null): void;
  dismiss(): void;
  reset(): void;
}

export const useUpdateStore = create<UpdateState>()(
  persist(
    (set) => ({
      available: null,
      checking: false,
      applying: false,
      error: null,
      dismissedVersion: null,
      dismissedAt: null,
      serverBuild: null,

      announceBuild: (version) => set({ serverBuild: version }),

      announce: (u) =>
        set((s) => ({
          available: u,
          error: null,
          // Новая версия поверх отложенной — баннер показываем снова.
          ...(u && u.version !== s.dismissedVersion ? { dismissedVersion: null, dismissedAt: null } : null),
        })),
      setChecking: (v) => set({ checking: v }),
      setApplying: (v) => set({ applying: v }),
      setError: (e) => set({ error: e, applying: false }),
      dismiss: () =>
        set((s) => ({
          dismissedVersion: s.available?.version ?? null,
          dismissedAt: Date.now(),
        })),
      reset: () => set({ available: null, checking: false, applying: false, error: null }),
    }),
    {
      name: "lc-update",
      // Персистим ТОЛЬКО отложенность: сама доступность версии — рантайм-факт.
      partialize: (s) => ({ dismissedVersion: s.dismissedVersion, dismissedAt: s.dismissedAt }),
    },
  ),
);

/** Показывать ли баннер: есть версия и её не откладывали за последние 24 ч (04 §6.3). */
export function selectBannerUpdate(s: UpdateState, now: number = Date.now()): UpdateInfo | null {
  if (!s.available) return null;
  if (s.dismissedVersion !== s.available.version) return s.available;
  if (s.dismissedAt && now - s.dismissedAt > DISMISS_TTL_MS) return s.available;
  return null;
}
