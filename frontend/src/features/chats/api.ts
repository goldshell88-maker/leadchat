import { queryClient } from "@/app/queryClient";
import { http, requestForm } from "@/shared/api/http";
import { qk, type ConversationFilters } from "@/shared/api/queryKeys";
import type {
  AssignResponse,
  ClientHistoryResponse,
  ConversationDetailDto,
  ConversationStatus,
  ConversationsPage,
  MediaDto,
  MessageDto,
  MessagesPage,
  ParticipantDto,
  SendMessageBody,
  TabCounts,
} from "@/shared/api/types";
import { rememberCursor } from "@/shared/realtime/cursorRegistry";
import { useUnreadStore } from "@/shared/stores/unreadStore";

/** GET /conversations (01 §5.1) — offset-пагинация, сортировка серверная фиксированная. */
export async function fetchConversations(f: ConversationFilters, offset: number): Promise<ConversationsPage> {
  const p = new URLSearchParams();
  // Поиск идёт ПО ВСЕМУ, включая закрытые и чужие (UX-аудит, docs/17 §Т3).
  //
  // Вкладка при непустом запросе не сужает: интерфейс на время поиска и так
  // приглушает вкладки, обещая искать везде, — раньше это обещание было
  // ложным. Из «Моих» диалог коллеги не находился никогда, а закрытый не
  // находился ни из одной вкладки, и оператор говорил клиенту «вы к нам не
  // обращались» при 454 тысячах диалогов истории.
  //
  // ⚠ «ПОКАЗЫВАТЬ ЗАКРЫТЫЕ» ЕДЕТ ТОЙ ЖЕ ВКЛАДКОЙ `any`, А НЕ НОВЫМ ПАРАМЕТРОМ.
  // На сервере `any` уже означает ровно это — «везде, включая закрытые», — заведён
  // для поиска и покрыт тестами. Второй параметр про то же самое дал бы два способа
  // сказать одно, и они разъехались бы на первой же правке условия вкладок.
  // Вкладка «Все» включает закрытые и архив ВСЕГДА (требование владельца
  // 17.08): история из Авито живёт в закрытых, и «Все» без неё врали именем.
  p.set("tab", f.q || f.withClosed || f.tab === "all" ? "any" : f.tab);
  if (f.q) p.set("q", f.q);
  // Статус едет отдельным параметром, а не подменяет вкладку: у сервера это
  // разные условия, и заданный явно он переопределяет умолчание вкладки
  // «кроме закрытых» — иначе «Мои + Закрытые» давало бы пустой список.
  if (f.status) p.set("status", f.status);
  if (f.accountId) p.set("account_id", f.accountId);
  if (f.assigneeId) p.set("assignee_id", f.assigneeId);
  // «Без ответственного» — свой параметр, а не `assignee_id=null`: довод
  // записан у поля в `queryKeys.ts` и повторяет серверный.
  if (f.unassigned) p.set("unassigned", "true");
  if (f.tag) p.set("tag", f.tag);
  // «Ждут ответа»: работает и на «Всех», где вкладка уходит как `any` —
  // закрытые отсеивает сам предикат ожидания, а не вкладка.
  if (f.waitingOnly) p.set("waiting_only", "true");

  p.set("limit", "50");
  p.set("offset", String(offset));
  const page = await http.get<ConversationsPage>(`/conversations?${p.toString()}`);
  // Сервер — истина по счётчикам: каждая загруженная страница засеивает unreadStore (03 §2.2).
  useUnreadStore.getState().seedFromRows(page.items);
  return page;
}

/**
 * GET /conversations/counts — сколько диалогов взято и сколько не отвечено.
 *
 * ⚠ ПОЧЕМУ СПРАШИВАЕМ СЕРВЕР, А НЕ СЧИТАЕМ ЗАГРУЖЕННОЕ. Бейдж у «Моих» уже
 * стоял и его сняли: он считался по строкам, что успели приехать, а список
 * идёт страницами по 50 — «Мои 3» превращалось в «Мои 17» от одной прокрутки.
 * Довод целиком записан на месте снятого бейджа в `ChatListPane.tsx`.
 */
export function fetchTabCounts(): Promise<TabCounts> {
  return http.get<TabCounts>("/conversations/counts");
}

/** GET /conversations/{id} (01 §5.2) — деталь для шапки; deep-link работает без списка. */
export function fetchConversation(id: string): Promise<ConversationDetailDto> {
  return http.get<ConversationDetailDto>(`/conversations/${encodeURIComponent(id)}`);
}

