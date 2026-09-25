import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useNotificationStore } from "@/features/notifications/store";
import type { ConversationDto, MessageDto, NotifyEventData } from "@/shared/api/types";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";

/**
 * Уведомления браузера 07.09 — жалоба владельца дословно: «говорят, что не
 * работают уведомления… нужны уведомления, когда пришёл диалог, когда передали
 * диалог, чтобы можно было принять его или отклонить там же в уведомлении».
 *
 * Проверяем то, что решает эта часть работы: КАКОЙ запрос уходит в мост.
 * Показывать его или промолчать — дело моста (web.ts / tauri), и у него свои
 * проверки; здесь стережём продуктовый смысл события — вид, ключ склейки,
 * кнопки, живучесть карточки — и адресность: карточка появляется ровно тогда,
 * когда сервер пометил событие как «про тебя».
 */

const МОЙ = fakeUser.id;
const ЧУЖОЙ = "11111111-2222-3333-4444-555555555555";

function строка(id: string, overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id,
    status: "new",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: "Иван Петров", phone: null, avito_rating: null },
    assignee: null,
    item: { title: "Ремонт iPhone 13", url: null, price: null },
    last_message: { body: "актуально?", direction: "in", created_at: "2026-09-07T09:00:00Z" },
    unread_count: 1,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-09-07T09:00:00Z",
    ...overrides,
  };
}

function входящее(convId: string): MessageDto {
  return {
    id: `m-${convId}`,
    conversation_id: convId,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "Здравствуйте, актуально?",
    attachments: [],
    delivery_status: "delivered",
    created_at: "2026-09-07T09:05:00Z",
  } as MessageDto;
}

/** Кадр центра уведомлений; по умолчанию — адресный (`audience_hint` не задан). */
function запись(overrides: Partial<NotifyEventData> = {}): NotifyEventData {
  return {
    id: "n-1",
    level: "warning",
    severity: "warning",
    kind: "conversation.awaiting_you",
    title: "Клиент ждёт вашего ответа",
    text: "20 минут",
    entity: { type: "conversation", id: "conv-1" },
    ...overrides,
  };
}

