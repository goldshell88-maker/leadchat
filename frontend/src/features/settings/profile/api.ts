import { http } from "@/shared/api/http";
import type { MyChannelsResponse, SupportAccepted } from "@/shared/api/types";

/**
 * POST /support/message (14 §2.2 и §4) — «Написать администратору» из профиля.
 * Доступно ЛЮБОЙ авторизованной роли, включая наблюдателя: отдельного права на
 * это нет, оно есть у всех, кто вообще работает в системе.
 */
export function sendAdminMessage(subject: string, text: string): Promise<SupportAccepted> {
  return http.post<SupportAccepted>("/support/message", { subject, text });
}

/**
 * GET /me/channels (7.2) — каналы, диалоги которых попадают этому сотруднику
 * во «Входящие». Доступно ЛЮБОЙ авторизованной роли: `accounts:read` есть
 * только у админа и руководителя, а знать свой список должен как раз менеджер.
 * Сервер возвращает и явно назначенные каналы, и открытые всем (`access`).
 */
export function fetchMyChannels(): Promise<MyChannelsResponse> {
  return http.get<MyChannelsResponse>("/me/channels");
}

/**
 * Ключ кэша «Моих каналов». Место по правилу 03 §2.1 — `shared/api/queryKeys.ts`,
 * туда он и должен переехать: здесь лежит временно, queryKeys.ts в этом заходе
 * чужая зона (см. cross-boundary).
 */
export const myChannelsKey = ["me", "channels"] as const;
