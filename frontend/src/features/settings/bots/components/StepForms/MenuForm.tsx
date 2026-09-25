import { ActionIcon, Button, Group, Select, Stack, Text, TextInput } from "@mantine/core";
import type { BotMenuOption, BotMenuStep } from "@/shared/api/types";
import { TIMEOUT_OPTIONS, selectToTimeout, timeoutToSelect } from "../../scenario";
import { IntInput } from "../IntInput";
import { KeywordsInput } from "../KeywordsInput";
import { StepRefSelect } from "../StepRefSelect";
import { VariableTextarea } from "../VariableTextarea";
import type { StepFormProps } from "./types";
import { IconX } from "@/shared/ui/Icon";

/**
 * Номер нового варианта — следующий за самым большим среди id `opt_N` и
 * ключевых слов-цифр (проверка 24.09). Было `длина + 1`: после удаления
 * первого из двух новый вариант получал `opt_2` и «2» — дубль уцелевшего,
 * и совпадение всегда уходило в первый.
 */
function nextOptionNumber(options: BotMenuOption[]): number {
  const used = options
    .flatMap((o) => [o.id.replace(/^opt_/, ""), ...o.match])
    .map((v) => Number(v.trim()))
    .filter((n) => Number.isInteger(n) && n > 0);
  return Math.max(0, ...used) + 1;
}

/**
 * 📋 menu — вопрос с вариантами (02 §1.3). Нативных кнопок в API Авито нет,
 * поэтому меню — нумерованный текст, а матчинг идёт по ключевым словам.
 */
export function MenuForm({ step, stepIds, vars, onChange }: StepFormProps<BotMenuStep>) {
  const p = step.params;
  const patch = (params: Partial<BotMenuStep["params"]>) => onChange({ ...step, params: { ...p, ...params } });

  const setOption = (index: number, next: Partial<BotMenuOption>) =>
    patch({ options: p.options.map((o, i) => (i === index ? { ...o, ...next } : o)) });

  return (
    <Stack gap="var(--lc-space-3)">
      <VariableTextarea
        label="Текст вопроса"
        description="Варианты перечисляйте цифрами — клиент отвечает номером или словом"
        value={p.text}
        vars={vars}
        onChange={(text) => patch({ text })}
      />

      <TextInput
        label="Переменная (необязательно)"
        description="Сюда запишется id выбранного варианта"
        value={p.var ?? ""}
        onChange={(e) => patch({ var: e.currentTarget.value || undefined })}
        styles={{ input: { fontFamily: "var(--mantine-font-family-monospace, monospace)" } }}
        size="sm"
      />

      <div>
        <Text fz="sm" fw={500} c="var(--lc-text-1)" mb={4}>
          Варианты ({p.options.length}/10)
        </Text>
        <Stack gap="var(--lc-space-2)">
          {p.options.map((o, i) => (
            <Group key={i} align="flex-end" gap="var(--lc-space-2)" wrap="nowrap" className="bot-option-row">
              <TextInput
                label={i === 0 ? "Вариант" : undefined}
                aria-label={`Вариант ${i + 1}: название`}
                value={o.label}
                onChange={(e) => setOption(i, { label: e.currentTarget.value })}
                size="sm"
                style={{ flex: "1 1 30%" }}
              />
              <KeywordsInput
                label={i === 0 ? "Ключевые слова через запятую" : undefined}
                ariaLabel={`Вариант ${i + 1}: ключевые слова`}
                value={o.match}
                onChange={(match) => setOption(i, { match })}
                style={{ flex: "1 1 40%" }}
              />
              <div style={{ flex: "1 1 30%" }}>
                <StepRefSelect
                  label={i === 0 ? "переход →" : undefined}
                  ariaLabel={`Вариант ${i + 1}: переход`}
                  value={o.next}
                  stepIds={stepIds}
                  onChange={(next) => setOption(i, { next: next ?? o.next })}
                />
              </div>
              <ActionIcon
                variant="subtle"
                color="red"
                aria-label={`Удалить вариант ${i + 1}`}
                onClick={() => patch({ options: p.options.filter((_, idx) => idx !== i) })}
              >
                <IconX size={14} />
              </ActionIcon>
            </Group>
          ))}
        </Stack>
        <Button
          variant="subtle"
          size="compact-sm"
          mt="var(--lc-space-2)"
          disabled={p.options.length >= 10}
          onClick={() => {
            const n = nextOptionNumber(p.options);
            patch({
              options: [
                ...p.options,
                { id: `opt_${n}`, label: "Новый вариант", match: [String(n)], next: stepIds[0] ?? "" },
              ],
            });
          }}
        >
          Добавить вариант
        </Button>
      </div>

      <VariableTextarea
        label="Retry-текст"
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

      <StepRefSelect
        label="мимо вариантов →"
        description="Несовпадение считается «мимо сценария» (условие handoff №4)"
        value={step.on_no_match ?? null}
        stepIds={stepIds}
        nullable
        onChange={(v) => onChange({ ...step, on_no_match: v })}
      />
      <StepRefSelect
        label="по таймауту →"
        value={step.on_timeout ?? null}
        stepIds={stepIds}
        nullable
        onChange={(v) => onChange({ ...step, on_timeout: v })}
      />
    </Stack>
  );
}
