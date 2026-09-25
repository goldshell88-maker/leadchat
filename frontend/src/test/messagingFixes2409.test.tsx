import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import {
  useRetryMessage,
  useSendMessage,
  type SendVars,
} from "@/features/chats/hooks/useSendMessage";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDto, MessageDto } from "@/shared/api/types";
import { replaceMessageInCache } from "@/shared/realtime/applyWsEvent";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { showToast } from "@/shared/ui/toast";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import {
  CONV_ID,
  makeConversation,
  renderWithProviders,
  seedEmptyThread,
  threadMessages,
} from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

/**
 * СООБЩЕНИЯ: ПРАВКИ ПО ПРОВЕРКЕ 24.09.
 *
 *  - ответ POST приходит позже кадра «доставлено» и не откатывает его к часам;
 *  - ответ на сообщение клиента не теряет цитату ни при отказе, ни при повторе;
 *  - неотправленную реплику бота видно и её можно повторить: автора у неё нет,
 *    и прежнее правило «автор или администратор» не пускало никого;
 *  - файл, который сервер не принял, называет причину.
 */

const TEMP_ID = "018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a60";
const EMPTY_PAGE = {
  prev_cursor: null,
  next_cursor: null,
  has_more_before: false,
  has_more_after: false,
};

function outgoing(over: Partial<MessageDto> = {}): MessageDto {
  return {
    id: TEMP_ID,
    conversation_id: CONV_ID,
    direction: "out",
    sender_type: "operator",
    sender: { id: fakeUser.id, full_name: fakeUser.full_name },
    body: "Мастер будет в 14:00, устроит?",
    attachments: [],
    delivery_status: "pending",
    client_message_id: TEMP_ID,
    created_at: "2026-09-24T10:00:00Z",
    ...over,
  };
}

function clientMessage(): MessageDto {
  return {
    ...outgoing(),
    id: "msg-client-1",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "Когда приедет мастер?",
    delivery_status: "delivered",
    client_message_id: null,
  };
}

function seedThread(items: MessageDto[]): void {
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [{ items, page: EMPTY_PAGE }],
    pageParams: [null],
  });
}

function signIn(): void {
  resetSessionStore({
    user: fakeUser,
    permissions: fakeMe.permissions as never,
    accessToken: "t",
    bootstrapped: true,
  });
}

function SendHost({ vars }: { vars: SendVars }) {
  const send = useSendMessage(CONV_ID);
  return (
    <button type="button" onClick={() => send.mutate(vars)}>
      отправить
    </button>
  );
}

function RetryHost({ msg }: { msg: MessageDto }) {
  const retry = useRetryMessage(CONV_ID);
  return (
    <MessageBubble
      msg={msg}
      prev={null}
      clientId="client-1"
      clientName="Иван"
      onRetry={(m) => retry.mutate(m)}
    />
  );
}

