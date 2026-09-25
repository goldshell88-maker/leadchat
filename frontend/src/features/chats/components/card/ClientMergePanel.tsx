import { useState } from "react";
import { Button, Text } from "@mantine/core";
import { ApiError } from "@/shared/api/http";
import { toast } from "@/shared/ui/toast";
import type {
  ClientCardRef,
  ClientIdentityDto,
  MergeCandidate,
  MergedFrom,
} from "./clientApi";
import { cardTitle, useJoinGroup, useMergeClients, useUnmergeClients } from "./clientApi";
import { formatPhone } from "./phone";
import { formatDate } from "@/shared/lib/formatTime";
import "./client-card.css";

/**
 * Ручное объединение и разъединение карточек — правка 9 от 12 августа.
 *
 * ЗАЧЕМ ЭТО ВООБЩЕ ЕСТЬ. Автоматика склеивает карточки по идентификатору Авито
 * и подтверждает телефоном. Телефон известен у 3 обращений из 37; про
 * идентификатор Авито нигде не обещает, что он общий для разных наших
 * аккаунтов. То есть в подавляющем большинстве случаев машине опереться не на
 * что, а видит правду только диспетчер, читающий переписку.
 *
 * ПОЧЕМУ ПОДСКАЗКА, А НЕ АВТОМАТ. 11 августа молчаливая склейка по совпавшему
 * идентификатору собрала под одним именем восемь человек из разных городов.
 * Здесь у каждого предложения написано, ЧТО совпало, и стоит кнопка: сильный
 * довод (телефон) от слабого (имя — «Иван» это каждый десятый) отличает
 * человек, а не мы за него.
 *
 * ПОЧЕМУ «РАЗЪЕДИНИТЬ» РЯДОМ, А НЕ В НАСТРОЙКАХ. Ошибочное объединение
 * замечают ровно там, где его сделали, — в открытой карточке, через минуту
 * после нажатия. Спрятать откат на другой экран значило бы оставить
 * диспетчера с чужой перепиской и без выхода.
 *
 * ОДНА СТРОКА НА КАРТОЧКУ (владелец 13.09: «нагромождение»). Три объединения
 * с четырьмя строками объяснений каждое занимали половину карточки и
 * повторяли друг друга слово в слово. Теперь: строка «объединена с «…»» и
 * кнопка; чем подтверждено — короткой пометкой; общее объяснение — один раз
 * под списком; больше двух — свёрнуто в «ещё N».
 */

const REASON_TEXT: Record<MergeCandidate["reason"], string> = {
  phone: "тот же телефон",
  phone_candidate: "тот же номер, распознанный в переписке",
  external_id: "тот же идентификатор Авито",
  name: "то же имя",
};

/**
 * Чем проверять довод. Текст у каждой причины СВОЙ: общий «проверьте
 * переписку» под совпавшим номером советовал бы искать глазами то, что и так
 * написано строкой выше, а под совпавшим именем — единственное, что вообще
 * можно сделать.
 */
const REASON_HINT: Record<MergeCandidate["reason"], string> = {
  phone: "Телефон — сильный довод.",
  phone_candidate:
    "Номер вычитан из переписки и никем не подтверждён — сверьте, тот ли это человек.",
  external_id:
    "Проверьте переписку: идентификатор Авито совпадает и у разных людей.",
  name: "Проверьте переписку: «Иван» — это каждый десятый.",
};

/** Сколько объединений показывать развёрнуто; остальное — по «ещё N». */
/** Подсказка про отмену — в подсказке кнопки, а не строкой в каждой
 * карточке: её читают один раз за всё время работы (аудит 15.09). */
const ПОДСКАЗКА_РАЗЪЕДИНИТЬ =
  "Ошиблись — «Разъединить», и больше эту пару автоматика не тронет.";

function склонение(n: number): string {
  const r10 = n % 10;
  const r100 = n % 100;
  if (r10 === 1 && r100 !== 11) return "карточкой";
  return "карточками";
}

function CardLine({ card }: { card: ClientCardRef }) {
  return (
    <Text fz="sm" c="var(--lc-text-1)">
      {cardTitle(card)}
      {card.phone ? ` · ${formatPhone(card.phone)}` : ""}
    </Text>
  );
}

/** Чем подтверждено объединение — одной короткой пометкой, а не абзацем. */
function пометка(row: MergedFrom): {
  text: string;
  warn: boolean;
} {
  if (row.auto)
    return {
      text: "Автоматически: обе карточки назвали один номер",
      warn: false,
    };
  if (row.confidence === "confirmed")
    return { text: "Подтверждено: телефон совпал", warn: false };
  return { text: "Предположительно: телефоны не совпадали", warn: true };
}

