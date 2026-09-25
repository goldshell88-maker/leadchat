import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, ConversationDto, ConversationsPage, MessageDto } from "@/shared/api/types";
import { previewText } from "@/shared/lib/messagePreview";
import { plural } from "@/shared/lib/plural";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { getBridgeOrNull } from "./bridge";
import { закрытьПоТегу } from "./serviceWorker";

/**
 * Точка интеграции тостов в общий код (04 §4.1). Одна строка в
 * `shared/realtime/notify.ts` — и десктоп получает уведомления, а браузер
 * показывает своё окно: мост сам решает, дошло ли дело до показа (нет
 * разрешения, человек смотрит в эту вкладку — молча выходим), а без моста
 * функция выходит сразу.
 *
 * Решение «показывать ли» и троттлинг живут в мостах (`tauri/notifier.ts`,
 * `web.ts`); здесь — только сборка заголовка «Иван Петров · Ремонт iPhone 13»
 * из кэша и продуктовый смысл события: вид, ключ склейки, кнопки.
 */

/**
 * ⚠ ПРАВИЛО ОДНО НА ВСЕ ЧЕТЫРЕ УВЕДОМЛЕНИЯ: показываем только то, что СЕРВЕР
 * пометил как «про тебя», и пометку берём ИЗ КАДРА, а не вычисляем заново.
 *
 *   сообщение — `assignee_id === мой id` (кадр `message:new`, `wsEvents.ts`)
 *   передача  — `is_for_you`  (подставляет хаб получателю, `ws/hub.py`)
 *   очередь   — `can_claim`   (тот же хаб; отбор по каналу — невыдачей кадра)
 *   центр     — `audience_hint === null`, то есть строка адресная
 *
 * ПОЧЕМУ НЕ СЧИТАТЬ САМИМ. У сервера есть матрица прав и список каналов
 * человека, у экрана — только доехавшее. Второй экземпляр правила разойдётся с
 * первым на первой же правке ролей, и разойдётся молча: новая роль либо
 * перестанет получать уведомления, либо начнёт получать чужие.
 *
 * ПОЧЕМУ ЭТО ВООБЩЕ ЗАПИСАНО. 4705 входящих от клиентов в сутки и ~2000
 * постановок в очередь (замер боя 05.09) на 30–33 человека с общими каналами.
 * Уведомление «на всякий случай» здесь — это не лишняя карточка, а выключенные
 * уведомления: звук по такому же поводу уже выключали руками (02.09,
 * `shared/realtime/notify.ts`).
 */

function findRow(convId: string): ConversationDto | null {
  const detail = queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail(convId));
  if (detail) return detail;
  for (const [, data] of queryClient.getQueriesData<InfiniteData<ConversationsPage>>({
    queryKey: CONVERSATIONS_LIST_KEY,
  })) {
    for (const page of data?.pages ?? []) {
      const row = page.items.find((r) => r.id === convId);
      if (row) return row;
    }
  }
  return null;
}

function titleFor(row: ConversationDto | null): string {
  const parts = [row?.client.name, row?.item?.title].filter(Boolean);
  return parts.length ? parts.join(" · ") : "Новое сообщение";
}

/**
 * Кнопка «Ответить» в тосте есть только у ролей с `messages:send`
 * (у head/observer её быть не должно, 11 §7.2). Читаем стор напрямую —
 * тост вызывается из WS-обработчика, где хуков нет.
 */
function canReplyNow(): boolean {
  return useSessionStore.getState().permissions.includes("messages:send");
}

/** Входящее от клиента: тост, если окно не в фокусе. Сообщения бота не тостят (04 §4.1). */
export function toastForMessage(msg: MessageDto): void {
  if (msg.direction !== "in" || msg.sender_type !== "client") return;
  const bridge = getBridgeOrNull();
  if (!bridge) return;
  const row = findRow(msg.conversation_id);
  void bridge.notify({
    title: titleFor(row),
    // Один источник слов с лентой и строкой списка (правка 8): пустое тело
    // больше не превращается в «Вложение» там, где вложения нет.
    body: previewText(msg),
    conversationId: msg.conversation_id,
    attribution: row?.account.title,
    kind: "message",
    // Без direction/senderType фильтр «только от клиента» на стороне Rust
    // не отработает и в лог уйдёт предупреждение (04 §4.1).
    direction: msg.direction,
    senderType: msg.sender_type,
    canReply: canReplyNow(),
  });
}

/**
 * ⚑ Тег карточки передачи. Живёт отдельной константой, потому что этим же
 * тегом карточку ЗАМЕЩАЕТ отмена передачи (см. `тегЗаписиЦентра`): у
 * предложения стоит `requireInteraction`, и само оно не погаснет никогда.
 */
