import { ActionIcon, Button, Group, Stack } from "@mantine/core";
import type { BotSendStep } from "@/shared/api/types";
import { IconX } from "@/shared/ui/Icon";
import { SEND_POOL_MAX, sendVariants } from "../../scenario";
import { StepRefSelect } from "../StepRefSelect";
import { VariableTextarea } from "../VariableTextarea";
import type { StepFormProps } from "./types";

/**
 * 💬 send — отправить сообщение (02 §1.3).
 *
 * Несколько формулировок — пул (решение владельца 26.08): клиенту уходит одна,
 * и разные клиенты получают разные слова. Серверная заготовка «Первичный
 * приём» держит пул в дожиме, и редактор на нём падал (проверка 24.09).
 * Одна оставшаяся формулировка снова пишется строкой — так её читает всё
 * остальное.
 */
export function SendForm({ step, stepIds, vars, onChange }: StepFormProps<BotSendStep>) {
  const variants = sendVariants(step.params.text);
  const pool = variants.length > 1;
  const setVariants = (next: string[]) =>
    onChange({ ...step, params: { text: next.length === 1 ? next[0] : next } });

  return (
    <Stack gap="var(--lc-space-3)">
      {variants.map((text, i) => (
        <Group key={i} align="flex-end" gap="var(--lc-space-2)" wrap="nowrap">
          <div style={{ flex: 1 }}>
            <VariableTextarea
              label={pool ? `Формулировка ${i + 1}` : "Текст"}
              description={
                i === 0
                  ? "1–1000 символов. Доставка асинхронная — бот не ждёт подтверждения Авито"
                  : undefined
              }
              value={text}
              vars={vars}
              onChange={(value) => setVariants(variants.map((v, j) => (j === i ? value : v)))}
            />
          </div>
          {pool && (
            <ActionIcon
              variant="subtle"
              color="red"
              mb={6}
              aria-label={`Удалить формулировку ${i + 1}`}
              onClick={() => setVariants(variants.filter((_, j) => j !== i))}
            >
              <IconX size={14} />
            </ActionIcon>
          )}
        </Group>
      ))}
      <Button
        variant="subtle"
        size="compact-sm"
        style={{ alignSelf: "flex-start" }}
        disabled={variants.length >= SEND_POOL_MAX}
        onClick={() => setVariants([...variants, ""])}
      >
        Добавить формулировку
      </Button>
      <StepRefSelect
        label="далее →"
        value={step.next}
        stepIds={stepIds}
        onChange={(next) => onChange({ ...step, next: next ?? step.next })}
      />
    </Stack>
  );
}
