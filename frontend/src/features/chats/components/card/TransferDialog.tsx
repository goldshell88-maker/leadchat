import { useMemo, useState } from "react";
import { Button, Group, Modal, Text, Textarea } from "@mantine/core";
import { useAssignConversation, useAssignableUsers } from "../../hooks/useConversationActions";
import { PeoplePicker } from "./PeoplePicker";
import "./client-card.css";

/**
 * «→ Передать» (11 §2.4). Список — GET /users/assignable (01 §3.1): только
 * активные admin/manager с онлайн-точками. Комментарий уходит системным
 * сообщением в ленту (01 §5.5). Модалка Mantine: focus-trap и Esc — из коробки,
 * автофокус — в поле поиска.
 *
 * Передача ОТДАЁТ диалог: ответственный меняется, спрос переходит. Когда нужен
 * второй человек, а не замена, — рядом стоит «Позвать» (`InviteDialog`).
 */
export function TransferDialog({
  convId,
  opened,
  currentAssigneeId,
  onClose,
}: {
  convId: string;
  opened: boolean;
  currentAssigneeId?: string | null;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [comment, setComment] = useState("");

  const users = useAssignableUsers(opened);
  const assign = useAssignConversation(convId);

  // Передать тому, кто и так ведёт диалог, нечего.
  const disabled = useMemo(
    () => new Set(currentAssigneeId ? [currentAssigneeId] : []),
    [currentAssigneeId],
  );

  const close = () => {
    setQuery("");
    setSelected(null);
    setComment("");
    onClose();
  };

  const submit = () => {
    if (!selected) return;
    assign.mutate(
      { assigneeId: selected, comment: comment.trim() || undefined },
      { onSuccess: close },
    );
  };

  return (
    <Modal opened={opened} onClose={close} title="Передать диалог" centered size="md" trapFocus>
      <PeoplePicker
        users={users}
        query={query}
        onQueryChange={setQuery}
        selected={selected}
        onSelect={setSelected}
        disabledIds={disabled}
        /*
         * ⚠ ТОЛЬКО ТЕ, КТО В СЕТИ (решение владельца 28.08). Переданный диалог
         * не появляется ни в очереди, ни в «Моих» получателя, пока тот не
         * примет передачу: отданный ушедшему домой, он не виден никому и ждёт
         * молча, а отдающий уверен, что дело сделано. Сервер такую передачу
         * тоже отклоняет (`resolve_assignee`, reason `assignee_offline`) —
         * список здесь избавляет от лишнего нажатия, а не заменяет запрет.
         */
        onlineOnly
        emptyText="Сейчас никого нет в сети — передать некому"
      />

      <Textarea
        label="Комментарий коллеге (необязательно)"
        placeholder="торгуется, дай скидку до 10%"
        autosize
        minRows={2}
        maxRows={4}
        value={comment}
        onChange={(e) => setComment(e.currentTarget.value)}
        mt="var(--lc-space-3)"
      />
      <Text fz="xs" c="var(--lc-text-3)" mt={4}>
        ⓘ Комментарий увидит только команда
      </Text>

      {/* Класс — ради прилипания подвала к низу окна: разбор и числа замера
          в client-card.css у `.card-dialog__foot`. */}
      <Group justify="flex-end" mt="var(--lc-space-4)" className="card-dialog__foot">
        <Button variant="subtle" onClick={close}>
          Отмена
        </Button>
        <Button disabled={!selected} loading={assign.isPending} onClick={submit}>
          Передать
        </Button>
      </Group>
    </Modal>
  );
}
