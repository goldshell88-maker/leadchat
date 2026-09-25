import { formatClock, formatListTime } from "@/shared/lib/formatTime";
import { plural } from "@/shared/lib/plural";

/**
 * Время уведомления «в человеческом виде» (14 §3). Внутри часа — «12 минут
 * назад», сегодня — часы, дальше — обычная метка списка (вчера / 12 авг).
 * Общий хелпер 10 §7.4 остаётся источником форматов дат, здесь только «назад».
 */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

export function formatNotificationTime(iso: string, now: Date = new Date()): string {
  const delta = now.getTime() - Date.parse(iso);
  if (!Number.isFinite(delta)) return "";
  if (delta < MINUTE) return "только что";
  if (delta < HOUR) {
    const m = Math.floor(delta / MINUTE);
    return `${m} ${plural(m, "минуту", "минуты", "минут")} назад`;
  }
  const label = formatListTime(iso, now);
  // Сегодня formatListTime отдаёт часы — этого достаточно; вчера и раньше
  // добавляем время, иначе «вчера» без часа читается хуже, чем нужно.
  return label === formatClock(iso) ? label : `${label}, ${formatClock(iso)}`;
}

/** «повторялось 12 раз» — подпись подавленных повторов (14 §4). */
export function formatRepeat(count: number | undefined): string | null {
  if (!count || count < 2) return null;
  return `повторялось ${count} ${plural(count, "раз", "раза", "раз")}`;
}
