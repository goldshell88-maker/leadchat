import { describe, expect, it } from "vitest";
import {
  WAIT_LATE_MIN,
  WAIT_WARN_MIN,
  waitingForRow,
  waitingSince,
} from "@/shared/lib/waiting";

const NOW = Date.parse("2026-08-06T12:00:00Z");
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString();

describe("сколько клиент ждёт ответа (UX-аудит, docs/17 §Т1–Т2)", () => {
  it("до пяти минут уровень спокойный — чипа в списке не будет", () => {
    expect(waitingSince(ago(1), NOW)?.level).toBe("calm");
    expect(waitingSince(ago(WAIT_WARN_MIN - 1), NOW)?.level).toBe("calm");
  });

  it("с пяти минут — предупреждение, с пятнадцати — тревога", () => {
    expect(waitingSince(ago(WAIT_WARN_MIN), NOW)?.level).toBe("warn");
    expect(waitingSince(ago(12), NOW)?.level).toBe("warn");
    expect(waitingSince(ago(WAIT_LATE_MIN), NOW)?.level).toBe("late");
    expect(waitingSince(ago(180), NOW)?.level).toBe("late");
  });

  it("подпись сокращается по мере роста: минуты, часы, дни", () => {
    expect(waitingSince(ago(7), NOW)?.text).toBe("7 мин");
    expect(waitingSince(ago(59), NOW)?.text).toBe("59 мин");
    expect(waitingSince(ago(60), NOW)?.text).toBe("1 ч");
    expect(waitingSince(ago(60 * 26), NOW)?.text).toBe("1 дн");
  });

  it("меньше минуты и пустая метка — ничего", () => {
    expect(waitingSince(ago(0), NOW)).toBeNull();
    expect(waitingSince(null, NOW)).toBeNull();
    expect(waitingSince("не дата", NOW)).toBeNull();
  });

  it("строка списка: ждёт только тот, у кого есть непрочитанные", () => {
    // Непрочитанные означают, что последним писал клиент, а мы не открывали.
    expect(waitingForRow({ unread_count: 2, last_message_at: ago(20) }, NOW)?.level).toBe("late");
    // Прочитан — значит оператор диалог видел; счётчик просрочки здесь был бы
    // ложной тревогой и приучил бы не обращать на него внимания.
    expect(waitingForRow({ unread_count: 0, last_message_at: ago(20) }, NOW)).toBeNull();
  });

  it("значение зависит от переданного «сейчас», а не от системных часов", () => {
    // Ради этого `now` и приходит снаружи: посчитанное внутри useMemo
    // замерзало бы на первой отрисовке, и «ждёт 1 мин» висело бы часами.
    const iso = ago(3);
    expect(waitingSince(iso, NOW)?.level).toBe("calm");
    expect(waitingSince(iso, NOW + 20 * 60_000)?.level).toBe("late");
  });
});
