import type {
  AccountRef,
  ClientRef,
  ConversationDto,
  DeliveryStatus,
  ItemRef,
  MessageDto,
  NotifyEventData,
  UserRef,
  VoiceTranscriptStatus,
} from "@/shared/api/types";

/**
 * Каталог серверных событий — 01 §11.2–11.3 (полный и нормативный).
 * Единый конверт: { type, ts, data }. Неизвестный type молча игнорируется
 * (forward-совместимость).
 */

/**
 * patch строки диалога (01 §11.3): изменённые поля объекта из §5.1; вложенные
 * объекты приходят частично (например client: {phone}); + unread_delta.
 */
export type ConversationPatch = Partial<
  Omit<ConversationDto, "client" | "account" | "item" | "assignee" | "last_message">
> & {
  client?: Partial<ClientRef>;
  account?: Partial<AccountRef>;
  item?: Partial<ItemRef> | null;
  assignee?: UserRef | null;
  /**
   * Кому принадлежит диалог — ТОЛЬКО идентификатор.
   *
   * ⚠ ЗАЧЕМ ОТДЕЛЬНО ОТ `assignee` (обратная связь диспетчера 02.09). Звук на
   * входящее играл у всех и по любому диалогу — за смену около шестисот
   * сигналов на человека, из которых его касается меньше десятой части.
   * Чтобы звенеть только по своим, экрану нужен хозяин диалога В КАДРЕ: из
   * кэша он известен не всегда, а неизвестность означала бы либо тишину по
   * своему диалогу, либо возврат шума.
   *
   * Имя сюда не кладём: на вопрос «мой ли это диалог» его не требуется, а
   * тянуть его — лишний поход в базу на каждое входящее.
   */
  assignee_id?: string | null;
  last_message?: ConversationDto["last_message"];
  unread_delta?: number;
};

export type WsServerEvent =
  | { type: "pong"; ts: string; data: { n: number } }
  | {
      type: "message:new";
      ts: string;
      data: {
        conversation_id: string;
        message: MessageDto;
        conversation_patch: ConversationPatch;
      };
    }
  | {
      type: "message:status";
      ts: string;
      data: {
        conversation_id: string;
        message_id: string;
        // `dismissed` — вопрос системы об адресе, который не дошёл до клиента
        // (`services/messages.py::undelivered_status`): долгом не становится, но
        // и «доставлено» не является.
        delivery_status: Extract<DeliveryStatus, "delivered" | "failed" | "dismissed">;
        error?: string;
        /**
         * То, что изменилось В СТРОКЕ СПИСКА от судьбы этого пузыря (#26):
         * «ответ не ушёл» и вернувшаяся отметка ожидания. Едет тем же кадром,
         * а не отдельным событием, — два кадра приходят порознь, и в зазоре
         * между ними строка показывала бы успешно отвеченный диалог.
         *
         * Необязательное: у `delivered` менять в строке нечего.
         */
        conversation_patch?: ConversationPatch;
      };
    }
  /**
   * Расшифровка голосового досчиталась (06.09).
   *
   * ⚠ ДО ЭТОГО КАДРА ТЕКСТ ЕХАЛ ТОЛЬКО ВМЕСТЕ С СООБЩЕНИЕМ, и подпись под
   * записью честно говорила «появится при следующем открытии диалога». На
   * скриншоте владельца 06.09 закрытый диалог с двумя голосовыми и без единой
   * строки текста: «не понимаю, где находится расшифровка». Whisper считает
   * запись секунды-минуты — дольше, чем человек смотрит на пузырь, — и без
   * кадра текст приезжал уже тому, кто в диалог вернулся.
   *
   * Строение как у `message:status`: адрес сообщения плюс те два поля строки,
   * которые изменились. `voice_transcript` при `failed`/`too_long` — `null`.
   */
  | {
      type: "message:transcript";
      ts: string;
      data: {
        conversation_id: string;
        message_id: string;
        voice_transcript: string | null;
        voice_transcript_status: VoiceTranscriptStatus;
      };
    }
  | {
      type: "conversation:updated";
      ts: string;
      data: {
        conversation_id: string;
        patch: ConversationPatch;
        /** Чем кончилось предложение передачи — только на кадрах его исхода. */
        transfer_outcome?: "accepted" | "declined" | "cancelled" | "expired";
        /** Кто принял, отказался или отменил; у истечения — null. */
        transfer_actor?: UserRef | null;
      };
    }
  | {
      type: "conversation:assigned";
      ts: string;
      data: {
        conversation_id: string;
        assignee: UserRef;
        assigned_by: UserRef;
        comment: string | null;
        is_for_you: boolean;
        /** Предложение ждёт «Принять / Отказаться»; false — прямое назначение. */
        offer?: boolean;
      };
    }
  | {
      type: "typing";
      ts: string;
      data: { conversation_id: string; source: "client" | "operator"; user?: UserRef };
    }
  /**
   * Состав зрителей диалога (SCEN-48). Приходит только тем, у кого этот диалог
   * открыт, и несёт список ЦЕЛИКОМ — «пришёл такой-то» тут не бывает, см.
   * `viewersStore`.
   */
  | {
      type: "conversation:viewers";
      ts: string;
      data: { conversation_id: string; viewers: UserRef[] };
    }
  | {
      /**
       * Карточка клиента изменилась на сервере — перезапроси её.
       *
       * ⚠ САМИХ ДАННЫХ В КАДРЕ НЕТ, И ЭТО НАМЕРЕННО. Повод завести кадр —
       * телефон, извлечённый из текста входящего (жалоба владельца 31.08
       * «чтобы номер привязался, приходится обновлять страницу»). Телефон —
       * персональные данные клиента, у карточки есть своя ручка с проверкой
       * прав, и рассылать номер всем подписчикам ради экономии одного запроса
       * нельзя. Кадр несёт только «что перезапросить».
       */
      type: "client:updated";
      ts: string;
      data: { client_id: string; conversation_id: string | null; reason?: string };
    }
  | {
      type: "presence:online";
      ts: string;
      data: { user_id: string; status: "online" | "away" | "offline" };
    }
  | {
      type: "message:deleted";
      ts: string;
      data: { conversation_id: string; message_id: string; direction?: string };
    }
  | {
      type: "account:backfill";
      ts: string;
      data: { account_id: string; phase?: string; processed?: number; total?: number | null };
    }
  | { type: "account:needs_reauth"; ts: string; data: { account_id: string; title: string } }
  /**
   * notify (01 §11.3) — две жизни у одного кадра (14 §4): без `id` это прежний
   * служебный тост, с `id` — строка центра уведомлений со своими полями
   * (kind, severity, entity, action, repeat_count). Тело описано один раз, в
   * `NotifyEventData`: расхождение объявления и разбора уже стоило приведения
   * типа на месте в applyWsEvent.
   */
  | { type: "notify"; ts: string; data: NotifyEventData };
