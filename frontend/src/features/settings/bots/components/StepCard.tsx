import { useEffect, useState } from "react";
import { Accordion, ActionIcon, Badge, Button, Group, Modal, Select, Text, TextInput, Tooltip } from "@mantine/core";
import type { BotScenarioIssue, BotStep, BotStepType } from "@/shared/api/types";
import { useBotDraft } from "../draftStore";
import { STEP_META, stepMeta, stepSummary } from "../scenario";
import { StepForm } from "./StepForms";
import { IconAlert, IconArrowDown, IconArrowUp, IconX, IconXCircle } from "@/shared/ui/Icon";

/**
 * Карточка шага (02 §5.1, 11 §5.2): иконка типа, редактируемый `id`
 * (моноширинный — при переименовании все ссылки на шаг чинятся автоматически),
 * сводка одной строкой; разворот — форма параметров под конкретный тип.
 * Красная рамка — ошибка валидации, жёлтая — предупреждение.
 */
export function StepCard({
  step,
  index,
  total,
  stepIds,
  vars,
  isEntry,
  issues,
  focused,
  open,
}: {
  step: BotStep;
  index: number;
  total: number;
  stepIds: string[];
  vars: string[];
  isEntry: boolean;
  issues: BotScenarioIssue[];
  focused: boolean;
  /** Карточка раскрыта: форму монтируем только тогда (сценарий — до 200 шагов). */
  open: boolean;
}) {
  const renameStep = useBotDraft((s) => s.renameStep);
  const replaceStep = useBotDraft((s) => s.replaceStep);
  const changeStepType = useBotDraft((s) => s.changeStepType);
  const moveStep = useBotDraft((s) => s.moveStep);
  const removeStep = useBotDraft((s) => s.removeStep);
  const setEntry = useBotDraft((s) => s.setEntry);

  const [idDraft, setIdDraft] = useState(step.id);
  /** Почему набранный идентификатор не принят; null — принят или ещё не проверяли. */
  const [idError, setIdError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: "type"; type: BotStepType } | { kind: "delete" } | null>(null);

  useEffect(() => {
    setIdDraft(step.id);
    setIdError(null);
  }, [step.id]);

  const meta = stepMeta(step.type);
  const level = issues.some((i) => i.level === "error") ? "error" : issues.length ? "warning" : null;

  /*
   * ОТКАЗ ПЕРЕИМЕНОВАТЬ ШАГ ГОВОРИТСЯ ВСЛУХ (BOT-04).
   *
   * Было: занятый идентификатор молча возвращал поле к старому значению —
   * `setIdDraft(step.id)` без единого слова. Со стороны это выглядит как
   * поломка сохранения: человек набрал `greeting`, ушёл фокусом, увидел в поле
   * прежнее `step_3` и решил, что поле не работает. Дальше он набирает то же
   * самое ещё раз, потому что причины ему не назвали, а «уже занят» — вещь,
   * которую видно только из списка всех шагов.
   *
   * Теперь набранное остаётся в поле рядом с объяснением: сценарий трогаем
   * только при годном значении, а поле показывает, ЧТО именно не приняли и
   * почему. Молчаливого отката больше нет ни в одной ветке — пустое имя тоже
   * называется, а не гасится.
   *
   * Ошибка снимается первой же правкой (onChange) и Escape'ом: висящая
   * красная подпись под уже исправленным полем врёт не меньше, чем молчание.
   */
  const commitId = () => {
    const next = idDraft.trim();
    if (next === step.id) {
      // Пробелы по краям — не переименование; выравниваем поле и молчим.
      setIdDraft(step.id);
      setIdError(null);
      return;
    }
    if (!next) {
      setIdError("Идентификатор пустой — на такой шаг не сослаться");
      return;
    }
    if (stepIds.includes(next)) {
      setIdError(`«${next}» уже занят другим шагом`);
      return;
    }
    setIdError(null);
    renameStep(step.id, next);
  };

  return (
    <Accordion.Item
      value={step.id}
      className="step-card"
      data-issue={level ?? undefined}
      data-focus={focused || undefined}
      data-step-id={step.id}
    >
      <div className="step-card__head">
        <span className="step-card__icon" aria-hidden="true" title={meta.label}>
          {meta.icon}
        </span>
        <TextInput
          aria-label={`Идентификатор шага ${step.id}`}
          value={idDraft}
          error={idError}
          onChange={(e) => {
            setIdDraft(e.currentTarget.value);
            setIdError(null);
          }}
          onBlur={commitId}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            if (e.key === "Escape") {
              setIdDraft(step.id);
              setIdError(null);
            }
          }}
          size="xs"
          w={150}
          styles={{
            input: {
              fontFamily: "var(--mantine-font-family-monospace, monospace)",
            },
          }}
        />
        {isEntry && (
          <Badge size="xs" variant="light" color="lp">
            старт
          </Badge>
        )}
        <Accordion.Control className="step-card__control">
          <Text fz="sm" c="var(--lc-text-2)" truncate>
            {stepSummary(step)}
          </Text>
        </Accordion.Control>
        <div className="step-card__actions">
          {level === "error" && (
            <span aria-label="Ошибка в шаге">
              <IconXCircle size={14} />
            </span>
          )}
          {level === "warning" && (
            <span aria-label="Предупреждение в шаге">
              <IconAlert size={14} />
            </span>
          )}
          <Tooltip label="Выше">
            <ActionIcon
              variant="subtle"
              size="sm"
              aria-label={`Переместить выше: ${step.id}`}
              disabled={index === 0}
              onClick={() => moveStep(step.id, -1)}
            >
              <IconArrowUp size={14} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Ниже">
            <ActionIcon
              variant="subtle"
              size="sm"
              aria-label={`Переместить ниже: ${step.id}`}
              disabled={index === total - 1}
              onClick={() => moveStep(step.id, 1)}
            >
              <IconArrowDown size={14} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Удалить шаг">
            <ActionIcon
              variant="subtle"
              color="red"
              size="sm"
              aria-label={`Удалить шаг ${step.id}`}
              onClick={() => setConfirm({ kind: "delete" })}
            >
              <IconX size={14} />
            </ActionIcon>
          </Tooltip>
        </div>
      </div>

      <Accordion.Panel>
        {!open ? null : (
          <>
            <Group justify="space-between" align="flex-end" mb="var(--lc-space-3)">
              <Select
                label="Тип шага"
                data={STEP_META.map((m) => ({
                  value: m.type,
                  label: `${m.icon} ${m.label}`,
                }))}
                value={step.type}
                onChange={(v) => v && v !== step.type && setConfirm({ kind: "type", type: v as BotStepType })}
                allowDeselect={false}
                comboboxProps={{ withinPortal: false }}
                size="sm"
                w={220}
              />
              {!isEntry && (
                <Button variant="subtle" size="compact-sm" onClick={() => setEntry(step.id)}>
                  Сделать стартовым
                </Button>
              )}
            </Group>

            <StepForm step={step} stepIds={stepIds} vars={vars} onChange={(next) => replaceStep(step.id, next)} />

            {issues.length > 0 && (
              <div className="step-card__issues">
                {issues.map((issue, i) => (
                  <Text key={i} fz="xs" c={issue.level === "error" ? "var(--lc-danger-text)" : "var(--lc-text-2)"}>
                    {issue.level === "error" ? <IconXCircle size={13} /> : <IconAlert size={13} />}{" "}
                    {issue.message}
                  </Text>
                ))}
              </div>
            )}
          </>
        )}
      </Accordion.Panel>

      <Modal
        opened={confirm !== null}
        onClose={() => setConfirm(null)}
        title={confirm?.kind === "delete" ? "Удалить шаг?" : "Сменить тип шага?"}
        centered
        withinPortal={false}
      >
        <Text fz="sm" c="var(--lc-text-2)" mb="var(--lc-space-4)">
          {confirm?.kind === "delete"
            ? `Шаг ${step.id} будет удалён. Переходы других шагов на него придётся поправить вручную.`
            : "Параметры шага сбросятся на значения по умолчанию нового типа."}
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={() => setConfirm(null)}>
            Отмена
          </Button>
          <Button
            color={confirm?.kind === "delete" ? "red" : undefined}
            onClick={() => {
              if (confirm?.kind === "delete") removeStep(step.id);
              else if (confirm?.kind === "type") changeStepType(step.id, confirm.type);
              setConfirm(null);
            }}
          >
            {confirm?.kind === "delete" ? "Удалить" : "Сменить тип"}
          </Button>
        </Group>
      </Modal>
    </Accordion.Item>
  );
}
