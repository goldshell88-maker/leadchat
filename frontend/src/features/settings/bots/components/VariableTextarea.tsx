import { useRef } from "react";
import { ActionIcon, Group, Menu, Text, Textarea, Tooltip } from "@mantine/core";

/**
 * Текстовое поле шага с тулбаром-подстановкой переменных (02 §5.1, 11 §5.2):
 * кнопка `{…}` → список системных плейсхолдеров и переменных `ask`/`menu`,
 * объявленных выше по списку. Вставка идёт в позицию курсора.
 *
 * Словарь — латинский (02 §1.2) и действует только в сценариях ботов;
 * у быстрых ответов менеджеров свой, русский — их не смешивать.
 */
export function VariableTextarea({
  label,
  description,
  value,
  vars,
  onChange,
  placeholder,
  minRows = 2,
  maxLength = 1000,
  error,
}: {
  label: string;
  description?: string;
  value: string | null | undefined;
  vars: string[];
  onChange: (next: string) => void;
  placeholder?: string;
  minRows?: number;
  maxLength?: number;
  error?: string | null;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const text = value ?? "";

  const insert = (name: string) => {
    const token = `{${name}}`;
    const el = ref.current;
    const start = el?.selectionStart ?? text.length;
    const end = el?.selectionEnd ?? text.length;
    const next = `${text.slice(0, start)}${token}${text.slice(end)}`;
    onChange(next);
    // Каретка встаёт после вставленного плейсхолдера — можно печатать дальше.
    requestAnimationFrame(() => {
      if (!el) return;
      el.focus();
      const caret = start + token.length;
      el.setSelectionRange(caret, caret);
    });
  };

  return (
    <div className="bot-field">
      <Group justify="space-between" align="center" gap={4} mb={2}>
        <Text component="label" fz="sm" fw={500} c="var(--lc-text-1)">
          {label}
        </Text>
        <Menu shadow="md" width={220} withinPortal={false}>
          <Menu.Target>
            <Tooltip label="Вставить переменную">
              <ActionIcon variant="subtle" size="sm" aria-label={`Вставить переменную: ${label}`}>
                {"{…}"}
              </ActionIcon>
            </Tooltip>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Label>Переменные</Menu.Label>
            {vars.map((name) => (
              <Menu.Item key={name} onClick={() => insert(name)}>
                {`{${name}}`}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>
      </Group>
      <Textarea
        ref={ref}
        aria-label={label}
        value={text}
        onChange={(e) => onChange(e.currentTarget.value)}
        placeholder={placeholder}
        autosize
        minRows={minRows}
        maxRows={10}
        error={error ?? undefined}
        size="sm"
      />
      <Group justify="space-between" gap={4} mt={2}>
        {description ? (
          <Text fz="xs" c="var(--lc-text-3)">
            {description}
          </Text>
        ) : (
          <span />
        )}
        <Text fz="xs" c={text.length > maxLength ? "var(--lc-danger-text)" : "var(--lc-text-3)"}>
          {text.length} / {maxLength}
        </Text>
      </Group>
    </div>
  );
}
