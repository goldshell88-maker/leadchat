import { Group, Slider, Stack, Text } from "@mantine/core";
import type { BotAiAnswerStep } from "@/shared/api/types";
import { IntInput } from "../IntInput";
import { StepRefSelect } from "../StepRefSelect";
import type { StepFormProps } from "./types";

/**
 * ✨ ai_answer — ответ через Claude по базе знаний бота (02 §1.3, §3.2).
 * Недоступность или таймаут AI не блокируют доставку: движок делает
 * handoff(reason="ai_unavailable") без ретраев (DESIGN 4.5, 02 §3.3).
 */
export function AiAnswerForm({ step, stepIds, onChange }: StepFormProps<BotAiAnswerStep>) {
  const p = step.params;
  const patch = (params: Partial<BotAiAnswerStep["params"]>) => onChange({ ...step, params: { ...p, ...params } });
  const threshold = p.confidence_threshold ?? 0.6;

  return (
    <Stack gap="var(--lc-space-3)">
      <div>
        <Text fz="sm" fw={500} c="var(--lc-text-1)">
          Порог уверенности: {threshold.toFixed(2)}
        </Text>
        <Slider
          min={0}
          max={1}
          step={0.05}
          value={threshold}
          onChange={(v) => patch({ confidence_threshold: v })}
          label={(v) => v.toFixed(2)}
          aria-label="Порог уверенности"
          mt={6}
        />
        <Text fz="xs" c="var(--lc-text-3)" mt={4}>
          Ниже порога (или если модель сама просит человека) — переход «при низкой уверенности»
        </Text>
      </div>

      <Group grow align="flex-start">
        <IntInput
          label="Макс. длина ответа"
          description="символов"
          min={100}
          max={1000}
          value={p.max_reply_len ?? 800}
          onChange={(max_reply_len) => patch({ max_reply_len })}
        />
        <IntInput
          label="Глубина контекста"
          description="последних сообщений диалога"
          min={2}
          max={30}
          value={p.context_messages ?? 10}
          onChange={(context_messages) => patch({ context_messages })}
        />
      </Group>

      <Text fz="xs" c="var(--lc-text-3)">
        Приватность: перед отправкой в модель телефоны в тексте заменяются на {"{PHONE}"} — найденный номер
        остаётся в переменных бота и в карточке клиента
      </Text>

      <StepRefSelect
        label="далее →"
        value={step.next}
        stepIds={stepIds}
        onChange={(next) => onChange({ ...step, next: next ?? step.next })}
      />
      <StepRefSelect
        label="при низкой уверенности →"
        value={step.on_low_confidence ?? null}
        stepIds={stepIds}
        nullable
        onChange={(v) => onChange({ ...step, on_low_confidence: v })}
      />
    </Stack>
  );
}
