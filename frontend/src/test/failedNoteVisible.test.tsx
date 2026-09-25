import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { MessageDto } from "@/shared/api/types";
import { fakeUser } from "./helpers";
import { CONV_ID, renderWithProviders } from "./render";

/**
 * ПРОВАЛИВШАЯСЯ ЗАМЕТКА НЕ ИМЕЕТ ПРАВА ВЫГЛЯДЕТЬ СОХРАНЁННОЙ.
 *
 * Крест, «Повторить» и «Удалить» рисовались только у `kind === "out"`, а
 * внутренняя заметка приходит в пузырь с `direction === "note"`. Оператор писал
 * «клиент просил перезвонить после 18:00, скидку не обещать», запрос падал — и
 * на экране оставалась обычная жёлтая плашка с именем автора и временем.
 * Заметки при этом не было нигде: ни у него, ни у сменщика, который назавтра
 * пообещает скидку. Потеря без единого следа хуже видимой ошибки: с ошибкой
 * человек хотя бы напишет второй раз.
 *
 * Отправляется заметка тем же путём и повторяется тем же `useRetryMessage`
 * (внутри он зовёт `sendNote`) — значит и разговор о неудаче обязан быть тот же.
 */

const TEMP_ID = "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59";

function note(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: TEMP_ID, // локальная строка очереди: id === client_message_id
    conversation_id: CONV_ID,
    direction: "note",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: "Клиент просил перезвонить после 18:00, скидку не обещать",
    attachments: [],
    delivery_status: "failed",
    client_message_id: TEMP_ID,
    created_at: "2026-08-12T10:12:00Z",
    ...overrides,
  } as MessageDto;
}

function renderNote(msg: MessageDto) {
  return renderWithProviders(
    <MessageBubble
      msg={msg}
      prev={null}
      clientId="client-1"
      clientName="Ольга Никитина"
      onRetry={() => {}}
      onDiscard={() => {}}
    />,
  );
}

describe("заметка, которая не сохранилась", () => {
  it("говорит об этом словами и даёт обе кнопки", () => {
    renderNote(note());

    expect(screen.getByText(/Заметка не сохранилась/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Удалить" })).toBeInTheDocument();
  });

  it("причину от сервера показывает рядом, а не прячет", () => {
    // «Не сохранилась» без причины — повод открыть поддержку; с причиной
    // («нет сети») человек просто дожидается связи и жмёт «Повторить».
    const { container } = renderNote(note({ delivery_error: "Нет соединения" }));

    expect(container.querySelector(".msg__failed-note")?.textContent).toContain(
      "Заметка не сохранилась: Нет соединения",
    );
  });

  it("в метаданных пузыря стоит крест, а не пустое место", () => {
    const { container } = renderNote(note());

    const failed = container.querySelector(".msg__meta .msg__status--failed");
    expect(failed).not.toBeNull();
    // Слова у значка свои: заметку никуда не доставляют, она СОХРАНЯЕТСЯ.
    expect(failed?.getAttribute("aria-label")).toBe("Не сохранилась");
  });

  it("пока заметка уходит — часы, а когда ушла — ни галочки, ни ошибки", () => {
    const pending = renderNote(note({ delivery_status: "pending" }));
    expect(screen.getByLabelText("Сохраняется")).toBeInTheDocument();
    expect(screen.queryByText(/не сохранилась/i)).not.toBeInTheDocument();
    pending.unmount();

    /*
     * У сохранённой заметки галочки НЕТ намеренно. «Доставлено» рядом с
     * записью, которую видят только свои, — обещание про клиента, которого
     * никто не давал; заметке нужны ровно два состояния, и оба тревожные.
     */
    const { container } = renderNote(note({ delivery_status: "delivered" }));
    expect(container.querySelector(".msg__status")).toBeNull();
    expect(container.querySelector(".msg__failed-note")).toBeNull();
  });

  it("у входящего сообщения клиента значков доставки по-прежнему нет", () => {
    // Проверка границы правки: «не доставлено» бывает только у того, что
    // отправляли МЫ. У чужого сообщения крест означал бы чужую беду.
    const { container } = renderNote(
      note({ direction: "in", sender_type: "client", sender: null, delivery_status: "failed" }),
    );

    expect(container.querySelector(".msg__status")).toBeNull();
    expect(container.querySelector(".msg__failed-note")).toBeNull();
  });
});
