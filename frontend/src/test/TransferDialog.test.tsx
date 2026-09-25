import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TransferDialog } from "@/features/chats/components/card/TransferDialog";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { notifyAssignedToMe } from "@/shared/realtime/notify";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread, threadMessages } from "./render";

vi.mock("@/shared/realtime/notify", () => ({
  notifyNewMessage: vi.fn(),
  notifyAssignedToMe: vi.fn(),
}));

const ASSIGNABLE = {
  items: [
    { id: "u-petr", full_name: "Пётр Ковалёв", role: "manager", is_online: true },
    { id: "u-oleg", full_name: "Олег Иванов", role: "manager", is_online: false },
  ],
};

describe("TransferDialog — передача диалога (11 §2.4, 01 §5.5)", () => {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];

  beforeEach(() => {
    calls.length = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : {} });
        if (url.includes("/users/assignable")) return jsonResponse(200, ASSIGNABLE);
        if (url.endsWith("/assign")) {
          return jsonResponse(200, {
            conversation: makeConversation({ assignee: { id: "u-petr", full_name: "Пётр Ковалёв" } }),
            system_message: {
              id: "sys-1",
              conversation_id: CONV_ID,
              direction: "system",
              sender_type: "system",
              sender: null,
              body: "Диалог передан: Анна Смирнова → Пётр Ковалёв. Комментарий: торгуется",
              attachments: [],
              delivery_status: "delivered",
              created_at: "2026-08-05T10:05:00Z",
            },
          });
        }
        return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("в списке ТОЛЬКО те, кто в сети", async () => {
    /*
     * ⚠ РЕШЕНИЕ ВЛАДЕЛЬЦА 28.08 дословно: «сделай так, чтобы не было
     * возможности передать диалог человеку в офлайн, и показывались для
     * передачи только в онлайне».
     *
     * Раньше офлайн-сотрудники были в списке — просто с серой точкой и
     * подписью «(офлайн)». А передача не как приглашение: пока получатель её не
     * примет, диалог не появляется ни в очереди, ни в «Моих» — то есть
     * отданный ушедшему домой, он не виден НИКОМУ и ждёт молча, а отдающий
     * уверен, что дело сделано.
     */
    renderWithProviders(<TransferDialog convId={CONV_ID} opened onClose={vi.fn()} />);
    await screen.findByRole("radio", { name: /Пётр Ковалёв/ });
    expect(
      screen.queryByRole("radio", { name: /Олег Иванов/ }),
      "офлайн-сотрудник остался в списке передачи",
    ).toBeNull();
  });

  it("ПОЗВАТЬ коллегу можно и в офлайн — правила у двух окон разные", async () => {
    /*
     * Приглашение диалог не отдаёт: за клиента по-прежнему отвечает тот, кто
     * вёл, а позванный прочтёт уведомление, когда придёт. Запрещать это значило
     * бы обрезать способ спросить совета у того, кто в теме, — из-за правила,
     * придуманного для другого действия.
     */
    const { InviteDialog } = await import("@/features/chats/components/card/InviteDialog");
    renderWithProviders(
      <InviteDialog convId={CONV_ID} opened alreadyIn={[]} onClose={vi.fn()} />,
    );
    expect(await screen.findByRole("radio", { name: /Олег Иванов/ })).toBeInTheDocument();
  });

  it("показывает сотрудников с индикатором онлайна и передаёт с комментарием", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderWithProviders(<TransferDialog convId={CONV_ID} opened onClose={onClose} />);

    const petr = await screen.findByRole("radio", { name: /Пётр Ковалёв/ });

    // Автофокус — в поле поиска (11 §2.4).
    expect(screen.getByLabelText("Кому")).toHaveFocus();

    await user.type(screen.getByLabelText("Кому"), "Пётр");

    await user.click(petr);
    await user.type(screen.getByLabelText(/Комментарий коллеге/), "торгуется");
    await user.click(screen.getByRole("button", { name: "Передать" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.includes(`/conversations/${CONV_ID}/assign`))).toBe(true);
    });
    const assign = calls.find((c) => c.url.endsWith("/assign"));
    expect(assign?.body).toMatchObject({ assignee_id: "u-petr", comment: "торгуется" });

    // Комментарий уходит системным сообщением в ленту (01 §5.5).
    await waitFor(() => {
      expect(threadMessages().some((m) => m.direction === "system")).toBe(true);
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("кнопка «Передать» заблокирована, пока никто не выбран", async () => {
    renderWithProviders(<TransferDialog convId={CONV_ID} opened onClose={vi.fn()} />);
    await screen.findByRole("radio", { name: /Пётр Ковалёв/ });
    expect(screen.getByRole("button", { name: "Передать" })).toBeDisabled();
  });

  it("у получателя conversation:assigned даёт звук и тост (11 §2.4)", () => {
    applyWsEvent({
      type: "conversation:assigned",
      ts: "2026-08-05T10:05:00Z",
      data: {
        conversation_id: CONV_ID,
        assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
        assigned_by: { id: "u-head", full_name: "Мария Соколова" },
        comment: "торгуется",
        is_for_you: true,
      },
    });
    expect(notifyAssignedToMe).toHaveBeenCalled();
  });
});
