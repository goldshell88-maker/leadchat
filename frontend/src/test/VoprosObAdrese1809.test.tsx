import { beforeEach, describe, expect, it } from "vitest";
import type { InfiniteData } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import { kindOf } from "@/features/chats/components/thread/messageSeries";
import { describeFrame } from "@/features/feed/describe";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, ConversationsPage, MessageDto } from "@/shared/api/types";
import { applyNewMessage } from "@/shared/realtime/applyWsEvent";
import type { WsServerEvent } from "@/shared/realtime/wsEvents";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ВОПРОС СИСТЕМЫ ОБ АДРЕСЕ В ЛЕНТЕ (владелец 18.09).
 *
 * Сервер пишет исходящее `('out','system')` — единственный писатель
 * `workers/address_ask.py`. Лента обязана назвать автора честно: не «Бот»
 * (бота могло и не быть), не «Вы:» в превью (оператор этого не писал), не
 * «ответ отправлен» в ленте событий (клиенту никто не ответил). Недоставленный
 * вопрос (`dismissed`, `services/messages.py::undelivered_status`) — серая
 * подпись без «Повторить»/«Снять»: долга нет, повторять некому.
 */

const ВОПРОС = "Подскажите, пожалуйста, адрес: улица, дом, подъезд";

function система(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    id: "m-sys",
    conversation_id: "c-1",
    direction: "out",
    sender_type: "system",
    sender: null,
    body: ВОПРОС,
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-09-18T10:10:00Z",
    ...overrides,
  };
}

function бот(overrides: Partial<MessageDto> = {}): MessageDto {
  return { ...система(), id: "m-bot", sender_type: "bot", body: "Здравствуйте! Чем помочь?", ...overrides };
}

beforeEach(() => {
  resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
});

