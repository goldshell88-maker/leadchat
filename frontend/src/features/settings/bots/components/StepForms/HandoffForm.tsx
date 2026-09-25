import { Badge, Stack, TagsInput, TextInput } from "@mantine/core";
import type { BotHandoffStep } from "@/shared/api/types";
import { VariableTextarea } from "../VariableTextarea";
import type { StepFormProps } from "./types";

/**
 * 👤 handoff — передать менеджеру (02 §1.3). Терминальный: движок
 * останавливается, диалог возвращается в общую очередь (`status='new'`),
 * менеджеру уходит заметка-сводка с собранными переменными (02 §4).
 */
export function HandoffForm({ step, vars, onChange }: StepFormProps<BotHandoffStep>) {
  const p = step.params;
  const patch = (params: Partial<BotHandoffStep["params"]>) => onChange({ ...step, params: { ...p, ...params } });

  return (
    <Stack gap="var(--lc-space-3)">
      <Badge color="gray" variant="light" style={{ alignSelf: "flex-start" }}>
        завершает сценарий
      </Badge>
      <TextInput
        label="Причина"
        description="Попадёт в bot_vars.handoff.reason и в журнал аудита"
        value={p.reason ?? "scenario"}
        onChange={(e) => patch({ reason: e.currentTarget.value })}
        size="sm"
      />
      <VariableTextarea
        label="Комментарий менеджеру"
        description="Ляжет заметкой в диалог — её видят только сотрудники"
        value={p.comment}
        vars={vars}
        onChange={(comment) => patch({ comment: comment || null })}
        minRows={2}
      />
      <TagsInput
        label="Теги"
        description="Добавятся к conversations.tags"
        value={p.tags ?? []}
        onChange={(tags) => patch({ tags })}
        size="sm"
        comboboxProps={{ withinPortal: false }}
      />
    </Stack>
  );
}
