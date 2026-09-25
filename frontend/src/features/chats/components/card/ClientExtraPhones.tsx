import { useState } from "react";
import { Button, Text } from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { ApiError } from "@/shared/api/http";
import { formatClock, formatDividerLabel } from "@/shared/lib/formatTime";
import { showToast, toast } from "@/shared/ui/toast";
import type { PhoneEntry } from "./clientApi";
import { useMakePhonePrimary, useResolvePhoneCandidate } from "./clientApi";
import { formatPhone } from "./phone";
import "./client-card.css";

/**
 * ДОПОЛНИТЕЛЬНЫЕ НОМЕРА ЧЕЛОВЕКА — СО ДЕЙСТВИЕМ, А НЕ СТРОКОЙ ТЕКСТА.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «когда клиент даёт 2 номер, то исправить
 * можно только основной номер, второй изменить нельзя. Так же нельзя поменять
 * их местами».
 *
 * ЧТО БЫЛО. Здесь печаталась одна строка: «Ещё номера этого человека: +7 …,
 * +7 …». Ни скопировать по одному, ни сделать основным, ни поменять местами.
 *
 * ⚠ С 12.09 НОМЕРА СЮДА КЛАДЁТ АВТОМАТИКА — БЕЗ ВОПРОСА (правило владельца:
 * «всё, что клиент написал в своей переписке, — его номера»). Поэтому у каждой
 * строки стоит ДОКАЗАТЕЛЬСТВО: откуда номер, когда, кто решил, слово рядом
 * («жена», «мастер»), — и второе действие «Не его номер». Заполненный основной
 * автоматика не меняет никогда; похожий на него номер помечается «возможно,
 * исправление», а решает человек одним нажатием «Сделать основным».
 *
 * ⚠ ПРАВИТЬ НОМЕР НА МЕСТЕ НЕЛЬЗЯ ПО СУЩЕСТВУ. Строка дополнительного номера —
 * это доказательство: «этот номер назван в такой-то переписке». Переписав в
 * ней цифры, мы получили бы доказательство, ссылающееся на сообщение, которого
 * не было.
 *
 * ⚠ У НОМЕРА ПРИСОЕДИНЁННОЙ КАРТОЧКИ ДЕЙСТВИЙ НЕТ. Сделать его основным
 * значило бы перенести номер через границу карточек, а «Разъединить» его назад
 * не забирает — у победителя остался бы телефон чужого человека.
 */
