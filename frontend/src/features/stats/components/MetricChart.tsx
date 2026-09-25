import { useMemo, useState } from "react";
import { Button, Select, Text, Tooltip } from "@mantine/core";
import type { TimeseriesGroup, TimeseriesMetric, TimeseriesResponse } from "@/shared/api/types";
import { useInlineSize } from "@/shared/lib/useInlineSize";
import { formatDuration, formatDurationAxis, formatNumber, formatPointLabel } from "../lib/format";
import { labelIndexes, labelWidth } from "../lib/axisLabels";

/**
 * График рядов (11 §6.2, данные — 06 §4.2). Своя SVG-отрисовка вместо
 * charting-библиотеки: бюджет бандла ≤ 300 КБ gzip, а нужны ровно два вида —
 * столбцы для счётчиков и линия для медианы времени ответа (у неё пустой день —
 * РАЗРЫВ линии: null ≠ 0).
 */

const METRIC_LABELS: Record<TimeseriesMetric, string> = {
  conversations_new: "Новые диалоги",
  conversations_closed: "Закрыто диалогов",
  messages_in: "Входящие сообщения",
  messages_out: "Исходящие сообщения",
  frt_operator_median: "Первый ответ (медиана)",
  phones_collected: "Собрано телефонов",
};

const DURATION_METRICS: ReadonlySet<TimeseriesMetric> = new Set(["frt_operator_median"]);

/**
 * Оговорки к тем метрикам, у которых сумма столбиков законно не совпадает с
 * одноимённой карточкой наверху.
 *
 * ⚠ ЭТО НЕ ОШИБКА СЧЁТА, А СВОЙСТВО «УНИКАЛЬНЫХ В ГРАНИЦАХ» (разбор 08.09).
 * Столбик считает уникальные диалоги ВНУТРИ ДНЯ, карточка — внутри всего
 * периода. Диалог, закрытый в понедельник, переоткрытый и закрытый в среду,
 * попадёт в оба столбика и один раз в карточку. Сложив столбики и сравнив с
 * карточкой, читатель видит расхождение и читает его как ошибку — ровно та же
 * беда, что была у «ИТОГО» в таблице людей.
 */
const METRIC_NOTES: Partial<Record<TimeseriesMetric, string>> = {
  conversations_closed:
    "Столбик — уникальные диалоги за день. Закрытый дважды в разные дни попадёт в оба столбика, " +
    "а в карточку «Закрыто» — один раз, поэтому сумма столбиков бывает больше",
};

/**
 * ⚠ ШИРИНА СИСТЕМЫ КООРДИНАТ РАВНА ФАКТИЧЕСКОЙ ШИРИНЕ БЛОКА (правка 08.09).
 *
 * Было: `viewBox="0 0 720 240"` при `width: 100%` — картинка растягивалась
 * ЦЕЛИКОМ, вместе с кеглем подписей. Замер на стенде: окно 1180 — SVG 880×293
 * и подпись 15,9 пикселя, окно 1181 — 482×161 и подпись 8,7. Один пиксель
 * ширины ужимал график почти вдвое, и на ноутбуках 1181–1366 даты на оси были
 * нечитаемы.
 *
 * Теперь ширина меряется (`useInlineSize`) и подставляется в `viewBox`:
 * масштаб всегда 1:1, единица координат равна пикселю, кегль подписи равен
 * своему `--lc-fz-note`. Высоту `H` держим постоянной — она и была
 * постоянной по замыслу (заготовка `.stats-skeleton--chart` обещает 240), и
 * тянуть график по вертикали не за чем.
 *
 * `W_FALLBACK` — ширина до первого замера и там, где мерить нечем (jsdom в
 * сторожах, браузер без `ResizeObserver`). Прежние 720: график в этом случае
 * ведёт себя ровно как раньше.
 */
const W_FALLBACK = 720;
const H = 240;
const PAD = { top: 12, right: 12, bottom: 28, left: 48 };
const PLOT_H = H - PAD.top - PAD.bottom;

/**
 * Потолок ширины столбика.
 *
 * Ширина берётся долей от колонки, а колонка — это всё поле построения,
 * делённое на число точек. При ОДНОЙ точке делить не на что: 62% от 660px дают
 * плиту в 409px, и график перестаёт читаться как график (проверка боя 14
 * августа, период в один день с группировкой «по дням»).
 *
 * 48px — ширина, при которой столбик ещё столбик: примерно вдвое шире
 * обычного при недельном периоде и заметно уже подписи под ним. Хитбоксы
 * потолка НЕ получают: попадать мышью человек должен во всю колонку.
 */
