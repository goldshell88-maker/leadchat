import { useMemo, useState } from "react";
import { Button, Select } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import "dayjs/locale/ru";
import { DateRangeInput } from "@/shared/ui/DateRangeInput";
import { EmptyState } from "@/shared/ui/EmptyState";
import { qk, type AuditFilters } from "@/shared/api/queryKeys";
import type { AuditFilterOptions, AuditLogEntry } from "@/shared/api/types";
import { formatClock, formatDate, moscowZoneHint, zonedTimeTitle } from "@/shared/lib/formatTime";
import { moscowToday, presetPeriod, shiftDate } from "@/shared/lib/period";
import { AUDIT_PAGE_SIZE, fetchAuditFilterOptions, fetchAuditLog } from "./api";
import { Pager } from "./Pager";
import { auditEntityLabel } from "./auditEntities";

/** Пресеты периода журнала: «Всё время» — без границ по датам. */
type AuditPreset = "all" | "today" | "last7" | "last30" | "custom";

const PRESET_OPTIONS: Array<{ value: AuditPreset; label: string }> = [
  { value: "all", label: "Всё время" },
  { value: "today", label: "Сегодня" },
  { value: "last7", label: "7 дней" },
  { value: "last30", label: "30 дней" },
  { value: "custom", label: "Произвольный" },
];

function presetToRange(preset: AuditPreset): { dateFrom?: string; dateTo?: string } {
  if (preset === "all" || preset === "custom") return {};
  const p = presetPeriod(preset);
  return { dateFrom: p.dateFrom, dateTo: p.dateTo };
}

type Option = { value: string; label: string };

const ACTOR_SUFFIX: Record<AuditFilterOptions["actors"][number]["state"], string> = {
  active: "",
  inactive: " (отключён)",
  deleted: " (удалён)",
};

function sortedOptions(labels: Map<string, string>, picked: Option | null): Option[] {
  // Выбранный пункт остаётся в списке, даже когда источник его больше не
  // содержит (справочник не пришёл, страница опустела): иначе фильтр действует,
  // а поле показывает «все».
  if (picked && !labels.has(picked.value)) labels.set(picked.value, picked.label);
  return [...labels.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label, "ru"));
}

