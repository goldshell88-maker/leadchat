import { Badge, Checkbox, Select, Stack, Text } from "@mantine/core";
import type { BotCloseOutcome, BotCloseStep } from "@/shared/api/types";
import { VariableTextarea } from "../VariableTextarea";
import type { StepFormProps } from "./types";

/**
 * ✅ close — закрыть диалог (02 §1.3). Терминальный. Если клиент напишет
 * снова, воркер входящих переоткроет диалог и бот может стартовать заново —
 * автоматического закрытия по таймауту в системе нет (решение владельца).
 */
/**
 * Итог закрытия — словарь `app/services/leads.py:OUTCOMES`, подписи прежнего
 * окна «Чем закончилось обращение?». Только отсюда сценарный бот и заводит
 * заявки (решение владельца 15.08): без поля в форме бот, собранный в
 * интерфейсе, не создавал их никогда (проверка 24.09).
 */
const OUTCOME_OPTIONS: { value: BotCloseOutcome; label: string }[] = [
  { value: "visit", label: "Выезд назначен" },
  { value: "declined", label: "Отказ" },
  { value: "not_our_profile", label: "Не наш профиль" },
  { value: "spam", label: "Спам" },
  { value: "no_reply", label: "Нет ответа" },
];

export function CloseForm({ step, vars, onChange }: StepFormProps<BotCloseStep>) {
  const p = step.params;
  const patch = (params: Partial<BotCloseStep["params"]>) => onChange({ ...step, params: { ...p, ...params } });

  return (
    <Stack gap="var(--lc-space-3)">
      <Badge color="gray" variant="light" style={{ alignSelf: "flex-start" }}>
        завершает сценарий
      </Badge>
      <Checkbox
        label="Закрыть молча"
        checked={p.silent ?? false}
        onChange={(e) => patch({ silent: e.currentTarget.checked })}
      />
      {!p.silent && (
        <VariableTextarea
          label="Прощальное сообщение"
          value={p.text}
          vars={vars}
          onChange={(text) => patch({ text: text || null })}
          minRows={1}
        />
      )}
      <Select
        label="Итог диалога"
        description="«Выезд назначен» — диалог уйдёт заявкой в лид-центр"
        placeholder="Без итога"
        data={OUTCOME_OPTIONS}
        value={p.outcome ?? null}
        onChange={(v) => patch({ outcome: (v ?? undefined) as BotCloseOutcome | undefined })}
        clearable
        comboboxProps={{ withinPortal: false }}
        size="sm"
      />
      <Text fz="xs" c="var(--lc-text-3)">
        В ленту добавится заметка «Диалог закрыт ботом»
      </Text>
    </Stack>
  );
}
