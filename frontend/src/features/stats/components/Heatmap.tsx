import { useMemo } from "react";
import { Badge, Button, Text } from "@mantine/core";
import type { HeatmapResponse } from "@/shared/api/types";
import {
  DOW_SHORT,
  HEAT_LEVELS,
  buildHeatScale,
  heatTooltip,
  isWorkHour,
  toMatrix,
  workBand,
  type WorkHours,
} from "../lib/heatmap";
import { formatHourRange } from "../lib/format";

/**
 * Тепловая карта 7×24 (11 §6.2, данные — 06 §4.3): CSS grid, без canvas и
 * библиотек. Интенсивность — по квантилям выборки (06 §3.4), оттенки от
 * `--lc-heat-1` к `--lc-heat-5` (16 §3.14) — в stats.css.
 * Фильтр по менеджеру на карту не действует: входящие менеджеру не принадлежат.
 *
 * Рабочее окно подчёркнуто на шкале часов (`workBand`): вопрос к этой карте
 * ровно один — сколько спроса приходит там, где дежурного нет.
 */
export function Heatmap({
  data,
  isPending,
  isError,
  onRetry,
  managerFilterActive = false,
  workHours,
}: {
  data?: HeatmapResponse;
  isPending: boolean;
  isError: boolean;
  onRetry(): void;
  managerFilterActive?: boolean;
  /** Рабочее окно из настроек (`work_hours` в ответе summary, FUNC-42). */
  workHours?: WorkHours;
}) {
  const grid = useMemo(() => toMatrix(data?.cells), [data]);
  const scale = useMemo(() => buildHeatScale(grid.flat()), [grid]);
  const band = workBand(workHours);
  const bandLabel = band ? formatHourRange(workHours) : null;

  return (
    <section className="lc-card stats-panel stats-heatmap" aria-label="Тепловая карта входящих">
      <header className="stats-panel__head">
        <div>
          <Text component="h2" className="stats-panel__title">
            Когда пишут клиенты
          </Text>
          <Text fz="xs" c="var(--lc-text-3)">
            Входящие сообщения, день недели × час — видно, когда нужен дежурный
          </Text>
        </div>
        {managerFilterActive && (
          <Badge size="sm" variant="light" color="gray">
            все менеджеры
          </Badge>
        )}
      </header>

      {isPending ? (
        <div className="stats-skeleton stats-skeleton--heatmap" aria-hidden="true" />
      ) : isError ? (
        <div className="stats-error" role="alert">
          <Text fz="sm" c="var(--lc-text-2)">
            Не получилось загрузить карту
          </Text>
          <Button size="xs" variant="outline" onClick={onRetry}>
            Повторить
          </Button>
        </div>
      ) : (
        <>
          <div className="heatmap" role="table" aria-label="Входящие по дням недели и часам">
            <div className="heatmap__row heatmap__row--hours" role="row" aria-hidden="true">
              <span className="heatmap__day" />
              {Array.from({ length: 24 }, (_, h) => (
                <span key={`h-${h}`} className="heatmap__hour" data-work={isWorkHour(h, workHours) || undefined}>
                  {h % 3 === 0 ? h : ""}
                </span>
              ))}
            </div>

            {/* Черта под рабочим окном — своей строкой сетки, не в ячейках
                часов: подписаны только каждые три часа, а окно обязано
                подчёркиваться сплошь. То же окно названо словами в легенде —
                на карту смотрят и мельком, и внимательно. */}
            {band && (
              <div className="heatmap__row heatmap__band" role="presentation" aria-hidden="true">
                <span />
                <i style={{ gridColumn: `${band.column} / span ${band.span}` }} />
              </div>
            )}
            {grid.map((row, dowIdx) => (
              <div key={`row-${dowIdx}`} className="heatmap__row" role="row">
                <span className="heatmap__day" role="rowheader">
                  {DOW_SHORT[dowIdx]}
                </span>
                {row.map((value, hour) => {
                  const label = heatTooltip(dowIdx + 1, hour, value);
                  return (
                    <span
                      key={`c-${dowIdx}-${hour}`}
                      className="heatmap__cell"
                      role="cell"
                      data-level={scale.level(value)}
                      title={label}
                      aria-label={label}
                    />
                  );
                })}
              </div>
            ))}
          </div>

          {/* Легенда БОЛЬШЕ НЕ `aria-hidden`: раньше в ней были только пустые
              квадраты со словами «меньше/больше», а теперь здесь единственное
              место, где рабочее окно названо числами. Спрятать его от читалки
              значило бы спрятать сам смысл карты. */}
          <div className="heatmap__legend">
            <span>меньше</span>
            {Array.from({ length: HEAT_LEVELS }, (_, i) => (
              <span
                key={i}
                className="heatmap__cell heatmap__cell--legend"
                data-level={i + 1}
                aria-hidden="true"
              />
            ))}
            <span>больше</span>
            {bandLabel && <span className="heatmap__legend-note">подчёркнуты рабочие часы {bandLabel}</span>}
          </div>
        </>
      )}
    </section>
  );
}