const BAR_MAX_W = 48;

/**
 * «Красивый» потолок оси: множитель × 10^n — подписи читаемые.
 *
 * ⚠ У СЧЁТНЫХ МЕТРИК НАБОР МНОЖИТЕЛЕЙ ДРУГОЙ (находка 23.08 №22). Делений на
 * оси три — `[0, top/2, top]`, — и при множителях 1/2/2.5/5/10 середина
 * оказывалась дробной: три диалога за час давали потолок 5 и подпись «2,5» под
 * меткой «диалоги». Половины диалога не бывает; ось, показывающая её, врёт о
 * самой природе величины.
 *
 * Для счётных берём только ЧЁТНЫЕ множители — тогда `top/2` целое при любом
 * `10^n`. Потолок от этого не грубеет: для трёх это 4, а не 5, то есть шкала
 * даже плотнее. Длительностям дробная середина не мешает («2 м 30 с» — обычное
 * время), там набор прежний.
 */
function niceCeil(v: number, целые = false): number {
  if (!(v > 0)) return целые ? 2 : 1;
  const pow = 10 ** Math.floor(Math.log10(v));
  for (const m of целые ? [2, 4, 10] : [1, 2, 2.5, 5, 10]) {
    if (v <= m * pow) return m * pow;
  }
  return 10 * pow;
}

export interface MetricChartProps {
  data?: TimeseriesResponse;
  isPending: boolean;
  isError: boolean;
  onRetry(): void;
  metric: TimeseriesMetric;
  onMetricChange(m: TimeseriesMetric): void;
  group: TimeseriesGroup;
  onGroupChange(g: TimeseriesGroup): void;
  /** «По часам» разрешено только при периоде ≤ 7 дней (06 §4.2) — иначе 400. */
  hourAllowed: boolean;
  /*
   * ⚠ ВЫБОР ДНЯ В ПОЧАСОВОМ РЕЖИМЕ (просьба владельца 22.08).
   *
   * Неделя по часам — это 168 столбиков: общая форма видна, а «что было
   * позавчера в обед» из неё уже не вычитать. Стрелки переставляют период на
   * одни сутки, не трогая ни метрику, ни группировку, — тот же приём, что у
   * пресетов «сегодня» и «вчера», только шагом.
   *
   * Показываются ТОЛЬКО в почасовом режиме: по дням шаг в сутки бессмыслен,
   * там для этого есть пресеты периода.
   */
  dayLabel?: string;
  onDayShift?(delta: number): void;
  onWholeWeek?(): void;
  /**
   * Вперёд идти некуда — показанный день и есть сегодняшний.
   *
   * Кнопка гасится, а не молча ничего не делает: нажатие без последствий
   * человек читает как поломку и жмёт ещё раз. Границу знает StatsPage —
   * «сегодня» здесь московское, и графику о нём знать незачем.
   */
  dayForwardDisabled?: boolean;
}

