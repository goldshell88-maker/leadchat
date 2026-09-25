import { http } from "@/shared/api/http";
import type { InviteInfo, LoginResponse } from "@/shared/api/types";

/** GET /auth/invite/{token} — validate the invite before showing the form (01 §2.4). */
export function getInvite(token: string): Promise<InviteInfo> {
  return http.get<InviteInfo>(`/auth/invite/${encodeURIComponent(token)}`, { auth: false });
}

/** POST /auth/invite/accept — sets the password and logs the user in (01 §2.4). */
export function acceptInvite(token: string, password: string): Promise<LoginResponse> {
  return http.post<LoginResponse>("/auth/invite/accept", { token, password }, { auth: false });
}

/**
 * POST /support/password-reset (14 §2.2 и §4) — заявка сотрудника «не помню
 * пароль». Без авторизации. Ответ намеренно не разбирается: страница показывает
 * один и тот же текст и для существующей, и для несуществующей учётки.
 */
export function requestPasswordReset(email: string): Promise<void> {
  return http.post<void>("/support/password-reset", { email }, { auth: false });
}
