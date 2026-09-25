import { beforeEach, describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ThreadActions } from "@/features/chats/components/thread/ThreadActions";
import type { ConversationDetailDto } from "@/shared/api/types";
import { fakeUser, resetSessionStore } from "./helpers";
import { makeConversation, renderWithProviders } from "./render";

/**
 * Гость не закрывает чужой диалог (жалоба владельца 04.09).
 *
 * ⚠ ДОСЛОВНО (имя заменено): «я когда закрываю диалог у себя, он так же
 * закрывается у Зуева Дениса». Зашедший сам видит чужой диалог в «Моих» рядом
 * со своими, и «Закрыть» читается им как «убрать у себя» — а закрывает
 * разговор клиенту.
 * В бою это стоило живого диалога: закрыт в 14:38, клиент написал в 14:42, и
 * хозяину пришлось принимать его заново.
 *
 * Запрет держит сервер (403 `guest_cannot_close`); здесь проверяется, что
 * человек в этот отказ не упирается — пункта попросту нет, а убрать диалог у
 * себя ему предлагают кнопкой «Выйти».
 *
 * ⚠ И ГРАНИЦЫ, А НЕ ТОЛЬКО ЗАПРЕТ. Правило узкое: оно про «зашёл сам», а не
 * про участие вообще и не про чужие диалоги вообще. Позванный коллега закрывал
 * диалог и обязан закрывать дальше — его позвали помогать.
 */
describe("Гость не закрывает чужой диалог", () => {
  const ХОЗЯИН = { id: "b6e0a0f2-0000-4000-8000-000000000001", full_name: "Зуев Денис" };

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "conversations:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
  });

  const noop = () => {};

  function renderActions(conversation: ConversationDetailDto) {
    return renderWithProviders(
      <ThreadActions
        conversation={conversation}
        onInvite={noop}
        onTransfer={noop}
        onBlock={noop}
        onClose={noop}
        closePending={false}
      />,
    );
  }

  async function openMenu() {
    await userEvent.click(screen.getByLabelText("Действия с диалогом"));
  }

  const участник = (kind: "self" | "invited") => ({
    id: fakeUser.id,
    full_name: fakeUser.full_name,
    reason: null,
    invited_at: null,
    kind,
  });

  it("зашедшему самому пункта «Закрыть диалог» не показывают", async () => {
    renderActions(makeConversation({ assignee: ХОЗЯИН, participants: [участник("self")] }));
    await openMenu();

    // ⚠ СНАЧАЛА ДОЖДАТЬСЯ МЕНЮ, ПОТОМ ПРОВЕРЯТЬ ОТСУТСТВИЕ. Выпадающее меню
    // приезжает через портал не в тот же кадр: спроси мы про «Закрыть» сразу,
    // проверка зеленела бы на пустом экране и прошла бы даже с ВЕРНУВШИМСЯ
    // пунктом.
    expect(await screen.findByText("Позвать коллегу")).toBeTruthy();
    expect(screen.queryByText(/Закрыть диалог/)).toBeNull();
  });

  it("позванному коллеге пункт оставляют", async () => {
    renderActions(makeConversation({ assignee: ХОЗЯИН, participants: [участник("invited")] }));
    await openMenu();

    expect(await screen.findByText(/Закрыть диалог/)).toBeTruthy();
  });

  it("хозяину пункт оставляют, даже если рядом есть гость", async () => {
    renderActions(
      makeConversation({
        participants: [
          { id: ХОЗЯИН.id, full_name: ХОЗЯИН.full_name, reason: null, invited_at: null, kind: "self" },
        ],
      }),
    );
    await openMenu();

    expect(await screen.findByText(/Закрыть диалог/)).toBeTruthy();
  });

  it("тому, кто в диалог не заходил, пункт оставляют", async () => {
    // Закрывать чужой диалог из «Все» и «Разбора» приходилось 19 раз за 30
    // дней — эту дорогу правило не трогает.
    renderActions(makeConversation({ assignee: ХОЗЯИН, participants: [] }));
    await openMenu();

    expect(await screen.findByText(/Закрыть диалог/)).toBeTruthy();
  });
});
