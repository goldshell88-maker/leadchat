import { useMemo, useState } from "react";
import { Button, Loader, Text } from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { ApiError } from "@/shared/api/http";
import { formatClock, formatDividerLabel } from "@/shared/lib/formatTime";
import { IconPhone } from "@/shared/ui/Icon";
import { toast } from "@/shared/ui/toast";
import { useThreadMessages } from "../../hooks/useConversationActions";
import type { ClientCardRef, PhoneCandidate, PhoneCandidateDecision } from "./clientApi";
import { cardTitle, useResolvePhoneCandidate } from "./clientApi";
import { formatPhone } from "./phone";
import "./client-card.css";

/**
 * РАСПОЗНАННЫЙ В ПЕРЕПИСКЕ НОМЕР — ВОПРОСОМ ОПЕРАТОРУ, А НЕ ЗАПИСЬЮ В КАРТОЧКУ.
 *
 * ТРЕБОВАНИЕ ВЛАДЕЛЬЦА, ПУНКТ 4 (12 августа): «телефон уже есть и отличается →
 * не перезаписываем, показываем вторым кандидатом с действиями „заменить“,
 * „добавить“, „отклонить“».
 *
 * ПОЧЕМУ ВОПРОС, А НЕ АВТОМАТ. Номер из карточки набирают и диктуют мастеру
 * вслух. У человека законно бывает два номера («звоните жене»), а рядом с
 * настоящим номером в переписке лежат строки, номером не являющиеся: код
 * домофона, номер заказа, модель техники. Молчаливая перезапись стоит звонка не
 * тому человеку, и заметить её нечем — карточка после подмены выглядит
 * совершенно обычно.
 *
 * ГЛАВНОЕ ЗДЕСЬ НЕ ТРИ КНОПКИ, А ФРАЗА ЦЕЛИКОМ. «Распознан телефон
 * +7 900 111-22-40» без текста сообщения — это предложение, которое нечем
 * проверить: такие либо принимают не глядя, либо перестают замечать через
 * неделю. Поэтому сообщение показывается ПРЯМО ЗДЕСЬ, рядом с кнопками, а не
 * «где-то выше в ленте»: решение принимают, дочитав фразу, и уводить за ней на
 * другой экран значило бы разлучить вопрос с ответом.
 *
 * ПУСТОЙ СПИСОК НЕ РИСУЕТ БЛОКА ВОВСЕ. Сервер кладёт сюда только ожидающих
 * решения; «распознанных номеров нет» — это сообщение об отсутствии данных
 * ростом с личность клиента у каждого второго диалога.
 *
 * ДВОЙНИКИ ПОСЛЕ РЕШЕНИЯ НЕ СКЛЕИВАЮТСЯ САМИ — второе прямое требование
 * владельца: «по распознанному номеру не запускаем автообъединение карточек,
 * только предложение в блоке „Возможно, это тот же человек“». Ответ сервера
 * приносит совпавшие карточки (`twins`), и мы НАЗЫВАЕМ их в тосте, а кнопку
 * «Объединить» рисует один-единственный блок ниже (`ClientMergePanel`, тот же
 * список `merge-candidates`, который эта же мутация и сбрасывает). Второе место
 * с той же кнопкой означало бы два ответа на один вопрос в одной колонке.
 */

/** Пороговое имя блока: одна подпись на все предложения, а не на каждое. */
function blockTitle(count: number): string {
  return count === 1 ? "Распознан телефон в переписке" : "Распознаны телефоны в переписке";
}

/** «12 августа в 16:35» / «сегодня в 16:35» — тем же словарём, что и лента. */
function whenText(iso: string): string {
  return `${formatDividerLabel(iso).toLowerCase()} в ${formatClock(iso)}`;
}

/**
 * Само сообщение, из которого добыт номер.
 *
 * ЛЕНТА БЕРЁТСЯ ТЕМ ЖЕ ЗАПРОСОМ, ЧТО И У ПЕРЕПИСКИ (`useThreadMessages`, ключ
 * `["messages", convId]`). Для открытого диалога это НОЛЬ запросов: страница
 * уже в кэше, мы просто находим в ней сообщение по идентификатору. Для чужого
 * диалога того же клиента — ровно тот запрос, который лента сделала бы сама,
 * если бы оператор туда перешёл, и его результат там и останется.
 *
 * СООБЩЕНИЕ МОЖЕТ НЕ НАЙТИСЬ, И ЭТО НОРМАЛЬНО: пересчёт по истории (`rescan`)
 * достаёт номера из переписки любой давности, а в кэше лежат последние 50
 * сообщений. Врать «сообщение пустое» в этом случае нельзя — оно есть, просто
 * не у нас под рукой; поэтому здесь честная строка и путь к нему.
 */
