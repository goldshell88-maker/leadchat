import { useEffect } from "react";
import { RouterProvider } from "react-router-dom";
import { startRealtime, stopRealtime } from "@/shared/realtime/realtime";
import { formatDocumentTitle, subscribeBadges } from "@/shared/stores/badges";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { ErrorBoundary } from "./ErrorBoundary";
import { Providers } from "./providers";
import { router } from "./router";

export function App() {
  // Silent refresh by httpOnly cookie on start (03 §6): a live cookie means
  // the user never sees /login — straight to the workplace.
  useEffect(() => {
    void useSessionStore.getState().bootstrap();
  }, []);

  // WS lifecycle (03 §3.1): singleton connects after login, closes on logout.
  useEffect(() => {
    const sync = (loggedIn: boolean) => {
      if (loggedIn) startRealtime();
      else stopRealtime();
    };
    sync(Boolean(useSessionStore.getState().user));
    const unsubscribe = useSessionStore.subscribe((s, prev) => {
      const now = Boolean(s.user);
      const before = Boolean(prev.user);
      if (now !== before) sync(now);
    });
    return () => {
      unsubscribe();
      stopRealtime();
    };
  }, []);

  // Бейджи в title (03 §3.5, DESIGN 3.3, 7.1 п.5): «(N) LeadChat» — непрочитанные,
  // «[M] (N) LeadChat» — плюс очередь «Входящие». Формат и правило «не складывать»
  // живут в одном месте — shared/stores/badges.ts.
  useEffect(
    () =>
      subscribeBadges((b) => {
        document.title = formatDocumentTitle(b);
      }),
    [],
  );

  return (
    <Providers>
      {/*
        Последний рубеж (аудит SHELL-01). Всё, что падает ВНУТРИ маршрутов,
        ловят граница в `AppLayout` и `errorElement` на самих маршрутах — они
        ближе и сохраняют больше. Сюда доходит только падение самого
        `RouterProvider`: разбор адреса, создание роутера, ошибка в его
        собственной отрисовке. Без этой обёртки такое падение выносит React из
        дерева целиком, и человек получает белый экран без единого слова.
      */}
      <ErrorBoundary where="app">
        <RouterProvider router={router} />
      </ErrorBoundary>
    </Providers>
  );
}
