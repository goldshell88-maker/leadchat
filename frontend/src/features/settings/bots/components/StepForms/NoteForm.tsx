import { Stack } from "@mantine/core";
import type { BotNoteStep } from "@/shared/api/types";
import { StepRefSelect } from "../StepRefSelect";
import { VariableTextarea } from "../VariableTextarea";
import type { StepFormProps } from "./types";

/** 🗒 note — внутренняя заметка в ленту диалога, клиенту не уходит (02 §1.3). */
export function NoteForm({ step, stepIds, vars, onChange }: StepFormProps<BotNoteStep>) {
  return (
    <Stack gap="var(--lc-space-3)">
      <VariableTextarea
        label="Текст заметки"
        description="Видна только сотрудникам — жёлтая вставка в ленте"
        value={step.params.text}
        vars={vars}
        onChange={(text) => onChange({ ...step, params: { text } })}
      />
      <StepRefSelect
        label="далее →"
        value={step.next}
        stepIds={stepIds}
        onChange={(next) => onChange({ ...step, next: next ?? step.next })}
      />
    </Stack>
  );
}