export function ClientExtraPhones({
  clientId,
  convId,
  phones,
  rejected = [],
  editable,
}: {
  clientId: string | undefined;
  convId: string | null;
  /** Номера человека КРОМЕ основного, с доказательством каждого. */
  phones: readonly PhoneEntry[];
  /** Снятые «Не его номер» — возвращаются одним нажатием. */
  rejected?: readonly { value: string; candidate_id: string }[];
  editable: boolean;
}) {
  const navigate = useNavigate();
  const [ждём, setЖдём] = useState<string | null>(null);
  const сделать = useMakePhonePrimary(clientId, convId);
  const снять = useResolvePhoneCandidate(clientId, convId);

  if (phones.length === 0 && rejected.length === 0) return null;

  const можно = editable && clientId !== undefined && convId !== null;

  function основным(phone: string): void {
    setЖдём(phone);
    сделать.mutate(phone, {
      onSuccess: (r) => {
        showToast({
          message: r.changed
            ? `Основной номер — ${formatPhone(phone)}. Прежний остался в списке`
            : "Этот номер уже основной",
          color: "lp",
        });
      },
      onError: (e) => {
        showToast({
          message: e instanceof ApiError ? e.message : "Не получилось сменить основной номер",
          color: "red",
        });
      },
      onSettled: () => setЖдём(null),
    });
  }

  function вернуть(candidateId: string, value: string): void {
    setЖдём(value);
    снять.mutate(
      { candidateId, decision: "add" },
      {
        onSuccess: () => toast.success("Номер возвращён в карточку"),
        onError: (e) =>
          toast.error("Не получилось", e instanceof ApiError ? e.message : "Попробуйте ещё раз"),
        onSettled: () => setЖдём(null),
      },
    );
  }

  function неЕго(entry: PhoneEntry): void {
    if (!entry.candidate_id) return;
    setЖдём(entry.value);
    снять.mutate(
      { candidateId: entry.candidate_id, decision: "reject" },
      {
        onSuccess: () =>
          toast.success("Номер снят с карточки", "Больше не добавим его по этой переписке"),
        onError: (e) => {
          if (e instanceof ApiError && e.status === 409) {
            toast.info("Уже решено", "Кто-то из коллег снял этот номер раньше");
            return;
          }
          toast.error("Не получилось", e instanceof ApiError ? e.message : "Попробуйте ещё раз");
        },
        onSettled: () => setЖдём(null),
      },
    );
  }

  // Номера ОТКРЫТОГО диалога — первыми: по ним звонят прямо сейчас.
  const упорядочено = [...phones].sort(
    (a, b) => Number(b.conversation_id === convId) - Number(a.conversation_id === convId),
  );

  return (
    <div className="card-extra-phones">
      {phones.length > 0 && (
        <Text component="p" fz="xs" c="var(--lc-text-3)" className="card-extra-phones__title">
          {phones.length === 1 ? "Ещё номер этого человека" : "Ещё номера этого человека"}
        </Text>
      )}
      <ul className="card-extra-phones__list">
        {упорядочено.map((entry) => {
          const свой = clientId !== undefined && entry.client_id === clientId;
          const занято = ждём !== null;
          return (
            <li key={entry.value} className="card-extra-phones__item">
              <span className="card-extra-phones__value lc-num">{formatPhone(entry.value)}</span>
              <Text
                component="span"
                fz="xs"
                c="var(--lc-text-3)"
                className="card-extra-phones__note"
              >
                {подпись(entry, convId)}
              </Text>
              {entry.near_primary && (
                <Text
                  component="span"
                  fz="xs"
                  c="var(--lc-warning-text)"
                  className="card-extra-phones__near"
                  data-testid="near-primary"
                >
                  отличается от основного одной цифрой — возможно, исправление
                </Text>
              )}
              {entry.conversation_id && entry.conversation_id !== convId && (
                <Button
                  variant="subtle"
                  size="compact-xs"
                  onClick={() => navigate(`/chats/${entry.conversation_id}`)}
                >
                  Открыть тот диалог
                </Button>
              )}
              {можно && свой && (
                <Button
                  size="compact-xs"
                  variant="subtle"
                  loading={ждём === entry.value && сделать.isPending}
                  disabled={занято && ждём !== entry.value}
                  aria-label={`Сделать ${formatPhone(entry.value)} основным номером`}
                  onClick={() => основным(entry.value)}
                >
                  Сделать основным
                </Button>
              )}
              {можно && свой && entry.candidate_id && entry.source !== "swap" && (
                <Button
                  size="compact-xs"
                  variant="subtle"
                  color="gray"
                  loading={ждём === entry.value && снять.isPending}
                  disabled={занято && ждём !== entry.value}
                  aria-label={`Снять ${formatPhone(entry.value)}: не его номер`}
                  onClick={() => неЕго(entry)}
                >
                  Не его номер
                </Button>
              )}
            </li>
          );
        })}
        {rejected.map((r) => (
          <li key={`rejected-${r.value}`} className="card-extra-phones__item">
            <span className="card-extra-phones__value lc-num card-extra-phones__value--rejected">
              {formatPhone(r.value)}
            </span>
            <Text component="span" fz="xs" c="var(--lc-text-3)" className="card-extra-phones__note">
              снят: не его номер
            </Text>
            {можно && (
              <Button
                size="compact-xs"
                variant="subtle"
                loading={ждём === r.value && снять.isPending}
                disabled={ждём !== null && ждём !== r.value}
                aria-label={`Вернуть ${formatPhone(r.value)} в карточку`}
                onClick={() => вернуть(r.candidate_id, r.value)}
              >
                Вернуть
              </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** «из переписки, сам · сегодня в 14:02 · со словом «жена»» — одной строкой. */
function подпись(entry: PhoneEntry, convId: string | null): string {
  const части: string[] = [];
  if (entry.source === "swap") части.push("был основным");
  else if (entry.source === "manual") части.push("вписан руками");
  else if (entry.source === "dialog")
    части.push(entry.decided_by === "operator" ? "из переписки, подтверждён" : "из переписки, сам");
  else if (entry.source === "voice")
    части.push(entry.decided_by === "operator" ? "из голосового, подтверждён" : "из голосового, сам");
  else части.push("присоединённая карточка");
  if (entry.conversation_id && entry.conversation_id === convId) части.push("в этом диалоге");
  if (entry.message_at) части.push(whenText(entry.message_at));
  if (entry.hint) части.push(`со словом «${entry.hint}»`);
  return части.join(" · ");
}

function whenText(iso: string): string {
  return `${formatDividerLabel(iso).toLowerCase()} в ${formatClock(iso)}`;
}
