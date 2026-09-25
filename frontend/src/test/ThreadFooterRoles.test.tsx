import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ThreadFooter } from "@/features/chats/components/composer/ThreadFooter";
import type { Permission } from "@/shared/auth/usePermissions";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/** Ролевые варианты низа панели — 11 §2.5, 03 §5.3. */
describe("ThreadFooter — ролевые варианты", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response));
  });

  function renderAs(role: typeof fakeUser.role, permissions: Permission[]) {
    resetSessionStore({
      user: { ...fakeUser, role },
      permissions,
      accessToken: "t",
      bootstrapped: true,
    });
    return renderWithProviders(<ThreadFooter convId={CONV_ID} conversation={makeConversation()} />);
  }

  it("менеджер получает полный композер: поле сообщения, ⚡ и 📎", () => {
    renderAs("manager", ["conversations:read", "messages:send", "notes:write", "templates:own"]);

    expect(screen.getByLabelText("Текст сообщения")).toBeInTheDocument();
    expect(screen.getByLabelText("Быстрые ответы")).toBeInTheDocument();
    expect(screen.getByLabelText("Прикрепить файл")).toBeInTheDocument();
    expect(screen.queryByText(/Режим просмотра/)).not.toBeInTheDocument();
  });

  it("руководитель: плашка «Режим просмотра» + композер только заметок, без ⚡ и 📎", () => {
    const { container } = renderAs("head", ["conversations:read", "notes:write", "conversations:manage"]);

    expect(screen.getByText(/Режим просмотра — назначьте менеджера или передайте диалог/)).toBeInTheDocument();
    expect(screen.getByLabelText("Текст заметки")).toBeInTheDocument();
    expect(screen.queryByLabelText("Текст сообщения")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Быстрые ответы")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Прикрепить файл")).not.toBeInTheDocument();
    // Переключателя режимов нет — композер закреплён в заметке.
    expect(screen.queryByRole("button", { name: "Сообщение" })).not.toBeInTheDocument();
    expect(container.querySelector('.composer[data-note="true"]')).not.toBeNull();
  });

  it("наблюдатель: композера нет вовсе — лента идёт до низа", () => {
    const { container } = renderAs("observer", ["conversations:read"]);

    expect(container.querySelector(".composer")).toBeNull();
    expect(screen.queryByText(/Режим просмотра/)).not.toBeInTheDocument();
  });
});
