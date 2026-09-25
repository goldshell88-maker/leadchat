import { useMutation } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { scheduleListRefetch } from "@/shared/realtime/listRefetch";
import { getBridge, getBridgeOrNull, isTauri } from "@/platform/bridge";
import { qk } from "@/shared/api/queryKeys";
import type { AttachmentDto, ConversationDetailDto, MessageDto } from "@/shared/api/types";
import {
  appendMessage,
  applyNewMessage,
  patchMessageInCache,
  replaceMessageInCache,
} from "@/shared/realtime/applyWsEvent";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { пересобратьПорядок } from "@/features/chats/components/list/listOrder";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { dismissMessage, retryMessage, sendMessage, sendNote } from "../api";
import { ApiError } from "@/shared/api/http";
import { showToast } from "@/shared/ui/toast";
import { applyOutboxReport, dropOptimisticRow, markOutboxPending, serverTwinArrived } from "./outboxSync";

/**
 * Оптимистичная отправка (03 §3.4): temp → серверный pending → delivered/failed.
 * `client_message_id` = tempId = crypto.randomUUID() — он же ключ идемпотентности
 * (01 §1.6, SET NX 24 ч в 08 §8.3): повтор с тем же id вернёт уже созданное
 * сообщение, дубля у клиента не будет.
 *
 * В десктопе путь отправки ВСЕГДА один — через outbox (03 §7, 04 §5.3): и
 * онлайн, и офлайн, HTTP делает Rust. В вебе — прямой POST, как и раньше.
 */

export interface SendVars {
  text: string;
  isNote: boolean;
  /** crypto.randomUUID(); при «Повторить» переиспользуется тот же — идемпотентность. */
  tempId: string;
  attachments?: AttachmentDto[];
  /**
   * Отправка идёт ИЗ КОМПОЗЕРА, то есть из того самого поля, которое читает
   * черновик диалога. Только такая отправка имеет право черновик гасить и
   * возвращать. Подробности — в комментариях `onMutate` и `onError`.
   */
  ownsDraft?: boolean;
  /**
   * На какое сообщение отвечаем (просьба владельца 02.09). Наша собственная
   * связь: у Авито цитирования в API нет, клиенту она не уедет.
   */
  replyTo?: MessageDto | null;
}

/** Локальный temp-пузырь отличается от серверного тем, что его id === client_message_id. */
export function isLocalTempMessage(msg: MessageDto): boolean {
  return Boolean(msg.client_message_id) && msg.id === msg.client_message_id;
}

