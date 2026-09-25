import { useState } from "react";
import { Button, Group, Modal, Stack, Text } from "@mantine/core";
import { copyText } from "./clipboard";
import "./team.css";
import { IconAlert, IconCopy } from "@/shared/ui/Icon";

/**
 * Экран результата приглашения (11 §4.2): ссылка установки пароля показывается
 * ОДИН раз — почтового сервиса в MVP нет, админ передаёт её сотруднику сам.
 * Тем же окном админ отвечает на заявку «не помню пароль» из центра уведомлений.
 */

export function OneTimeLinkModal({
  opened,
  title,
  url,
  hint,
  onClose,
}: {
  opened: boolean;
  title: string;
  url: string;
  hint?: string;
  onClose(): void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const ok = await copyText(url);
    setCopied(ok);
    if (ok) window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Modal opened={opened} onClose={onClose} title={title} centered>
      <Stack gap="var(--lc-space-3)">
        <Text fz="sm" c="var(--lc-text-2)">
          Передайте сотруднику ссылку установки пароля:
        </Text>
        <code className="team-link" data-testid="invite-link">
          {url}
        </code>
        <Text fz="xs" c="var(--lc-warning-text)">
          <IconAlert size={14} /> {hint ?? "Ссылка показывается один раз и действует 72 часа. Потеряли — перевыпустите в меню сотрудника."}
        </Text>
        <Group justify="space-between">
          <Button
            variant="outline"
            /* Значок только у «Скопировать»: у «Скопировано» он был бы вторым
               подтверждением того же, а подпись уже сменилась. */
            leftSection={copied ? undefined : <IconCopy size={16} />}
            onClick={() => void handleCopy()}
          >
            {copied ? "Скопировано" : "Скопировать"}
          </Button>
          <Button onClick={onClose}>Готово</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
