/**
 * «СНЯТЬ» У НЕОТПРАВЛЕННОГО (жалоба владельца 08.09: «статус „не отправлено“
 * не пропадает»).
 *
 * Красную метку диалога держит само наличие сообщения в отказе, и выхода из
 * этого состояния было ровно два: удачный повтор или ничего. Повтор помогает
 * не всегда — канал отвалился, диалог закрылся, текст устарел. Живой случай:
 * оператор не стал повторять, набрал тот же текст заново и отправил новым
 * сообщением; оно ушло, а метка осталась навсегда.
 *
 * ⚠ ГЛАВНОЕ — ЧТО СНЯТИЕ НЕ УДАЛЕНИЕ. Сообщение остаётся в переписке с
 * пометкой «снято»: клиент его не получил, и это факт разговора. Проверка,
 * смотрящая только на исчезновение красного, зеленела бы и у кода, который
 * пузырь просто стирает.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { MessageDto } from "@/shared/api/types";
import { fakeUser } from "./helpers";
import { renderWithProviders } from "./render";

const ОСНОВА: MessageDto = {
  id: "srv-9",
  conversation_id: "conv-1",
  direction: "out",
  sender_type: "operator",
  sender: { id: fakeUser.id, full_name: fakeUser.full_name },
  body: "смогу вам помочь, когда готовы меня принять?",
  attachments: [],
  delivery_status: "failed",
  client_message_id: "other-uuid",
  created_at: "2026-09-08T05:07:00Z",
};

describe("Снятие неотправленного", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("у серверного отказа есть «Снять», и она зовёт обработчик", async () => {
    const снять = vi.fn();
    renderWithProviders(
      <MessageBubble msg={ОСНОВА} prev={null} clientId="c1" clientName="Иван" onDismiss={снять} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Снять" }));
    expect(снять).toHaveBeenCalledWith(ОСНОВА);
  });

  it("снятое показывается как «снято», а не исчезает и не кричит", () => {
    renderWithProviders(
      <MessageBubble
        msg={{ ...ОСНОВА, delivery_status: "dismissed" }}
        prev={null}
        clientId="c1"
        clientName="Иван"
        onDismiss={() => {}}
      />,
    );
    /* Текст сообщения на месте: снятие не стирает переписку. */
    expect(screen.getByText(/смогу вам помочь/)).toBeInTheDocument();
    const подпись = screen.getByText(/Не отправлено · снято/);
    expect(подпись).toBeInTheDocument();
    /*
     * ⚠ И НЕ КРАСНЫМ. Красный цвет означает «сделай что-нибудь», а делать уже
     * нечего: человек решение принял. Проверяем класс, потому что цвет берётся
     * из токена и в jsdom не вычисляется.
     */
    expect(подпись.className).toMatch(/dismissed/);
    expect(screen.queryByRole("button", { name: "Снять" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
  });

  it("у местного пузыря «Снять» не показывают — там своё «Удалить»", () => {
    /*
     * Отрицательная проверка: два действия с одним смыслом рядом путали бы.
     * У местного (POST не дошёл) сообщения `id` равен `client_message_id`.
     */
    const местное = { ...ОСНОВА, id: ОСНОВА.client_message_id! };
    renderWithProviders(
      <MessageBubble
        msg={местное}
        prev={null}
        clientId="c1"
        clientName="Иван"
        onDismiss={() => {}}
        onDiscard={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Снять" })).toBeNull();
    expect(screen.getByRole("button", { name: "Удалить" })).toBeInTheDocument();
  });
});
