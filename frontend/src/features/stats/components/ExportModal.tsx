import { useState } from "react";
import { Button, Checkbox, Group, Modal, Radio, Stack, Text } from "@mantine/core";
import type { ExportFormat, ExportSheet } from "@/shared/api/types";
import { formatPeriodLabel, type Period } from "@/shared/lib/period";
import { formatDayLabel } from "../lib/format";
import { useStatsExport } from "../hooks/useStatsExport";

const SHEET_LABELS: Array<{ value: ExportSheet; label: string }> = [
  { value: "summary", label: "Сводка" },
  { value: "managers", label: "Менеджеры" },
  { value: "conversations", label: "Диалоги" },
];

/**
 * Модалка экспорта (11 §6.3): формат → листы → «Выгрузить».
 * CSV — только лист «Диалоги» (06 §5.2), поэтому чекбоксы заменяются подписью.
 * Прогресс и ссылка на файл живут в тостах (useStatsExport).
 */
export function ExportModal({
  opened,
  onClose,
  period,
  accountId,
  managerIds,
}: {
  opened: boolean;
  onClose(): void;
  period: Period;
  accountId?: string;
  managerIds: string[];
}) {
  const [format, setFormat] = useState<ExportFormat>("xlsx");
  const [sheets, setSheets] = useState<ExportSheet[]>(["summary", "managers", "conversations"]);
  const { start, isStarting, running } = useStatsExport();

  const submit = () => {
    start(
      {
        format,
        date_from: period.dateFrom,
        date_to: period.dateTo,
        account_id: accountId ?? null,
        manager_id: managerIds,
        sheets: format === "csv" ? ["conversations"] : sheets,
      },
      { onSuccess: () => onClose() },
    );
  };

  return (
    <Modal opened={opened} onClose={onClose} title="Выгрузка статистики" centered>
      <Stack gap="var(--lc-space-4)">
        <Text fz="sm" c="var(--lc-text-2)">
          Период: {formatPeriodLabel(period, formatDayLabel)}
        </Text>

        <Radio.Group
          label="Формат"
          value={format}
          onChange={(v) => setFormat(v as ExportFormat)}
          aria-label="Формат выгрузки"
        >
          <Group gap="var(--lc-space-4)" mt="var(--lc-space-2)">
            <Radio value="xlsx" label="XLSX" />
            <Radio value="csv" label="CSV" />
          </Group>
        </Radio.Group>

        {format === "xlsx" ? (
          <Checkbox.Group
            label="Листы"
            value={sheets}
            onChange={(v) => setSheets(v as ExportSheet[])}
            aria-label="Листы выгрузки"
          >
            <Group gap="var(--lc-space-4)" mt="var(--lc-space-2)">
              {SHEET_LABELS.map((s) => (
                <Checkbox key={s.value} value={s.value} label={s.label} />
              ))}
            </Group>
          </Checkbox.Group>
        ) : (
          <Text fz="sm" c="var(--lc-text-3)">
            CSV содержит только лист «Диалоги» — по строке на диалог
          </Text>
        )}

        {managerIds.length > 0 && (
          <Text fz="xs" c="var(--lc-text-3)">
            Выгрузка учитывает выбранный фильтр по менеджерам ({managerIds.length})
          </Text>
        )}

        <Group justify="flex-end" gap="var(--lc-space-2)">
          <Button variant="subtle" onClick={onClose}>
            Отмена
          </Button>
          <Button
            onClick={submit}
            loading={isStarting}
            disabled={running || (format === "xlsx" && sheets.length === 0)}
          >
            Выгрузить
          </Button>
        </Group>

        {running && (
          <Text fz="xs" c="var(--lc-text-3)">
            Предыдущая выгрузка ещё готовится — дождитесь её окончания
          </Text>
        )}
      </Stack>
    </Modal>
  );
}
