import { useState } from "react";
import { Modal, Popover, Text, UnstyledButton } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { qk } from "@/shared/api/queryKeys";
import type { MyTodayStats } from "@/shared/api/types";
import { fetchMyToday } from "./api";
import { formatDuration, formatNumber } from "./lib/format";
import "./stats.css";
import { IconAlert } from "@/shared/ui/Icon";

/**
 * Один запрос на всё приложение: и подвал списка, и пункт меню аватара на
 * мобильной раскладке читают один ключ кэша (11 §6.4). Обновление — раз в 60 с
 * и по фокусу окна: серверный кэш (не короче интервала опроса — 06.09 поднят
 * до 120 с, иначе кэш промахивался ВСЕГДА) делает это дёшево (06 §6).
 */
function useMyToday() {
  return useQuery({
    queryKey: qk.stats.myToday,
    queryFn: fetchMyToday,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    staleTime: 30_000,
    retry: 1,
  });
}

/** Семь строк разбивки — одинаковы в поповере и в мобильной модалке (11 §6.4). */
function MyTodayRows({ data }: { data?: MyTodayStats }) {
  const waiting = data?.waiting_reply_now ?? 0;
  // Третий элемент — признак «подсветить строку». Раньше он выводился из
  // самой подписи, по её первому символу: текст работал одновременно данными,
  // и любая его правка — перевод, замена значка, лишний пробел — тихо
  // выключала подсветку.
  const rows: Array<[string, string, boolean?]> = [
    ["В работе сейчас", formatNumber(data?.active_now)],
    ["Ждут моего ответа", formatNumber(data?.waiting_reply_now), true],
    ["Взято сегодня", formatNumber(data?.taken_today)],
    ["Закрыто сегодня", formatNumber(data?.closed_today)],
    ["Отправлено сообщений", formatNumber(data?.messages_sent_today)],
    ["Первый ответ (медиана)", formatDuration(data?.frt_median_sec_today)],
    /*
     * ⚠ «ОТВЕЧЕНО ДИАЛОГОВ» ПЕРЕИМЕНОВАНО 08.09: считается здесь то же, что в
     * колонке «Ответил первым» на «Статистике» (`stats.py:answered_today` —
     * диалоги, где первым ответил ИМЕННО ЭТОТ человек). Прежнее слово было
     * последним местом со старым именем: на «Статистике» тем же словом
     * называется карточка, которая считает ЛЮБОЙ ответ оператора, и одно
     * слово над двумя разными числами уже стоило владельцу вопроса.
     */
    ["Ответил первым", formatNumber(data?.answered_today)],
  ];

  return (
    <dl className="my-today__grid">
      {rows.map(([label, value, warn]) => (
        <div key={label} className="my-today__row" data-warn={warn && waiting > 0 ? "" : undefined}>
          <dt>{warn && waiting > 0 ? <><IconAlert size={13} /> {label}</> : label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Виджет «моя статистика за сегодня» (11 §6.4, данные — 06 §6.1).
 * Живёт в подвале левой колонки `/chats` у менеджера (право `stats:own`;
 * экрана `/stats` у него нет).
 */
export function MyTodayWidget() {
  const q = useMyToday();
  const d = q.data;
  const waiting = d?.waiting_reply_now ?? 0;

  if (q.isPending) {
    return (
      <div className="my-today my-today--loading" aria-hidden="true">
        <span className="stats-skeleton stats-skeleton--line" />
      </div>
    );
  }

  if (q.isError || !d) {
    return (
      <div className="my-today my-today--muted">
        <Text fz="xs" c="var(--lc-text-3)">
          Статистика за сегодня недоступна
        </Text>
      </div>
    );
  }

  return (
    <Popover position="top-start" withArrow shadow="md" width={280}>
      <Popover.Target>
        <UnstyledButton className="my-today" aria-label="Моя статистика за сегодня">
          {/*
            ⚠ ЧИСЛО КРУПНЕЕ СЛОВА. Строка «Сегодня: 12 взято · 8 закрыто» вся
            набиралась 12-м кеглем одним цветом: показатель, ради которого
            подвал и существует, стоял вровень со служебными словами вокруг.
            Диспетчер смотрит сюда мельком между диалогами — и мельком тут
            нечего было поймать. Слова остаются те же (по ним ходит
            RoleChrome.test.tsx), меняется только вес подачи.
          */}
          <span className="my-today__line">
            Сегодня: <b className="my-today__num">{formatNumber(d.taken_today)}</b> взято ·{" "}
            <b className="my-today__num">{formatNumber(d.closed_today)}</b> закрыто
          </span>
          <span className="my-today__line my-today__line--sub">
            FRT {formatDuration(d.frt_median_sec_today)}
            {waiting > 0 && (
              <>
                {" · "}
                <span className="my-today__warn">
                  <IconAlert size={13} /> {formatNumber(waiting)} ждут ответа
                </span>
              </>
            )}
          </span>
        </UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown>
        <Text fw={600} fz="sm" mb="var(--lc-space-2)" c="var(--lc-text-1)">
          Моя статистика за сегодня
        </Text>
        <MyTodayRows data={d} />
      </Popover.Dropdown>
    </Popover>
  );
}

/**
 * Тот же виджет строкой в окне под именем сотрудника — мобильная раскладка
 * (11 §6.4). На узком экране `/chats/:id` показывает только ленту, левой
 * колонки с подвалом на экране нет, а цифры менеджеру нужны и там.
 *
 * Это была `Menu.Item`, пока окно сотрудника было выпадающим меню Mantine.
 * Стало обычной кнопкой: снаружи теперь `Popover`, и `Menu.Item` в нём
 * рисовался бы без стилей и с ролью `menuitem` без меню вокруг.
 */
export function MyTodayMenuItem() {
  const [opened, setOpened] = useState(false);
  const q = useMyToday();

  return (
    <>
      <button type="button" onClick={() => setOpened(true)}>
        Моя статистика за сегодня
      </button>
      <Modal opened={opened} onClose={() => setOpened(false)} title="Моя статистика за сегодня" centered>
        {q.isError || !q.data ? (
          <Text fz="sm" c="var(--lc-text-3)">
            Статистика за сегодня недоступна
          </Text>
        ) : (
          <MyTodayRows data={q.data} />
        )}
      </Modal>
    </>
  );
}
