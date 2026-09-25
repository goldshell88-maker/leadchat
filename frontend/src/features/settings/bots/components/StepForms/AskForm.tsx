import { Group, Select, Stack, Text, TextInput } from "@mantine/core";
import type { BotAskStep, BotValidate } from "@/shared/api/types";
import { TIMEOUT_OPTIONS, VALIDATE_OPTIONS, selectToTimeout, timeoutToSelect } from "../../scenario";
import { IntInput } from "../IntInput";
import { StepRefSelect } from "../StepRefSelect";
import { VariableTextarea } from "../VariableTextarea";
import type { StepFormProps } from "./types";
import { IconAlert } from "@/shared/ui/Icon";

const CYRILLIC = /[а-яё]/i;

function validateKind(v: BotValidate | undefined): string {
  if (v && typeof v === "object") return "regex";
  return v ?? "any";
}

/** ❓ ask — задать вопрос и ждать ответ (02 §1.3, поля 11 §5.2). */
export function AskForm({ step, stepIds, vars, onChange }: StepFormProps<BotAskStep>) {
  const p = step.params;
  const kind = validateKind(p.validate);
  const regex = p.validate && typeof p.validate === "object" ? p.validate.regex : "";
  let regexError: string | null = null;
  if (kind === "regex" && regex) {
    try {
      new RegExp(regex);
    } catch {
      regexError = "Регулярка не компилируется";
    }
  }

  const patch = (params: Partial<BotAskStep["params"]>) => onChange({ ...step, params: { ...p, ...params } });

  return (
    <Stack gap="var(--lc-space-3)">
      <VariableTextarea
        label="Вопрос"
        description="Можно оставить пустым — тогда вопрос уже задан предыдущим шагом send"
        value={p.text}
        vars={vars}
        onChange={(text) => patch({ text: text || null })}
      />

      <Group grow align="flex-start">
        <TextInput
          label="Переменная"
          description="Латиница, с буквы: phone, problem"
          value={p.var}
          onChange={(e) => patch({ var: e.currentTarget.value })}
          error={CYRILLIC.test(p.var) ? "Только латиница — это имя ключа в bot_vars" : undefined}
          styles={{ input: { fontFamily: "var(--mantine-font-family-monospace, monospace)" } }}
          size="sm"
        />
        <Select
          label="Валидатор"
          data={VALIDATE_OPTIONS}
          value={kind}
          onChange={(v) =>
            patch({ validate: v === "regex" ? { regex: regex || ".+" } : ((v ?? "any") as BotValidate) })
          }
          allowDeselect={false}
          comboboxProps={{ withinPortal: false }}
          size="sm"
        />
      </Group>

      {kind === "regex" && (
        <TextInput
          label="Регулярное выражение"
          description="re.search по ответу клиента, до 200 символов"
          value={regex}
          onChange={(e) => patch({ validate: { regex: e.currentTarget.value } })}
          error={regexError ?? undefined}
          styles={{ input: { fontFamily: "var(--mantine-font-family-monospace, monospace)" } }}
          size="sm"
        />
      )}

      <VariableTextarea
        label="Retry-текст"
        description="Отправляется, когда ответ не прошёл валидацию"
        value={p.retry_text}
        vars={vars}
        onChange={(text) => patch({ retry_text: text || null })}
        minRows={1}
      />

      <Group grow align="flex-start">
        <IntInput
          label="Попыток"
          min={1}
          max={5}
          value={p.max_attempts ?? 2}
          onChange={(max_attempts) => patch({ max_attempts })}
        />
        <Select
          label="Таймаут"
          data={TIMEOUT_OPTIONS}
          value={timeoutToSelect(p.timeout)}
          onChange={(v) => patch({ timeout: selectToTimeout(v) })}
          allowDeselect={false}
          comboboxProps={{ withinPortal: false }}
          size="sm"
        />
      </Group>
      {p.timeout === null || p.timeout === undefined ? (
        <Text fz="xs" c="var(--lc-warning-text)">
          <IconAlert size={14} /> Без таймаута бот может ждать ответа вечно
        </Text>
      ) : null}

      <StepRefSelect
        label="ответ →"
        value={step.next}
        stepIds={stepIds}
        onChange={(next) => onChange({ ...step, next: next ?? step.next })}
      />
      <StepRefSelect
        label="по таймауту →"
        value={step.on_timeout ?? null}
        stepIds={stepIds}
        nullable
        onChange={(v) => onChange({ ...step, on_timeout: v })}
      />
      <StepRefSelect
        label="попытки исчерпаны →"
        value={step.on_invalid ?? null}
        stepIds={stepIds}
        nullable
        onChange={(v) => onChange({ ...step, on_invalid: v })}
      />
    </Stack>
  );
}