export function newClientMessageId(): string {
  // crypto.randomUUID есть во всех целевых браузерах; в jsdom-тестах — полифилл в setup.
  return globalThis.crypto?.randomUUID?.() ?? `tmp-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function buildTempMessage(convId: string, v: SendVars): MessageDto {
  const user = useSessionStore.getState().user;
  return {
    id: v.tempId,
    conversation_id: convId,
    direction: v.isNote ? "note" : "out",
    sender_type: "operator",
    // Отдел кладём СРАЗУ: подпись автора в ленте с 04.09 включает его, и
    // без него оптимистичная реплика показала бы «Иванов», а через полсекунды
    // сменилась бы на «Иванов (ОКК)» — мигание на только что отправленном.
    sender: user
      ? { id: user.id, full_name: user.full_name, department: user.department }
      : null,
    body: v.text,
    attachments: v.attachments ?? [],
    // Заметка «доставлена» сразу (01 §6.4) — но до ответа сервера всё равно ⏳.
    delivery_status: "pending",
    client_message_id: v.tempId,
    created_at: new Date().toISOString(),
    // ⚠ ЦИТАТА ЕСТЬ УЖЕ В ОПТИМИСТИЧНОМ ПУЗЫРЕ. Дождись мы её от сервера — и
    // собственный ответ полсекунды выглядел бы обращённым в никуда, а связь
    // «появлялась» бы задним числом. Данные для неё у нас на руках: человек
    // сам выбрал сообщение, из него же собирается снимок.
    ...(v.replyTo && !v.isNote
      ? {
          reply_to_id: v.replyTo.id,
          reply_to: {
            id: v.replyTo.id,
            direction: v.replyTo.direction,
            body: (v.replyTo.body ?? "").slice(0, 100),
            truncated: (v.replyTo.body ?? "").length > 100,
            has_attachments: Boolean(v.replyTo.attachments?.length),
          },
        }
      : null),
  };
}

/**
 * Идёт ли отправка через локальную очередь. Только в Tauri и только пока в
 * сообщении нет вложений: файлы уже уехали по HTTP (01 §6.5), а схема outbox
 * хранит лишь текст (04 §5.2) — такое сообщение сети всё равно требует.
 */
export function usesOutbox(v: { attachments?: AttachmentDto[] }): boolean {
  if (!isTauri()) return false;
  if (getBridgeOrNull()?.kind !== "tauri") return false;
  return !v.attachments?.length;
}

/**
 * Десктоп-путь: `outbox_push` возвращает `client_msg_id`, он же становится
 * `client_message_id` пузыря (03 §7). Серверного сообщения здесь нет — ⏳
 * висит до WS `message:new`, где строка дедуплицируется (04 §5.4).
 */
async function sendViaOutbox(convId: string, v: SendVars): Promise<MessageDto> {
  const bridge = getBridge();
  const clientMsgId = await bridge.offlineQueue.push({
    conversationId: convId,
    kind: v.isNote ? "note" : "message",
    text: v.text,
    clientMessageId: v.tempId,
  });
  // Флаш сразу после push (04 §5.3 п.2); нет сети — строка просто ждёт в очереди.
  void bridge.offlineQueue
    .drain()
    .then(applyOutboxReport)
    .catch(() => {});
  const temp = buildTempMessage(convId, v);
  return { ...temp, id: clientMsgId, client_message_id: clientMsgId };
}

/** Оператор вмешался — бот замолкает навсегда (02 §2.6, DESIGN §4.3 п.6). */
function muteBotOptimistically(convId: string): void {
  const me = useSessionStore.getState().user;
  queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (old) => {
    if (!old) return old;
    const takenOver = old.status === "new" && !old.assignee && me;
    return {
      ...old,
      bot_active: false,
      // «Кто взял — тот и ведёт» (01 §6.2): первый ответ забирает диалог себе.
      ...(takenOver
        ? {
            status: "in_progress" as const,
            assignee: { id: me.id, full_name: me.full_name, department: me.department },
          }
        : null),
    };
  });
}

export function useSendMessage(convId: string) {
  return useMutation<MessageDto, Error, SendVars, { temp: MessageDto }>({
    mutationFn: (v) => {
      if (usesOutbox(v)) return sendViaOutbox(convId, v);
      const body = {
        text: v.text,
        client_message_id: v.tempId,
        ...(v.attachments?.length ? { attachments: v.attachments.map((a) => ({ media_id: a.media_id })) } : null),
        // Заметка не может быть ответом: на заметку и с заметки цитировать
        // нечего, а сервер такую отправку отверг бы 422.
        ...(v.replyTo && !v.isNote ? { reply_to_id: v.replyTo.id } : null),
      };
      return v.isNote ? sendNote(convId, body) : sendMessage(convId, body);
    },

    onMutate: async (v) => {
      await queryClient.cancelQueries({ queryKey: qk.messages.list(convId) });
      const temp = buildTempMessage(convId, v);
      if (v.isNote) {
        // Заметка клиенту не уходит и превью строки списка не меняет — только лента.
        appendMessage(convId, temp);
      } else {
        applyNewMessage(convId, temp);
        // Своя отправка обязана поднять строку СРАЗУ, не дожидаясь конца
        // заморозки порядка (жалоба владельца 02.09). Разбор — в `listOrder.ts`.
        пересобратьПорядок();
        muteBotOptimistically(convId);
      }
      /*
       * ЧЕРНОВИК ПРИНАДЛЕЖИТ КОМПОЗЕРУ, А НЕ ЛЮБОЙ ОТПРАВКЕ.
       *
       * Здесь стояло безусловное `clearDraft(convId)`, а отправок в диалог
       * две: композер по центру и форма заметки в карточке клиента справа
       * (ClientCardPane → NotesSection). Обе зовут этот хук с одним convId.
       * Оператор набирал ответ клиенту, вспоминал про заметку, писал её в
       * правой колонке — и поле в центре пустело, а режим
       * «Сообщение/Заметка» сбрасывался в «Сообщение». Ctrl+Z к этому полю
       * не относится, вернуть было нечем: оставалось набирать заново, то
       * есть терять ровно ту минуту, за которую меряется первый ответ
       * (M1 аудита, docs/29 §D).
       *
       * Флаг ставит только композер, умолчание — «чужой черновик не
       * трогать»: новый путь отправки, забывший про флаг, испортит себе
       * очистку поля, но не сотрёт набранный ответ клиенту.
       */
      if (v.ownsDraft) useChatUiStore.getState().clearDraft(convId);
      return { temp };
    },

    onSuccess: (serverMsg, v) => {
      // temp → серверный id; накопленный «обгоняющий» message:status применится тут же.
      replaceMessageInCache(convId, v.tempId, serverMsg);
    },

    onError: (e, v) => {
      /*
       * У УПАВШЕГО СООБЩЕНИЯ РОВНО ОДНО МЕСТО — ЛИБО ПОЛЕ, ЛИБО ПУЗЫРЬ.
       *
       * Раньше делалось и то и другое сразу: пузырь помечался «Не
       * отправилось · Повторить · Удалить» И тот же текст возвращался в
       * пустое поле ввода. Рядом оказывались два способа повторить одно
       * сообщение — нажать «Повторить» и нажать Enter, — и это РАЗНЫЕ
       * отправки: у второй новый `client_message_id`, то есть идемпотентность
       * её не сдержит. Клиент получал от компании один и тот же ответ дважды,
       * а оператор был уверен, что отправил один раз. Хуже того, два места
       * живут разное время: пузырь оптимистичный, сервер сообщения не создал,
       * и F5 стирает его бесследно — а поле переживает перезагрузку, потому
       * что черновики лежат в localStorage.
       *
       * Поэтому: вернули текст в поле — пузырь убираем. Оставили пузырь —
       * поле не трогаем. Предпочитаем поле: оно переживает перезагрузку, и
       * повтор там обычный, с новым идентификатором.
       *
       * ПУЗЫРЬ ОСТАЁТСЯ ЕДИНСТВЕННЫМ МЕСТОМ В ТРЁХ СЛУЧАЯХ:
       *
       * 1. Поле уже занято. Отказ приходит не мгновенно: отправил → пишет
       *    следующее → секунд через десять падает первая отправка. Вернуть
       *    старый текст поверх набранного — потерять набранное (M2 аудита,
       *    docs/29 §D), а склеить нельзя: шов приходится на середину фразы, и
       *    клиент получит мешанину. Пробелы считаем пустотой: одиночный
       *    перенос строки — не работа оператора.
       * 2. Заметка из карточки клиента (`ownsDraft` не выставлен). Подставить
       *    её текст в поле ответа КЛИЕНТУ — худший из исходов.
       * 3. В сообщении есть вложения. Поле хранит только текст; вернув его и
       *    убрав пузырь, мы бы молча потеряли уже загруженные файлы —
       *    переотправить их умеет одна кнопка «Повторить».
       */
      /*
       * ⚠ СНАЧАЛА — А НЕ УШЛО ЛИ ОНО ВСЁ-ТАКИ.
       *
       * Ниже всё построено на «сообщение не создано». Один случай это ломает:
       * сервер публикует `message:new` ВНУТРИ обработчика POST, до ответа, и
       * кадр приходит в том числе автору. Если ответ потерялся по дороге
       * (обрыв, таймаут, 502 после коммита), сообщение у клиента уже есть — а
       * вкладка видит отказ и честно делает худшее: возвращает текст в поле.
       * На экране одновременно пузырь с «Доставлено» и тот же текст в поле,
       * без единого слова о том, что произошло. Оператор дожимает Enter, и
       * клиент получает один ответ дважды: у повторной отправки НОВЫЙ
       * `client_message_id`, так что идемпотентность её не сдержит, а отозвать
       * сообщение в Авито нельзя.
       *
       * Близнец в ленте — единственный надёжный признак, что оно ушло.
       */
      if (serverTwinArrived(convId, v.tempId)) {
        dropOptimisticRow(convId, v.tempId);
        if (v.ownsDraft) useChatUiStore.getState().clearDraft(convId);
        showToast({
          title: "Сообщение ушло",
          message: "Ответ сервера потерялся по дороге, но клиент его получил — повторять не нужно",
          color: "lp",
        });
        scheduleListRefetch();
        return;
      }
      const fieldBusy = Boolean(useChatUiStore.getState().drafts[convId]?.text.trim());
      const backToField = Boolean(v.ownsDraft) && !fieldBusy && !v.attachments?.length;
      // Причину называет сервер («диалог ведут другие операторы», «канал
      // отключён»…). Без неё текст молча возвращался в поле, и человек жал
      // Enter снова и снова.
      const причина =
        e instanceof ApiError
          ? e.message
          : "Нет связи с сервером — сообщение не отправлено, попробуйте ещё раз";
      if (backToField) {
        // Вместе с цитатой: без неё повторный Enter уходил бы без связи с
        // сообщением клиента, и оператор бы этого не заметил.
        useChatUiStore
          .getState()
          .setDraft(convId, { text: v.text, isNote: v.isNote, replyTo: v.replyTo ?? null });
        dropOptimisticRow(convId, v.tempId);
        showToast({ title: "Сообщение не отправлено", message: причина, color: "red" });
      } else {
        // Пузырь остаётся с ✗ и кнопками «Повторить»/«Удалить» (MessageBubble).
        patchMessageInCache(convId, v.tempId, { delivery_status: "failed", delivery_error: причина });
      }
      // Сообщение не создано — значит и побочных эффектов 01 §6.2 не было:
      // откатываем оптимистичные «взял в работу» и «бот замолчал» по серверу.
      if (!v.isNote) {
        void queryClient.invalidateQueries({ queryKey: qk.conversations.detail(convId) });
      }
      /*
       * ⚠ И СПИСОК СЛЕВА — ИНАЧЕ ОН ВРЁТ, ЧТО КЛИЕНТУ ОТВЕТИЛИ.
       *
       * `onMutate` зовёт `applyNewMessage`, а тот безусловно переписывает строку
       * во ВСЕХ закэшированных списках: превью становится «Вы: …», а
       * `last_message_at` — временем нажатия, и строка уезжает наверх. Откат
       * трогал только ленту и деталь; список оставался с чужой правдой, и сам он
       * не перерисуется — `staleTime` тридцать секунд, `refetchOnWindowFocus`
       * выключен.
       *
       * Видно это так: отправка не прошла, в ленте у пузыря красный крест, а
       * слева диалог первый в списке с подписью «Вы: …». Оператор идёт дальше,
       * уверенный, что ответил; клиент ждёт.
       *
       * `scheduleListRefetch` схлопывает пачку отказов в один перезапрос — на
       * упавшей сети их бывает несколько подряд.
       */
      scheduleListRefetch();
    },
  });
}

/**
 * «Повторить» (03 §3.4, 04 §5.3):
 *  - десктоп, строка в очереди — `outbox_retry` (сброс attempts) + флаш;
 *  - temp-пузырь (POST не дошёл) — повторный POST с тем же client_message_id;
 *  - серверный failed (воркер исчерпал 5 ретраев) — POST /messages/{id}/retry.
 */
export function useRetryMessage(convId: string) {
  return useMutation<MessageDto, Error, MessageDto>({
    mutationFn: async (msg) => {
      if (isLocalTempMessage(msg) && usesOutbox(msg)) {
        const bridge = getBridge();
        await bridge.offlineQueue.retry(msg.id);
        void bridge.offlineQueue
          .drain()
          .then(applyOutboxReport)
          .catch(() => {});
        return { ...msg, delivery_status: "pending", delivery_error: null };
      }
      if (isLocalTempMessage(msg)) {
        const body = {
          text: msg.body ?? "",
          client_message_id: msg.client_message_id as string,
          ...(msg.attachments.length ? { attachments: msg.attachments.map((a) => ({ media_id: a.media_id })) } : null),
          ...(msg.reply_to_id ? { reply_to_id: msg.reply_to_id } : null),
        };
        return msg.direction === "note" ? sendNote(convId, body) : sendMessage(convId, body);
      }
      return retryMessage(msg.id);
    },
    onMutate: (msg) => {
      // ⚠ ОДНА РЕАЛИЗАЦИЯ НА ОДИН СМЫСЛ. Здесь стояла та же строка, что и в
      // теле `markOutboxPending` — «строка очереди снова в работе». Функция при
      // этом лежала мёртвой: обход неиспользуемых экспортов 27.08 нашёл её
      // среди семнадцати таких. Дубль опаснее мусора: правку «⏳ вместо ✗»
      // сделали бы в одном месте из двух.
      markOutboxPending(convId, msg.id);
    },
    onSuccess: (serverMsg, msg) => {
      replaceMessageInCache(convId, msg.id, serverMsg);
    },
    onError: (e, msg) => {
      patchMessageInCache(convId, msg.id, { delivery_status: "failed" });
      /*
       * ⚠ ОТКАЗ ПОВТОРА ГОВОРИТ СЛОВАМИ, А НЕ КРЕСТИКОМ (правка 08.09).
       *
       * Здесь стоял только возврат `failed`. Со стороны человека это выглядит
       * так: нажал «Повторить», крестик мигнул на ⏳ и вернулся крестиком.
       * Ни строчки о причине. И хуже всего это ровно тогда, когда повтор
       * бесполезен по своей природе: сервер отвечает 409
       * `account_needs_reauth` — канал Авито отвалился, и никакое число
       * нажатий сообщение не отправит. Диспетчер жмёт снова и снова, а клиент
       * тем временем ждёт.
       *
       * Текст берём У СЕРВЕРА, а не пишем заготовку: тот же довод, что у
       * удаления заметки ниже — заготовка «проверьте связь» врала бы на
       * отвалившемся канале, на потерянных правах и на любой пятисотке.
       * Своя фраза остаётся только там, где ответа сервера нет вовсе (обрыв
       * связи, таймаут).
       */
      showToast({
        title: "Не отправилось",
        message:
          e instanceof ApiError ? e.message : "Связь не отвечает — попробуйте ещё раз",
        color: "red",
      });
    },
  });
}

/**
 * «Снять» у серверного неотправленного (08.09): `failed` -> `dismissed`.
 *
 * ⚠ ЭТО НЕ УДАЛЕНИЕ. Сообщение остаётся в переписке с пометкой «снято»:
 * клиент его не получил, и это факт разговора, который может понадобиться при
 * разборе. Меняется одно — диалог перестаёт числиться в долгу, и красная
 * метка «ответ не ушёл» гаснет у всей смены.
 */
export function useDismissMessage(convId: string) {
  return useMutation<MessageDto, Error, MessageDto>({
    mutationFn: (msg) => dismissMessage(msg.id),
    onSuccess: (serverMsg, msg) => {
      replaceMessageInCache(convId, msg.id, serverMsg);
    },
    onError: (e) =>
      showToast({
        title: "Не получилось снять",
        message: e instanceof ApiError ? e.message : "Попробуйте ещё раз",
        color: "red",
      }),
  });
}

/**
 * «Удалить» рядом с ✗ (04 §5.3): в десктопе убирает строку из очереди
 * насовсем, в вебе — просто гасит несостоявшийся пузырь. Серверные сообщения
 * не удаляются: там кнопки нет (у них своя судьба — retry воркера).
 */
export function useDiscardMessage(convId: string) {
  return useMutation<void, Error, MessageDto>({
    mutationFn: async (msg) => {
      if (!isLocalTempMessage(msg)) return;
      const bridge = getBridgeOrNull();
      if (bridge?.kind === "tauri") await bridge.offlineQueue.remove(msg.id);
    },
    onSuccess: (_r, msg) => {
      // Черновик не трогаем: оператор мог уже набрать в поле что-то новое.
      dropOptimisticRow(convId, msg.id);
    },
  });
}
