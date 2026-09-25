import { beforeEach, describe, expect, it } from "vitest";
import { act, render } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import { useRoleUiSync } from "@/shared/auth/useRoleUiSync";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { makeConversation } from "./render";

/**
 * Смена сотрудника на одном компьютере (11 §2.5).
 *
 * Тонкость, ради которой тест написан: `AppLayout` — единственный хозяин хука,
 * и при принудительном разлогине (кадр 4403, «сотрудник отключён») `RequireAuth`
 * уводит на `/login` и РАЗМОНТИРУЕТ его. Если хук помнит предыдущую личность
 * внутри `useRef`, память умирает вместе с компонентом: следующий человек
 * входит в той же вкладке, хук считает это первым входом и НЕ чистит кэш —
 * новому сотруднику достаются диалоги, счётчики и статистика уволенного.
 * Поэтому личность обязана пережить размонтирование.
 */
function Probe() {
  useRoleUiSync();
  return null;
}

const SECOND_USER = {
  id: "9f2b7c31-0e44-4a52-8c10-7d5b6a2e4c88",
  email: "boris@partner-lead-centre.ru",
  full_name: "Борис Кузнецов",
  role: "manager" as const,
  is_active: true,
};

function seedPreviousEmployeeData() {
  queryClient.setQueryData(qk.conversations.detail("conv-1"), makeConversation());
  useUnreadStore.setState({
    byConversation: { "conv-1": { count: 3, assigneeId: fakeUser.id, status: "in_progress" } },
  });
}

describe("Смена сотрудника в одной вкладке (11 §2.5)", () => {
  beforeEach(() => {
    queryClient.clear();
    useUnreadStore.getState().clear();
    useChatUiStore.setState({ filters: { tab: "all" }, drafts: {}, draftsOwnerId: null, uiIdentity: null });
    resetSessionStore();
  });

  it("после принудительного разлогина и входа другого человека кэш предыдущего не остаётся", async () => {
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    const first = render(<Probe />);
    seedPreviousEmployeeData();

    // Кадр 4403: сессия гаснет, RequireAuth уводит на /login — AppLayout размонтирован.
    await act(async () => {
      useSessionStore.getState().clear();
    });
    first.unmount();

    // Тот же компьютер, та же вкладка: входит другой сотрудник.
    await act(async () => {
      resetSessionStore({ user: SECOND_USER, permissions: [], accessToken: "t2", bootstrapped: true });
    });
    render(<Probe />);

    expect(queryClient.getQueryData(qk.conversations.detail("conv-1"))).toBeUndefined();
    expect(useUnreadStore.getState().byConversation).toEqual({});
  });

  it("первый вход в приложение кэш не чистит — данные, загруженные при старте, остаются", async () => {
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    render(<Probe />);
    // Данные приехали уже после того, как хук отработал первый монтаж.
    await act(async () => {
      seedPreviousEmployeeData();
    });

    expect(queryClient.getQueryData(qk.conversations.detail("conv-1"))).toBeDefined();
    expect(useChatUiStore.getState().filters.tab).toBe("mine");
  });

  it("смена роли того же сотрудника чистит кэш: состав экрана и права другие", async () => {
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    render(<Probe />);
    seedPreviousEmployeeData();

    await act(async () => {
      resetSessionStore({
        user: { ...fakeUser, role: "observer" },
        permissions: [],
        accessToken: "t",
        bootstrapped: true,
      });
    });

    expect(queryClient.getQueryData(qk.conversations.detail("conv-1"))).toBeUndefined();
    expect(useChatUiStore.getState().filters.tab).toBe("all");
  });
});
