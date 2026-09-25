import { useState } from "react";
import { DatePickerInput, DatesProvider } from "@mantine/dates";
import { moscowToday, parseIsoDate, toIsoDate } from "@/shared/lib/period";

type Range = [Date | null, Date | null];

/**
 * «Произвольный период» журналов: показывает нажатое сразу, а наружу отдаёт
 * только собранный диапазон (даты — ГГГГ-ММ-ДД по Москве).
 *
 * ⚠ ЗАЧЕМ СВОЙ ЧЕРНОВИК (проверка 24.09). Календарь Mantine управляемый: первый
 * щелчок даёт `[день, null]`, и пока это не лежит в `value`, для календаря
 * выбора не было — второй щелчок снова считался первым, и собрать период было
 * нельзя вовсе. Так жили журналы аудита и уведомлений; в статистике то же
 * починено 13.08 (72444fe) своим кодом.
 */
export function DateRangeInput({
  from,
  to,
  onChange,
  ariaLabel,
}: {
  from?: string | null;
  to?: string | null;
  onChange: (from: string, to: string) => void;
  ariaLabel: string;
}) {
  const [draft, setDraft] = useState<Range | null>(null);
  const applied: Range = [from ? parseIsoDate(from) : null, to ? parseIsoDate(to) : null];
  return (
    <DatesProvider settings={{ locale: "ru", firstDayOfWeek: 1, weekendDays: [0, 6] }}>
      <DatePickerInput
        size="xs"
        w={220}
        type="range"
        aria-label={ariaLabel}
        placeholder="Выберите даты"
        valueFormat="D MMM YYYY"
        maxDate={parseIsoDate(moscowToday()) ?? undefined}
        value={draft ?? applied}
        onChange={(value) => {
          const [start, end] = value;
          if (!start || !end) {
            setDraft(value);
            return;
          }
          setDraft(null);
          onChange(toIsoDate(start), toIsoDate(end));
        }}
      />
    </DatesProvider>
  );
}