export function ClientMergePanel({
  identity,
  candidates,
  clientId,
  convId,
  canManage,
}: {
  identity: ClientIdentityDto | undefined;
  candidates: MergeCandidate[];
  clientId: string;
  convId: string | null;
  canManage: boolean;
}) {
  const merge = useMergeClients(clientId, convId);
  const join = useJoinGroup(clientId, convId);
  const unmerge = useUnmergeClients(clientId, convId);
  const [всё, setВсё] = useState(false);

  const fail = (e: unknown) => {
    // 422 «уже объединена» — не поломка: автоматика или коллега успели раньше,
    // карточку обновит кадр. Красный тост научил бы бояться обычного хода дел.
    if (
      e instanceof ApiError &&
      e.status === 422 &&
      (e.code === "already_merged" || e.code === "target_is_merged")
    ) {
      toast.info("Уже объединено", "Карточка обновится сама");
      return;
    }
    toast.error(
      "Не получилось",
      e instanceof ApiError ? e.message : "Попробуйте ещё раз",
    );
  };

  const merged = identity?.merged_from ?? [];
  const target = identity?.merged_into ?? null;

  if (merged.length === 0 && candidates.length === 0 && !target) return null;

  // ДВА ОДИНАКОВЫХ БЛОКА ПО 79 px — ОДНОЙ СТРОКОЙ С РАСКРЫТИЕМ (аудит
  // 15.09): «Объединена автоматически по телефону с карточкой «Елизавета»»
  // дважды подряд не различить ни по имени, ни по дате. Одна связь
  // показывается сразу; две и больше — сводкой, в раскрытии по строке на
  // связь: карточка, дата, «Разъединить».
  const свёрнуто = merged.length >= 2 && !всё;
  const показаны = свёрнуто ? [] : merged;
  const естьАвто = merged.some((r) => r.auto);

  return (
    <div className="card-merge">
      {/* Эта карточка сама уведена в другую. В интерфейсе почти не случается —
          диалоги при объединении переезжают к победителю, — но новый чат на
          её идентификатор Авито заведёт диалог именно здесь, и тогда оператор
          обязан понимать, что открыл «хвост», а не новую карточку. */}
      {target && (
        <Text fz="xs" c="var(--lc-warning-text)">
          Эта карточка объединена с «{cardTitle(target)}» — телефон и история
          там.
        </Text>
      )}

      {свёрнуто && (
        <div className="card-merge__row card-merge__row--compact">
          <div className="card-merge__text">
            <Text fz="sm" c="var(--lc-text-1)">
              Объединена с {merged.length} {склонение(merged.length)}
              {естьАвто ? " по номеру" : ""}
            </Text>
          </div>
          <Button
            variant="subtle"
            size="compact-xs"
            onClick={() => setВсё(true)}
          >
            Показать
          </Button>
        </div>
      )}
      {показаны.map((row) => {
        const п = пометка(row);
        return (
          <div
            key={row.id}
            className="card-merge__row card-merge__row--compact"
          >
            <div className="card-merge__text">
              <Text fz="sm" c="var(--lc-text-1)">
                {row.auto
                  ? `Объединена автоматически по телефону с карточкой «${cardTitle(row)}»`
                  : `Объединена вручную с карточкой «${cardTitle(row)}»`}
              </Text>
              {/* СТЕПЕНЬ ДОВЕРИЯ СЛОВАМИ, а не значком: «подтверждено» и
                  «предположительно» — это разница между «звоните» и «сначала
                  проверьте, тот ли это человек». Дата — чтобы две связи
                  различались хотя бы ею. */}
              <Text
                fz="xs"
                c={п.warn ? "var(--lc-warning-text)" : "var(--lc-text-3)"}
              >
                {п.text}
                {row.merged_at ? ` · ${formatDate(row.merged_at)}` : ""}
              </Text>
            </div>
            {canManage && (
              <Button
                variant="subtle"
                size="compact-xs"
                className="card-merge__unmerge"
                loading={unmerge.isPending}
                title={row.auto ? ПОДСКАЗКА_РАЗЪЕДИНИТЬ : undefined}
                onClick={() =>
                  unmerge.mutate(row.id, {
                    onSuccess: (r) =>
                      toast.success(
                        "Карточки разъединены",
                        `Диалогов возвращено: ${r.moved_conversations}`,
                      ),
                    onError: fail,
                  })
                }
              >
                Разъединить
              </Button>
            )}
          </div>
        );
      })}
      {всё && merged.length >= 2 && (
        <Button
          variant="subtle"
          size="compact-xs"
          onClick={() => setВсё(false)}
        >
          Свернуть
        </Button>
      )}

      {canManage &&
        candidates.map((row) => (
          <div key={row.id} className="card-merge__row" data-hint="true">
            <div className="card-merge__text">
              <Text fz="sm" c="var(--lc-text-1)">
                Возможно, это тот же человек: «{cardTitle(row)}»
              </Text>
              <Text fz="xs" c="var(--lc-text-3)">
                Совпало: {REASON_TEXT[row.reason]}. {REASON_HINT[row.reason]}
                {row.vetoed
                  ? " Эту пару уже разъединяли — автоматика её не тронет."
                  : ""}
              </Text>
              <CardLine card={row} />
            </div>
            {row.has_group && row.mine_has_group ? (
              <Text fz="xs" c="var(--lc-text-3)">
                В обе карточки уже объединяли — сначала разъедините одну из групп
              </Text>
            ) : (
              <Button
                variant="outline"
                size="compact-xs"
                loading={merge.isPending || join.isPending}
                onClick={() =>
                  (row.has_group ? join : merge).mutate(row.id, {
                    onSuccess: (r) =>
                      toast.success(
                        "Карточки объединены",
                        `Диалогов перенесено: ${r.moved_conversations}. Можно разъединить обратно.`,
                      ),
                    onError: fail,
                  })
                }
              >
                Объединить
              </Button>
            )}
          </div>
        ))}
    </div>
  );
}
