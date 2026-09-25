/**
 * Московское время в интерфейсе (docs/38 §7).
 *
 * ЗАЧЕМ МОДУЛЬ ОСТАЛСЯ, КОГДА ОТЛОЖКИ НЕТ. Заводился он под пресеты отложки
 * («Через час», «Завтра утром», «В понедельник»), и вместе с ней 12 августа
 * ушли `moscowAt`, `daysToNextMonday`, `moscowFromLocalInput` — считать
 * будущие сроки больше некому. Остался `formatMoscow`: им подписана дата
 * закрытия в карточке клиента, и это не наследство, а самостоятельный
 * потребитель.
 *
 * ПОЧЕМУ НЕ ПО ЧАСАМ БРАУЗЕРА. У пользователя настройки часового пояса в
 * системе нет — колонки в `users` не существует, проверено. Вся отчётность
 * при этом считается по Москве (`ZoneInfo("Europe/Moscow")` в
 * `conversation_table` и `stats`). Диспетчеры могут сидеть не в Москве, и
 * «закрыт 12 авг, 23:40», посчитанное по их часам, разошлось бы с отчётом,
 * который читает владелец.
 *
 * ПОЧЕМУ ФИКСИРОВАННЫЕ +03:00, А НЕ `Intl` С ПОЯСОМ. Россия отменила
 * сезонный перевод часов в 2014 году: Москва постоянно UTC+3, без перехода на
 * летнее время. Смещение здесь — не упрощение, а факт, и оно даёт точный
 * инстант без разбора строк `Intl.DateTimeFormat`. Если пояс когда-нибудь
 * снова начнёт двигаться, чинить надо будет и здесь, и в `formatTime.ts`, где
 * московское время уже показывается через `MSK_TIME_ZONE`.
 */

/** Москва круглый год UTC+3 — сезонного перевода в России нет с 2014 года. */
const MSK_OFFSET_MINUTES = 3 * 60;

/**
 * День недели ПО МОСКВЕ: 0 — воскресенье, как у `Date.getDay()`.
 *
 * Нужен затем же, зачем и всё в этом файле: отчётность у продукта московская
 * (06 §0.1). Браузер оператора во Владивостоке на семь часов впереди, и
 * `new Date().getDay()` даёт ему СЛЕДУЮЩИЙ день — подписи под столбиками
 * уезжают на сутки относительно самих столбиков, которые сервер построил от
 * московской даты.
 */
export function moscowDayOfWeek(at: Date = new Date()): number {
  return new Date(at.getTime() + MSK_OFFSET_MINUTES * 60_000).getUTCDay();
}

/** Части московского времени для момента `at`. */
function moscowParts(at: Date): { y: number; m: number; d: number; hh: number; mm: number } {
  const shifted = new Date(at.getTime() + MSK_OFFSET_MINUTES * 60_000);
  return {
    y: shifted.getUTCFullYear(),
    m: shifted.getUTCMonth(),
    d: shifted.getUTCDate(),
    hh: shifted.getUTCHours(),
    mm: shifted.getUTCMinutes(),
  };
}

const MONTHS = [
  "янв", "фев", "мар", "апр", "мая", "июн",
  "июл", "авг", "сен", "окт", "ноя", "дек",
]; // prettier-ignore

/** «14 авг, 10:00» по Москве — подпись даты закрытия в карточке клиента. */
export function formatMoscow(at: Date | string): string {
  const date = typeof at === "string" ? new Date(at) : at;
  if (Number.isNaN(date.getTime())) return "";
  const { m, d, hh, mm } = moscowParts(date);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d} ${MONTHS[m]}, ${pad(hh)}:${pad(mm)}`;
}