function CandidateMessage({
  candidate,
  sameDialog,
  onOpenDialog,
}: {
  candidate: PhoneCandidate;
  sameDialog: boolean;
  onOpenDialog: () => void;
}) {
  const messages = useThreadMessages(candidate.conversation_id);
  const found = useMemo(
    () =>
      candidate.message_id
        ? (messages.data?.pages.flatMap((p) => p.items) ?? []).find(
            (m) => m.id === candidate.message_id,
          )
        : undefined,
    [messages.data, candidate.message_id],
  );

  if (messages.isPending) return <Loader size="xs" color="lp" />;

  /*
   * НАЙДЕННАЯ ФРАЗА ПОКАЗЫВАЕТСЯ РАНЬШЕ ЛЮБЫХ ОТКАЗОВ, И ПОРЯДОК ВЕТОК ЗДЕСЬ
   * НЕ КОСМЕТИКА. У ленты, лежащей в кэше, может провалиться ФОНОВОЕ
   * обновление: `isError` при этом истинно, а сообщение — вот оно, в `data`.
   * Проверь мы отказ первым, оператор потерял бы фразу, которая уже у нас на
   * руках, — ровно в тот момент, когда решает, чей это телефон. (Поймано на
   * стенде вёрстки: при недоступном сервере первое предложение показывало
   * «не получилось» вместо готового текста.)
   */
  // У голосового тело пусто — номер вычитан из расшифровки, и цитировать надо
  // её: без фразы предложение нечем проверить, а сама запись — ещё и
  // переслушать (расшифровка машинная и подписана так у пузыря).
  const цитата = found?.body || found?.voice_transcript;
  if (цитата) {
    return (
      // Текст клиента переносится как написан (`white-space: pre-wrap`) и не
      // обрезается многоточием: обрезанная фраза — это ровно та слепота, от
      // которой блок и заводился. Слишком длинное прокручивается внутри рамки.
      <blockquote className="card-candidate__quote">{цитата}</blockquote>
    );
  }

  /*
   * ОТКАЗ СЕТИ И «СООБЩЕНИЯ НЕТ В ЗАГРУЖЕННОЙ ЧАСТИ» — РАЗНЫЕ НОВОСТИ.
   *
   * Ниже стоит честное «сообщение выше по переписке»; сказать это, не сумев
   * ленту получить, значило бы выдать догадку за знание — оператор пошёл бы
   * искать глазами то, чего мы не проверяли. Здесь мы говорим ровно то, что
   * произошло, и даём повторить.
   */
  if (messages.isError) {
    return (
      <div className="card-candidate__links">
        <Text fz="xs" c="var(--lc-text-3)">
          Не получилось загрузить сообщение.
        </Text>
        <Button variant="subtle" size="compact-xs" onClick={() => void messages.refetch()}>
          Повторить
        </Button>
      </div>
    );
  }

  return (
    <div className="card-candidate__links">
      <Text fz="xs" c="var(--lc-text-3)">
        {sameDialog
          ? "Сообщение выше по переписке — прокрутите ленту, чтобы прочитать целиком."
          : "Сообщение в другом диалоге этого клиента."}
      </Text>
      {!sameDialog && (
        <Button variant="subtle" size="compact-xs" onClick={onOpenDialog}>
          Открыть тот диалог
        </Button>
      )}
    </div>
  );
}

