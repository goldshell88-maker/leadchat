import { ActionIcon, Button, Checkbox, Group, Select, Stack, Text, TextInput } from "@mantine/core";
import type { BotCondition, BotConditionKind, BotConditionStep, BotWeekDay } from "@/shared/api/types";
import { CONDITION_KINDS, WEEK_DAYS, crossesMidnight } from "../../scenario";
import { KeywordsInput } from "../KeywordsInput";
import { StepRefSelect } from "../StepRefSelect";
import type { StepFormProps } from "./types";
import { IconX } from "@/shared/ui/Icon";

function emptyCondition(kind: BotConditionKind): BotCondition {
  switch (kind) {
    case "work_hours":
      return { kind, from: "10:00", to: "20:00", timezone: "Europe/Moscow" };
    case "var_exists":
      return { kind, var: "phone" };
    case "var_equals":
      return { kind, var: "phone", value: "" };
    case "text_contains":
      return { kind, keywords: [] };
    case "text_matches":
      return { kind, regex: ".+" };
  }
}

function ConditionFields({ cond, onChange }: { cond: BotCondition; onChange: (next: BotCondition) => void }) {
  switch (cond.kind) {
    case "work_hours":
      return (
        <Stack gap={4} style={{ flex: 1 }}>
          <Group gap="var(--lc-space-2)" align="flex-end" wrap="nowrap">
            <TextInput
              type="time"
              aria-label="Рабочее время с"
              value={cond.from}
              onChange={(e) => onChange({ ...cond, from: e.currentTarget.value })}
              size="sm"
              w={110}
            />
            <TextInput
              type="time"
              aria-label="Рабочее время до"
              value={cond.to}
              onChange={(e) => onChange({ ...cond, to: e.currentTarget.value })}
              size="sm"
              w={110}
            />
            <TextInput
              aria-label="Часовой пояс"
              value={cond.timezone ?? "Europe/Moscow"}
              onChange={(e) => onChange({ ...cond, timezone: e.currentTarget.value })}
              size="sm"
              style={{ flex: 1 }}
            />
          </Group>
          <Group gap={4}>
            {WEEK_DAYS.map((d) => (
              <Checkbox
                key={d.value}
                size="xs"
                label={d.label}
                checked={(cond.days ?? WEEK_DAYS.map((x) => x.value)).includes(d.value)}
                onChange={(e) => {
                  const current = new Set<BotWeekDay>(cond.days ?? WEEK_DAYS.map((x) => x.value));
                  if (e.currentTarget.checked) current.add(d.value);
                  else current.delete(d.value);
                  onChange({ ...cond, days: WEEK_DAYS.map((x) => x.value).filter((v) => current.has(v)) });
                }}
              />
            ))}
          </Group>
          {crossesMidnight(cond.from, cond.to) && (
            <Text fz="xs" c="var(--lc-text-3)">
              Интервал через полночь — день относится к его началу
            </Text>
          )}
        </Stack>
      );
    case "var_exists":
      return (
        <TextInput
          aria-label="Имя переменной"
          value={cond.var}
          onChange={(e) => onChange({ ...cond, var: e.currentTarget.value })}
          size="sm"
          style={{ flex: 1 }}
        />
      );
    case "var_equals":
      return (
        <Group gap="var(--lc-space-2)" wrap="nowrap" style={{ flex: 1 }}>
          <TextInput
            aria-label="Имя переменной"
            value={cond.var}
            onChange={(e) => onChange({ ...cond, var: e.currentTarget.value })}
            size="sm"
          />
          <TextInput
            aria-label="Значение переменной"
            value={cond.value}
            onChange={(e) => onChange({ ...cond, value: e.currentTarget.value })}
            size="sm"
            style={{ flex: 1 }}
          />
        </Group>
      );
    case "text_contains":
      return (
        <KeywordsInput
          ariaLabel="Ключевые слова через запятую"
          value={cond.keywords}
          onChange={(keywords) => onChange({ ...cond, keywords })}
          style={{ flex: 1 }}
        />
      );
    case "text_matches":
      return (
        <TextInput
          aria-label="Регулярное выражение"
          value={cond.regex}
          onChange={(e) => onChange({ ...cond, regex: e.currentTarget.value })}
          size="sm"
          style={{ flex: 1 }}
          styles={{ input: { fontFamily: "var(--mantine-font-family-monospace, monospace)" } }}
        />
      );
  }
}

