import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Text } from "@mantine/core";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { qk, type StatsQuery } from "@/shared/api/queryKeys";
import type { ManagerRow, ManagersSort, SortOrder, TimeseriesGroup, TimeseriesMetric } from "@/shared/api/types";
import { moscowZoneHint } from "@/shared/lib/formatTime";
import { toast } from "@/shared/ui/toast";
import {
  daysInPeriod,
  formatPeriodLabel,
  hourGroupAllowed,
  moscowToday,
  parseIsoDate,
  periodTooLong,
  presetPeriod,
  type Period,
  type PeriodPreset,
} from "@/shared/lib/period";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { PageHeader } from "@/shared/ui/PageHeader";
import { fetchHeatmap, fetchManagers, fetchStatsSummary, fetchTimeseries } from "./api";
import { formatDayLabel } from "./lib/format";
import { dayForwardBlocked, dayStepTarget } from "./lib/dayStep";
import { ExportModal } from "./components/ExportModal";
import { Heatmap } from "./components/Heatmap";
import { ManagersTable } from "./components/ManagersTable";
import { MetricChart } from "./components/MetricChart";
import { StatsFilters } from "./components/StatsFilters";
import { SummaryCards, type SummaryPayload } from "./components/SummaryCards";
import "./stats.css";

/*
 * Закрытые словари значений адреса. Сверка с ними — не педантизм: адрес
 * приходит из закладки и чужого сообщения, и незнакомое значение обязано
 * молча стать умолчанием, а не пустым селектом над неверной выборкой.
 */
const PRESETS = ["today", "yesterday", "last7", "last30", "this_month", "prev_month", "custom"] as const;
const METRICS = [
  "conversations_new",
  "conversations_closed",
  "messages_in",
  "messages_out",
  "frt_operator_median",
  "phones_collected",
] as const;
const MANAGER_SORTS = [
  "messages_sent",
  "taken",
  "answered",
  "closed",
  "frt_median_sec",
  "frt_median_biz_sec",
] as const;

/**
 * Экран статистики `/stats` (11 §6) — роли A и H (право `stats:all`, guard в
 * app/router.tsx). Фильтры общие для всех блоков; тепловая карта фильтр по
 * менеджеру игнорирует (входящие менеджеру не принадлежат, 06 §4.3).
 */
