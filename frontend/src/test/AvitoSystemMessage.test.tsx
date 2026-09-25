import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { renderWithProviders } from "./render";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { MessageDto } from "@/shared/api/types";

/**
 * Служебное сообщение Авито в ленте (требование владельца №10 от 11 августа:
 * «нет подсветки системных сообщений от Авито»).
 *
 * Проверяется ровно две вещи, и вторая важнее первой:
 * 1. запись Авито видно и она подписана источником;
 * 2. она НЕ пузырь — ни клиента, ни оператора, — и наша собственная системная
 *    запись от неё не изменилась. Стоило бы подать её пузырём, и оператор
 *    отвечал бы площадке как человеку.
 */

function base(): MessageDto {
  return {
    id: "m-1",
    conversation_id: "c-1",
    direction: "system",
    sender_type: "system",
    sender: null,
    body: "Статус: Новый → В работе. Иванов",
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-08-11T09:00:00Z",
  };
}

function avito(): MessageDto {
  return { ...base(), sender_type: "avito", body: "Объявление снято с публикации" };
}

describe("Служебное сообщение Авито в ленте", () => {
  it("видно, подписано источником и подано чипом, а не пузырём", () => {
    const { container } = render(
      <MessageBubble msg={avito()} prev={null} clientId="cl-1" clientName="Иван" />,
    );

    expect(screen.getByText(/Сообщение Авито/)).toBeTruthy();
    expect(screen.getByText(/Объявление снято с публикации/)).toBeTruthy();
    // Чип, а не пузырь: пузырь дал бы .msg__bubble и увёл запись Авито в
    // манеру переписки.
    expect(container.querySelector(".msg--system")).not.toBeNull();
    expect(container.querySelector(".msg__bubble")).toBeNull();
    expect(container.querySelector(".msg--in")).toBeNull();
    expect(container.querySelector(".msg--out")).toBeNull();
  });

  it("наша собственная системная запись подписи «Сообщение Авито» не получает", () => {
    render(<MessageBubble msg={base()} prev={null} clientId="cl-1" clientName="Иван" />);

    expect(screen.queryByText(/Сообщение Авито/)).toBeNull();
    expect(screen.getByText(/Статус: Новый → В работе/)).toBeTruthy();
  });

  it("текст показывается как пришёл — вид сообщения в слова не переводится", () => {
    // Перечня видов у Авито мы не знаем (docs/30), поэтому подпись у всех
    // одна. Выдуманная расшифровка вида была бы хуже отсутствующей: по ней
    // стали бы принимать решения.
    render(
      <MessageBubble
        msg={{ ...avito(), body: "Заказ по Авито Доставке отменён покупателем" }}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
      />,
    );

    expect(screen.getByText(/Заказ по Авито Доставке отменён покупателем/)).toBeTruthy();
  });
});

describe("Исторические служебные записи Авито (аудит 19.08)", () => {
  /**
   * ЧТО УВИДЕЛ ВЛАДЕЛЕЦ. В ленте среди слов клиента шли строки вида
   * «[Системное сообщение] ✏️ Пользователь создал чат, но пока ничего не
   * написал» — обычными пузырями, как реплика человека. Оператор отвечает на
   * сообщение, которого никто не писал.
   *
   * ПОЧЕМУ ТАК ВЫШЛО. С 17 августа сервер разбирает такие записи отдельной
   * веткой и кладёт их системными — новые метятся верно, и в базе после
   * правки таких «клиентских» записей ноль. Но 29 штук прежней поры подняла
   * загрузка истории: у них `direction: "in"`, `sender_type: "client"`.
   * Переписывать историю в базе ради показа нельзя, поэтому лента узнаёт их
   * по приставке — тем же признаком, каким узнаёт сервер.
   */
  function историческая(): MessageDto {
    return {
      ...base(),
      direction: "in",
      sender_type: "client",
      body: "[Системное сообщение] ✏️ Пользователь создал чат, но пока ничего не написал",
    };
  }

  it("историческая запись подана чипом Авито, а не словами клиента", () => {
    const { container } = render(
      <MessageBubble msg={историческая()} prev={null} clientId="cl-1" clientName="Иван" />,
    );

    expect(container.querySelector(".msg--system")).not.toBeNull();
    expect(screen.getByText(/Сообщение Авито/)).toBeTruthy();
    expect(container.querySelector(".msg__bubble")).toBeNull();
  });

  it("настоящее слово клиента осталось пузырём", () => {
    // Через провайдеры: у обычного сообщения живёт карточка клиента (запрос
    // личности), у служебного чипа её нет — потому соседним тестам провайдер
    // и не нужен.
    const { container } = renderWithProviders(
      <MessageBubble msg={базовое_клиентское()} prev={null} clientId="cl-1" clientName="Иван" />,
    );
    expect(container.querySelector(".msg--system")).toBeNull();
  });
});

function базовое_клиентское(): MessageDto {
  return {
    ...base(),
    direction: "in",
    sender_type: "client",
    body: "Здравствуйте, сколько стоит ремонт холодильника?",
  };
}
