import { Button, Text } from "@mantine/core";
import { useClaimConversation, useDeclineConversation, useGoToNextInQueue } from "./useInbox";
import "./inbox.css";
import { IconCheckCircle } from "@/shared/ui/Icon";
import { useEffect, useRef } from "react";
import { useActionBus } from "@/features/hotkeys/actionBus";

/**
 * Низ ленты для диалога из очереди (7.1 п.2, образец — Jivo, 15 §2.1).
 * Лента над этой панелью видна целиком и намеренно: решение «беру / не беру»
 * принимается по тексту обращения, а не по имени клиента в списке. Поля ввода
 * здесь нет — пока диалог не принят, писать в него нельзя.
 */
export function InboxDecisionBar({ convId }: { convId: string }) {
  const claim = useClaimConversation(convId);
  const decline = useDeclineConversation(convId);

  /*
   * Клавиши Ctrl+R и Ctrl+Backspace (UX-аудит, docs/17 §Т7).
   *
   * ⚠ ЗДЕСЬ БЫЛО «Ctrl+Enter и Ctrl+Backspace» — неправдой это стало ещё до
   * 30.08 (дефект SCEN-23: основным сочетанием приёма давно был Ctrl+R), а
   * 30.08 Ctrl+Enter ушёл в отправку сообщения совсем. Точный список живёт в
   * `features/hotkeys/catalog.ts`, справка строится из него же — переписывать
   * его в комментариях значит заводить второй источник правды.
   *
   * Подписка на счётчик, а не прямой вызов: обработчик клавиш — слушатель на
   * окне, хуков в нём нет. Панель решения существует ровно тогда, когда
   * принимать и отклонять вообще есть что, поэтому она и слушает.
   *
   * `mutate` не вызываем повторно, пока предыдущий запрос в полёте: клавишу
   * легко нажать дважды, а «Принять» дважды — это гонка с самим собой.
   */
  const claimNonce = useActionBus((s) => s.claimNonce);
  const declineNonce = useActionBus((s) => s.declineNonce);
  const seen = useRef({ claim: claimNonce, decline: declineNonce });
  useEffect(() => {
    if (claimNonce !== seen.current.claim) {
      seen.current.claim = claimNonce;
      if (!claim.isPending) claim.mutate();
    }
    if (declineNonce !== seen.current.decline) {
      seen.current.decline = declineNonce;
      if (!decline.isPending) decline.mutate(undefined);
    }
  }, [claimNonce, declineNonce, claim, decline]);
  const busy = claim.isPending || decline.isPending;

  return (
    <footer className="inbox-decision" aria-label="Решение по диалогу из очереди">
      <Text fz="sm" c="var(--lc-text-2)" className="inbox-decision__hint">
        Диалог ждёт в очереди — примите его, чтобы ответить
      </Text>
      <div className="inbox-decision__actions">
        <Button
          color="lp"
          size="sm"
          loading={claim.isPending}
          disabled={busy}
          onClick={() => claim.mutate()}
        >
          Принять диалог
        </Button>
        <Button
          variant="default"
          size="sm"
          loading={decline.isPending}
          disabled={busy}
          onClick={() => decline.mutate()}
        >
          Отклонить
        </Button>
      </div>
    </footer>
  );
}

/**
 * Диалог забрали, пока оператор его читал (7.1 п.4). Кнопок больше нет — не
 * потому что «нельзя нажать», а потому что нажимать больше не во что: решение
 * приняли за него. Текст ровно тот же, что и в тосте опоздавшему, чтобы два
 * пути одного события не выглядели двумя разными неприятностями.
 */
export function InboxClaimedBanner({ convId, by }: { convId: string; by: string }) {
  const goNext = useGoToNextInQueue(convId);

  return (
    <footer className="inbox-decision inbox-decision--claimed" role="status">
      <Text fz="sm" c="var(--lc-text-1)" className="inbox-decision__hint">
        <IconCheckCircle size={15} /> Диалог принял {by}
      </Text>
      <div className="inbox-decision__actions">
        <Button variant="default" size="sm" onClick={goNext}>
          Следующий в очереди
        </Button>
      </div>
    </footer>
  );
}
