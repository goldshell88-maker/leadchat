/**
 * Единый хелпер времени (10 §7.4). Часовой пояс — локальный для сотрудника.
 *
 * | Когда     | Список диалогов | Лента (разделители) | Метка сообщения |
 * |-----------|-----------------|---------------------|-----------------|
 * | Сегодня   | 14:02           | «Сегодня»           | 14:02           |
 * | Вчера     | вчера           | «Вчера»             | 14:02           |
 * | Этот год  | 12 мар          | «12 марта»          | 14:02           |
 * | Раньше    | 12.03.2024      | «12 марта 2024»     | 14:02           |
 */

const MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const MONTHS_GENITIVE = [
  "января",
  "февраля",
  "марта",
  "апреля",
  "мая",
  "июня",
  "июля",
  "августа",
  "сентября",
  "октября",
  "ноября",
  "декабря",
];

function isSameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function isYesterday(d: Date, now: Date): boolean {
  const y = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  return isSameDay(d, y);
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** «14:02» — местное время (метка сообщения; сегодняшняя строка списка). */
export function formatClock(iso: string): string {
  const d = new Date(iso);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** Время в строке списка диалогов. */
export function formatListTime(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (isSameDay(d, now)) return formatClock(iso);
  if (isYesterday(d, now)) return "вчера";
  if (d.getFullYear() === now.getFullYear()) return `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]}`;
  return `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}.${d.getFullYear()}`;
}

/** Подпись дата-разделителя ленты. */
export function formatDividerLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (isSameDay(d, now)) return "Сегодня";
  if (isYesterday(d, now)) return "Вчера";
  const base = `${d.getDate()} ${MONTHS_GENITIVE[d.getMonth()]}`;
  return d.getFullYear() === now.getFullYear() ? base : `${base} ${d.getFullYear()}`;
}

/** «12.03.2024» для дат в карточках (аккаунты: «Подключён …», «Токен: … до …»). */
export function formatDate(iso: string): string {
  const d = new Date(iso);
  return `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}.${d.getFullYear()}`;
}

/** Ключ календарного дня в локальной TZ — для сравнения соседних сообщений. */
export function localDayKey(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/* ----------------------------------------------------------------- пояса --
 *
 * ОТКУДА ЭТО ЗДЕСЬ (дефект 19). Один и тот же момент подписан на соседних
 * экранах разными числами. Список чатов и лента печатают время сотрудника —
 * ему нужно «до обеда это было или после». «Разбор диалогов» печатает
 * московское — отчёт общий, и руководитель в Москве с руководителем во
 * Владивостоке обязаны называть одно и то же число, иначе спор о том, когда
 * ответили клиенту, нечем разрешить. Оба правила верны, отказываться нельзя ни
 * от одного: у сотрудника на UTC+10 одна и та же строка получала «21:24» в
 * списке и «11.08 14:24» в таблице, и разницу в семь часов он держал в голове
 * при каждой сверке.
 *
 * Лечится не сменой пояса, а ПОДПИСЬЮ: время на обоих экранах получает
 * одинаковую расшифровку, где названы ОБА пояса сразу. Расшифровка одна на
 * оба экрана и живёт здесь — иначе они разойдутся в словах, и сверять придётся
 * уже подписи.
 *
 * Москва берётся ИМЕНЕМ ЗОНЫ, а не константой «+3»: смещение считает браузер.
 * Константа была бы верна сегодня (переводов часов в РФ нет с 2014 года) и
 * молча соврала бы в тот день, когда их вернут.
 */
export const MSK_TIME_ZONE = "Europe/Moscow";

type ZoneShape = "date-time" | "time";

const ZONE_SHAPES: Record<ZoneShape, Intl.DateTimeFormatOptions> = {
  "date-time": { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" },
  time: { hour: "2-digit", minute: "2-digit" },
};

/** Построенные Intl-форматтеры дороги, а строк списка на экране сорок. */
const zoneFormatters = new Map<string, Intl.DateTimeFormat>();

function zoneFormatter(timeZone: string, shape: ZoneShape): Intl.DateTimeFormat {
  const key = `${shape}|${timeZone}`;
  let fmt = zoneFormatters.get(key);
  if (!fmt) {
    try {
      fmt = new Intl.DateTimeFormat("ru-RU", { timeZone, ...ZONE_SHAPES[shape] });
    } catch {
      // Незнакомое имя зоны Intl отвергает исключением. Уронить из-за этого
      // строку списка нельзя — откатываемся на московское время, оно здесь
      // опорное и подписано словом «по Москве», то есть не притворяется чужим.
      fmt = new Intl.DateTimeFormat("ru-RU", { timeZone: MSK_TIME_ZONE, ...ZONE_SHAPES[shape] });
    }
    zoneFormatters.set(key, fmt);
  }
  return fmt;
}

/** Часовой пояс сотрудника — тот, в котором браузер рисует ему часы. */
export function localTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || MSK_TIME_ZONE;
}

/** «11.08 14:24» в заданном поясе. Пустая строка — момент не разбирается. */
export function formatInZone(iso: string, timeZone: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return zoneFormatter(timeZone, "date-time").format(d).replace(", ", " ");
}

/**
 * Расшифровка момента в ОБОИХ поясах — подпись (`title`) к любому времени на
 * экране: «11.08 21:24 у вас · 11.08 14:24 по Москве».
 *
 * Дата стоит у обеих половин, а не только у первой: на UTC+10 и московские
 * сутки расходятся, и «02:00 у вас · 19:00 по Москве» без даты читается как
 * опечатка, хотя это разные календарные дни.
 *
 * `undefined` — момента нет: `title` тогда не ставится вовсе, а не показывает
 * пустую подсказку.
 */
export function zonedTimeTitle(
  iso: string | null | undefined,
  timeZone: string = localTimeZone(),
): string | undefined {
  if (!iso) return undefined;
  const local = formatInZone(iso, timeZone);
  if (!local) return undefined;
  const msk = formatInZone(iso, MSK_TIME_ZONE);
  // Часы сотрудника и московские совпали — второй раз то же самое не пишем.
  // «14:24 у вас · 14:24 по Москве» выглядит поломкой и учит не читать
  // подсказку вообще; а не читают её тогда и там, где она нужна.
  return local === msk ? `${msk} по Москве` : `${local} у вас · ${msk} по Москве`;
}

/**
 * Предупреждение для экрана, который печатает московское время («Разбор
 * диалогов»): назвать оба часа СРАЗУ, а не по наведению на каждую ячейку.
 *
 * `null` — часы сотрудника и московские совпадают, и говорить не о чем.
 * Показывать разницу числом («+7 ч») намеренно не стали: знак этой разницы
 * человек всё равно прикидывает заново, а два живых времени рядом читаются
 * без вычитания.
 */
export function moscowZoneHint(
  now: Date = new Date(),
  timeZone: string = localTimeZone(),
  /**
   * Что именно на этом экране московское. Умолчание — прежнее слово в слово:
   * на «Разборе диалогов» функцию спрашивают не ради текста, а ради ответа
   * «есть ли о чём говорить», и менять там строку было бы правкой мимо цели.
   *
   * Второй экран появился 14 августа. На статистике московские не «время в
   * таблице», а сами ДАТЫ: «сегодня» там считается по Москве (`period.ts`),
   * поэтому у сотрудника, у которого на часах уже завтра, пресет «Сегодня»
   * показывает вчерашние сутки — и объяснить это на экране было нечем.
   */
  what: string = "Время в таблице московское",
): string | null {
  const local = zoneFormatter(timeZone, "time").format(now);
  const msk = zoneFormatter(MSK_TIME_ZONE, "time").format(now);
  if (local === msk) return null;
  return `${what}: сейчас в Москве ${msk}, у вас ${local}.`;
}