describe("Сообщения — правки 24.09", () => {
  const requests: Array<{ url: string; method: string; body: unknown }> = [];
  let respond: (url: string, method: string) => Response;

  beforeEach(() => {
    requests.length = 0;
    queryClient.clear();
    signIn();
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();
    respond = () => jsonResponse(200, { items: [], page: EMPTY_PAGE });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        const body =
          typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : init?.body;
        requests.push({ url, method, body });
        return respond(url, method);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.mocked(showToast).mockClear();
  });

  it("ответ POST не откатывает «доставлено», пришедшее раньше него", () => {
    // Кадр message:new лёг серверным близнецом раньше ответа POST — уже доставленным.
    seedThread([outgoing(), outgoing({ id: "srv-1", delivery_status: "delivered" })]);

    act(() => {
      replaceMessageInCache(CONV_ID, TEMP_ID, outgoing({ id: "srv-1" }));
    });

    const rows = threadMessages();
    expect(rows.map((m) => m.id)).toEqual(["srv-1"]);
    expect(rows[0].delivery_status).toBe("delivered");
  });

  it("сбой доставки, пришедший раньше ответа POST, остаётся на экране вместе с причиной", () => {
    seedThread([
      outgoing(),
      outgoing({ id: "srv-2", delivery_status: "failed", delivery_error: "Авито: чат недоступен" }),
    ]);

    act(() => {
      replaceMessageInCache(CONV_ID, TEMP_ID, outgoing({ id: "srv-2" }));
    });

    const [row] = threadMessages();
    expect(row.delivery_status).toBe("failed");
    expect(row.delivery_error).toBe("Авито: чат недоступен");
  });

  it("отказ отправки возвращает в поле и текст, и цитату", async () => {
    respond = () =>
      jsonResponse(409, errorEnvelope("account_needs_reauth", "Аккаунт Авито отключён"));
    const quoted = clientMessage();
    const { container } = renderWithProviders(
      <SendHost
        vars={{
          text: "Мастер будет в 14:00, устроит?",
          isNote: false,
          tempId: "tmp-quote",
          ownsDraft: true,
          replyTo: quoted,
        }}
      />,
    );
    act(() => (container.querySelector("button") as HTMLButtonElement).click());

    await waitFor(() =>
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe(
        "Мастер будет в 14:00, устроит?",
      ),
    );
    expect(useChatUiStore.getState().drafts[CONV_ID]?.replyTo?.id).toBe(quoted.id);
  });

  it("повтор неотправленного ответа уходит с той же цитатой", async () => {
    const user = userEvent.setup();
    respond = () => jsonResponse(200, outgoing({ id: "srv-3" }));
    renderWithProviders(
      <RetryHost msg={outgoing({ delivery_status: "failed", reply_to_id: "msg-client-1" })} />,
    );

    await user.click(screen.getByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    const post = requests.find((r) => r.method === "POST");
    expect(post?.body).toMatchObject({
      client_message_id: TEMP_ID,
      reply_to_id: "msg-client-1",
    });
  });

  describe("неотправленная реплика бота", () => {
    function botReply(over: Partial<MessageDto> = {}): MessageDto {
      return outgoing({
        id: "srv-bot-1",
        sender_type: "bot",
        sender: null,
        body: "Подскажите, какая модель телефона?",
        delivery_status: "failed",
        delivery_error: "Авито: чат недоступен",
        client_message_id: null,
        ...over,
      });
    }

    const sizeProps = ["offsetHeight", "offsetWidth"] as const;
    const originalSizes = sizeProps.map((prop) =>
      Object.getOwnPropertyDescriptor(HTMLElement.prototype, prop),
    );

    beforeEach(() => {
      // Лента виртуальная и считает строки по размерам узла; в jsdom их нет.
      Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
        configurable: true,
        get: () => 900,
      });
      Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
        configurable: true,
        get: () => 600,
      });
    });

    afterEach(() => {
      sizeProps.forEach((prop, i) => {
        const original = originalSizes[i];
        if (original) Object.defineProperty(HTMLElement.prototype, prop, original);
        else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop];
      });
    });

    function openThread(items: MessageDto[]) {
      queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
      seedThread(items);
      respond = (url, method) =>
        method === "GET" && url.includes(`/conversations/${CONV_ID}/messages`)
          ? jsonResponse(200, { items, page: EMPTY_PAGE })
          : jsonResponse(200, { items: [], page: EMPTY_PAGE });
      return renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    }

    it("видна оператору диалога, и её можно повторить или снять", async () => {
      openThread([botReply()]);

      const bubble = (await screen.findByText("Подскажите, какая модель телефона?")).closest(
        '[role="listitem"]',
      ) as HTMLElement;
      expect(within(bubble).getByText(/Не доставлено: Авито: чат недоступен/)).toBeTruthy();
      expect(within(bubble).getByRole("button", { name: "Повторить" })).toBeTruthy();
    });

    it("доставленная реплика бота о доставке молчит", async () => {
      openThread([botReply({ delivery_status: "delivered", delivery_error: null })]);

      const bubble = (await screen.findByText("Подскажите, какая модель телефона?")).closest(
        '[role="listitem"]',
      ) as HTMLElement;
      expect(within(bubble).queryByRole("button", { name: "Повторить" })).toBeNull();
      expect(within(bubble).queryByText(/Не доставлено/)).toBeNull();
    });

    it("чужой неотправленный ответ коллеги по-прежнему не чинится", async () => {
      openThread([
        botReply({
          sender_type: "operator",
          sender: { id: "user-other", full_name: "Пётр Иванов" },
          body: "Ответ коллеги",
        }),
      ]);

      const bubble = (await screen.findByText("Ответ коллеги")).closest(
        '[role="listitem"]',
      ) as HTMLElement;
      expect(within(bubble).getByText(/Не доставлено/)).toBeTruthy();
      expect(within(bubble).queryByRole("button", { name: "Повторить" })).toBeNull();
    });
  });

  it("файл, который сервер не принял, называет причину словами сервера", async () => {
    const user = userEvent.setup();
    respond = (url, method) =>
      method === "POST" && url.includes("/media")
        ? jsonResponse(422, errorEnvelope("media_rejected", "Такой файл отправить нельзя"))
        : jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
    const conversation = { ...makeConversation(), status: "in_progress" } as ConversationDto;
    const { container } = renderWithProviders(
      <Composer convId={CONV_ID} conversation={conversation} />,
    );

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["x"], "photo.jpg", { type: "image/jpeg" }));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Не загрузилось: photo.jpg",
          message: "Такой файл отправить нельзя",
        }),
      ),
    );
    expect(screen.getByText("не загрузилось").getAttribute("title")).toBe(
      "Такой файл отправить нельзя",
    );
  });
});
