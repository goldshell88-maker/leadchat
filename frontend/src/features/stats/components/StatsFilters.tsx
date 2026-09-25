import { useMemo, useState } from "react";
import { Button, MultiSelect, Select, Text, Tooltip } from "@mantine/core";
import { DatePickerInput, DatesProvider } from "@mantine/dates";
import "dayjs/locale/ru";
import { useAssignableUsersQuery, useAvitoAccountsQuery } from "@/shared/api/reference";
import { IconDownload } from "@/shared/ui/Icon";
import { подписьКанала } from "@/shared/lib/channelLabel";
import { formatDayLabel, formatRefreshedAt } from "../lib/format";
import { resolveCustomPeriod } from "../lib/customPeriod";
import {
  PERIOD_PRESET_LABELS,
  formatPeriodLabel,
  moscowToday,
  parseIsoDate,
  type Period,
  type PeriodPreset,
} from "@/shared/lib/period";

/**
 * Шапка `/stats` (11 §6.1): период · менеджеры · аккаунт · «Данные на HH:MM» ·
 * «⬇ Экспорт». Даты — по Москве (06 §0.1), поэтому и «сегодня» берётся из MSK.
 */
export function StatsFilters({
  preset,
  period,
  onPresetChange,
  onCustomPeriod,
  managerIds,
  onManagersChange,
  accountId,
  onAccountChange,
  refreshedAt,
  onExport,
  extraManagers,
}: {
  preset: PeriodPreset;
  period: Period;
  onPresetChange(p: PeriodPreset): void;
  onCustomPeriod(p: Period): void;
  managerIds: string[];
  onManagersChange(ids: string[]): void;
  accountId?: string;
  onAccountChange(id?: string): void;
  refreshedAt: string | null;
  onExport(): void;
  /** id → подпись для сотрудников, которых нет в справочнике (отключённые). */
  extraManagers?: Record<string, string>;
}) {
  const users = useAssignableUsersQuery();
  const accounts = useAvitoAccountsQuery();
  const refreshedLabel = formatRefreshedAt(refreshedAt);

  /**
   * Слишком длинный период отбивается ЗДЕСЬ, а не ответом сервера (STATS-05).
   *
   * Проверка `periodTooLong` была написана вместе с лимитом и не вызывалась
   * ни из одного места продукта. У календаря ограничен только `maxDate`,
   * нижней границы нет — выбрать 2020–2026 можно в два клика. Дальше все
   * четыре запроса экрана получали 400 `period_too_long` разом, и человек
   * видел «Не получилось загрузить статистику» без единого слова о причине;
   * вернуть экран к жизни можно было только угадав, что виноват период.
   *
   * Выбор не применяется вовсе: показать пустой экран и подпись под ним
   * значит оставить на нём цифры, которых никто не считал. Календарь
   * остаётся на прежнем (рабочем) периоде, и сообщение говорит, что делать.
   */
  const [rangeError, setRangeError] = useState<string | null>(null);

  /**
   * `/users/assignable` отдаёт только АКТИВНЫХ admin/manager (01 §3.1), а в
   * отчёте остаются и отключённые сотрудники с активностью за период (06 §4.4).
   * Без объединения клик по строке уволенного менеджера положил бы в фильтр id,
   * которого нет в `data`, — Mantine показал бы пилюлю с голым UUID.
   */
  const managerOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const u of users.data?.items ?? []) map.set(u.id, u.full_name);
    for (const [id, label] of Object.entries(extraManagers ?? {})) {
      if (!map.has(id)) map.set(id, label);
    }
    return [...map.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label, "ru"));
  }, [users.data, extraManagers]);

  /**
   * НЕДОСОБРАННЫЙ ДИАПАЗОН ЖИВЁТ ЗДЕСЬ, И БЕЗ ЭТОГО КАЛЕНДАРЬ НЕ РАБОТАЛ ВОВСЕ.
   *
   * НАЙДЕНО ПРОВЕРКОЙ ИНТЕРФЕЙСА 12 августа: «клик по дню ничего не делает,
   * день не подсвечивается как начало диапазона». И это была правда — выбрать
   * произвольный период было НЕЛЬЗЯ ни одним способом.
   *
   * Причина в том, что `DatePickerInput` управляемый: что показать, решает
   * `value`. Значение собиралось только из ПРИМЕНЁННОГО периода, а первый клик
   * приходит как `[дата, null]` — диапазон ещё не собран, применять нечего, и
   * обработчик выходил ранним `return`. Состояние не менялось, календарь
   * перерисовывался со старым диапазоном, первый клик пропадал бесследно.
   * Второй клик после этого выглядел для календаря снова первым — и так по
   * кругу, до бесконечности.
   *
   * Поэтому промежуточный выбор держится отдельно от применённого периода:
   * `draft` — то, что человек нажал и ещё не досбирал; `null` — досбирать
   * нечего, показываем применённое.
   */
  const [draft, setDraft] = useState<[Date | null, Date | null] | null>(null);

  const appliedRange: [Date | null, Date | null] = [
    parseIsoDate(period.dateFrom),
    parseIsoDate(period.dateTo),
  ];
  const rangeValue = draft ?? appliedRange;

  return (
    <div className="stats-filters">
      <Select
        size="xs"
        w={150}
        aria-label="Период"
        data={(Object.keys(PERIOD_PRESET_LABELS) as PeriodPreset[]).map((value) => ({
          value,
          label: PERIOD_PRESET_LABELS[value],
        }))}
        value={preset}
        allowDeselect={false}
        onChange={(v) => {
          if (!v) return;
          // Ушли с произвольного, бросив выбор на полпути, — недособранный
          // диапазон забываем. Иначе вернувшись человек увидит подсвеченным
          // день, который выбирал десять минут назад и передумал.
          setDraft(null);
          setRangeError(null);
          onPresetChange(v as PeriodPreset);
        }}
      />

      {preset === "custom" ? (
        <DatesProvider settings={{ locale: "ru", firstDayOfWeek: 1, weekendDays: [0, 6] }}>
          <DatePickerInput
            size="xs"
            w={220}
            type="range"
            aria-label="Произвольный период"
            placeholder="Выберите даты"
            valueFormat="D MMM YYYY"
            maxDate={parseIsoDate(moscowToday()) ?? undefined}
            value={rangeValue}
            error={rangeError}
            onChange={(value) => {
              const [from, to] = value;
              // ПОКАЗЫВАЕМ НАЖАТОЕ ПЕРВЫМ ДЕЛОМ, до всякого разбора: календарь
              // управляемый, и пока `value` не изменится, выбор для человека не
              // случился. Именно этой строки не хватало, чтобы выбрать период
              // было можно вообще.
              setDraft(value);
              const result = resolveCustomPeriod(from, to);
              if (result.kind === "wait") return; // ждём вторую дату
              if (result.kind === "error") {
                // Диапазон оставляем на экране вместе с ошибкой: человек должен
                // видеть, что именно он выбрал, а не гадать, о чём сообщение.
                setRangeError(result.message);
                return;
              }
              setRangeError(null);
              // Применили — дальше показываем применённый период, а не черновик.
              setDraft(null);
              onCustomPeriod(result.period);
            }}
          />
        </DatesProvider>
      ) : (
        <Text fz="xs" c="var(--lc-text-3)" className="stats-filters__period">
          {formatPeriodLabel(period, formatDayLabel)}
        </Text>
      )}

      <MultiSelect
        size="xs"
        w={220}
        aria-label="Менеджеры"
        placeholder={managerIds.length ? undefined : "Менеджеры: все"}
        searchable
        clearable
        data={managerOptions}
        value={managerIds}
        onChange={onManagersChange}
      />

      <Select
        size="xs"
        w={190}
        /* Подпись канала — «название · источник», и живые значения длиннее поля:
           «Бригада Андрея Владиславовича КП · Б6» не помещается и обрезается
           многоточием — то есть по какому каналу посчитан экран, из фильтра не
           прочитать. Класс отдаёт полю ОСТАТОК строки, когда он есть (разбор —
           у `.stats-filters__channel` в stats.css); ширину 190 он не отменяет,
           а делает нижней границей. */
        className="stats-filters__channel"
        /* «Канал», а не «Аккаунт»: та же сущность на «Разборе диалогов» и в
           «Чатах» называется каналом, и человек ходит между этими экранами
           подряд. Слово «аккаунт» остаётся там, где его подключают, — в
           настройках: там речь и правда об учётной записи Авито. */
        aria-label="Фильтр по каналу"
        placeholder="Канал: все"
        clearable
        data={(accounts.data?.items ?? []).map((a) => ({ value: a.id, label: подписьКанала(a) }))}
        value={accountId ?? null}
        onChange={(v) => onAccountChange(v ?? undefined)}
      />

      <div className="stats-filters__tail">
        {/*
          Подсказка называет, к ЧЕМУ относится метка. «Агрегаты обновляются раз
          в час» читалось как «весь экран обновляется раз в час», хотя половина
          чисел на нём живая, — и объясняло расхождение между соседними
          карточками ровно наоборот (STATS-04). Что именно посчитано по витрине,
          а что при открытии, подписано у каждой группы карточек.
        */}
        {refreshedLabel && (
          <Tooltip
            label="Время последнего пересчёта витрины статистики — раз в час, в HH:05"
            withArrow
            position="bottom"
          >
            <Text fz="xs" c="var(--lc-text-3)">
              {refreshedLabel}
            </Text>
          </Tooltip>
        )}
        {/*
          «Выгрузить отчёт», а не «Выгрузить CSV» (проверка 24.09): кнопка
          открывает окно, где по умолчанию выбран XLSX из трёх листов, и
          подпись про CSV обещала не тот файл. Глагол тот же, что на «Разборе
          диалогов», — человек ищет глазами то же слово. Значок наш, не эмодзи:
          «⬇» рисует операционная система, и на Windows он другой.
        */}
        <Button
          size="xs"
          variant="default"
          leftSection={<IconDownload size={14} />}
          onClick={onExport}
        >
          Выгрузить отчёт
        </Button>
      </div>
    </div>
  );
}
