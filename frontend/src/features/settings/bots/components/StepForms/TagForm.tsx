import { Stack, TagsInput } from "@mantine/core";
import type { BotTagStep } from "@/shared/api/types";
import { StepRefSelect } from "../StepRefSelect";
import type { StepFormProps } from "./types";

/** 🏷 tag — повесить теги на диалог, без сообщений клиенту (02 §1.3). */
export function TagForm({ step, stepIds, onChange }: StepFormProps<BotTagStep>) {
  return (
    <Stack gap="var(--lc-space-3)">
      <TagsInput
        label="Теги"
        description="До 50 символов каждый; дубликаты схлопываются"
        value={step.params.tags}
        onChange={(tags) => onChange({ ...step, params: { tags } })}
        size="sm"
        comboboxProps={{ withinPortal: false }}
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
