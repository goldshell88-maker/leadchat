import { http, request } from "@/shared/api/http";
import type { AuditFilters, TeamFilters } from "@/shared/api/queryKeys";
import type { Role } from "@/shared/auth/usePermissions";
import type {
  AuditFilterOptions,
  AuditLogPage,
  InviteIssued,
  TeamUserEnvelope,
  TeamUsersPage,
} from "@/shared/api/types";

/** Размер страницы журнала — таблица читается глазами, не бесконечной лентой. */
export const AUDIT_PAGE_SIZE = 50;

/** GET /audit-log (01 §9.7) — право `audit:read` (admin, head; только чтение). */
export function fetchAuditLog(f: AuditFilters): Promise<AuditLogPage> {
  const p = new URLSearchParams();
  p.set("limit", String(AUDIT_PAGE_SIZE));
  p.set("offset", String(f.offset));
  if (f.userId) p.set("user_id", f.userId);
  if (f.action) p.set("action", f.action);
  if (f.dateFrom) p.set("date_from", f.dateFrom);
  if (f.dateTo) p.set("date_to", f.dateTo);
  return http.get<AuditLogPage>(`/audit-log?${p.toString()}`);
}

/** GET /audit-log/filters — то же право `audit:read`. */
export function fetchAuditFilterOptions(): Promise<AuditFilterOptions> {
  return http.get<AuditFilterOptions>("/audit-log/filters");
}

/* --- Сотрудники (01 §3, право `users:manage` — только admin) --------------- */

export const TEAM_PAGE_SIZE = 50;

/** GET /users (01 §3.1). `is_active=true` — пока не попросили показать отключённых. */
export function fetchTeamUsers(f: TeamFilters): Promise<TeamUsersPage> {
  const p = new URLSearchParams();
  p.set("limit", String(TEAM_PAGE_SIZE));
  p.set("offset", String(f.offset));
  if (f.q) p.set("q", f.q);
  if (f.role) p.set("role", f.role);
  if (!f.includeInactive) p.set("is_active", "true");
  return http.get<TeamUsersPage>(`/users?${p.toString()}`);
}

/** POST /users (01 §3.2) — приглашение; ссылка в ответе показывается один раз. */
export function inviteUser(body: { email: string; full_name: string; role: Role }): Promise<InviteIssued> {
  return http.post<InviteIssued>("/users", body);
}

/**
 * Новая одноразовая ссылка для сотрудника. Ручки две и путать их нельзя (01 §3.3):
 * `/invite` перевыпускает НЕиспользованное приглашение и отвечает 422, если
 * пароль уже установлен; `/reset-password` — то самое «восстановление через
 * администратора» для работающего сотрудника (14 §2.2).
 */
export function issueUserLink(user: { id: string; invite_pending: boolean }): Promise<InviteIssued> {
  const path = user.invite_pending ? "invite" : "reset-password";
  return http.post<InviteIssued>(`/users/${encodeURIComponent(user.id)}/${path}`);
}

/**
 * PATCH /users/{id} (01 §3.4) — смена роли действует немедленно.
 *
 * `department: ""` означает «убрать отдел» и отличимо от «не меняли»
 * (поле не прислали): снять отдел человек должен уметь так же, как поставить.
 */
export function updateUser(
  id: string,
  body: {
    role?: Role;
    full_name?: string;
    // Почта — логин сотрудника. Занята другим — сервер отвечает 409 email_taken.
    email?: string;
    handles_conversations?: boolean;
    department?: string;
    // Пустая строка — «снять цвет», симметрично отделу. Поле уже слал
    // `saveField`, а в типе его не было.
    color?: string;
  },
): Promise<TeamUserEnvelope> {
  return http.patch<TeamUserEnvelope>(`/users/${encodeURIComponent(id)}`, body);
}

/**
 * POST /users/{id}/deactivate (01 §3.5): refresh-токены в denylist, `user.id`
 * в `revoked_users` — сотрудника выбрасывает из системы мгновенно. Диалоги за
 * ним остаются: их переназначает руководитель вручную, чтобы не терять контекст.
 */
export function deactivateUser(id: string): Promise<TeamUserEnvelope> {
  return http.post<TeamUserEnvelope>(`/users/${encodeURIComponent(id)}/deactivate`);
}

export function activateUser(id: string): Promise<TeamUserEnvelope> {
  return http.post<TeamUserEnvelope>(`/users/${encodeURIComponent(id)}/activate`);
}


/**
 * DELETE /users/{id} — удалить сотрудника (требование заказчика от 7 августа:
 * «сделай так, чтобы можно было удалять сотрудников, а не просто отключать»).
 *
 * На сервере это ОТМЕТКА, а не стирание строки, и это осознанно: за человеком
 * тянется история — кто закрыл диалог, кто отправил сообщение, кто менял
 * настройки. Стереть строку значило бы обесценить журнал аудита, а он
 * единственный ответ на вопрос «кто это сделал». Для человека разницы нет:
 * удалённый исчезает из всех списков, его сессии рвутся, а незакрытые диалоги
 * возвращаются в очередь.
 */
export function deleteUser(id: string): Promise<TeamUserEnvelope> {
  return request<TeamUserEnvelope>(`/users/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/**
 * POST /users/{id}/set-password — администратор задаёт пароль напрямую.
 *
 * Отдельно от «Сбросить пароль», и разница ровно та, о которой просил
 * заказчик. Сброс выдаёт одноразовую ссылку: её надо переслать, человек
 * должен по ней перейти и придумать пароль — три шага и ожидание. Здесь
 * администратор говорит пароль вслух, и сотрудник входит сразу.
 *
 * Сессии сотрудника при этом рвутся: смена пароля обязана выгонять из старых
 * окон, иначе «сменили пароль» ничего не значит.
 */
export function setUserPassword(id: string, password: string): Promise<TeamUserEnvelope> {
  return http.post<TeamUserEnvelope>(`/users/${encodeURIComponent(id)}/set-password`, { password });
}
