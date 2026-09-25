/**
 * Форматирование чисел статистики. Бэкенд отдаёт длительности целыми секундами
 * (06 §4), человекочитаемый вид — здесь; формулировки — словарь метрик 06 §1.
 */

const NUMBER_FMT = new Intl.NumberFormat("ru-RU");

export const EM_DASH = "—";

export function formatNumber(v: number | null | undefined): string {
  return typeof v === "number" && Number.isFinite(v) ? NUMBER_FMT.format(v) : EM_DASH;
}

/** «35 с» · «1 м 35 с» · «2 ч 05 м» (11 §6.1). null → «—» (нет данных ≠ ноль). */
export function formatDuration(sec: number | null | undefined): string {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec < 0) return EM_DASH;
  const s = Math.round(sec);
  if (s < 60) return `${s} с`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) {
    const rest = s % 60;
    return rest === 0 ? `${minutes} м` : `${minutes} м ${rest} с`;
  }
  const hours = Math.floor(minutes / 60);
  const restMin = minutes % 60;
  return restMin === 0 ? `${hours} ч` : `${hours} ч ${String(restMin).padStart(2, "0")} м`;
}

/**
 * Длительность для ПОДПИСИ ОСИ — та же лестница, но без пробелов (находка №6).
 *
 * У оси Y ровно 40 единиц viewBox: подпись прижата к `x = PAD.left - 8` = 40 и
 * растёт влево, а слева от нуля SVG режет по viewBox. «1 ч 20 м» — восемь
 * знаков, это ~52 единицы: у метрики «Первый ответ» подписи обрезались посреди
 * числа, то есть ось врала. Тот же смысл в пяти знаках («1ч20м», ~32) помещается
 * с запасом.
 *
 * ⚠ ТОЛЬКО ДЛЯ ОСИ. В подсказке и в карточке остаётся полная форма: там место
 * есть, и «1 ч 20 м» читается человеком, а не считывается краем глаза.
 */
export function formatDurationAxis(sec: number | null | undefined): string {
  return formatDuration(sec).replace(/ /g, "");
}

/**
 * «09:00–21:00» — рабочее окно из настроек (FUNC-42).
 *
 * Окно НАСТРАИВАЕТСЯ, а печаталось числами прямо в разметке: смени владелец
 * часы на 09:00–21:00 — цифра под подписью считалась бы по новым часам, а
 * подпись обещала бы старые. Формат один на все места, где окно называют
 * вслух: подсказка первого ответа и легенда тепловой карты.
 */
export function formatHourRange(hours: { start_hour: number; end_hour: number } | undefined): string | null {
  if (!hours) return null;
  const pad = (h: number) => `${String(h).padStart(2, "0")}:00`;
  return `${pad(hours.start_hour)}–${pad(hours.end_hour)}`;
}

/** Проценты метрики «закрыто ботом»: «18,6 %». */
export function formatPercent(v: number | null | undefined, digits = 1): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return EM_DASH;
  return `${v.toFixed(digits).replace(".", ",").replace(/,0$/, "")} %`;
}

/** Знак дельты типографским минусом: «+11,4 %», «−3,7 пп», «—» при null. */
export function formatDelta(deltaPct: number | null | undefined, unit: "%" | "пп" = "%"): string {
  if (typeof deltaPct !== "number" || !Number.isFinite(deltaPct)) return EM_DASH;
  const rounded = Math.round(deltaPct * 10) / 10;
  const sign = rounded > 0 ? "+" : rounded < 0 ? "−" : "";
  const abs = Math.abs(rounded).toFixed(1).replace(".", ",").replace(/,0$/, "");
  return `${sign}${abs} ${unit}`;
}

/**
 * «Данные на 14:05» — метка свежести MV (11 §6, 06 §3.2).
 * Время показываем по Москве: все бизнес-определения статистики — MSK (06 §0.1).
 */
const MSK_TIME_FMT = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Europe/Moscow",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatRefreshedAt(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  /*
   * ⚠ ПОЯС НАЗВАН ВСЛУХ (проверка боя 14 августа). Стояло «Данные на 17:05» —
   * число московское, а сотрудник на UTC+10 читает его как своё и видит время
   * из будущего. Экран, к слову, отстал от собственного сервера: в выгрузке
   * подпись «(МСК)» сервер ставит давно (`stats.py`).
   *
   * Лечится именно подписью, а не сменой пояса: все бизнес-определения
   * статистики московские (06 §0.1), и «сегодня» на этом экране — московские
   * сутки. Сделай мы час местным, он разошёлся бы с периодом под ним.
   */
  return `Данные на ${MSK_TIME_FMT.format(d)} по Москве`;
}

const MSK_DATE_FMT = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Europe/Moscow",
  day: "numeric",
  month: "short",
});

/** «2026-08-01» → «1 авг.» (подписи оси графика и заголовок периода). */
export function formatDayLabel(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? date : MSK_DATE_FMT.format(d);
}

/** Метка точки ряда: дата при group=day, «4 авг., 13:00» при group=hour. */
export function formatPointLabel(ts: string, group: "day" | "hour"): string {
  if (group === "day") return formatDayLabel(ts);
  const [date, time] = ts.split("T");
  return `${formatDayLabel(date)}, ${time?.slice(0, 5) ?? ""}`;
}