export function MetricChart({
  data,
  dayLabel,
  onDayShift,
  onWholeWeek,
  dayForwardDisabled,
  isPending,
  isError,
  onRetry,
  metric,
  onMetricChange,
  group,
  onGroupChange,
  hourAllowed,
}: MetricChartProps) {
  const [hovered, setHovered] = useState<number | null>(null);
  /*
   * Меряем блок графика, а не окно: окно содержимому не достаётся целиком —
   * слева рельса приложения (218 развёрнутая, 72 свёрнутая), да и колонок в
   * раскладке экрана то одна, то две. Ширина блока — единственная величина,
   * которая знает обо всём этом сразу.
   */
  const [plotRef, plotWidth] = useInlineSize();
  const W = plotWidth !== null && plotWidth > 0 ? plotWidth : W_FALLBACK;
  /* Нижняя граница не декоративная: в кадр, когда блок ещё нулевой ширины
     (свёрнутая панель, первый замер), поле построения ушло бы в минус, и
     столбики уехали бы за левый край вместо того, чтобы не нарисоваться. */
  const полеПостроения = Math.max(1, W - PAD.left - PAD.right);
  /*
   * ⚠ ПОДПИСИ ТОЧЕК — ПО ГРУППИРОВКЕ САМОГО РЯДА, А НЕ ПО ПОЛОЖЕНИЮ
   * ПЕРЕКЛЮЧАТЕЛЯ.
   *
   * Пока едет новый срез, на экране остаётся прежний (`keepPreviousData` в
   * StatsPage) — иначе график гас в скелетон на каждое нажатие. Значит
   * переключатель уже стоит на «по часам», а точки на экране ещё дневные. Верь
   * мы переключателю, `formatPointLabel` получил бы дневную метку «2026-08-22»
   * с указанием разобрать её как часовую: `split("T")` не находит времени, и
   * под осью появляется «22 авг., » с висящей запятой.
   *
   * Переключателю по-прежнему верят КНОПКИ — они показывают выбор человека, а
   * не содержимое ответа.
   */
  const dataGroup = data?.group ?? group;
  const isDuration = DURATION_METRICS.has(metric);
  const formatValue = (v: number | null) =>
    v === null ? "нет данных" : isDuration ? formatDuration(v) : formatNumber(v);

  const points = useMemo(() => data?.points ?? [], [data]);
  const geometry = useMemo(() => {
    const values = points.map((p) => p.value);
    const maxValue = values.reduce<number>((m, v) => (typeof v === "number" && v > m ? v : m), 0);
    const top = niceCeil(maxValue, !isDuration);
    const n = points.length;
    const colW = n > 0 ? полеПостроения / n : полеПостроения;
    const y = (v: number) => PAD.top + PLOT_H - (v / top) * PLOT_H;
    const cx = (i: number) => PAD.left + colW * (i + 0.5);
    /*
     * ⚠ «ДАННЫХ НЕТ» И «ДАННЫЕ НУЛЕВЫЕ» — РАЗНЫЕ ВЕЩИ (28.08).
     *
     * Здесь стояло `values.some((v) => v > 0)`, и по этому признаку пряталась
     * вся разметка оси и печаталось «Нет данных за выбранный период». Но сервер
     * для СЧЁТНЫХ метрик отдаёт сплошной занулённый ряд: `stats.py` —
     * `empty = None if metric in _NULLABLE_METRICS else 0`, то есть пустых точек
     * у «новых диалогов», «закрытых», «сообщений» и «собранных телефонов» не
     * бывает вовсе. Ветка, ради которой признак и писался, у счётчиков
     * недостижима, а срабатывала она ровно в обратном случае — когда все
     * значения настоящие нули.
     *
     * Как это выглядело: руководитель выбирает канал, по которому за месяц не
     * было ни одного обращения (именно это он и проверяет — мёртвый канал).
     * Карточка показывает 0, а график в двух сантиметрах ниже — «Нет данных за
     * выбранный период», без оси и без нулевой линии. Два разных утверждения об
     * одном и том же: он читает график как сбой отчёта, меняет период и уходит,
     * так и не увидев факта «канал молчит месяц».
     *
     * Спрашиваем то, что имели в виду: есть ли РЯД. Ось при сплошных нулях
     * рисуется без деления на ноль — `niceCeil(0)` возвращает 2.
     */
    const естьРяд = values.some((v) => typeof v === "number");
    return { top, n, colW, y, cx, hasData: естьРяд };
  }, [points, isDuration, полеПостроения]);

  const ticks = [0, geometry.top / 2, geometry.top];
  /*
   * Образец подписи — САМАЯ ДЛИННАЯ из настоящих, а не последняя.
   *
   * Порог наложения считается по одному образцу на всю ось, и взятая наугад
   * подпись его занижает: «1 авг., 06:00» короче «16 сент., 06:00» на два
   * знака, то есть на тринадцать единиц viewBox. Период, начавшийся в
   * однозначный день или задевший месяц с длинным сокращением, получал зазор
   * под короткую подпись, а рисовал длинную — и она налезала на соседку.
   *
   * Перебор дешёвый: точек не больше 168 (неделя по часам), считается один раз
   * на смену ряда.
   */
  const sampleLabel = useMemo(
    () =>
      points.reduce((longest, p) => {
        const label = formatPointLabel(p.ts, dataGroup);
        return label.length > longest.length ? label : longest;
      }, ""),
    [points, dataGroup],
  );
  const xLabels = labelIndexes(geometry.n, 8, sampleLabel, полеПостроения);

  /** Линия рвётся на null (06 §4.2: «нет данных» ≠ ноль). */
  const segments = useMemo(() => {
    const out: Array<Array<{ x: number; y: number }>> = [];
    let current: Array<{ x: number; y: number }> = [];
    points.forEach((p, i) => {
      if (p.value === null || p.value === undefined) {
        if (current.length) out.push(current);
        current = [];
        return;
      }
      current.push({ x: geometry.cx(i), y: geometry.y(p.value) });
    });
    if (current.length) out.push(current);
    return out;
  }, [points, geometry]);

  const hoveredPoint = hovered !== null ? points[hovered] : undefined;

  return (
    <section className="lc-card stats-panel stats-chart" aria-label="График метрики">
      <header className="stats-panel__head">
        <Text component="h2" className="stats-panel__title">
          График
        </Text>
        <div className="stats-chart__controls">
          <Select
            size="xs"
            w={210}
            aria-label="Метрика графика"
            data={(Object.keys(METRIC_LABELS) as TimeseriesMetric[]).map((value) => ({
              value,
              label: METRIC_LABELS[value],
            }))}
            value={metric}
            allowDeselect={false}
            onChange={(v) => v && onMetricChange(v as TimeseriesMetric)}
          />
          {group === "hour" && onDayShift && (
            <div className="chart-day" role="group" aria-label="Выбор дня">
              {/* Стрелки и подпись — одна сегментированная группа, как «по дням /
                  по часам» рядом: три висящих в воздухе прямоугольника не
                  читались бы как один переключатель. */}
              <div className="chart-toggle">
                <button
                  type="button"
                  className="chart-toggle__btn"
                  aria-label="Предыдущий день"
                  onClick={() => onDayShift(-1)}
                >
                  ‹
                </button>
                <span className="chart-day__label">{dayLabel || "день"}</span>
                <button
                  type="button"
                  className="chart-toggle__btn"
                  aria-label="Следующий день"
                  disabled={dayForwardDisabled}
                  title={dayForwardDisabled ? "Это последний день с данными" : undefined}
                  onClick={() => onDayShift(1)}
                >
                  ›
                </button>
              </div>
              {onWholeWeek && (
                <button
                  type="button"
                  className="chart-toggle__btn chart-day__all"
                  onClick={onWholeWeek}
                  title="Показать всю неделю по часам"
                >
                  неделя
                </button>
              )}
            </div>
          )}
          <div className="chart-toggle" role="group" aria-label="Группировка">
            <button
              type="button"
              className="chart-toggle__btn"
              data-active={group === "day" || undefined}
              aria-pressed={group === "day"}
              onClick={() => onGroupChange("day")}
            >
              по дням
            </button>
            <Tooltip
              label="Период сожмётся до последней недели: по часам показываем не больше семи дней"
              disabled={hourAllowed}
              withArrow
              position="bottom"
            >
              <span className="chart-toggle__wrap">
                <button
                  type="button"
                  className="chart-toggle__btn"
                  data-active={group === "hour" || undefined}
                  aria-pressed={group === "hour"}
                  onClick={() => onGroupChange("hour")}
                >
                  по часам
                  {!hourAllowed && <span className="chart-toggle__hint"> · неделя</span>}
                </button>
              </span>
            </Tooltip>
          </div>
        </div>
      </header>

      {isPending ? (
        <div className="stats-skeleton stats-skeleton--chart" aria-hidden="true" />
      ) : isError ? (
        <div className="stats-error" role="alert">
          <Text fz="sm" c="var(--lc-text-2)">
            Не получилось загрузить график
          </Text>
          <Button size="xs" variant="outline" onClick={onRetry}>
            Повторить
          </Button>
        </div>
      ) : (
        <div className="stats-chart__plot" ref={plotRef}>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            className="stats-chart__svg"
            role="img"
            aria-label={`${METRIC_LABELS[metric]}, группировка ${group === "day" ? "по дням" : "по часам"}`}
            onMouseLeave={() => setHovered(null)}
          >
            {/*
              Сетка и подписи оси рисуются, только когда есть что показывать
              (аудит 7 августа, docs/19). Пустой график с размеченной осью
              0 / 0,5 / 1 и пунктиром по нулю читается как «данные есть, и они
              нулевые» — то есть ровно наоборот к правде «данных нет вовсе».
              Разница существенная: в первом случае руководитель делает вывод
              о работе команды, во втором — понимает, что надо сменить период.
            */}
            {geometry.hasData &&
              ticks.map((t) => (
              <g key={t}>
                <line
                  x1={PAD.left}
                  x2={W - PAD.right}
                  y1={geometry.y(t)}
                  y2={geometry.y(t)}
                  className="chart-grid"
                />
                <text x={PAD.left - 8} y={geometry.y(t) + 4} className="chart-axis-label" textAnchor="end">
                  {isDuration ? formatDurationAxis(t) : formatNumber(t)}
                </text>
              </g>
            ))}

            {isDuration
              ? segments.map((seg, i) => (
                  <polyline
                    key={i}
                    className="chart-line"
                    points={seg.map((p) => `${p.x},${p.y}`).join(" ")}
                    fill="none"
                  />
                ))
              : points.map((p, i) => {
                  const v = p.value ?? 0;
                  // Потолок ширины: при одной точке (период в один день) колонка
                  // получала всё поле построения, и 62% от него — плита в 409px
                  // поперёк экрана. Столбик перестаёт читаться как столбик.
                  const barW = Math.min(BAR_MAX_W, Math.max(2, geometry.colW * 0.62));
                  const top = geometry.y(v);
                  return (
                    <rect
                      key={p.ts}
                      className="chart-bar"
                      data-hovered={hovered === i || undefined}
                      x={geometry.cx(i) - barW / 2}
                      y={v > 0 ? top : PAD.top + PLOT_H - 1}
                      width={barW}
                      height={v > 0 ? Math.max(1, PAD.top + PLOT_H - top) : 1}
                      rx={2}
                    />
                  );
                })}

            {isDuration &&
              points.map((p, i) =>
                p.value === null || p.value === undefined ? null : (
                  <circle
                    key={p.ts}
                    className="chart-dot"
                    data-hovered={hovered === i || undefined}
                    cx={geometry.cx(i)}
                    cy={geometry.y(p.value)}
                    r={hovered === i ? 4 : 2.5}
                  />
                ),
              )}

            {/* Прозрачные колонки-хитбоксы: наведение по всей высоте, не только по столбику. */}
            {points.map((p, i) => (
              <rect
                key={`hit-${p.ts}`}
                className="chart-hit"
                x={PAD.left + geometry.colW * i}
                y={PAD.top}
                width={geometry.colW}
                height={PLOT_H}
                onMouseEnter={() => setHovered(i)}
                aria-label={`${formatPointLabel(p.ts, dataGroup)} — ${formatValue(p.value ?? null)}`}
              />
            ))}

            {points.map((p, i) =>
              xLabels.has(i) ? (
                <text
                  key={`lbl-${p.ts}`}
                  x={geometry.cx(i)}
                  y={H - 8}
                  className="chart-axis-label"
                  /*
                   * Крайние подписи прижимаются внутрь, а не центрируются.
                   *
                   * По центру половина подписи уходит за границу viewBox и
                   * срезается: в часовом режиме правая дата «8 авг 14:00»
                   * теряла хвост. Полстроки за краем — это не «немного
                   * обрезано», это неверная дата на экране.
                   */
                  textAnchor={
                    geometry.cx(i) + labelWidth(sampleLabel) / 2 > W - PAD.right
                      ? "end"
                      : geometry.cx(i) - labelWidth(sampleLabel) / 2 < PAD.left
                        ? "start"
                        : "middle"
                  }
                >
                  {formatPointLabel(p.ts, dataGroup)}
                </text>
              ) : null,
            )}
          </svg>

          {hoveredPoint && (
            <div
              className="chart-tooltip"
              role="status"
              style={{ left: `${(geometry.cx(hovered ?? 0) / W) * 100}%` }}
            >
              <span className="chart-tooltip__ts">{formatPointLabel(hoveredPoint.ts, dataGroup)}</span>
              <span className="chart-tooltip__value">{formatValue(hoveredPoint.value ?? null)}</span>
            </div>
          )}

          {!geometry.hasData && (
            <Text className="stats-chart__empty" fz="sm" c="var(--lc-text-3)">
              Нет данных за выбранный период
            </Text>
          )}
        </div>
      )}

      {/*
        Оговорка под графиком, а не в подсказке у заголовка: вопрос «почему
        сумма столбиков не равна карточке» возникает, когда человек СМОТРИТ на
        столбики, — ответ должен быть там же. Показывается только у тех
        метрик, где расхождение законно и объяснимо.
      */}
      {METRIC_NOTES[metric] && (
        <Text component="p" fz="xs" c="var(--lc-text-3)" className="stats-chart__note">
          {METRIC_NOTES[metric]}
        </Text>
      )}
    </section>
  );
}