function CandidateRow({
  candidate,
  clientId,
  convId,
}: {
  candidate: PhoneCandidate;
  clientId: string;
  convId: string | null;
}) {
  const navigate = useNavigate();
  const resolve = useResolvePhoneCandidate(clientId, convId);
  /*
   * Фраза из ОТКРЫТОГО диалога показывается сразу, из чужого — по нажатию.
   *
   * Правило одно: показываем даром то, что уже у нас на руках, и ходим по сети
   * только когда человек об этом попросил. Лента открытого диалога лежит в
   * кэше — читать её бесплатно; за чужой пришлось бы качать пятьдесят
   * сообщений на каждое предложение, даже если оператор в него не смотрел.
   */
  const sameDialog = candidate.conversation_id === convId;
  const [revealed, setRevealed] = useState(false);
  const human = formatPhone(candidate.phone);
  const when = candidate.message_at ?? candidate.detected_at;
  const openDialog = () => navigate(`/chats/${candidate.conversation_id}`);

  const decide = (decision: PhoneCandidateDecision) => {
    resolve.mutate(
      { candidateId: candidate.id, decision },
      {
        onSuccess: (result) => {
          if (decision === "reject") {
            // Отклонённый номер не возвращается предложением после каждого
            // следующего сообщения с ним же — иначе подсказки перестают читать.
            toast.success("Номер отклонён", "Больше не предложим этот номер по этой переписке");
            return;
          }
          toast.success(
            decision === "replace" ? "Телефон карточки заменён" : "Номер добавлен вторым",
            twinsLine(result.twins),
          );
        },
        onError: (e) => {
          if (e instanceof ApiError && e.status === 409) {
            toast.info("Решение уже принято", "Кто-то из коллег ответил на это предложение раньше");
            return;
          }
          // 409 — тот же вопрос висел у коллеги, и он ответил первым. Это не
          // поломка, и красный тост про «ошибку» здесь научил бы бояться
          // обычного хода дел; предложение с экрана уберёт рефетч (onSettled).
          toast.error("Не получилось", e instanceof ApiError ? e.message : "Попробуйте ещё раз");
        },
      },
    );
  };

  const pending = resolve.isPending;
  const pendingDecision = resolve.variables?.decision;

  return (
    <div className="card-candidate">
      <div className="card-candidate__phone">
        <IconPhone size={15} />
        <span className="card-phone__value">{human}</span>
      </div>

      <Text fz="xs" c="var(--lc-text-3)">
        {/* КАК НАПИСАНО В СООБЩЕНИИ — рядом с тем, что мы из этого поняли.
            «+7(900)1112240» и «+7 900 111-22-40» — одно и то же ровно до тех
            пор, пока это можно сверить глазами. */}
        В сообщении: «{candidate.raw}»
        {when ? ` · ${whenText(when)}` : ""}
        {candidate.source === "rescan" ? " · найден при пересчёте прежней переписки" : ""}
        {/* Машинная расшифровка — не рука клиента: ослышка в одной цифре даёт
            правдоподобный номер, и оператор обязан знать, что сверять надо со
            звуком, а не с написанным. */}
        {candidate.source === "voice" ? " · из голосового (расшифровка)" : ""}
      </Text>

      {sameDialog || revealed ? (
        <CandidateMessage candidate={candidate} sameDialog={sameDialog} onOpenDialog={openDialog} />
      ) : (
        <div className="card-candidate__links">
          <Button
            variant="subtle"
            size="compact-xs"
            onClick={() => setRevealed(true)}
            aria-label={`Показать сообщение с номером ${human}`}
          >
            Показать сообщение
          </Button>
        </div>
      )}

      <div className="card-candidate__actions">
        <Button
          size="compact-xs"
          variant="outline"
          disabled={pending}
          loading={pending && pendingDecision === "replace"}
          aria-label={`Заменить телефон карточки на ${human}`}
          onClick={() => decide("replace")}
        >
          Заменить
        </Button>
        <Button
          size="compact-xs"
          variant="outline"
          disabled={pending}
          loading={pending && pendingDecision === "add"}
          aria-label={`Добавить ${human} вторым номером`}
          onClick={() => decide("add")}
        >
          Добавить
        </Button>
        <Button
          size="compact-xs"
          variant="subtle"
          disabled={pending}
          loading={pending && pendingDecision === "reject"}
          aria-label={`Отклонить ${human}`}
          onClick={() => decide("reject")}
        >
          Отклонить
        </Button>
      </div>
    </div>
  );
}

/** Двойники — НАЗВАНЫ, но не склеены. Кнопка одна и живёт в блоке объединения. */
function twinsLine(twins: ClientCardRef[]): string | undefined {
  if (twins.length === 0) return undefined;
  return `Тот же номер есть в карточке «${twins.map(cardTitle).join("», «")}» — предложение объединить появится ниже, само ничего не склеится.`;
}

export function ClientPhoneCandidates({
  clientId,
  convId,
  phone,
  candidates,
}: {
  clientId: string;
  convId: string | null;
  /** Телефон, который стоит в карточке СЕЙЧАС: от него зависит смысл «заменить». */
  phone: string | null;
  candidates: PhoneCandidate[];
}) {
  if (candidates.length === 0) return null;
  return (
    <div className="card-candidates">
      <Text fz="sm" fw={500} c="var(--lc-text-1)">
        {blockTitle(candidates.length)}
      </Text>
      <Text fz="xs" c="var(--lc-text-3)">
        {/*
          ЧТО СДЕЛАЕТ КАЖДАЯ КНОПКА — СЛОВАМИ И ОДИН РАЗ НА БЛОК. «Заменить» у
          карточки с телефоном и у карточки без него делает разное, а выглядит
          одинаково: в первом случае прежний номер уезжает из поля и остаётся
          только в журнале. Строка одна на все предложения намеренно —
          повторять её под каждым значило бы трижды написать одно и то же в
          колонке шириной 340 пикселей.
        */}
        {phone
          ? `Сейчас в карточке ${formatPhone(phone)}: «заменить» поставит распознанный вместо него, «добавить» оставит оба.`
          : "Телефона в карточке нет: «заменить» сделает распознанный основным, «добавить» оставит его вторым номером."}
      </Text>
      {candidates.map((c) => (
        <CandidateRow key={c.id} candidate={c} clientId={clientId} convId={convId} />
      ))}
    </div>
  );
}
