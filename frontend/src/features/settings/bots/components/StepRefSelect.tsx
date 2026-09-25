import { Select } from "@mantine/core";

/**
 * Переход на шаг: Select со списком `id` всех шагов сценария (02 §5.1).
 * Для nullable-ссылок (`on_timeout`, `on_invalid`, `on_no_match`,
 * `on_low_confidence`) первым пунктом идёт «— дефолт: передать менеджеру —»:
 * null в JSON = дефолтный handoff движка (02 §1.3).
 */

export const DEFAULT_REF_VALUE = "__default_handoff__";
export const DEFAULT_REF_LABEL = "— дефолт: передать менеджеру —";

export function StepRefSelect({
  label,
  ariaLabel,
  value,
  stepIds,
  nullable = false,
  onChange,
  description,
}: {
  /** Видимая подпись; в таблицах вариантов её рисуют только у первой строки. */
  label?: string;
  /** Имя для скринридера, когда видимой подписи нет (строки таблиц). */
  ariaLabel?: string;
  value: string | null | undefined;
  stepIds: string[];
  nullable?: boolean;
  onChange: (next: string | null) => void;
  description?: string;
}) {
  const data = [
    ...(nullable ? [{ value: DEFAULT_REF_VALUE, label: DEFAULT_REF_LABEL }] : []),
    ...stepIds.map((id) => ({ value: id, label: id })),
  ];
  // Битую ссылку (шаг переименовали/удалили) показываем как есть — её ловит
  // валидатор `broken_ref`, молча подменять её на первый попавшийся шаг нельзя.
  const current = value ?? null;
  if (current && !stepIds.includes(current)) data.push({ value: current, label: `${current} (нет такого шага)` });

  return (
    <Select
      label={label}
      aria-label={ariaLabel}
      description={description}
      data={data}
      value={current ?? (nullable ? DEFAULT_REF_VALUE : null)}
      onChange={(v) => onChange(v === DEFAULT_REF_VALUE || v === null ? null : v)}
      allowDeselect={false}
      checkIconPosition="right"
      comboboxProps={{ withinPortal: false }}
      size="sm"
    />
  );
}
