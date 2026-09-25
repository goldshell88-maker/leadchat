import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import { UNSUPPORTED_MESSAGE_TEXT } from "@/shared/lib/messagePreview";
import type { ConversationDto, MessageDto } from "@/shared/api/types";
import { fakeUser } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ПРАВКА 8: «…» в ленте против «Вложение» в списке.
 *
 * Как это выглядело у проверяющего. 11 августа в 12:14 UTC в диалог приходит
 * сообщение 00000000-0811-4000-8000-000000000001. В строке списка написано
 * «Вложение» — диспетчер открывает диалог посмотреть, что прислал клиент, и
 * видит «…». Ни файла, ни текста; за день это повторяется столько раз,
 * сколько клиентов прислали голосовое или геопозицию.
 *
 * Корень был на сервере: разбор истории не знал нетекстовых видов Авито и
 * сохранял запись с `body=null` и пустыми вложениями (вылечено в
 * `app/integrations/avito/adapter.py`). Здесь проверяется интерфейс — и то,
 * ради чего он правился: об одном сообщении система говорит ОДНО.
 */

const AT = "2026-08-11T12:14:00Z";

function message(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: "00000000-0811-4000-8000-000000000001",
    conversation_id: "conv-1",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: null,
    attachments: [],
    delivery_status: "delivered",
    created_at: AT,
    ...overrides,
  };
}

function renderBubble(msg: MessageDto) {
  return renderWithProviders(
    <MessageBubble msg={msg} prev={null} clientId="client-1" clientName="Ольга Никитина" />,
  );
}

function row(body: string | null): ConversationDto {
  return {
    id: "conv-1",
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: "client-1", name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт ноутбуков", url: null, price: null },
    last_message: { body, direction: "in", created_at: AT },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: AT,
  } as ConversationDto;
}

describe("входящее, у которого нечего показать", () => {
  it("в ленте больше нет многоточия", () => {
    const { container } = renderBubble(message());

    expect(screen.getByText(UNSUPPORTED_MESSAGE_TEXT)).toBeTruthy();
    expect(container.textContent).not.toContain("…");
  });

  it("подпись-заглушка отличается от слов клиента", () => {
    // Иначе оператор отвечает на фразу, которую написали не ему.
    renderBubble(message());

    expect(
      screen.getByText(UNSUPPORTED_MESSAGE_TEXT).classList.contains("msg__text--unsupported"),
    ).toBe(true);
  });

  it("настоящий текст остаётся обычным текстом", () => {
    renderBubble(message({ body: "Здравствуйте, когда мастер?" }));

    const p = screen.getByText("Здравствуйте, когда мастер?");
    expect(p.classList.contains("msg__text--unsupported")).toBe(false);
  });

  it("под фотографией подписи не появляется", () => {
    // Вложение рисует себя само; слово «Фотография» рядом было бы вторым
    // таким же словом в одном пузыре.
    const { container } = renderBubble(
      message({
        attachments: [
          { media_id: "avito_image_1", kind: "image", name: "Фотография", url: "https://a/b.jpg" },
        ],
      }),
    );

    expect(container.querySelector(".msg__text")).toBeNull();
    expect(container.querySelector(".msg__image")).toBeTruthy();
  });
});

describe("список и лента об одном сообщении", () => {
  it("говорят одними словами", () => {
    /*
     * Так выглядит сообщение ПОСЛЕ правки сервера: вид «Геопозиция» разобран,
     * человекочитаемое описание положено в `body` — единственное поле, которое
     * видят оба места (`ConversationDto.last_message` вложений не несёт).
     * Пока подпись сочинял каждый сам, здесь стояли «…» и «Вложение».
     */
    const { unmount } = renderBubble(message({ body: "Геопозиция" }));
    expect(screen.getByText("Геопозиция")).toBeTruthy();
    unmount();

    renderWithProviders(
      <ConversationListItem row={row("Геопозиция")} active={false} now={Date.parse(AT)} onOpen={() => {}} showChannel />,
    );
    expect(screen.getByText("Геопозиция")).toBeTruthy();
  });
});