/** Строка таблицы: детали (JSONB) раскрываются по клику — read-only журнал. */
function AuditRow({ entry, actionLabel }: { entry: AuditLogEntry; actionLabel: string }) {
  const [open, setOpen] = useState(false);
  const hasDetails = Boolean(entry.details && Object.keys(entry.details).length > 0);
  const title = entry.description || actionLabel;

  return (
    <>
      <tr>
        {/*
          ⚠ НА ЭТОМ ЭКРАНЕ ДВА ПОЯСА, И ДО 14 АВГУСТА ОБ ЭТОМ НЕ ГОВОРИЛОСЬ
          НИЧЕГО. Строка печатается местным временем, а период над таблицей
          режется по МОСКОВСКИМ суткам — и на сервере тоже (`audit.py`, MSK).
          На UTC+10 запись, показанная как «14.08 02:30», попадает в московское
          13-е, то есть в выборку «вчера»: человек ищет её в «сегодня» и не
          находит, а объяснить это по экрану нечем.

          Лечится подписью, а не сменой пояса: пояс строки местный намеренно
          («когда это было по моим часам»), пояс периода московский намеренно
          («сутки одни на всю компанию»). Подсказка называет оба — то самое
          правило, что записано у `zonedTimeTitle`.
        */}
        <td className="audit__time" data-label="Время" title={zonedTimeTitle(entry.created_at)}>
          {formatDate(entry.created_at)} {formatClock(entry.created_at)}
        </td>
        <td data-label="Сотрудник">
          {entry.user?.full_name ?? <span className="audit__system">система</span>}
        </td>
        {/* Подпись считает бэкенд. Класс — не украшение: из пяти ячеек строки
            ищут именно эту, и только она набрана средним весом (team.css). */}
        <td className="audit__action" data-label="Действие">
          {title}
        </td>
        <td className="audit__entity" data-label="Объект">
          {auditEntityLabel(entry.entity)}
          {entry.entity_id && <span className="audit__id">{entry.entity_id.slice(0, 8)}</span>}
        </td>
        {/* Подписи «Детали» у ячейки нет намеренно: в карточном режиме она
            встала бы рядом со словом «детали» на самой кнопке. Ячейка без
            `data-label` занимает всю ширину карточки (lc-table-cards.css). */}
        <td>
          {hasDetails ? (
            /*
              Имя кнопки называет ЗАПИСЬ, а не только действие. В журнале таких
              кнопок пятьдесят на страницу, и подряд они звучали как «детали,
              детали, детали» — из списка ссылок программы чтения с экрана было
              не выбрать нужную. Видимая подпись осталась короткой: глазу
              строка и так известна, он видит её слева.
            */
            <button
              type="button"
              className="audit__toggle"
              aria-expanded={open}
              aria-label={`${open ? "Скрыть" : "Показать"} детали: ${title}`}
              onClick={() => setOpen((v) => !v)}
            >
              {open ? "скрыть" : "детали"}
            </button>
          ) : (
            <span className="audit__system">—</span>
          )}
        </td>
      </tr>
      {open && hasDetails && (
        <tr className="audit__details-row">
          <td colSpan={5}>
            <pre className="audit__details">{JSON.stringify(entry.details, null, 2)}</pre>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * Вкладка «Журнал аудита» (11 §4.2, данные — 01 §9.7). Право `audit:read`:
 * admin и head, только чтение. Фильтры — период, сотрудник, действие; страницы
 * по 50 записей (offset-пагинация 01 §1.4).
 */
export function AuditLogTab() {
  const [preset, setPreset] = useState<AuditPreset>("last7");
  const [custom, setCustom] = useState<{ dateFrom?: string; dateTo?: string }>({});
  const [user, setUser] = useState<Option | null>(null);
  const [action, setAction] = useState<Option | null>(null);
  const [offset, setOffset] = useState(0);

  const range = preset === "custom" ? custom : presetToRange(preset);
  const filters: AuditFilters = {
    ...range,
    userId: user?.value,
    action: action?.value,
    offset,
  };

  const q = useQuery({ queryKey: qk.auditLog(filters), queryFn: () => fetchAuditLog(filters) });
  // Пункты фильтров — с сервера: весь реестр действий и все сотрудники, включая
  // отключённых и удалённых (их действия журнал хранит). Раньше «Действие»
  // бралось из своего словаря на 23 пункта, и из 72 действий журнала 50 выбрать
  // было нельзя — все `settings.*` в том числе; «Сотрудник» — из назначаемых
  // операторов, без руководителя и ушедших (проверка 24.09).
  const options = useQuery({
    queryKey: qk.auditFilterOptions,
    queryFn: fetchAuditFilterOptions,
    staleTime: 5 * 60_000,
  });

  const actionLabels = useMemo(
    () => new Map((options.data?.actions ?? []).map((a) => [a.action, a.label])),
    [options.data],
  );

  const userOptions = useMemo(() => {
    const labels = new Map<string, string>();
    for (const u of options.data?.actors ?? []) labels.set(u.id, u.full_name + ACTOR_SUFFIX[u.state]);
    for (const row of q.data?.items ?? []) {
      if (row.user && !labels.has(row.user.id)) labels.set(row.user.id, row.user.full_name);
    }
    return sortedOptions(labels, user);
  }, [options.data, q.data, user]);

  const actionOptions = useMemo(() => {
    const labels = new Map(actionLabels);
    // Действие вне реестра (переименованное, записанное мимо него) видно по
    // строкам страницы — под своим именем, как его и показывает журнал.
    for (const row of q.data?.items ?? []) if (!labels.has(row.action)) labels.set(row.action, row.action);
    return sortedOptions(labels, action);
  }, [actionLabels, q.data, action]);

  // Пояс периода: сутки фильтра московские (сервер режет по MSK), а время в
  // строках местное. Молчать о двух поясах на одном экране нельзя.
  const zoneNote = useMemo(
    () => moscowZoneHint(new Date(), undefined, "Период считается по московским суткам"),
    [],
  );

  const resetPaging = () => setOffset(0);
  const total = q.data?.page.total ?? 0;
  const shown = q.data?.items.length ?? 0;

  return (
    <section className="audit" aria-label="Журнал аудита">
      <div className="audit__filters">
        <Select
          size="xs"
          w={150}
          aria-label="Период журнала"
          data={PRESET_OPTIONS}
          value={preset}
          allowDeselect={false}
          onChange={(v) => {
            if (!v) return;
            setPreset(v as AuditPreset);
            if (v === "custom" && !custom.dateFrom) {
              setCustom({ dateFrom: shiftDate(moscowToday(), -6), dateTo: moscowToday() });
            }
            resetPaging();
          }}
        />

        {preset === "custom" && (
          <DateRangeInput
            ariaLabel="Произвольный период журнала"
            from={custom.dateFrom}
            to={custom.dateTo}
            onChange={(dateFrom, dateTo) => {
              setCustom({ dateFrom, dateTo });
              resetPaging();
            }}
          />
        )}

        <Select
          size="xs"
          w={220}
          aria-label="Сотрудник"
          placeholder="Сотрудник: все"
          clearable
          searchable
          data={userOptions}
          value={user?.value ?? null}
          onChange={(v, option) => {
            setUser(v ? { value: v, label: option.label } : null);
            resetPaging();
          }}
        />

        <Select
          size="xs"
          w={260}
          aria-label="Действие"
          placeholder="Действие: любое"
          clearable
          searchable
          data={actionOptions}
          value={action?.value ?? null}
          onChange={(v, option) => {
            setAction(v ? { value: v, label: option.label } : null);
            resetPaging();
          }}
        />
      </div>

      {/*
        Пояс периода назван — вторая половина той же починки, что подпись у
        времени строки. Показывается только тем, у кого часы и правда расходятся
        с московскими: подпись, повторяющая очевидное, учит не читать подписи.
      */}
      {zoneNote && (
        <p className="audit__zone-note">{zoneNote}</p>
      )}

      {q.isPending ? (
        <div className="audit__skeleton" aria-hidden="true">
          {Array.from({ length: 8 }, (_, i) => (
            <span key={i} className="audit__skeleton-line" />
          ))}
        </div>
      ) : q.isError ? (
        <EmptyState
          illustration="error"
          title="Не получилось загрузить журнал"
          description="Нажмите «Повторить». Если не помогает — обновите страницу"
          live="alert"
          action={
            <Button size="xs" variant="outline" onClick={() => void q.refetch()}>
              Повторить
            </Button>
          }
        />
      ) : shown === 0 ? (
        <EmptyState illustration="archive" title="За выбранный период записей нет" />
      ) : (
        <>
          <div className="audit__scroll lc-scroll-x">
            {/*
              ⚠ НА УЗКОМ ЭКРАНЕ СТРОКА — КАРТОЧКА (lc-table-cards.css, порог 900).
              Пяти колонок в 375 пикселях не бывает: замер на стенде показал 324
              пикселя, уехавших вправо, — то есть «Объект» и кнопка «детали»
              недостижимы, пока человек не догадается прокрутить таблицу вбок.
              Соседняя таблица сотрудников живёт по этому рецепту с 12 августа,
              журнал остался единственной таблицей раздела без него.
            */}
            <table className="lc-table audit__table lc-table--cards">
              <thead>
                <tr>
                  <th scope="col">Время</th>
                  <th scope="col">Сотрудник</th>
                  <th scope="col">Действие</th>
                  <th scope="col">Объект</th>
                  <th scope="col">Детали</th>
                </tr>
              </thead>
              <tbody>
                {q.data.items.map((entry) => (
                  <AuditRow
                    key={entry.id}
                    entry={entry}
                    actionLabel={actionLabels.get(entry.action) ?? entry.action}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Подвал снаружи ветки со списком — по той же причине, что и в таблице
          сотрудников: с опустевшей страницы должен быть выход. */}
      {!q.isPending && !q.isError && (
        <Pager
          offset={offset}
          shown={shown}
          total={total}
          pageSize={AUDIT_PAGE_SIZE}
          noun={["запись", "записи", "записей"]}
          onOffset={setOffset}
        />
      )}
    </section>
  );
}