describe("Уведомления браузера 07.09", () => {
  let calls: ReturnType<typeof makeFakeTauriBridge>["calls"];

  beforeEach(() => {
    const fake = makeFakeTauriBridge();
    calls = fake.calls;
    enterTauriRuntime(fake.bridge);
    // `users:manage` — чтобы административная рассылка ДОХОДИЛА до разбора:
    // иначе её отсеет фильтр получателей, и проверка «до окна не дошло»
    // зеленела бы по чужой причине (см. `audienceAllowed`).
    resetSessionStore({
      user: fakeUser,
      permissions: [...fakeMe.permissions, "users:manage", "audit:read"] as never,
    });
    useChatUiStore.setState({ soundEnabled: false, activeConversationId: null });
    useInboxStore.setState({ ids: {}, escalatedIds: {}, count: 2, escalated: 0, claimedNotice: null });
    useNotificationStore.getState().clear();
  });

  afterEach(() => {
    leaveTauriRuntime();
    useNotificationStore.getState().clear();
    useInboxStore.getState().clear();
  });

  describe("Пришёл диалог (`inbox:new`)", () => {
    /**
     * ДИВЕРСИЯ: в `applyInboxNew` (applyWsEvent.ts) убрать вызов
     * `toastForInbox(...)`, оставив один `playInboxChime()` — то есть вернуть
     * состояние до 07.09, когда приход диалога не уведомлял вообще.
     */
    it("новый диалог в очереди уходит в мост карточкой с кнопками решения", () => {
      applyWsEvent({
        type: "inbox:new",
        ts: "2026-09-07T09:00:00Z",
        data: { conversation_id: "q-1", conversation: строка("q-1"), can_claim: true },
      });

      expect(calls.notify).toHaveBeenCalledTimes(1);
      expect(calls.notify.mock.calls[0][0]).toMatchObject({
        kind: "inbox",
        // ОДИН тег на всю очередь: ~2000 постановок в сутки, каждая адресована
        // десяткам людей. Свой тег на диалог — это стопка карточек.
        tag: "lc-inbox",
        conversationId: "q-1",
        // Вторая кнопка появилась 07.09 вместе с `inbox-decline`; её
        // собственные проверки — в `InboxDeclineFromNotify0709.test.tsx`.
        actions: [
          { action: "claim", title: "Принять" },
          { action: "inbox-decline", title: "Отклонить" },
        ],
      });
    });

    /**
     * ДИВЕРСИЯ: в `toastForInbox` (platform/toast.ts) заменить тело карточки на
     * имя клиента — `body: titleFor(findRow(convId))`.
     */
    it("в карточке размер очереди, а не имя клиента и не переписка", () => {
      applyWsEvent({
        type: "inbox:new",
        ts: "2026-09-07T09:00:00Z",
        data: { conversation_id: "q-1", conversation: строка("q-1"), can_claim: true },
      });

      const req = calls.notify.mock.calls[0][0];
      // Стор знал о двух; этот — третий.
      expect(req.body).toBe("3 диалога ждут во «Входящих»");
      expect(req.body).not.toContain("Иван Петров");
      expect(req.body).not.toContain("актуально");
      // Карточка всплывает на экране блокировки: там не должно быть и канала.
      expect(req.attribution).toBeUndefined();
    });

    /**
     * ДИВЕРСИЯ: в `applyInboxNew` вынести `toastForInbox(...)` из-под
     * `if (!returned)` — карточка станет приходить и на возврат.
     */
    it("диалог, вернувшийся после своего отказа, карточки не даёт", () => {
      applyWsEvent({
        type: "inbox:new",
        ts: "2026-09-07T09:00:00Z",
        data: {
          conversation_id: "q-9",
          conversation: строка("q-9"),
          can_claim: true,
          returned: true,
        },
      });

      expect(calls.notify).not.toHaveBeenCalled();
    });

    /**
     * ДИВЕРСИЯ: в `applyInboxNew` снять ранний выход `if (!canClaimNow(canClaim)) return;`.
     * Руководитель и наблюдатель видят очередь, но по ней не работают — карточка
     * с кнопкой «Принять» у них ложь.
     */
    it("без серверного `can_claim` карточки нет", () => {
      applyWsEvent({
        type: "inbox:new",
        ts: "2026-09-07T09:00:00Z",
        data: { conversation_id: "q-2", conversation: строка("q-2"), can_claim: false },
      });

      expect(calls.notify).not.toHaveBeenCalled();
    });
  });

  describe("Передали диалог (`conversation:assigned`)", () => {
    function передать(is_for_you: boolean, convId = "conv-1") {
      applyWsEvent({
        type: "conversation:assigned",
        ts: "2026-09-07T09:10:00Z",
        data: {
          conversation_id: convId,
          assignee: { id: is_for_you ? МОЙ : ЧУЖОЙ, full_name: "Кому передали" },
          assigned_by: { id: ЧУЖОЙ, full_name: "Пётр Сидоров" },
          comment: null,
          is_for_you,
        },
      });
    }

    /**
     * ДИВЕРСИЯ: в `toastForHandoff` (platform/toast.ts) убрать `actions` и
     * `requireInteraction` — вернуть карточку без кнопок, гаснущую через пять
     * секунд, то есть ровно то, чего не хватало владельцу.
     */
    it("передача даёт карточку с «Принять» и «Отклонить» и не гаснет сама", () => {
      передать(true);

      expect(calls.notify).toHaveBeenCalledTimes(1);
      expect(calls.notify.mock.calls[0][0]).toMatchObject({
        kind: "handoff",
        isForYou: true,
        conversationId: "conv-1",
        tag: "lc-handoff-conv-1",
        requireInteraction: true,
        actions: [
          { action: "accept", title: "Принять" },
          { action: "decline", title: "Отклонить" },
        ],
      });
    });

    /**
     * ДИВЕРСИЯ: в ветке `conversation:assigned` (applyWsEvent.ts) вызвать
     * `toastForHandoff` вне `if (e.data.is_for_you)` — карточку «Диалог передан
     * вам» получат все, кому пришёл кадр.
     */
    it("чужая передача карточки не даёт", () => {
      передать(false);

      expect(calls.notify).not.toHaveBeenCalled();
    });
  });

  describe("Записи центра уведомлений", () => {
    /**
     * ДИВЕРСИЯ: в `wsNotify.ts` убрать `if (личноеДляОкна(record)) …` —
     * состояние до 07.09, когда мост звали только при `severity === "critical"`,
     * а все критичные виды каталога адресованы администраторам.
     */
    it("личная запись доходит до окна браузера", () => {
      applyWsEvent({ type: "notify", ts: "2026-09-07T09:20:00Z", data: запись() });

      expect(calls.notify).toHaveBeenCalledTimes(1);
      expect(calls.notify.mock.calls[0][0]).toMatchObject({
        kind: "system",
        title: "Клиент ждёт вашего ответа",
        conversationId: "conv-1",
        tag: "lc-n-conversation.awaiting_you:conv-1",
      });
    });

    /**
     * ДИВЕРСИЯ: в `личноеДляОкна` (wsNotify.ts) убрать проверку
     * `record.audience === null` — рассылка по роли поедет в окно каждому, кто
     * её видит, то есть карточками о чужой работе.
     */
    it("административная рассылка до окна не доходит", () => {
      applyWsEvent({
        type: "notify",
        ts: "2026-09-07T09:20:00Z",
        data: запись({
          id: "n-2",
          kind: "inbound.stalled",
          title: "Приём сообщений остановился",
          audience_hint: "admin",
          entity: { type: "account", id: "acc-1" },
        }),
      });

      // Строку центра человек получил — а окна браузера нет.
      expect(useNotificationStore.getState().items).toHaveLength(1);
      expect(calls.notify).not.toHaveBeenCalled();
    });

    /**
     * ДИВЕРСИЯ: в `wsNotify.ts` заменить `ЛИЧНЫЕ_В_ОКНО.has(record.kind)` на
     * `true` — тогда в окно поедут все восемь личных видов, включая те три,
     * что уже сказаны другим способом.
     */
    it("личный вид не из списка окна не открывает", () => {
      applyWsEvent({
        type: "notify",
        ts: "2026-09-07T09:20:00Z",
        data: запись({
          id: "n-3",
          level: "info",
          severity: "info",
          kind: "conversation.closed_by_other",
          title: "Ваш диалог закрыл коллега",
        }),
      });

      expect(useNotificationStore.getState().items).toHaveLength(1);
      expect(calls.notify).not.toHaveBeenCalled();
    });

    /**
     * `conversation.assigned` адресуется получателю передачи — тому же
     * человеку и о том же событии, о котором уже сказала карточка `handoff` с
     * кнопками. Теги у них разные, склеиться они не могут: вышло бы две
     * карточки об одном, причём вторая — без кнопок.
     *
     * ДИВЕРСИЯ: добавить `"conversation.assigned"` в `ЛИЧНЫЕ_В_ОКНО`.
     */
    it("«вам передали диалог» из центра второй карточки не даёт", () => {
      передатьИЗаписать();

      // Ровно одна карточка на событие — та, что с кнопками.
      expect(calls.notify).toHaveBeenCalledTimes(1);
      expect(calls.notify.mock.calls[0][0]).toMatchObject({ kind: "handoff" });
    });

    function передатьИЗаписать() {
      applyWsEvent({
        type: "conversation:assigned",
        ts: "2026-09-07T09:30:00Z",
        data: {
          conversation_id: "conv-1",
          assignee: { id: МОЙ, full_name: "Я" },
          assigned_by: { id: ЧУЖОЙ, full_name: "Пётр Сидоров" },
          comment: null,
          is_for_you: true,
        },
      });
      applyWsEvent({
        type: "notify",
        ts: "2026-09-07T09:30:01Z",
        data: запись({
          id: "n-4",
          level: "info",
          severity: "info",
          kind: "conversation.assigned",
          title: "Вам передали диалог",
        }),
      });
    }

    /**
     * ⚠ КАРТОЧКА ПРЕДЛОЖЕНИЯ САМА НЕ ГАСНЕТ. Передающий забрал диалог назад, а
     * у получателя на экране по-прежнему «Принять / Отклонить» по тому, чего
     * ему больше не предлагают. Тот же тег заставляет браузер ПОДМЕНИТЬ
     * карточку вместо того, чтобы положить рядом.
     *
     * ДИВЕРСИЯ: в `тегЗаписиЦентра` (platform/toast.ts) убрать ветку
     * `ОТМЕНА_ПЕРЕДАЧИ` — тег станет обычным `lc-n-…`, и предложение повиснет.
     */
    it("отмена передачи замещает карточку предложения — тем же тегом", () => {
      applyWsEvent({
        type: "notify",
        ts: "2026-09-07T09:40:00Z",
        data: запись({
          id: "n-5",
          level: "info",
          severity: "info",
          kind: "conversation.transfer_cancelled",
          title: "Передачу отменили — принимать нечего",
        }),
      });

      expect(calls.notify).toHaveBeenCalledTimes(1);
      const req = calls.notify.mock.calls[0][0];
      expect(req.tag).toBe("lc-handoff-conv-1");
      // Замещающая карточка обязана гаснуть сама — иначе на месте одной
      // несгораемой появится другая.
      expect(req.requireInteraction).not.toBe(true);
    });
  });

  describe("Сообщения: только свой диалог", () => {
    function сообщение(assigneeId: string | null) {
      applyWsEvent({
        type: "message:new",
        ts: "2026-09-07T09:05:00Z",
        data: {
          conversation_id: "conv-7",
          message: входящее("conv-7"),
          conversation_patch: { assignee_id: assigneeId, unread_delta: 1 },
        },
      });
    }

    /**
     * ⚠ ГРАНИЦА: 4705 входящих от клиентов в сутки на 30–33 человека с общими
     * каналами. Уведомлять о каждом — это выключенные уведомления.
     *
     * ДИВЕРСИЯ: в `applyNewMessage` (applyWsEvent.ts) заменить
     * `if (я !== undefined && хозяин === я)` на `if (я !== undefined)`.
     */
    it("входящее в чужом диалоге карточки не даёт", () => {
      сообщение(ЧУЖОЙ);

      expect(calls.notify).not.toHaveBeenCalled();
    });

    it("входящее в своём диалоге — даёт", () => {
      сообщение(МОЙ);

      expect(calls.notify).toHaveBeenCalledTimes(1);
      expect(calls.notify.mock.calls[0][0]).toMatchObject({
        kind: "message",
        conversationId: "conv-7",
      });
    });
  });
});
