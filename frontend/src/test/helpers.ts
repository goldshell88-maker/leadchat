import type { SessionUser } from "@/shared/api/types";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { ACTIONS } from "@/features/hotkeys/catalog";

export const fakeUser: SessionUser = {
  id: "0d4f0a1e-6b7c-4b1e-9a2d-1f3e5c7a9b0d",
  email: "anna@partner-lead-centre.ru",
  full_name: "Анна Смирнова",
  role: "manager",
  is_active: true,
};

export const fakeMe = {
  ...fakeUser,
  // Ровно набор роли manager из app/core/rbac.py (01 §12) — фикстура не должна
  // расходиться с тем, что реально приходит в GET /auth/me.
  permissions: [
    "conversations:read",
    "messages:send",
    "conversations:manage",
    "notes:read",
    "notes:write",
    "templates:own",
    "stats:own",
  ],
};

/** Response-like object: keeps tests independent of the global Response constructor. */
export function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

export function errorEnvelope(code: string, message: string, details?: Record<string, unknown>) {
  return { error: { code, message, details, request_id: "req_test" } };
}

export function resetSessionStore(overrides?: Partial<ReturnType<typeof useSessionStore.getState>>) {
  useSessionStore.setState({
    user: null,
    permissions: [],
    accessToken: null,
    bootstrapped: false,
    ...overrides,
  });
}

/**
 * Все сочетания включены — для тестов, которые проверяют ДЕЙСТВИЕ, а не умолчания.
 *
 * ⚠ ЗАЧЕМ ЭТО ПОЯВИЛОСЬ 02.09. Владелец решил: клавиши изначально неактивны, все
 * кроме приёма диалога. Двенадцать проверок в шести файлах сразу покраснели — и
 * правильно: они нажимают Ctrl+D, Ctrl+T, Alt+1 и ждут, что что-то произойдёт.
 *
 * Эти проверки нужны и дальше: они стерегут, что действие делает обещанное,
 * когда клавиша нажата. Менять их на «ничего не происходит» значило бы потерять
 * покрытие самих действий. Поэтому они говорят вслух: «здесь сочетания
 * включены» — и проверяют своё.
 *
 * За то, что по умолчанию всё выключено, отвечает отдельный набор
 * (`HotkeysOffByDefault0209`), и только он.
 */
export function включитьВсеСочетания(): void {
  const свои: Record<string, string[]> = {};
  for (const a of ACTIONS) свои[a.id] = [...a.defaults];
  useSessionStore.setState({ hotkeys: свои });
}

/** Те же сочетания как объект — для чистых функций вроде `actionFor`. */
export function всеСочетания(): Record<string, readonly string[]> {
  const свои: Record<string, readonly string[]> = {};
  for (const a of ACTIONS) свои[a.id] = a.defaults;
  return свои;
}
