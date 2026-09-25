import { useEffect } from "react";
import { queryClient } from "@/app/queryClient";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";

/**
 * Синхронизация UI с текущей ролью (11 §2.5, 03 §5).
 *
 * Роль сотрудника может смениться прямо в работающем приложении (админ меняет её
 * в `/settings/team`, права применяются немедленно — 01 §3.4), и на том же
 * компьютере может войти другой человек. И то и другое меняет смысл экрана:
 * вкладка по умолчанию, фильтр «по менеджеру», состав карточки и композер.
 * Поэтому на смену пары (пользователь, роль) сбрасываем сессионное UI-состояние
 * и серверный кэш — чтобы наблюдатель не увидел данные, загруженные менеджером.
 *
 * Память о предыдущей личности лежит в `chatUiStore.uiIdentity`, а НЕ в `useRef`.
 * Хук живёт только внутри `AppLayout`, а принудительный разлогин (кадр 4403,
 * «сотрудник отключён») уводит `RequireAuth` на `/login` и размонтирует его —
 * память внутри компонента умерла бы вместе с ним, и следующий сотрудник в той
 * же вкладке выглядел бы «первым входом»: кэш уволенного остался бы на экране.
 * Стор переживает размонтирование, но не перезагрузку вкладки — там кэша нет и так.
 */
export function useRoleUiSync(): void {
  const user = useSessionStore((s) => s.user);
  const identity = user ? `${user.id}:${user.role}` : null;

  useEffect(() => {
    if (!user || !identity) return;
    const ui = useChatUiStore.getState();
    if (ui.uiIdentity === identity) return;
    const isSwitch = ui.uiIdentity !== null; // не первый вход в приложение

    ui.resetForRole({ id: user.id, role: user.role });
    if (isSwitch) {
      queryClient.clear();
      useUnreadStore.getState().clear();
    }
  }, [identity, user]);
}
