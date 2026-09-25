import { useEffect, useState } from "react";
import { TextInput } from "@mantine/core";

/**
 * Целое число в заданных границах (поля «Попыток», «Макс. длина ответа»,
 * «Глубина контекста» — 11 §5.2).
 *
 * Намеренно не `NumberInput` из Mantine: он тянет `react-number-format` (~65 КБ
 * исходника) ради форматирования дробей и разделителей разрядов, которых здесь
 * нет — все поля целые и двузначно-трёхзначные. Чанк редактора обязан влезать
 * в бюджет бандла (09 §приёмка), поэтому здесь обычный TextInput с
 * `inputMode="numeric"`: та же роль `textbox`, та же клавиатура на мобильном.
 *
 * Правка не дёргает сценарий на каждое нажатие: пока в поле пусто или значение
 * вне диапазона, наружу ничего не уходит — приводим к границам на blur.
 */
export function IntInput({
  label,
  description,
  min,
  max,
  value,
  onChange,
}: {
  label: string;
  description?: string;
  min: number;
  max: number;
  value: number;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(() => String(value));

  // Значение сменилось снаружи (выбрали другой шаг, откатили черновик) — перерисовываем.
  useEffect(() => {
    setDraft((prev) => (prev !== "" && Number(prev) === value ? prev : String(value)));
  }, [value]);

  return (
    <TextInput
      label={label}
      description={description}
      value={draft}
      inputMode="numeric"
      size="sm"
      onChange={(e) => {
        const digits = e.currentTarget.value.replace(/\D/g, "");
        setDraft(digits);
        const n = Number(digits);
        if (digits !== "" && n >= min && n <= max) onChange(n);
      }}
      onBlur={() => {
        const n = draft === "" ? value : Math.min(max, Math.max(min, Number(draft)));
        setDraft(String(n));
        if (n !== value) onChange(n);
      }}
    />
  );
}
