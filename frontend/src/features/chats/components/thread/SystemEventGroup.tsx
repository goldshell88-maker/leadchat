import { useState } from "react";
import type { MessageDto } from "@/shared/api/types";
import { formatClock, formatDividerLabel, localDayKey } from "@/shared/lib/formatTime";
import { plural } from "@/shared/lib/plural";
import { IconChevronDown } from "@/shared/ui/Icon";
import type { ThreadEvent } from "./threadEvents";

/**
 * Свёрнутая серия служебных записей — «История статусов: 12 событий ▾».
 *
 * ЗАЧЕМ (разбор живого экрана владельцем, 12 августа). Двенадцать плашек
 * «Диалог принят / возвращён во «Входящие» / отклонён / отказ отменён» подряд
 * вытеснили сообщения клиента за нижний край экрана: оператор открывал диалог
 * и вместо вопроса клиента читал историю нажатий коллег. Это лог, а не
 * переписка, и весить в ленте он обязан как одна строка.
 *
 * ЧТО ПОКАЗЫВАЕТ СВЁРНУТАЯ СТРОКА. Только счёт. Соблазн вынести в неё «самое
 * важное событие» был, и от него отказались: текущее состояние диалога и так
 * написано в шапке одним чипом статуса, а вторая формулировка того же самого,
 * собранная угадыванием по журналу, — это ещё один источник правды, который
 * рано или поздно разойдётся с первым.
 *
 * ПОГАШЕННЫЕ ПАРЫ НЕ ПРЯЧУТСЯ, А ГАСНУТ. «Принял → вернул через минуту» —
 * событие, которого как бы не было, но след его должен остаться: иначе
 * руководитель, разбирающий, почему клиент ждал сорок минут, не найдёт следа
 * вовсе. Приглушённая строка с пометкой «отменено» отвечает на оба вопроса.
 */
export function SystemEventGroup({
  events,
  prev,
}: {
  events: ThreadEvent[];
  /** Предыдущее СООБЩЕНИЕ ленты — по нему считается дата-разделитель. */
  prev: MessageDto | null;
}) {
  const [open, setOpen] = useState(false);
  const first = events[0];
  const showDivider = !prev || localDayKey(prev.created_at) !== localDayKey(first.msg.created_at);
  const count = events.length;
  const label = `История статусов: ${count} ${plural(count, "событие", "события", "событий")}`;

  return (
    <>
      {showDivider && (
        <div className="msg-divider" role="separator">
          <span>{formatDividerLabel(first.msg.created_at)}</span>
        </div>
      )}

      <div className="msg msg--events" role="listitem">
        <div className="thread-events">
          <button
            type="button"
            className="thread-events__toggle"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {label}
            {/*
              Стрелка поворачивается, а не подменяется вторым значком: две
              разные картинки на одну кнопку читаются как две разные кнопки.
            */}
            <IconChevronDown size={14} />
          </button>

          {open && (
            <ol className="thread-events__list">
              {events.map((e) => (
                <li
                  key={e.msg.id}
                  className="thread-events__item"
                  data-cancelled={e.cancelled || undefined}
                >
                  <span className="thread-events__time">{formatClock(e.msg.created_at)}</span>
                  <span className="thread-events__text">{e.text}</span>
                  {e.cancelled && <span className="thread-events__note">отменено</span>}
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </>
  );
}
