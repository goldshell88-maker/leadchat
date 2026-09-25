import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { useChatHotkeys } from "@/features/hotkeys/useChatHotkeys";
import { qk } from "@/shared/api/queryKeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { включитьВсеСочетания } from "./helpers";

const navigate = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

/**
 * ОБХОД НЕПРОЧИТАННЫХ НЕ ВСТАЁТ ПОСЛЕ ПЕРВОГО ШАГА (проверка 24.09).
 *
 * Открытие отмечает диалог прочитанным, и его счётчик в списке сразу нуль.
 * Обход искал открытый диалог только среди непрочитанных, не находил и молчал:
 * Alt+↓ срабатывал один раз.
 */

function seed(rows: Array<{ id: string; unread_count: number }>): void {
  queryClient.setQueryData(qk.conversations.list({ tab: "all" }), {
    pages: [{ items: rows }],
    pageParams: [0],
  });
}

function altDown(): void {
  renderHook(() => useChatHotkeys());
  window.dispatchEvent(
    new KeyboardEvent("keydown", { key: "ArrowDown", code: "ArrowDown", altKey: true, bubbles: true, cancelable: true }),
  );
}

describe("Alt+↓ — следующий с непрочитанными", () => {
  beforeEach(() => {
    включитьВсеСочетания();
    navigate.mockClear();
    queryClient.clear();
    useChatUiStore.setState({ filters: { tab: "all" }, inboxOpen: false });
  });

  it("из только что прочитанного идёт к следующему непрочитанному", () => {
    seed([
      { id: "c-1", unread_count: 0 },
      { id: "c-2", unread_count: 0 },
      { id: "c-3", unread_count: 0 },
      { id: "c-4", unread_count: 3 },
    ]);
    // c-2 открыли обходом, и открытие уже обнулило его счётчик.
    useChatUiStore.setState({ activeConversationId: "c-2" });

    altDown();

    expect(navigate).toHaveBeenCalledWith("/chats/c-4");
  });

  it("по кругу: за последним — первый непрочитанный сверху", () => {
    seed([
      { id: "c-1", unread_count: 1 },
      { id: "c-2", unread_count: 0 },
      { id: "c-3", unread_count: 0 },
    ]);
    useChatUiStore.setState({ activeConversationId: "c-3" });

    altDown();

    expect(navigate).toHaveBeenCalledWith("/chats/c-1");
  });
});