/**
 * GET /conversations/{id}/messages (01 §6.1) — cursor-пагинация (01 §1.4):
 * без курсора — последние 50 (первое открытие), before=<cursor> — старее (скролл вверх).
 * next_cursor свежей страницы запоминается для догона after= после reconnect.
 */
export async function fetchMessages(convId: string, before: string | null): Promise<MessagesPage> {
  const p = new URLSearchParams({ limit: "50" });
  if (before) p.set("before", before);
  const page = await http.get<MessagesPage>(`/conversations/${encodeURIComponent(convId)}/messages?${p.toString()}`);
  if (!before) rememberCursor(convId, page.page.next_cursor);
  return page;
}

/** POST /conversations/{id}/read (01 §5.3) — двигает read-маркер текущего пользователя. */
export function markConversationRead(id: string): Promise<void> {
  return http.post<void>(`/conversations/${encodeURIComponent(id)}/read`);
}

/* ------------------------------------------------------------------------- *
 *  Спринт 3 — отправка, заметки, статус, передача, история клиента
 * ------------------------------------------------------------------------- */

/** POST /conversations/{id}/messages (01 §6.2) — ответ мгновенный, delivery_status=pending. */
export function sendMessage(convId: string, body: SendMessageBody): Promise<MessageDto> {
  return http.post<MessageDto>(`/conversations/${encodeURIComponent(convId)}/messages`, body);
}

/** POST /conversations/{id}/notes (01 §6.4) — внутренняя заметка, delivered сразу. */
export function sendNote(convId: string, body: SendMessageBody): Promise<MessageDto> {
  return http.post<MessageDto>(`/conversations/${encodeURIComponent(convId)}/notes`, body);
}

/** POST /messages/{id}/retry (01 §6.3) — вернуть failed в очередь доставки. */
/**
 * POST /conversations/{id}/bot/mute — забрать диалог у бота, ничего не написав.
 *
 * До 29.08 бота глушила только отправка сообщения: чтобы вмешаться молча,
 * оператору приходилось что-то писать клиенту. Ручка делает то же, что делает
 * ответ, минус сам ответ — диалог назначается на того, кто забрал, и бот
 * замолкает навсегда.
 */
export function takeFromBot(convId: string): Promise<{ bot_active: boolean; taken: boolean }> {
  return http.post<{ bot_active: boolean; taken: boolean }>(
    `/conversations/${encodeURIComponent(convId)}/bot/mute`,
    {},
  );
}

/** DELETE /messages/{id} — только заметки, только автор (17.08). */
export function deleteNote(messageId: string): Promise<void> {
  return http.del(`/messages/${encodeURIComponent(messageId)}`);
}

/**
 * Ссылка на голосовое — спрашивается в момент нажатия «прослушать».
 *
 * У Авито ссылки временные, поэтому в сообщении их нет и хранить их негде:
 * сохранённая через час превратилась бы в битую. Сервер спрашивает Авито и
 * отдаёт свежую (жалоба владельца 19.08: «не грузятся голосовые сообщения»).
 */
export function voiceUrl(messageId: string): Promise<{ url: string }> {
  return http.get<{ url: string }>(`/messages/${encodeURIComponent(messageId)}/voice`);
}

/** POST /messages/{id}/dismiss — снять неотправленное с учёта (08.09). */
export function dismissMessage(messageId: string): Promise<MessageDto> {
  return http.post<MessageDto>(`/messages/${messageId}/dismiss`, {});
}

export function retryMessage(messageId: string): Promise<MessageDto> {
  return http.post<MessageDto>(`/messages/${encodeURIComponent(messageId)}/retry`);
}

/**
 * PATCH /conversations/{id}/status (01 §5.4).
 *
 * ОДНО ПОЛЕ И НИЧЕГО БОЛЬШЕ. Сюда же при закрытии ехал результат обращения
 * (`outcome`, `outcome_amount_rub`, план 21 C1), а рядом жили `POST` и
 * `DELETE /conversations/{id}/snooze` — «Отложить до…» и «Вернуть сейчас».
 * И то и другое снято 12 августа решением владельца; ручек отложки на сервере
 * больше нет вовсе, а поля результата ручка статуса теперь отвергает.
 */
export function patchConversationStatus(
  convId: string,
  status: ConversationStatus,
): Promise<ConversationDetailDto> {
  return http.patch<ConversationDetailDto>(`/conversations/${encodeURIComponent(convId)}/status`, {
    status,
  });
}