function тегПередачи(convId: string): string {
  return `lc-handoff-${convId}`;
}

/**
 * Диалог встал в очередь «Входящие» — единственное уведомление на ВСЮ очередь.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 07.09: «нужны уведомления, когда пришёл диалог». До этого
 * на кадр `inbox:new` звучал только сигнал (`playInboxChime`), и человек с CRM
 * поверх LeadChat не узнавал о новом клиенте ничем, кроме звука, — а звук он
 * уже однажды выключил.
 *
 * ⚠ ОДИН ТЕГ НА ВСЮ ОЧЕРЕДЬ, И ЭТО ГЛАВНОЕ В ФУНКЦИИ. Замер боя 05.09: 1357
 * новых диалогов и 625 возвратов в сутки, каналы открыты 30–33 операторам —
 * около 2000 постановок в очередь, и каждая адресована десяткам людей.
 * Карточка с собственным тегом на каждую означала бы лавину: браузер копит их
 * стопкой, а разбирать стопку из сорока карточек человек не станет — он
 * запретит уведомления. С одним тегом браузер ЗАМЕЩАЕТ прежнюю карточку новой,
 * и на экране всегда ровно одна строка с текущим размером очереди.
 *
 * ⚠ БЕЗ ИМЕНИ КЛИЕНТА И БЕЗ ПЕРЕПИСКИ. Карточка всплывает на экране блокировки
 * чужого компьютера — там, где рядом сидят люди не из компании. Размер очереди
 * зовёт к работе не хуже имени, а показывать нечего.
 *
 * `convId` — диалог ИЗ ЭТОГО кадра: другого адреса у карточки нет, и обе
 * кнопки берут именно его. Гонка «пока нажимал, забрал коллега» — норма
 * (сервер отвечает 409), а не поломка; что при этом видит человек, разобрано
 * в `отказНеПрошёл` (features/chats/inbox/useInbox.ts).
 *
 * ⚠ ВТОРАЯ КНОПКА — «Отклонить» (07.09, просьба владельца дословно: «чтобы
 * можно было принять его или отклонить там же в уведомлении»). Одной «Принять»
 * было мало: диспетчер с CRM поверх LeadChat, которому этот диалог не по
 * профилю, иначе обязан открыть приложение только чтобы убрать строку с глаз.
 */
export function toastForInbox(convId: string, ждутВсего: number): void {
  const bridge = getBridgeOrNull();
  if (!bridge) return;
  // Кадр уже посчитан в сторе очереди к моменту вызова, поэтому меньше одного
  // здесь быть не может; замок стоит на случай несведённого счётчика — «0
  // диалогов ждут» звало бы к пустой очереди.
  const сколько = Math.max(1, ждутВсего);
  void bridge.notify({
    title: "Новый диалог в очереди",
    body: `${сколько} ${plural(сколько, "диалог", "диалога", "диалогов")} ${plural(
      сколько,
      "ждёт",
      "ждут",
      "ждут",
    )} во «Входящих»`,
    conversationId: convId,
    kind: "inbox",
    tag: "lc-inbox",
    // Кнопки рисует только сервис-воркер; без него мост покажет карточку без
    // них, и клик по ней всё равно откроет диалог.
    //
    // ⚠ `inbox-decline`, А НЕ `decline`. Отказ от ОЧЕРЕДИ и отказ от ПЕРЕДАЧИ —
    // разные ручки сервера (`/conversations/{id}/decline` против
    // `/conversations/{id}/transfer/decline`) с разными последствиями: первый
    // прячет диалог из моей очереди, второй возвращает его передавшему. Общее
    // имя на два действия однажды отказалось бы не от того, и молча.
    actions: [
      { action: "claim", title: "Принять" },
      { action: "inbox-decline", title: "Отклонить" },
    ],
    // Права проверил сервер (`can_claim`), и сюда мы попадаем только с ними —
    // см. правило в шапке файла. Отвечать из очереди нечего: диалог ещё ничей.
    canReply: false,
  });
}

/**
 * Строка центра уведомлений (14 §4): «при открытом десктоп-клиенте уведомление
 * превращается в нативное окно Windows».
 *
 * ⚠ КОГО СЮДА ПУСКАТЬ, РЕШАЕТ НЕ ЭТОТ ФАЙЛ, а `features/notifications/wsNotify.ts`:
 * там же лежит и поимённый список личных видов с доводами. Раньше правило было
 * «только критичные», и до диспетчера не доходило НИЧЕГО — все критичные виды
 * каталога адресованы администраторам (`services/notifications.py`, KINDS).
 *
 * `conversationId` берём из связанной сущности, если она диалог: тогда клик по
 * тосту откроет переписку. У серверных событий (диск, сертификат, планировщик)
 * диалога нет, и Rust просто поднимает окно.
 */
