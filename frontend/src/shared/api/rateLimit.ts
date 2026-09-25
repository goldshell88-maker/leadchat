import { plural } from "@/shared/lib/plural";
import { ApiError } from "./http";

/**
 * Ограничение частоты (01 §1.3): сервер отвечает 429 с кодом `rate_limited`
 * и полем `details.retry_after_sec`.
 *
 * Почему это отдельный модуль, а не три строки в форме: 429 — единственный
 * ответ, который НЕЛЬЗЯ ни выдать за успех, ни повторить молча. Выдать за
 * успех — человек будет ждать письма, которого никто не получал; повторить
 * молча — попытка снова упрётся в тот же счётчик. Показываем срок и ждём.
 */

/** Срок в секундах из ответа 429; null — это не ограничение частоты. */
export function retryAfterSec(err: unknown): number | null {
  if (!(err instanceof ApiError) || err.status !== 429) return null;
  const raw = err.details?.retry_after_sec;
  const sec = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(sec) || sec <= 0) return 0; // 429 без срока — «попробуйте позже»
  return Math.ceil(sec);
}

/**
 * «через 45 секунд» / «через 3 минуты». Ноль и отсутствие срока дают «позже»:
 * выдумывать точное время, которого сервер не назвал, нельзя.
 */
export function formatRetryAfter(sec: number | null): string {
  if (sec === null || sec <= 0) return "позже";
  if (sec < 60) return `через ${sec} ${plural(sec, "секунду", "секунды", "секунд")}`;
  const minutes = Math.ceil(sec / 60);
  if (minutes < 60) return `через ${minutes} ${plural(minutes, "минуту", "минуты", "минут")}`;
  const hours = Math.ceil(minutes / 60);
  return `через ${hours} ${plural(hours, "час", "часа", "часов")}`;
}