/** POST /conversations/{id}/assign (01 §5.5) — назначить/передать + системное сообщение в ленту. */
export function assignConversation(
  convId: string,
  assigneeId: string | null,
  comment?: string,
): Promise<AssignResponse> {
  return http.post<AssignResponse>(`/conversations/${encodeURIComponent(convId)}/assign`, {
    assignee_id: assigneeId,
    ...(comment ? { comment } : null),
  });
}

/**
 * Позвать коллегу / убрать позванного (docs/19). Ответственный НЕ меняется —
 * этим обе ручки и отличаются от `assign`. Ответ обеих — актуальный состав.
 */
export function inviteParticipant(
  convId: string,
  userId: string,
  reason?: string,
): Promise<{ participants: ParticipantDto[] }> {
  return http.post(`/conversations/${encodeURIComponent(convId)}/participants`, {
    user_id: userId,
    ...(reason ? { reason } : null),
  });
}

export function removeParticipant(
  convId: string,
  userId: string,
): Promise<{ participants: ParticipantDto[] }> {
  return http.del(
    `/conversations/${encodeURIComponent(convId)}/participants/${encodeURIComponent(userId)}`,
  );
}

/** GET /conversations/{id}/client-history (01 §5.6) — прошлые диалоги клиента. */
export function fetchClientHistory(convId: string): Promise<ClientHistoryResponse> {
  return http.get<ClientHistoryResponse>(`/conversations/${encodeURIComponent(convId)}/client-history`);
}

/**
 * GET /users/assignable (01 §3.1) — активные admin/manager для «→ Передать».
 * Живёт в shared/api/reference: тот же справочник питает фильтр «по менеджеру»
 * в списке чатов и на /stats; реэкспорт — чтобы не менять импорты фичи.
 */
export { fetchAssignableUsers } from "@/shared/api/reference";

/**
 * POST /media (01 §6.5) — multipart, до 20 МБ, jpeg/png/webp/pdf.
 * http-хелпер шлёт только JSON, поэтому здесь свой fetch с теми же правилами
 * (Bearer + credentials + error-envelope).
 */
export async function uploadMedia(file: File, signal?: AbortSignal): Promise<MediaDto> {
  const form = new FormData();
  form.append("file", file);
  return requestForm<MediaDto>("/media", form, signal);
}

/**
 * Куда идти после закрытия диалога в обычном списке.
 *
 * Тот же приём, что и в очереди (`nextInboxId`), но по кэшу списка чатов:
 * сосед снизу, а если закрытый был последним — сосед сверху. `null` означает
 * «идти некуда», и тогда никуда не идём.
 *
 * ⚠ СОСЕД СЧИТАЕТСЯ ПО ТОМУ СПИСКУ, ЧТО СЕЙЧАС НА ЭКРАНЕ, И ФИЛЬТРЫ ПОЭТОМУ
 * ПРИХОДЯТ ПАРАМЕТРОМ.
 *
 * Здесь стоял перебор ВСЕХ закэшированных вариантов фильтров с доводом
 * «диалог физически находится ровно в одном списке». Довод неверен: один и
 * тот же диалог лежит и в «Моих», и во «Всех», и в любой прошлой выдаче
 * поиска — кэши живут по получасу. Побеждал первый попавшийся, то есть чаще
 * всего САМЫЙ СТАРЫЙ. Владелец заходит на «Все» (вкладка по умолчанию для
 * его роли), переключается на «Мои», закрывает свой диалог — и автопереход
 * уносит его к соседу по «Всем»: в чужую переписку или в архивный диалог
 * Авито, причём слева не подсвечена ни одна строка.
 *
 * Ровно так — по ключу видимой выдачи — считают соседа стрелки и J/K
 * (`useChatHotkeys.cachedRows`). Двух ответов на вопрос «кто следующий» на
 * одном экране быть не должно.
 */
export function nextConversationId(currentId: string, filters: ConversationFilters): string | null {
  const data = queryClient.getQueryData<{ pages: ConversationsPage[] }>(
    qk.conversations.list(filters),
  );
  const rows = data?.pages.flatMap((p) => p.items) ?? [];
  const idx = rows.findIndex((r) => r.id === currentId);
  if (idx === -1) return null;
  return rows[idx + 1]?.id ?? rows[idx - 1]?.id ?? null;
}

/** Закрепить/открепить диалог у СЕБЯ (требование от 7 августа). */
export function pinConversation(convId: string): Promise<void> {
  return http.post<void>(`/conversations/${encodeURIComponent(convId)}/pin`);
}

export function unpinConversation(convId: string): Promise<void> {
  return http.del<void>(`/conversations/${encodeURIComponent(convId)}/pin`);
}
