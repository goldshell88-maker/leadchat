/**
 * Ctrl+Enter ОТПРАВЛЯЕТ СООБЩЕНИЕ (30.08).
 *
 * ПОЧЕМУ ЭТО ВООБЩЕ ПОНАДОБИЛОСЬ. `Mod+Enter` был вторым сочетанием на «принять
 * диалог», а композер Enter с модификатором намеренно не отправлял. То есть
 * привычное по Jivo, Telegram и почте «Ctrl+Enter — отправить» уходило в приём
 * и открывало ПОСТОРОННЕГО человека из очереди: открытый диалог был уже принят
 * и в очереди не значился, поэтому приём брал первого ждущего. Отсюда жалоба
 * владельца «чат сам переключается на другого клиента, сообщения уходят не тем
 * людям».
 *
 * Прыжок закрыт отдельным замком (PinnedDialog3008), но одного замка мало:
 * клавиша тогда просто молчит, привычка остаётся, и жалоба меняет вид на
 * «почему не отправляется». Поэтому Mod+Enter снят с приёма и делает то, чего
 * от него ждут.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { ACTIONS } from "@/features/hotkeys/catalog";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders, seedEmptyThread, threadMessages } from "./render";

describe("Ctrl+Enter — отправка, а не приём диалога", () => {
  const urls: string[] = [];

  beforeEach(() => {
    urls.length = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return jsonResponse(201, {
          id: "srv-1",
          conversation_id: CONV_ID,
          direction: "out",
          sender_type: "operator",
          sender: { id: fakeUser.id, full_name: fakeUser.full_name },
          body: "Добрый день!",
          attachments: [],
          delivery_status: "pending",
          client_message_id: "x",
          created_at: "2026-08-30T10:00:00Z",
        });
      }),
    );
  });

  it("приём больше не висит на Mod+Enter", () => {
    /*
     * ⚠ БЕЗ ЭТОЙ ПРОВЕРКИ ОСТАЛЬНЫЕ СТОРОЖИЛИ БЫ ПОЛОВИНУ ПОЧИНКИ. Верни
     * кто-нибудь сочетание в список приёма — композер продолжит отправлять, но
     * рядом снова начнёт срабатывать приём, и ловушка вернётся.
     */
    const приём = ACTIONS.find((a) => a.id === "claim");
    expect(приём?.defaults, "Mod+Enter снова принимает диалог — ловушка вернулась").not.toContain(
      "Mod+Enter",
    );
    expect(приём?.defaults, "приём остался без сочетания вовсе").toContain("Mod+KeyR");
  });

  it("Ctrl+Enter отправляет набранное", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Добрый день!");
    await user.keyboard("{Control>}{Enter}{/Control}");

    expect(threadMessages(), "сообщение не ушло — привычка Ctrl+Enter снова молчит").toHaveLength(1);
    expect(urls.some((u) => u.includes(`/conversations/${CONV_ID}/messages`))).toBe(true);
  });

  it("Shift+Enter по-прежнему переносит строку, а не отправляет", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Первая");
    await user.keyboard("{Shift>}{Enter}{/Shift}");

    expect(threadMessages(), "перенос строки отправил недописанное").toHaveLength(0);
  });

  it("Ctrl+Shift+Enter не отправляет — это «закрыть диалог»", async () => {
    /*
     * Сочетание занято закрытием (`close`, Mod+Shift+Enter). Отправь оно ещё и
     * сообщение — человек закрывал бы диалог, попутно отправив недописанное.
     */
    const user = userEvent.setup();
    renderWithProviders(<Composer convId={CONV_ID} conversation={makeConversation()} />);

    await user.type(screen.getByLabelText("Текст сообщения"), "Недописанное");
    await user.keyboard("{Control>}{Shift>}{Enter}{/Shift}{/Control}");

    expect(threadMessages(), "закрытие диалога отправило недописанное").toHaveLength(0);
  });
});
