import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { renderWithProviders } from "./render";
import userEvent from "@testing-library/user-event";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { MessageDto } from "@/shared/api/types";

/**
 * ОТВЕТ НА КОНКРЕТНОЕ СООБЩЕНИЕ (просьба владельца 02.09).
 *
 * ⚠ ЧЕСТНАЯ ГРАНИЦА, И ОНА ЗАКРЕПЛЕНА ТЕСТОМ НАМЕРЕННО. Половина просьбы —
 * «видно, на что ответил КЛИЕНТ» — невыполнима: у Авито цитирования в API нет.
 * Проверено тремя независимыми способами: 90 800 вебхуков (поля нет ни под
 * каким именем), 153 492 сообщения содержимого ответа ручки чтения (ни одного
 * незнакомого ключа — разбор складывает такие под их же именем) и полный
 * перечень полей верхнего уровня, снятый сторожем в бою 02.09. В приложении
 * Авито цитирование есть, наружу не отдаётся.
 *
 * Делается вторая половина: на что ответил ДИСПЕТЧЕР. Замер боя за неделю:
 * 5 575 из 21 196 наших ответов (26,3 %) уходят после двух и более сообщений
 * клиента подряд — там сегодня не остаётся никакого следа.
 */

function сообщение(over: Partial<MessageDto> = {}): MessageDto {
  return {
    id: "m-2",
    conversation_id: "c-1",
    direction: "out",
    sender_type: "operator",
    sender: { id: "u-1", full_name: "Пётр" },
    body: "8 900 ₽",
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-09-02T09:00:00Z",
    ...over,
  };
}

const ЦИТАТА = {
  id: "m-1",
  direction: "in" as const,
  body: "Сколько будет стоить замена экрана?",
  truncated: false,
  has_attachments: false,
};

describe("Ответ на сообщение", () => {
  it("цитата видна в пузыре и названа именем клиента", () => {
    render(
      <MessageBubble
        msg={сообщение({ reply_to_id: "m-1", reply_to: ЦИТАТА })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
      />,
    );
    expect(screen.getByText("Сколько будет стоить замена экрана?")).toBeInTheDocument();
    expect(screen.getByText("Иван")).toBeInTheDocument();
  });

  it("свою цитату подписываем «Вы», а не именем клиента", () => {
    render(
      <MessageBubble
        msg={сообщение({ reply_to_id: "m-0", reply_to: { ...ЦИТАТА, direction: "out" } })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
      />,
    );
    expect(screen.getByText("Вы")).toBeInTheDocument();
  });

  it("удалённое сообщение названо, а не показано пустотой", () => {
    /*
     * Связь осталась, показывать нечего. Пустая цитата выглядела бы ответом в
     * никуда — человек решил бы, что сломалась лента.
     */
    render(
      <MessageBubble
        msg={сообщение({ reply_to_id: "m-1", reply_to: null })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
      />,
    );
    expect(screen.getByText("Сообщение удалено")).toBeInTheDocument();
  });

  it("нажатие на цитату ведёт к оригиналу", () => {
    const перейти = vi.fn();
    render(
      <MessageBubble
        msg={сообщение({ reply_to_id: "m-1", reply_to: ЦИТАТА })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onGoToQuoted={перейти}
      />,
    );
    screen.getByText("Сколько будет стоить замена экрана?").closest("button")!.click();
    expect(перейти).toHaveBeenCalledWith("m-1");
  });

  it("«Ответить» отдаёт наверх само сообщение", async () => {
    const ответить = vi.fn();
    const msg = сообщение({ direction: "in", sender_type: "client", body: "А во сколько?" });
    // Входящее тянет запрос личности клиента — ему нужен провайдер запросов.
    renderWithProviders(
      <MessageBubble
        msg={msg}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onReply={ответить}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Ответить на это сообщение" }));
    expect(ответить).toHaveBeenCalledWith(msg);
  });

  it("без права отправки кнопки «Ответить» нет", () => {
    /* Отвечать нечем — предлагать нельзя: кнопка, которая ничего не делает,
       хуже её отсутствия. */
    render(
      <MessageBubble msg={сообщение()} prev={null} clientId="cl-1" clientName="Иван" />,
    );
    expect(screen.queryByRole("button", { name: "Ответить на это сообщение" })).toBeNull();
  });

  it("на заметку ответить не предлагаем", () => {
    /*
     * Заметку не видят наблюдатели без права `notes:read`, а цитата едет в
     * общем поле и прав не спрашивает: через неё снимок утёк бы мимо
     * ограничения. Сервер такую отправку отвергает 422 — кнопка не должна
     * доводить человека до отказа.
     */
    render(
      <MessageBubble
        msg={сообщение({ direction: "note", body: "Клиент скандальный" })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onReply={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Ответить на это сообщение" })).toBeNull();
  });

  it("обычное сообщение без цитаты не обзавелось лишним", () => {
    const { container } = render(
      <MessageBubble msg={сообщение()} prev={null} clientId="cl-1" clientName="Иван" />,
    );
    expect(container.querySelector(".msg__quote")).toBeNull();
  });
});