describe("Пузырь вопроса системы", () => {
  it("раскладывается как пузырь бота, а не оператора", () => {
    expect(kindOf(система())).toBe("bot");
    expect(kindOf(бот())).toBe("bot");
    expect(kindOf({ ...система(), sender_type: "operator" })).toBe("out");
  });

  it("подписан «Система», а пузырь бота по-прежнему «Бот»", () => {
    renderWithProviders(
      <MessageBubble msg={система()} prev={null} clientId="cl-1" clientName="Иван" />,
    );
    expect(screen.getByText(/Система/)).toBeTruthy();
    expect(screen.queryByText(/^\s*Бот\s*$/)).toBeNull();

    renderWithProviders(<MessageBubble msg={бот()} prev={null} clientId="cl-1" clientName="Иван" />);
    expect(screen.getByText(/Бот/)).toBeTruthy();
  });

  it("недоставленный вопрос: «Не доставлено» с причиной и без кнопок", () => {
    renderWithProviders(
      <MessageBubble
        msg={система({ delivery_status: "dismissed", delivery_error: "чат закрыт" })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onRetry={() => {}}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByText(/Не доставлено · чат закрыт/)).toBeTruthy();
    expect(screen.queryByText("Повторить")).toBeNull();
    expect(screen.queryByText("Снять")).toBeNull();
  });

  it("пузырь бота при dismissed — как раньше: подписи о недоставке нет", () => {
    renderWithProviders(
      <MessageBubble
        msg={бот({ delivery_status: "dismissed" })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
      />,
    );
    expect(screen.queryByText(/Не доставлено/)).toBeNull();
  });
});

describe("Строка списка и лента событий", () => {
  function row(lm: ConversationDto["last_message"]): ConversationDto {
    return {
      id: "c-1",
      status: "new",
      channel: "avito",
      account: { id: "acc-1", title: "LP-Москва" },
      client: { id: "cl-1", name: "Иван", phone: null, avito_rating: null },
      assignee: null,
      item: null,
      last_message: lm,
      unread_count: 0,
      bot_active: false,
      tags: [],
      transferred_to_me: false,
      last_message_at: "2026-09-18T10:10:00Z",
    };
  }

  it("превью подписано «Система:», а исходящее оператора — «Вы:»", () => {
    renderWithProviders(
      <ConversationListItem
        row={row({
          body: ВОПРОС,
          direction: "out",
          created_at: "2026-09-18T10:10:00Z",
          sender_type: "system",
        } as ConversationDto["last_message"])}
        active={false}
        showChannel={false}
        now={Date.parse("2026-09-18T10:11:00Z")}
        onOpen={() => {}}
      />,
    );
    expect(screen.getByText(`Система: ${ВОПРОС}`)).toBeTruthy();

    renderWithProviders(
      <ConversationListItem
        row={row({ body: "Перезвоню", direction: "out", created_at: "2026-09-18T10:10:00Z" })}
        active={false}
        showChannel={false}
        now={Date.parse("2026-09-18T10:11:00Z")}
        onOpen={() => {}}
      />,
    );
    expect(screen.getByText("Вы: Перезвоню")).toBeTruthy();
  });

  it("в ленте событий — «система спросила адрес» нейтральным тоном, оператор — как было", () => {
    const кадр = (message: MessageDto): WsServerEvent =>
      ({
        type: "message:new",
        ts: "2026-09-18T10:10:00Z",
        data: { conversation_id: "c-1", message, conversation_patch: {} },
      }) as WsServerEvent;

    const системы = describeFrame(кадр(система()));
    expect(системы?.text).toBe("система спросила адрес");
    expect(системы?.tone).toBe("neutral");

    const оператора = describeFrame(
      кадр({ ...система(), sender_type: "operator", sender: { id: "u-1", full_name: "Пётр Иванов" } }),
    );
    expect(оператора?.text).toBe("ответил Пётр Иванов");
    expect(оператора?.tone).toBe("good");
  });

  it("недоставленный вопрос системы в ленте событий — не «ответ доставлен»", () => {
    const строка = describeFrame({
      type: "message:status",
      ts: "2026-09-18T10:10:05Z",
      data: {
        conversation_id: "c-1",
        message_id: "m-sys",
        delivery_status: "dismissed",
        error: "чат закрыт",
      },
    } as WsServerEvent);
    expect(строка?.text).toBe("вопрос системы не доставлен — чат закрыт");
    expect(строка?.tone).toBe("neutral");
  });

  it("«Снять» оператора (dismissed без причины) — не «вопрос системы» и не «ответ доставлен»", () => {
    // Тот же статус шлёт ручка `POST /messages/{id}/dismiss`; кадр без `error`.
    // ДИВЕРСИЯ: убрать различение по `reason` — строка припишет системе то,
    // что снял с учёта человек.
    const строка = describeFrame({
      type: "message:status",
      ts: "2026-09-18T10:10:05Z",
      data: {
        conversation_id: "c-1",
        message_id: "m-op",
        delivery_status: "dismissed",
        conversation_patch: { undelivered: false },
      },
    } as WsServerEvent);
    expect(строка?.text).toBe("неотправленное снято с учёта");
    expect(строка?.tone).toBe("neutral");
    expect(строка?.text).not.toContain("системы");
  });
});

describe("Кадр message:new и строка списка", () => {
  it("кладёт sender_type в last_message — иначе до перезагрузки превью подписано «Вы:»", () => {
    // Сервер отдаёт `sender_type` в каждой строке (`conversations.conversation_out`),
    // а строка, собранная из кадра, до 18.09 его теряла. ДИВЕРСИЯ: убрать поле из
    // `applyNewMessage` — проверка краснеет, а на экране «Вы: Подскажите…».
    queryClient.clear();
    const фильтры = { tab: "mine" } as const;
    const строка: ConversationDto = {
      id: "c-1",
      status: "new",
      channel: "avito",
      account: { id: "acc-1", title: "LP-Москва" },
      client: { id: "cl-1", name: "Иван", phone: null, avito_rating: null },
      assignee: null,
      item: null,
      last_message: { body: "Экран разбит", direction: "in", created_at: "2026-09-18T10:00:00Z" },
      unread_count: 1,
      bot_active: false,
      tags: [],
      transferred_to_me: false,
      last_message_at: "2026-09-18T10:00:00Z",
    };
    const страница: InfiniteData<ConversationsPage> = {
      pages: [{ items: [строка], page: { limit: 50, offset: 0, total: 1 } }],
      pageParams: [0],
    };
    queryClient.setQueryData(qk.conversations.list(фильтры), страница);

    applyNewMessage("c-1", система(), { unread_delta: 0 });

    const после = queryClient.getQueryData<InfiniteData<ConversationsPage>>(
      qk.conversations.list(фильтры),
    );
    const lm = после?.pages[0].items[0].last_message as (ConversationDto["last_message"] & {
      sender_type?: string;
    }) | null;
    expect(lm?.direction).toBe("out");
    expect(lm?.sender_type).toBe("system");
    expect(lm?.body).toBe(ВОПРОС);
  });
});
