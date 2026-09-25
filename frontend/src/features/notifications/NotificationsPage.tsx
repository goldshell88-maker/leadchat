import { useEffect, useMemo, useState } from "react";
import { Button, Checkbox, Group, Select, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import "dayjs/locale/ru";
import {
  qk,
  type NotificationFilters,
  type NotificationSeverityFilter,
} from "@/shared/api/queryKeys";
import type { NotificationActionResult, NotificationDto } from "@/shared/api/types";
import { presetPeriod, shiftDate, toIsoDate } from "@/shared/lib/period";
import { DateRangeInput } from "@/shared/ui/DateRangeInput";
import { EmptyState } from "@/shared/ui/EmptyState";
import { OneTimeLinkModal } from "@/features/settings/team/OneTimeLinkModal";
import { NOTIFICATIONS_PAGE_SIZE, fetchNotifications } from "./api";
import {
  KIND_CATALOG,
  SEVERITY_LABELS,
  isUnread,
  kindIcon,
  kindLabel,
  resolveAction,
  severityDotClass,
} from "./catalog";
import { selectHasUnconfirmedCritical, useNotificationStore } from "./store";
import { formatNotificationTime, formatRepeat } from "./time";
import {
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotificationAction,
  useNotificationsEnabled,
} from "./useNotifications";
import "./notifications.css";
import { PageHeader } from "@/shared/ui/PageHeader";
import { выбралСамПоАдресу } from "@/features/chats/выборДиалога";
import { толькоВнутренний } from "@/shared/lib/внутреннийАдрес";

/**
 * Полный журнал `/notifications` (14 §3): фильтры по важности, типу и периоду,
 * пагинация и выполнение действий. Ленивый чанк — экран нужен не на каждый день.
 */

type JournalPreset = "all" | "today" | "last7" | "last30" | "custom";

const PRESET_OPTIONS: Array<{ value: JournalPreset; label: string }> = [
  { value: "all", label: "Всё время" },
  { value: "today", label: "Сегодня" },
  { value: "last7", label: "7 дней" },
  { value: "last30", label: "30 дней" },
  { value: "custom", label: "Произвольный" },
];

const SEVERITY_OPTIONS: Array<{ value: NotificationSeverityFilter; label: string }> = [
  { value: "all", label: "Любая важность" },
  { value: "critical", label: SEVERITY_LABELS.critical },
  { value: "warning", label: SEVERITY_LABELS.warning },
  { value: "info", label: SEVERITY_LABELS.info },
];

function presetToRange(preset: JournalPreset): { dateFrom?: string; dateTo?: string } {
  if (preset === "all" || preset === "custom") return {};
  const p = presetPeriod(preset);
  return { dateFrom: p.dateFrom, dateTo: p.dateTo };
}

function JournalRow({
  notification,
  onLink,
}: {
  notification: NotificationDto;
  onLink(result: NotificationActionResult): void;
}) {
  const navigate = useNavigate();
  const markRead = useMarkNotificationRead();
  const runAction = useNotificationAction(onLink);
  const action = resolveAction(notification);
  const repeat = formatRepeat(notification.repeat_count);
  const unread = isUnread(notification);

  const handleAction = () => {
    if (!action) return;
    if (action.mode === "link") {
      // Адрес приходит с сервера — см. довод в `shared/lib/внутреннийАдрес.ts`.
      const куда = толькоВнутренний(action.to);
      if (!куда) return;
      if (unread) markRead.mutate(notification.id);
      // Нажатие в журнале уведомлений — осознанный выбор человека.
      выбралСамПоАдресу(куда);
      navigate(куда);
      return;
    }
    if (action.mode === "request") {
      runAction.mutate({ notification, code: action.code });
      return;
    }
    markRead.mutate(notification.id);
  };

  return (
    <tr className="lc-journal__row" data-severity={notification.severity} data-unread={unread || undefined}>
      <td className="lc-journal__time">
        {formatNotificationTime(notification.last_seen_at ?? notification.created_at)}
      </td>
      <td>
        <span className={severityDotClass(notification.severity)}>
          {SEVERITY_LABELS[notification.severity]}
        </span>
      </td>
      <td className="lc-journal__kind">
        <span aria-hidden="true">{kindIcon(notification.kind)}</span> {kindLabel(notification.kind)}
      </td>
      <td>
        <div className="lc-journal__title">{notification.title}</div>
        <div className="lc-journal__body">
          {notification.body ?? ""}
          {repeat ? ` · ${repeat}` : ""}
        </div>
      </td>
      <td className="lc-journal__actions">
        <Group gap="var(--lc-space-1)" justify="flex-end" wrap="nowrap">
          {action && (
            <Button
              size="compact-xs"
              variant={notification.severity === "critical" ? "filled" : "outline"}
              color={notification.severity === "critical" ? "red" : undefined}
              loading={runAction.isPending}
              onClick={handleAction}
            >
              {action.label}
            </Button>
          )}
          {unread && (
            <Button size="compact-xs" variant="subtle" onClick={() => markRead.mutate(notification.id)}>
              Прочитано
            </Button>
          )}
        </Group>
      </td>
    </tr>
  );
}

/*
 * ФИЛЬТРЫ ЖУРНАЛА ЖИВУТ В АДРЕСЕ СТРАНИЦЫ (NOTIF-04).
 *
 * Раньше все пять — важность, тип, период, произвольные даты, «только
 * непрочитанные» — и номер страницы лежали в `useState`. Администратор
 * отбирал «Критичное · 30 дней», уходил по ссылке уведомления в настройки,
 * жал «Назад» — и возвращался к «Всё время · любая важность», страница снова
 * первая. Разбор журнала начинался заново. Ссылку на отобранное коллеге тоже
 * было не послать: адрес фильтров не нёс.
 *
 * Источник истины — именно строка запроса, а не состояние рядом с ней: две
 * копии одного значения расходятся на первой же кнопке «Назад».
 */
const QP = {
  severity: "severity",
  kind: "kind",
  period: "period",
  from: "from",
  to: "to",
  unread: "unread",
  offset: "offset",
} as const;

const SEVERITY_VALUES: NotificationSeverityFilter[] = ["all", "critical", "warning", "info"];
const PRESET_VALUES = PRESET_OPTIONS.map((o) => o.value);

/** Шаг «Назад»: на страницу раньше, но не дальше последней, где строки ещё есть. */
function previousPageOffset(offset: number, total: number): number {
  const lastFilled =
    total > 0 ? Math.floor((total - 1) / NOTIFICATIONS_PAGE_SIZE) * NOTIFICATIONS_PAGE_SIZE : 0;
  return Math.max(0, Math.min(offset - NOTIFICATIONS_PAGE_SIZE, lastFilled));
}

/** Значения по умолчанию — то, что показывает чистый адрес `/notifications`. */
const DEFAULTS = { severity: "all" as NotificationSeverityFilter, preset: "all" as JournalPreset };

export function NotificationsPage() {
  const enabled = useNotificationsEnabled();
  const [params, setParams] = useSearchParams();

  const preset: JournalPreset = PRESET_VALUES.includes(params.get(QP.period) as JournalPreset)
    ? (params.get(QP.period) as JournalPreset)
    : DEFAULTS.preset;
  const severity: NotificationSeverityFilter = SEVERITY_VALUES.includes(
    params.get(QP.severity) as NotificationSeverityFilter,
  )
    ? (params.get(QP.severity) as NotificationSeverityFilter)
    : DEFAULTS.severity;
  const kind = params.get(QP.kind);
  const unreadOnly = params.get(QP.unread) === "1";
  const offset = Math.max(0, Number(params.get(QP.offset) ?? 0) || 0);
  const custom = { dateFrom: params.get(QP.from) ?? undefined, dateTo: params.get(QP.to) ?? undefined };

  /**
   * Любая правка фильтра сбрасывает страницу на первую — кроме самой смены
   * страницы. Иначе человек, отобравший «Критичное» на четвёртой странице,
   * увидел бы пустоту и решил, что критичного нет.
   */
  const patch = (next: Record<string, string | null>, keepOffset = false) => {
    const p = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v === null || v === "") p.delete(k);
      else p.set(k, v);
    }
    if (!keepOffset) p.delete(QP.offset);
    // replace: разбор журнала не должен превращать «Назад» в перелистывание
    // собственных фильтров — кнопка обязана уводить туда, откуда пришли.
    setParams(p, { replace: true });
  };

  const filtersTouched =
    severity !== DEFAULTS.severity || Boolean(kind) || preset !== DEFAULTS.preset || unreadOnly;

  const [link, setLink] = useState<NotificationActionResult | null>(null);
  const markAll = useMarkAllNotificationsRead();
  const hasUnconfirmedCritical = useNotificationStore(selectHasUnconfirmedCritical);

  const range = preset === "custom" ? custom : presetToRange(preset);
  const filters: NotificationFilters = {
    ...range,
    severity,
    kind: kind ?? undefined,
    unreadOnly: unreadOnly || undefined,
    offset,
  };

  const q = useQuery({
    queryKey: qk.notifications.list(filters),
    queryFn: () => fetchNotifications(filters),
    enabled,
  });

  // Счётчик приезжает вместе со списком — бейдж колокольчика не должен
  // расходиться с открытым журналом.
  const setUnread = useNotificationStore((s) => s.setUnread);
  const unreadFromList = q.data?.unread;
  useEffect(() => {
    if (typeof unreadFromList === "number") setUnread(unreadFromList);
  }, [unreadFromList, setUnread]);

  const kindOptions = useMemo(() => {
    const set = new Set<string>(Object.keys(KIND_CATALOG));
    for (const row of q.data?.items ?? []) set.add(row.kind);
    return [...set]
      .map((value) => ({ value, label: kindLabel(value) }))
      .sort((a, b) => a.label.localeCompare(b.label, "ru"));
  }, [q.data]);

  const total = q.data?.page.total ?? 0;
  const shown = q.data?.items.length ?? 0;

  if (!enabled) {
    return (
      <section className="lc-page lc-journal" aria-label="Уведомления">
        <PageHeader title="Уведомления" />
        <Text fz="sm" c="var(--lc-text-3)">
          Вашей роли уведомления не приходят
        </Text>
      </section>
    );
  }

  return (
    <section className="lc-page lc-journal" aria-label="Журнал уведомлений">
      <Group justify="space-between" mb="var(--lc-space-3)">
        {/* h1: это заголовок страницы, а не раздела внутри неё. Был h2 без
            единицы выше — для скринридера «подраздел неизвестно чего». */}
        <Title order={1} className="page-header__title">
          Уведомления
        </Title>
        <Button size="xs" variant="subtle" onClick={() => markAll.mutate()} loading={markAll.isPending}>
          Отметить все прочитанными
        </Button>
      </Group>

      {/* То же предупреждение, что и в колокольчике: кнопка одна и та же, и
          красные плашки висят над этим экраном тоже (они в каркасе). */}
      {hasUnconfirmedCritical && (
        <Text fz="xs" c="var(--lc-text-3)" mb="var(--lc-space-2)">
          Критичные останутся на экране, пока не подтвердите каждое
        </Text>
      )}

      <div className="lc-journal__filters">
        <Select
          size="xs"
          w={170}
          aria-label="Важность"
          data={SEVERITY_OPTIONS}
          value={severity}
          allowDeselect={false}
          // Умолчание из адреса выбрасываем: «?severity=all» — это мусор в
          // ссылке, которую человек пошлёт коллеге.
          onChange={(v) => v && patch({ [QP.severity]: v === DEFAULTS.severity ? null : v })}
        />
        <Select
          size="xs"
          /*
           * 345, А НЕ 260 (замер на бою 29.08). Под текст в поле было 217 px,
           * а три названия событий просят 247–279: «Канал отобрали: подписка
           * на события пропала» не влезало на 62 px и вставало как «Канал
           * отобрали: подписка на со…». Пришедший по готовой ссылке с уже
           * наложенным фильтром не понимал, что именно отфильтровано.
           *
           * 345 даёт 302 px под текст — запас 23 (8%). Ряд фильтров
           * переносится по строкам, места здесь хватает.
           */
          w={345}
          aria-label="Тип события"
          placeholder="Тип: любой"
          clearable
          searchable
          data={kindOptions}
          value={kind}
          onChange={(v) => patch({ [QP.kind]: v })}
        />
        <Select
          size="xs"
          w={150}
          aria-label="Период"
          data={PRESET_OPTIONS}
          value={preset}
          allowDeselect={false}
          onChange={(v) => {
            if (!v) return;
            // Умолчание произвольного периода — от ЛОКАЛЬНОГО «сегодня»
            // (NOTIF-05). Засев из московского дня спорил с самим календарём:
            // тот подсвечивает сегодняшнее число по часам компьютера, и у
            // сотрудника восточнее Москвы диапазон приезжал на день вперёд.
            const today = toIsoDate(new Date());
            const seed: Record<string, string | null> =
              v === "custom" && !custom.dateFrom
                ? { [QP.from]: shiftDate(today, -6), [QP.to]: today }
                : {};
            patch({ [QP.period]: v === DEFAULTS.preset ? null : v, ...seed });
          }}
        />
        {preset === "custom" && (
          <DateRangeInput
            ariaLabel="Произвольный период"
            from={custom.dateFrom}
            to={custom.dateTo}
            onChange={(dateFrom, dateTo) => patch({ [QP.from]: dateFrom, [QP.to]: dateTo })}
          />
        )}
        <Checkbox
          size="xs"
          label="Только непрочитанные"
          checked={unreadOnly}
          onChange={(e) => patch({ [QP.unread]: e.currentTarget.checked ? "1" : null })}
        />
        {/* Вернуться к исходному виду одной кнопкой: пять контролов руками
            переставлять никто не станет — журнал просто закроют (NOTIF-04). */}
        {filtersTouched && (
          <Button size="compact-xs" variant="subtle" onClick={() => setParams({}, { replace: true })}>
            Сбросить фильтры
          </Button>
        )}
      </div>

      {q.isPending ? (
        <div className="lc-journal__skeleton" aria-hidden="true">
          {Array.from({ length: 6 }, (_, i) => (
            <span key={i} className="lc-journal__skeleton-line" />
          ))}
        </div>
      ) : q.isError ? (
        /*
         * ⚠ ПУСТОЙ ЭКРАН ЖУРНАЛА СОБИРАЛСЯ ВРУЧНУЮ, ПОКА В ПРОДУКТЕ ЕСТЬ
         * ОБЩИЙ `EmptyState` (разбор 05.09).
         *
         * Здесь стояла ОДНА строка 13-м кеглем приглушённым цветом — тише
         * подписей фильтров над ней. У соседней «Живой ленты» ровно те же три
         * исхода (ошибка, отбор, тишина) давно нарисованы общим компонентом:
         * 88-пиксельный рисунок, заголовок 16/600, пояснение и место под
         * действие. Раздел, у которого пустота выглядит извинением, читается
         * как недоделанный — и это тем заметнее, что рядом сделано иначе.
         */
        <EmptyState
          illustration="error"
          title="Не получилось загрузить уведомления"
          description="Журнал не ответил. Связь могла оборваться на середине — нажмите «Повторить»"
          live="alert"
          action={
            <Button size="xs" variant="outline" onClick={() => void q.refetch()}>
              Повторить
            </Button>
          }
        />
      ) : shown === 0 && offset === 0 ? (
        /*
         * ПУСТОТА НАЗЫВАЕТ СВОЮ ПРИЧИНУ, А НЕ ОДНУ И ТУ ЖЕ НА ВСЕ СЛУЧАИ.
         *
         * Стояло «За выбранный период уведомлений нет» — на все исходы разом,
         * при том что период по умолчанию «Всё время» (проверка боя 14
         * августа: чистый адрес, пустой журнал, фраза про период, которого
         * никто не выбирал). Человек начинает менять период, который и так
         * самый широкий, и ничего не добивается.
         *
         * Данные для честного текста лежали рядом и не использовались:
         * `filtersTouched` считается строкой выше — той же, что зажигает
         * «Сбросить фильтры». Разведено по образцу «Живой ленты», где три
         * причины пустоты разделены давно.
         */
        !filtersTouched ? (
          <EmptyState
            illustration="done"
            title="Уведомлений пока нет"
            description="Сюда попадёт всё важное: аккаунты Авито, приём сообщений, диалоги без ответа"
            live="status"
          />
        ) : unreadOnly && severity === DEFAULTS.severity && !kind && preset === DEFAULTS.preset ? (
          <EmptyState
            illustration="done"
            title="Непрочитанных нет"
            description="Всё, что приходило, вы уже разобрали"
            live="status"
          />
        ) : (
          /*
           * ДЕЙСТВИЕ — ЗДЕСЬ, А НЕ ТОЛЬКО В РЯДУ ФИЛЬТРОВ. Кнопка сброса есть и
           * выше, но на пустом экране она стоит среди пяти контролов,
           * приглушённая, и глаз ищет её там же, где ставил отбор. Пустое
           * состояние обязано предлагать ровно один выход, и он ровно один:
           * снять то, что спрятало строки.
           *
           * ⚠ ПОДПИСЬ ДРУГАЯ — «СНЯТЬ ОТБОР», А НЕ «СБРОСИТЬ ФИЛЬТРЫ». Здесь
           * это не вторая копия кнопки, а окончание фразы «Уведомления есть,
           * но не те, что вы отобрали»; тем же словом «отбор» говорит пустое
           * состояние «Живой ленты». Одинаковая подпись в двух местах экрана
           * заставляла бы спрашивать, чем эти кнопки отличаются, — а они не
           * отличаются ничем, кроме места.
           */
          <EmptyState
            illustration="search"
            title="Под эти условия ничего не подошло"
            description="Уведомления есть, но не те, что вы отобрали"
            live="status"
            action={
              <Button size="xs" variant="outline" onClick={() => setParams({}, { replace: true })}>
                Снять отбор
              </Button>
            }
          />
        )
      ) : (
        <>
          {shown > 0 && (
            <div className="lc-journal__scroll">
              <table className="lc-table lc-journal__table">
                <thead>
                  <tr>
                    <th scope="col">Время</th>
                    <th scope="col">Важность</th>
                    <th scope="col">Событие</th>
                    <th scope="col">Что произошло</th>
                    <th scope="col" className="lc-journal__actions">
                      Действие
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {q.data.items.map((n) => (
                    <JournalRow key={n.id} notification={n} onLink={setLink} />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/*
            Подвал — и на опустевшей странице (проверка 24.09). Отметил
            прочитанными последние строки второй страницы «Только
            непрочитанные» — и оставался без «Назад», а пустое состояние
            писало «Непрочитанных нет», хотя на первой их ещё десятки. «Назад»
            ведёт на последнюю страницу, где строки есть.
          */}
          <Group justify="space-between" mt="var(--lc-space-3)">
            <Text fz="xs" c="var(--lc-text-3)">
              {shown > 0 ? `${offset + 1}–${offset + shown} из ${total}` : "На этой странице пусто"}
            </Text>
            <Group gap="var(--lc-space-2)">
              <Button
                size="xs"
                variant="default"
                disabled={offset === 0}
                onClick={() => patch({ [QP.offset]: String(previousPageOffset(offset, total)) }, true)}
              >
                Назад
              </Button>
              <Button
                size="xs"
                variant="default"
                disabled={offset + shown >= total}
                onClick={() => patch({ [QP.offset]: String(offset + NOTIFICATIONS_PAGE_SIZE) }, true)}
              >
                Вперёд
              </Button>
            </Group>
          </Group>
        </>
      )}

      <OneTimeLinkModal
        opened={Boolean(link?.result?.invite_url)}
        title="Новая ссылка установки пароля"
        url={link?.result?.invite_url ?? ""}
        hint="Ссылка показывается один раз и действует 72 часа. Передайте её сотруднику лично."
        onClose={() => setLink(null)}
      />
    </section>
  );
}