export function StatsPage() {
  const navigate = useNavigate();
  const setChatFilters = useChatUiStore((s) => s.setFilters);

  /*
   * СРЕЗ ЖИВЁТ В АДРЕСЕ, А НЕ В ПАМЯТИ ВКЛАДКИ (п. 12 отчёта тестирования).
   *
   * Довод записан у журнала уведомлений слово в слово про этот случай:
   * «ссылку на отобранное коллеге было не послать — адрес фильтров не нёс».
   * Руководитель настраивал период, канал и сотрудника, а F5 или возврат из
   * диалога сбрасывали всё к «30 дней, все подряд». «Разбор диалогов» так
   * живёт давно — /stats просто не догнали.
   *
   * Правила те же, что там: запись ЗАМЕНОЙ (`replace`, а не `push` — иначе
   * «Назад» отматывает чужие клики), умолчания в адрес не пишутся (чистый
   * /stats остаётся чистым), а НЕИЗВЕСТНОЕ значение из адреса молча заменяется
   * умолчанием — ссылка полугодовой давности обязана показать отчёт, а не
   * красный блок (урок `?period=-5`, который отдавал всю базу).
   */
  const [params, setParams] = useSearchParams();
  const patch = useCallback(
    (changes: Record<string, string | null>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(changes)) {
            if (v === null || v === "") next.delete(k);
            else next.set(k, v);
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const rawPreset = params.get("period") ?? "last30";
  const presetKnown = (PRESETS as readonly string[]).includes(rawPreset);
  // «Свой период» без пары настоящих дат — не срез, а огрызок адреса: молча
  // возвращаемся к умолчанию, как сортировка на «Разборе».
  const customFrom = params.get("from");
  const customTo = params.get("to");
  const customValid =
    rawPreset === "custom" &&
    customFrom !== null &&
    customTo !== null &&
    parseIsoDate(customFrom) !== null &&
    parseIsoDate(customTo) !== null &&
    customFrom <= customTo &&
    !periodTooLong({ dateFrom: customFrom, dateTo: customTo });
  const preset: PeriodPreset =
    rawPreset === "custom" ? (customValid ? "custom" : "last30") : presetKnown ? (rawPreset as PeriodPreset) : "last30";
  const period: Period = useMemo(
    () =>
      preset === "custom"
        ? { dateFrom: customFrom as string, dateTo: customTo as string }
        : presetPeriod(preset),
    [preset, customFrom, customTo],
  );

  const managerIds = useMemo(
    () => (params.get("managers") ?? "").split(",").filter(Boolean),
    [params],
  );
  const accountId = params.get("account") ?? undefined;

  const rawMetric = params.get("metric") ?? "conversations_new";
  const metric: TimeseriesMetric = (METRICS as readonly string[]).includes(rawMetric)
    ? (rawMetric as TimeseriesMetric)
    : "conversations_new";
  const group: TimeseriesGroup = params.get("group") === "hour" ? "hour" : "day";
  const rawSort = params.get("sort") ?? "messages_sent";
  const sort: ManagersSort = (MANAGER_SORTS as readonly string[]).includes(rawSort)
    ? (rawSort as ManagersSort)
    : "messages_sent";
  const order: SortOrder = params.get("dir") === "asc" ? "asc" : "desc";

  const [exportOpen, setExportOpen] = useState(false);

  // Считается один раз на отрисовку: строка меняется раз в минуту, а
  // перерисовывать из-за неё экран отчёта незачем — расхождение поясов за
  // время просмотра не появляется и не исчезает.
  const zoneNote = useMemo(() => moscowZoneHint(new Date(), undefined, "Даты и «сегодня» здесь московские"), []);

  const hourAllowed = hourGroupAllowed(period);
  // Период вырос — «по часам» больше не разрешено (06 §4.2), молча возвращаемся к дням.
  useEffect(() => {
    if (!hourAllowed && group === "hour") patch({ group: null });
  }, [hourAllowed, group, patch]);

  const query: StatsQuery = useMemo(
    () => ({
      dateFrom: period.dateFrom,
      dateTo: period.dateTo,
      accountId,
      managerIds: managerIds.length ? managerIds : undefined,
    }),
    [period.dateFrom, period.dateTo, accountId, managerIds],
  );
  const heatQuery = useMemo(
    () => ({ dateFrom: period.dateFrom, dateTo: period.dateTo, accountId }),
    [period.dateFrom, period.dateTo, accountId],
  );

  /*
   * ⚠ ПРЕЖНИЕ ДАННЫЕ ОСТАЮТСЯ НА ЭКРАНЕ, ПОКА ЕДУТ НОВЫЕ.
   *
   * Ключ каждого запроса содержит период, канал, менеджеров, метрику,
   * группировку и сортировку. Любая их смена — новый ключ, а новый ключ без
   * `placeholderData` означает `isPending`, то есть каждый блок честно уходил
   * в свою ветку скелетона. Клик по заголовку колонки схлопывал таблицу с
   * двенадцатью строками в серый прямоугольник высотой 200px: страница
   * укорачивалась, прокрутка прыгала вверх, а кнопка сортировки исчезала
   * из-под курсора вместе с фокусом. Смена периода гасила разом все четыре
   * блока — экран на секунду становился пустым.
   *
   * `keepPreviousData` показывает прошлый срез, пока считается новый: числа
   * на миг отстают, но экран не прыгает. Так уже сделан «Разбор диалогов»
   * (`features/table/TablePage.tsx`), и разнобой между двумя таблицами одного
   * продукта был сам по себе дефектом.
   *
   * ЧТО ПРИ ЭТОМ НЕЛЬЗЯ ЗАБЫТЬ: показанный ряд теперь может быть сгруппирован
   * НЕ ТАК, как просит переключатель, — поэтому график подписывает точки по
   * `data.group`, а не по пропсу (см. `MetricChart`).
   */
  const summary = useQuery({
    queryKey: qk.stats.summary(query),
    /*
     * Ответ шире описания в `shared/api/types.ts`: `work_hours`, `period_live`
     * и карточка очереди добавлены сервером позже, а общий файл контрактов
     * правят параллельно другие ветки. Тип расширен у карточек (SummaryPayload)
     * и читается здесь же — рабочие часы отсюда уходят в тепловую карту.
     */
    queryFn: (): Promise<SummaryPayload> => fetchStatsSummary(query),
    placeholderData: keepPreviousData,
  });
  const timeseries = useQuery({
    queryKey: qk.stats.timeseries(query, metric, group),
    queryFn: () => fetchTimeseries(query, metric, group),
    placeholderData: keepPreviousData,
  });
  const heatmap = useQuery({
    queryKey: qk.stats.heatmap(heatQuery),
    queryFn: () => fetchHeatmap(heatQuery),
    placeholderData: keepPreviousData,
  });
  const managers = useQuery({
    queryKey: qk.stats.managers(query, sort, order),
    queryFn: () => fetchManagers(query, sort, order),
    placeholderData: keepPreviousData,
  });

  const refreshedAt = summary.data?.refreshed_at ?? managers.data?.refreshed_at ?? null;

  /**
   * ВСЕ сотрудники из таблицы — добавка к справочнику фильтра.
   *
   * `/users/assignable` отдаёт тех, КОМУ МОЖНО ДАТЬ ДИАЛОГ, а отчёт помнит
   * всех, У КОГО БЫЛА АКТИВНОСТЬ за период (06 §4.4). Это разные множества, и
   * разница не только в уволенных: администратор, снятый с раздачи диалогов
   * (`handles_conversations = false`), в справочник не попадает никогда — а в
   * отчёте он есть с ненулевыми числами. Замер трёх списков 15 августа: такой
   * человек виден в таблице и ОТСУТСТВУЕТ в фильтре, то есть отфильтровать
   * отчёт по нему нельзя, а клик по его строке положил бы в фильтр id без
   * опции — Mantine показал бы пилюлю с голым UUID.
   *
   * ⚠ ПОЭТОМУ КОПИТСЯ КАЖДАЯ СТРОКА, А НЕ ТОЛЬКО ОТКЛЮЧЁННЫЕ. Правило слияния
   * в `StatsFilters` при этом бережёт справочник: его подпись побеждает, и
   * дубли не появляются. Накопление, а не «текущие строки»: при активном
   * фильтре таблица ужимается до одного человека, и остальных стало бы нечем
   * дофильтровать.
   */
  const [seenManagers, setSeenManagers] = useState<Record<string, string>>({});
  useEffect(() => {
    const rows = managers.data?.rows;
    if (!rows?.length) return;
    setSeenManagers((cur) => {
      let next: Record<string, string> | null = null;
      for (const r of rows) {
        const label = r.is_active ? r.full_name : `${r.full_name} (отключён)`;
        if (cur[r.manager_id] !== label) (next ??= { ...cur })[r.manager_id] = label;
      }
      return next ?? cur;
    });
  }, [managers.data]);

  const applyPreset = (next: PeriodPreset) => {
    /*
     * ⚠ «ПРОИЗВОЛЬНЫЙ» БЫЛ ПУНКТОМ БЕЗ ПОСЛЕДСТВИЙ — и это делало свой период
     * недостижимым вообще.
     *
     * Здесь стоял `if (next === "custom") return;` с объяснением «его ставит
     * onCustomPeriod парой from/to». Но `onCustomPeriod` зовёт КАЛЕНДАРЬ, а
     * календарь рисуется только когда в адресе уже лежит `period=custom` с
     * парой дат (`StatsFilters`). Круг замкнут: чтобы открыть календарь, нужен
     * период, который задаётся только календарём. Человек выбирал
     * «Произвольный», список закрывался, подпись откатывалась к прежнему
     * пресету — нажатие пропадало бесследно.
     *
     * Заготовкой берём ТЕКУЩИЙ применённый период: календарь открывается с
     * тем, что человек и так видит на экране, и правится кликами. Выдумывать
     * другой диапазон было бы хуже — он молча подменил бы выборку.
     *
     * Соседние экраны это уже умеют: журнал уведомлений и аудит при выборе
     * «Произвольный» засевают диапазон, чтобы календарю было что показать.
     */
    if (next === "custom") {
      patch({ period: "custom", from: period.dateFrom, to: period.dateTo });
      return;
    }
    patch({ period: next === "last30" ? null : next, from: null, to: null });
  };

  /** Клик по заголовку: та же колонка — разворот порядка, другая — desc с нуля (11 §6.2). */
  const changeSort = (next: ManagersSort) => {
    if (next === sort) patch({ dir: order === "asc" ? null : "asc" });
    else patch({ sort: next === "messages_sent" ? null : next, dir: null });
  };

  /** Клик по строке фильтрует весь экран по менеджеру; повторный — снимает фильтр. */
  const selectManager = (id: string) => {
    const next = managerIds.length === 1 && managerIds[0] === id ? [] : [id];
    patch({ managers: next.join(",") || null });
  };

  /**
   * «Диалоги ↗» — уход в /chats с фильтром по сотруднику.
   *
   * ВСЕ СУЖЕНИЯ ПЕРЕЧИСЛЕНЫ ЯВНО, включая те, что снимаются (STATS-07).
   * `setFilters` сливает переданное с текущим, а фильтры чатов живут между
   * визитами. Передавался только менеджер — и поиск, тег и статус, набранные
   * в чатах час назад, молча оставались наложенными. Руководитель приходил из
   * отчёта, где у сотрудника 34 диалога, видел два и считал, что врёт отчёт.
   *
   * Аккаунт Авито, наоборот, ПЕРЕНОСИТСЯ: на /stats он выбран, и уходить с
   * отчёта по одному каналу в список по всем девяти — терять то самое
   * сужение, ради которого отчёт и смотрели.
   *
   * Период перенести НЕЧЕМ: у списка диалогов нет фильтра по датам вовсе
   * (`ConversationFilters` в shared/api/queryKeys.ts), а вкладка «Все» на
   * сервере значит «все, кроме закрытых» (`conversations.py`, `_tab_condition`).
   * Обе оговорки теперь написаны в подсказке кнопки — молчать о них нельзя,
   * а починить их отсюда невозможно: это чужие файлы (см. notes к задаче).
   */
  const openChats = (row: ManagerRow) => {
    setChatFilters({
      tab: "all",
      assigneeId: row.manager_id,
      // Имя едет вместе с идентификатором: список диалогов покажет чипом, кем
      // именно он сужен. Взять его там больше неоткуда — при пустом результате
      // строк, из которых можно было бы вычитать сотрудника, нет вовсе.
      assigneeLabel: row.full_name,
      accountId,
      q: undefined,
      tag: undefined,
      status: undefined,
      // Сужения, поставленные в чатах раньше, сюда не едут: «Без
      // ответственного» вместе с сотрудником давало пустой список, «Ждут
      // ответа» — только ждущих (проверка 24.09, тот же дефект STATS-07).
      unassigned: undefined,
      waitingOnly: undefined,
      withClosed: undefined,
    });
    navigate("/chats");
  };

  return (
    <div className="lc-page stats-page">
      <header className="stats-page__head">
        {/* Общая шапка страницы, а не свой заголовок: раздел называл себя
            20-м кеглем при 28 у настроек и 24 у разбора диалогов — три
            размера на одно и то же место в иерархии.

            ⚠ СРЕЗ ЕДЕТ В ПРАВЫЙ СЛОТ ШАПКИ, А НЕ ВТОРОЙ СТРОКОЙ ПОД НЕЙ.
            Заголовок с описанием занимал всю ширину, а справа от него пустовало
            больше половины строки; сам срез шёл под ним отдельным рядом и
            стоил 44 пикселя вертикали (контрол 32 плюс зазор 12) — на экране,
            за которым сидят смену, эти пиксели отнимались у таблицы. Второй ряд
            контролов владелец уже отклонял на соседних экранах; слот `actions`
            у общей шапки для того и есть — им пользуются «Разбор диалогов»,
            «Каналы» и «Боты».

            Ряд переносится сам, когда не помещается: на окне уже примерно 1400
            он снова встаёт второй строкой — но уже прижатым вправо, а не
            распёртым во всю ширину. */}
        <PageHeader
          title="Статистика"
          description="Обращения, скорость ответа и нагрузка по операторам"
          actions={
            <StatsFilters
              preset={preset}
              period={period}
              onPresetChange={applyPreset}
              onCustomPeriod={(p) => patch({ period: "custom", from: p.dateFrom, to: p.dateTo })}
              managerIds={managerIds}
              onManagersChange={(ids) => patch({ managers: ids.join(",") || null })}
              accountId={accountId}
              onAccountChange={(id) => patch({ account: id ?? null })}
              refreshedAt={refreshedAt}
              onExport={() => setExportOpen(true)}
              extraManagers={seenManagers}
            />
          }
        />
        {/*
          ⚠ «СЕГОДНЯ» ЗДЕСЬ МОСКОВСКОЕ, И ДО 14 АВГУСТА ОБ ЭТОМ НЕ ГОВОРИЛОСЬ
          НИЧЕГО. Замер боя: у сотрудника на часах 13 августа, пресет «Сегодня»
          показывает 12-е. Это не ошибка расчёта — все бизнес-даты статистики
          московские (06 §0.1), иначе менеджер из Владивостока и сервер считали
          бы «сегодня» по-разному. Но человек-то видит расхождение и объяснения
          ему не дают.

          Функция та же, что на «Разборе», и спрашивается у неё то же: есть ли
          вообще о чём говорить. У сотрудника с московскими часами — `null`, и
          строки нет: подпись, повторяющая очевидное, учит не читать подписи.
        */}
        {zoneNote && (
          <Text fz="xs" c="var(--lc-text-3)">
            {zoneNote}
          </Text>
        )}
      </header>

      {summary.isError ? (
        <div className="stats-error stats-error--page" role="alert">
          <Text fz="sm" c="var(--lc-text-2)">
            Не получилось загрузить статистику
          </Text>
          <Button
            variant="outline"
            size="xs"
            onClick={() => {
              void summary.refetch();
              void timeseries.refetch();
              void heatmap.refetch();
              void managers.refetch();
            }}
          >
            Повторить
          </Button>
        </div>
      ) : summary.isPending ? (
        /* Скелетон повторяет то, что приедет: семь карточек и полоса под ними.
           На восьмой карточке (столько их было раньше) ряд переносился, а полосу
           скелетон не рисовал вовсе — приезд данных дёргал страницу по высоте. */
        <div className="stats-skeleton-cards" aria-hidden="true">
          <div className="stats-cards">
            {Array.from({ length: 7 }, (_, i) => (
              <div key={i} className="stats-skeleton stats-skeleton--card" />
            ))}
          </div>
          <div className="stats-skeleton stats-skeleton--strip" />
        </div>
      ) : (
        <SummaryCards summary={summary.data} />
      )}

      <div className="stats-page__row">
        <MetricChart
          data={timeseries.data}
          isPending={timeseries.isPending}
          isError={timeseries.isError}
          onRetry={() => void timeseries.refetch()}
          metric={metric}
          onMetricChange={(m) => patch({ metric: m === "conversations_new" ? null : m })}
          group={group}
          onGroupChange={(g) => {
            /*
             * ⚠ «ПО ЧАСАМ» БЫЛО ЗАПЕРТО ПРИ КАЖДОМ ОТКРЫТИИ ЭКРАНА
             * (жалоба владельца 22.08: «не доступна статистика по часам, хотя
             * уже должна работать»).
             *
             * Период по умолчанию — тридцать дней, а почасовая группировка
             * разрешена до семи (06 §4.2, и правило это верное: год по часам —
             * это 8760 точек). Кнопка была погашена всегда, а причина жила в
             * подсказке НА ПОГАШЕННОЙ кнопке — то есть там, куда не наводят.
             *
             * Теперь это не стена, а дверь: нажатие само сжимает период до
             * последних семи дней и включает часы. Молча менять период нельзя,
             * поэтому рядом стоит тост — человек видит, что именно произошло.
             */
            if (g === "hour" && !hourAllowed) {
              patch({ period: "last7", group: "hour" });
              toast.info(
                "Период сжат до недели",
                "По часам показываем не больше семи дней — иначе точек на графике больше, чем пикселей",
              );
              return;
            }
            patch({ group: g === "day" ? null : g });
          }}
          hourAllowed={hourAllowed}
          /*
           * ⚠ ШАГ ПО СУТКАМ В ПОЧАСОВОМ РЕЖИМЕ (просьба владельца 22.08:
           * «сделай так, чтобы можно было выбирать день»).
           *
           * Неделя по часам — 168 столбиков: общая форма видна, а «что было
           * позавчера в обед» уже не вычитать. Стрелки ставят период в ОДНИ
           * сутки и шагают по ним, не трогая ни метрику, ни группировку.
           *
           * Вперёд дальше сегодняшнего дня не пускаем: пустой график завтрашних
           * суток отвечает на вопрос, которого никто не задавал.
           */
          /*
           * Подпись — тем же форматтером, что и подписи оси (`formatDayLabel`),
           * и по Москве. Здесь стояла своя сборка через `toLocaleDateString`
           * от UTC-полудня: она считает дату в зоне БРАУЗЕРА, то есть на одном
           * экране подпись графика и подпись его же оси могли разойтись на
           * сутки у сотрудника восточнее UTC+12. Все бизнес-даты статистики —
           * московские (06 §0.1), и форматтер обязан быть один.
           */
          dayLabel={
            daysInPeriod(period) === 1
              ? formatDayLabel(period.dateFrom)
              : formatPeriodLabel(period, formatDayLabel)
          }
          onDayShift={(delta) => {
            const день = dayStepTarget(period, delta, moscowToday(new Date()));
            if (!день) return;
            patch({ period: "custom", from: день, to: день, group: "hour" });
          }}
          /*
           * Кнопка гасится, а не молчит: нажатие без последствий человек читает
           * как поломку и жмёт ещё раз. Куда ведёт стрелка и горит ли она —
           * один и тот же расчёт (`lib/dayStep`), разъехаться им нечем.
           */
          dayForwardDisabled={dayForwardBlocked(period, moscowToday(new Date()))}
          onWholeWeek={() => patch({ period: "last7", group: "hour" })}
        />
        <Heatmap
          data={heatmap.data}
          isPending={heatmap.isPending}
          isError={heatmap.isError}
          onRetry={() => void heatmap.refetch()}
          managerFilterActive={managerIds.length > 0}
          /*
           * Рабочие часы приезжают в ответе summary, а нужны карте: без них
           * вопрос «сколько спроса приходит там, где дежурного нет» решался
           * памятью читателя. Часы НАСТРАИВАЕМЫЕ (FUNC-42) — печатать их в
           * разметке нельзя, старый сервер их не пришлёт вовсе, и тогда карта
           * просто остаётся без черты.
           */
          workHours={summary.data?.work_hours}
        />
      </div>

      <ManagersTable
        data={managers.data}
        isPending={managers.isPending}
        isError={managers.isError}
        onRetry={() => void managers.refetch()}
        sort={sort}
        order={order}
        onSortChange={changeSort}
        selectedManagerIds={managerIds}
        onSelectManager={selectManager}
        /* Единственное действие пустого отчёта: снять сужение по сотрудникам.
           Пишем в тот же адрес, что и фильтр в шапке, — иначе селект остался бы
           с пилюлями над отчётом, который их уже не учитывает. */
        onClearManagers={() => patch({ managers: null })}
        onOpenChats={openChats}
      />

      <ExportModal
        opened={exportOpen}
        onClose={() => setExportOpen(false)}
        period={period}
        accountId={accountId}
        managerIds={managerIds}
      />
    </div>
  );
}
