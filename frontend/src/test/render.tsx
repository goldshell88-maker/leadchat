import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import type { ConversationDetailDto, MessageDto, MessagesPage } from "@/shared/api/types";
import { qk } from "@/shared/api/queryKeys";
import { fakeUser } from "./helpers";

/**
 * Провайдеры как в приложении. КЛЮЧЕВОЕ: используется тот же singleton
 * `queryClient`, что и в `applyWsEvent`/`useSendMessage` — иначе оптимистичные
 * патчи улетали бы в другой кэш, и тест проверял бы не то, что работает в проде.
 */
export function wrap(ui: ReactElement, route = "/chats") {
  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>
  );
}

export function renderWithProviders(ui: ReactElement, { route = "/chats" }: { route?: string } = {}) {
  return render(wrap(ui, route));
}

export const CONV_ID = "conv-1";

export function makeConversation(overrides: Partial<ConversationDetailDto> = {}): ConversationDetailDto {
  return {
    id: CONV_ID,
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: "client-1", name: "Иван Петров", phone: null, avito_rating: 4.9 },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт iPhone 13", url: null, price: "от 1500 ₽" },
    last_message: null,
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: null,
    bot_vars: {},
    external_chat_id: "u2i-abc123",
    first_client_at: null,
    client_conversations_count: 1,
    ...overrides,
  };
}

/** Пустая, но СУЩЕСТВУЮЩАЯ лента: appendMessage не создаёт кэш с нуля (03 §3.3). */
export function seedEmptyThread(convId = CONV_ID): void {
  queryClient.setQueryData(qk.messages.list(convId), {
    pages: [
      {
        items: [] as MessageDto[],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

/** Сообщения ленты из кэша — на них смотрят тесты оптимистичной отправки. */
export function threadMessages(convId = CONV_ID): MessageDto[] {
  const data = queryClient.getQueryData<{ pages: MessagesPage[] }>(qk.messages.list(convId));
  return data?.pages.flatMap((p) => p.items) ?? [];
}