/** 🔀 condition — ветвление без вопроса (02 §1.3): первое истинное побеждает. */
export function ConditionForm({ step, stepIds, onChange }: StepFormProps<BotConditionStep>) {
  const p = step.params;
  const patch = (params: Partial<BotConditionStep["params"]>) => onChange({ ...step, params: { ...p, ...params } });

  return (
    <Stack gap="var(--lc-space-3)">
      <Text fz="xs" c="var(--lc-text-3)">
        Условия проверяются сверху вниз, побеждает первое истинное
      </Text>

      {p.conditions.map((branch, i) => (
        <Group key={i} align="flex-start" gap="var(--lc-space-2)" wrap="nowrap" className="bot-condition-row">
          <Text fz="sm" c="var(--lc-text-2)" mt={6}>
            если
          </Text>
          <Select
            aria-label={`Вид условия ${i + 1}`}
            data={CONDITION_KINDS}
            value={branch.if.kind}
            onChange={(v) =>
              patch({
                conditions: p.conditions.map((c, idx) =>
                  idx === i ? { ...c, if: emptyCondition((v ?? "work_hours") as BotConditionKind) } : c,
                ),
              })
            }
            allowDeselect={false}
            comboboxProps={{ withinPortal: false }}
            size="sm"
            /*
             * 210, А НЕ 180. Самая длинная подпись — «переменная существует»
             * — просит 141 px, а под текст в поле 180 остаётся около 138:
             * условие открывалось как «переменная суще…».
             *
             * ⚠ ЭТО ЕДИНСТВЕННАЯ ИЗ ЧЕТЫРЁХ ПРАВОК ОБРЕЗКИ, НЕ ПРОВЕРЕННАЯ НА
             * ЖИВОМ ЭКРАНЕ: в бою нет ни одного бота, форму условия открыть
             * негде. 141 против 138 — разница внутри погрешности замера, так
             * что обрезка могла и не проявляться. Ширину подняли с запасом:
             * навредить она не может (ряд не тесный), а риск снимает.
             */
            w={210}
          />
          <ConditionFields
            cond={branch.if}
            onChange={(next) =>
              patch({ conditions: p.conditions.map((c, idx) => (idx === i ? { ...c, if: next } : c)) })
            }
          />
          <div style={{ width: 170 }}>
            <StepRefSelect
              ariaLabel={`Условие ${i + 1}: переход`}
              value={branch.next}
              stepIds={stepIds}
              onChange={(next) =>
                patch({
                  conditions: p.conditions.map((c, idx) => (idx === i ? { ...c, next: next ?? c.next } : c)),
                })
              }
            />
          </div>
          <ActionIcon
            variant="subtle"
            color="red"
            mt={4}
            aria-label={`Удалить условие ${i + 1}`}
            onClick={() => patch({ conditions: p.conditions.filter((_, idx) => idx !== i) })}
          >
            <IconX size={14} />
          </ActionIcon>
        </Group>
      ))}

      <Button
        variant="subtle"
        size="compact-sm"
        disabled={p.conditions.length >= 10}
        onClick={() =>
          patch({
            conditions: [...p.conditions, { if: emptyCondition("var_exists"), next: p.else }],
          })
        }
        style={{ alignSelf: "flex-start" }}
      >
        Добавить условие
      </Button>

      <StepRefSelect
        label="иначе →"
        description="Обязательная ветка: сюда бот идёт, если ни одно условие не сработало"
        value={p.else}
        stepIds={stepIds}
        onChange={(next) => patch({ else: next ?? p.else })}
      />
    </Stack>
  );
}