export function toastForNotification(n: {
  kind?: string;
  title: string;
  body?: string | null;
  entity?: { type: string; id?: string | null } | null;
}): void {
  const bridge = getBridgeOrNull();
  if (!bridge) return;
  const convId = n.entity?.type === "conversation" ? (n.entity.id ?? undefined) : undefined;
  void bridge.notify({
    title: n.title,
    body: n.body?.trim() || "Откройте центр уведомлений",
    conversationId: convId,
    kind: "system",
    tag: тегЗаписиЦентра(n.kind, convId),
    canReply: false,
  });
}

/** Вид уведомления, который забирает предложение передачи назад (14 §2.3). */
const ОТМЕНА_ПЕРЕДАЧИ = "conversation.transfer_cancelled";

/**
 * Ключ склейки для строки центра.
 *
 * ⚠ ОТМЕНА ПЕРЕДАЧИ ЗАМЕЩАЕТ КАРТОЧКУ ПРЕДЛОЖЕНИЯ, А НЕ ЛОЖИТСЯ РЯДОМ. У
 * предложения стоит `requireInteraction` — оно ждёт нажатия и само не гаснет.
 * Передающий забрал диалог назад, а у получателя на экране по-прежнему висит
 * «Принять / Отклонить» по диалогу, которого ему больше не предлагают: нажатие
 * упрётся в ошибку, и человек решит, что сломалось приложение. Тот же тег
 * заставляет браузер подменить карточку — на месте предложения появляется
 * «Передачу отменили — принимать нечего», уже гасимое само.
 *
 * Остальным строкам ключ собирается как серверный `dedup="entity"`: вид плюс
 * сущность. Два напоминания по ОДНОМУ диалогу — одна новость и одна карточка;
 * по разным диалогам — разные, их и правда две.
 */
function тегЗаписиЦентра(kind: string | undefined, convId: string | undefined): string {
  if (kind === ОТМЕНА_ПЕРЕДАЧИ && convId) return тегПередачи(convId);
  return `lc-n-${kind ?? "notify"}:${convId ?? "-"}`;
}

/**
 * ⚑ «Диалог передан вам» — приоритетный тост, показывается даже в фокусе (04 §4.1).
 *
 * ⚠ ЭТО ЕДИНСТВЕННАЯ КАРТОЧКА, КОТОРАЯ НЕ ГАСНЕТ САМА. Замер: 119 предложений
 * передачи за 30 дней — событие редкое, но дорогое. Пропущенное значит, что
 * диалог не взял ни получатель (не увидел), ни передавший (считает, что отдал),
 * и клиент ждёт до истечения предложения. Браузерная карточка живёт секунд
 * пять; человек с CRM поверх LeadChat её просто не застанет. `requireInteraction`
 * оставляет её висеть до нажатия — ровно для этого поле и есть.
 *
 * Тег персональный (`lc-handoff-<id>`), а не общий: две передачи — это два
 * разных решения, и склеивать их в одну карточку нельзя. Он же служит адресом
 * для замещения — см. `тегЗаписиЦентра`.
 *
 * Кнопки повторяют две ручки сервера: `accept` — принять
 * (`POST /conversations/{id}/claim`), `decline` — отказаться от передачи.
 * Рисует их сервис-воркер; без него карточка выйдет без кнопок, и клик по ней
 * откроет диалог — там обе кнопки есть на экране.
 */
/** Убрать карточку предложения: передачу приняли, отклонили, отменили или она истекла. */
export function closeHandoffCard(convId: string): void {
  void закрытьПоТегу(тегПередачи(convId));
}

export function toastForHandoff(
  convId: string,
  assignedBy: string,
  { offer = true }: { offer?: boolean } = {},
): void {
  const bridge = getBridgeOrNull();
  if (!bridge) return;
  const row = findRow(convId);
  void bridge.notify({
    title: offer ? "Диалог передан вам" : "Вам назначили диалог",
    body: `${assignedBy}: ${titleFor(row)}`,
    conversationId: convId,
    attribution: row?.account.title,
    kind: "handoff",
    isForYou: true,
    canReply: canReplyNow(),
    tag: тегПередачи(convId),
    // Прямое назначение решения не ждёт: такая карточка гаснет сама.
    requireInteraction: offer,
    actions: offer
      ? [
          { action: "accept", title: "Принять" },
          { action: "decline", title: "Отклонить" },
        ]
      : undefined,
  });
}
