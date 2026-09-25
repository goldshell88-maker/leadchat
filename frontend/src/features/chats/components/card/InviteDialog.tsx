import { useMemo, useState } from "react";
import { Button, Group, Modal, Text, Textarea } from "@mantine/core";
import { useAssignableUsers, useInviteParticipant } from "../../hooks/useConversationActions";
import { PeoplePicker } from "./PeoplePicker";
import "./client-card.css";

/**
 * «Позвать» (docs/19) — второй человек в диалоге, без смены ответственного.
 *
 * ЧЕМ ОТЛИЧАЕТСЯ ОТ «ПЕРЕДАТЬ», И ПОЧЕМУ ЭТО НАПИСАНО В ОКНЕ. Обе кнопки
 * стоят рядом и обе открывают список сотрудников — спутать их легко, а цена
 * ошибки разная: передал по ошибке — диалог ушёл вместе с ответственностью.
 * Поэтому подпись под заголовком говорит прямо, что диалог остаётся за вами.
 *
 * ПОЧЕМУ ПРИЧИНА ЗАМЕТНЕЕ, ЧЕМ КОММЕНТАРИЙ ПРИ ПЕРЕДАЧЕ. При передаче диалог
 * уходит целиком, и коллега разберётся, прочитав переписку. Позванный
 * открывает чужой диалог, чтобы ответить на один вопрос, и без причины ему
 * придётся выяснять, что от него хотели.
 */
export function InviteDialog({
  convId,
  opened,
  currentAssigneeId,
  alreadyIn,
  onClose,
}: {
  convId: string;
  opened: boolean;
  currentAssigneeId?: string | null;
  alreadyIn: readonly string[];
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const users = useAssignableUsers(opened);
  const invite = useInviteParticipant(convId);

  // Ответственный и уже позванные — не варианты: первого звать некуда, второй
  // уже здесь. Гасим их в списке, а не прячем: пропавшее из списка имя
  // читается как «сотрудника уволили».
  const disabled = useMemo(
    () => new Set([...alreadyIn, ...(currentAssigneeId ? [currentAssigneeId] : [])]),
    [alreadyIn, currentAssigneeId],
  );

  const close = () => {
    setQuery("");
    setSelected(null);
    setReason("");
    onClose();
  };

  const submit = () => {
    if (!selected) return;
    invite.mutate({ userId: selected, reason: reason.trim() || undefined }, { onSuccess: close });
  };

  return (
    <Modal opened={opened} onClose={close} title="Позвать в диалог" centered size="md" trapFocus>
      <Text fz="sm" c="var(--lc-text-2)" mb="var(--lc-space-3)">
        Диалог останется за вами. Коллега получит уведомление и увидит диалог
        в своих «Моих».
      </Text>

      <PeoplePicker
        users={users}
        query={query}
        onQueryChange={setQuery}
        selected={selected}
        onSelect={setSelected}
        disabledIds={disabled}
        label="Кого позвать"
      />

      <Textarea
        label="Зачем зовёте"
        placeholder="скажи, чинится ли эта модель"
        autosize
        minRows={2}
        maxRows={4}
        value={reason}
        onChange={(e) => setReason(e.currentTarget.value)}
        mt="var(--lc-space-3)"
      />
      <Text fz="xs" c="var(--lc-text-3)" mt={4}>
        ⓘ Придёт в уведомлении — коллега сразу поймёт, что от него нужно
      </Text>

      {/* Класс — ради прилипания подвала к низу окна: разбор и числа замера
          в client-card.css у `.card-dialog__foot`. */}
      <Group justify="flex-end" mt="var(--lc-space-4)" className="card-dialog__foot">
        <Button variant="subtle" onClick={close}>
          Отмена
        </Button>
        <Button disabled={!selected} loading={invite.isPending} onClick={submit}>
          Позвать
        </Button>
      </Group>
    </Modal>
  );
}
