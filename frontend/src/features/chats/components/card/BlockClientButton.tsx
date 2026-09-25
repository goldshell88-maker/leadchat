import { useState } from "react";
import { Button, Modal, Text, Textarea } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, http } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto } from "@/shared/api/types";
import { патчСтрок } from "@/shared/realtime/listCache";
import { toast } from "@/shared/ui/toast";
import "./client-card.css";

/**
 * Карточка клиента в ответе `POST /clients/{id}/block|unblock` (`clients._view`
 * на сервере) — ровно те поля, которые нужны строке списка и детали.
 */
interface КлиентПослеПометки {
  id: string;
  blocked: boolean;
  blocked_reason: string | null;
}

/**
 * Пометка «нежелательный клиент» (чёрный список, docs/19).
 *
 * СЛОВО «ЗАБЛОКИРОВАТЬ» ЗДЕСЬ НЕ ИСПОЛЬЗУЕТСЯ. Оно читается как «он больше не
 * сможет писать», и оператор с таким пониманием перестанет следить за
 * диалогом вовсе — а сообщения продолжат приходить. Окно обязано сказать
 * правду ДО нажатия, а не после.
 *
 * СНЯТИЕ — без подтверждения. Вернуть клиента в обычную работу безопасно:
 * худшее, что случится, — диалог снова встанет в очередь. Спрашивать
 * подтверждение у безопасного действия значит приучать нажимать «да» не
 * читая, и тогда оно не сработает там, где нужно.
 *
 * ОКНО УПРАВЛЯЕМОЕ, КНОПКИ ЗДЕСЬ НЕТ. Кнопка переехала в шапку ленты вместе с
 * остальными четырьмя действиями (запись Jivo от 7 августа). Оставь мы её и
 * тут — получилось бы два способа сделать одно и то же в трёхстах пикселях
 * друг от друга; проект уже наступал на это со вторым выбором статуса
 * (UX-аудит, docs/17 §С1).
 */
export function BlockClientDialog({
  clientId,
  convId,
  blocked,
  opened,
  onClose,
}: {
  clientId: string;
  convId: string;
  blocked: boolean;
  opened: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [why, setWhy] = useState("");
  /*
   * ⚠ ЗАКРЫТИЕ ОКНА ЧИСТИТ ПРИЧИНУ (находка 23.08 №15). Сброс стоял только на
   * успешном пути: передумал человек и закрыл окно — введённая причина
   * оставалась в поле, и при следующем открытии (уже про ДРУГОГО клиента) она
   * подставлялась как своя. Прочитать это как ошибку нельзя: поле выглядит
   * заполненным по делу.
   */
  const закрыть = () => {
    setWhy("");
    onClose();
  };
  const [busy, setBusy] = useState(false);

  /** Пометка из ответа — в деталь открытого диалога и во все его строки. */
  const применитьПометку = (ответ: КлиентПослеПометки) => {
    const пометка = { blocked: ответ.blocked, blocked_reason: ответ.blocked_reason ?? null };
    qc.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (old) =>
      old ? { ...old, client: { ...old.client, ...пометка } } : old,
    );
    патчСтрок(
      (r) => r.client.id === clientId,
      (r) => ({ ...r, client: { ...r.client, ...пометка } }),
    );
  };

  const act = async (kind: "block" | "unblock", body?: unknown) => {
    setBusy(true);
    try {
      const ответ = await http.post<КлиентПослеПометки>(
        `/clients/${encodeURIComponent(clientId)}/${kind}`,
        body,
      );
      /*
       * ⚠ ОДИН КРУГ, А НЕ ТРИ (замер 06.09). Здесь стояло: POST → `await`
       * инвалидации детали → `await` инвалидации корня `conversations` — три
       * последовательных круга ≈105 мс + 3×RTT со спиннером, а корень накрывал
       * все выдачи и деталь разом: 3–4 лишних запроса на одно нажатие.
       *
       * Ручка отвечает карточкой клиента (`clients._view`): пометку кладём в
       * деталь открытого диалога и в его строки сами. Остальные диалоги этого
       * клиента чинит сервер — он шлёт по ним `conversation:updated {client}`.
       */
      if (ответ?.id === clientId) {
        применитьПометку(ответ);
      } else {
        // Форма ответа не та, что обещана, — честнее спросить, чем угадывать.
        void qc.invalidateQueries({ queryKey: qk.conversations.detail(convId) });
      }
      onClose();
      setWhy("");
      toast.success(kind === "block" ? "Клиент помечен" : "Пометка снята");
    } catch (e) {
      toast.error("Не получилось", e instanceof ApiError ? e.message : "Попробуйте ещё раз");
    } finally {
      setBusy(false);
    }
  };

  // Снятие безопасно, поэтому окно не спрашивает «точно?», а сразу объясняет,
  // что произойдёт, и предлагает сделать это одной кнопкой.
  if (blocked) {
    return (
      <Modal opened={opened} onClose={закрыть} title="Снять пометку" centered>
        <Text fz="sm" c="var(--lc-text-2)" mb="var(--lc-space-3)">
          Клиент снова станет обычным: его обращения будут вставать в очередь и
          звенеть у команды.
        </Text>
        <div className="card-blocked__actions">
          <Button variant="default" size="xs" onClick={закрыть}>
            Отмена
          </Button>
          <Button size="xs" loading={busy} onClick={() => void act("unblock")}>
            Снять пометку
          </Button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal opened={opened} onClose={закрыть} title="Пометить клиента" centered>
      <Text fz="sm" c="var(--lc-text-2)" mb="var(--lc-space-3)">
        Его сообщения по-прежнему будут приходить и сохраняться — вы их не
        потеряете. Перестанет только одно: диалог не встанет в очередь и не
        будет звенеть у команды.
      </Text>
      <Textarea
        size="sm"
        autosize
        minRows={2}
        maxLength={300}
        label="Почему"
        description="Через полгода никто не вспомнит, кого и за что пометили"
        placeholder="Пишет каждый день, ничего не заказывает"
        value={why}
        onChange={(e) => setWhy(e.currentTarget.value)}
      />
      <div className="card-blocked__actions">
        <Button variant="default" size="xs" onClick={закрыть}>
          Отмена
        </Button>
        {/*
          КРАСНАЯ. Кнопка была залита акцентным зелёным — тем же, что «Принять
          диалог» и «Сохранить», то есть цветом согласия. Действие при этом
          обратное: клиент помечается нежелательным. Вход в это же окно из
          меню ленты помечен красным (`ThreadActions`), и на полпути цвет
          менялся на противоположный.
        */}
        <Button
          size="xs"
          color="red"
          loading={busy}
          onClick={() => void act("block", { reason: why })}
        >
          Пометить
        </Button>
      </div>
    </Modal>
  );
}
