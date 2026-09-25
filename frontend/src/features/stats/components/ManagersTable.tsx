import { Button, Text, Tooltip } from "@mantine/core";
import type { ManagerRow, ManagersResponse, ManagersSort, SortOrder } from "@/shared/api/types";
import { formatDuration, formatNumber } from "../lib/format";
import { IconForward } from "@/shared/ui/Icon";

/**
 * Таблица по менеджерам (11 §6.2, данные — 06 §4.4). Сортировка СЕРВЕРНАЯ
 * (`sort`/`order` уходят в запрос), пагинации нет — менеджеров десятки.
 * Строка «Итого» приходит с сервера: медиана суммы ≠ сумма медиан.
 * Клик по строке фильтрует весь экран по этому менеджеру.
 */

interface Column {
  key: ManagersSort | "full_name";
  label: string;
  /**
   * Расшифровка в подсказке. Нужна там, где короткая подпись колонки не
   * объясняет, что именно посчитано: «Первый ответ» и «В рабочие часы»
   * различаются не единицами, а тем, какие часы попали в расчёт.
   */
  hint?: string;
  sortable: boolean;
  numeric: boolean;
  render(row: ManagerRow): string;
  total(t: ManagersResponse["totals"]): string;
}

const COLUMNS: Column[] = [
  {
    key: "full_name",
    label: "Менеджер",
    sortable: false,
    numeric: false,
    render: (r) => r.full_name,
    total: () => "ИТОГО",
  },
  {
    key: "taken",
    label: "Принято",
    /*
     * Подписи у «Принято» и «Закрыто» появились по разбору отчёта тестирования
     * (п. 24): «Принято 19» при «Закрыто 21» читалось как ошибка счёта, а это
     * два разных определения, привязанных к разным событиям. Диалог, взятый до
     * начала периода и закрытый внутри него, в «Принято» не попадает вовсе —
     * закрытых законно бывает больше. Механизм подсказки тот же, что у FRT.
     */
    hint:
      "Диалоги, назначенные на человека в выбранный период. Взятые до его начала сюда не попадают. В ИТОГО переданный диалог считается один раз, поэтому сумма колонки может быть больше итога",
    sortable: true,
    numeric: true,
    render: (r) => formatNumber(r.taken),
    total: (t) => formatNumber(t.taken),
  },
  {
    key: "answered",
    /*
     * ⚠ БЫЛО «ОТВЕЧЕНО» — ТЕМ ЖЕ СЛОВОМ, ЧТО И КАРТОЧКА НАВЕРХУ, ПРИ ДРУГОМ
     * ЧИСЛЕ (аудит 08.09, H-03: на одном экране 29 757 и 10 006). Числа обоих
     * верны, но считают они РАЗНОЕ, и одинаковая подпись превращала это в
     * противоречие.
     *
     * Карточка считает диалоги, где оператор ответил хоть раз, — кто бы он ни
     * был. Здесь — диалоги, где ИЗВЕСТНО, КТО ответил первым: колонка привязана
     * к человеку, а без имени привязывать не к кому.
     *
     * ⚠ ОТКУДА БЕРУТСЯ БЕЗЫМЯННЫЕ ОТВЕТЫ, И ЭТО ГЛАВНОЕ. Замер по бою 08.09: у
     * 19 751 отвеченного диалога из 29 757 автор первого ответа не записан —
     * потому что отвечали не из LeadChat, а в самом приложении Авито, и имени
     * там взять негде. Отсев по ролям, на который можно было подумать, стоит
     * РОВНО НОЛЬ строк — проверено запросом.
     *
     * Разрыв исторический и быстро тает: доля исходящих без автора по месяцам —
     * июнь 100 %, июль 100 %, август 81,7 %, сентябрь 0,6 %. На свежих периодах
     * два числа почти сходятся, на «всём времени» расходятся вдвое.
     */
    label: "Ответил первым",
    hint:
      "Диалоги, где этот человек ответил клиенту первым, — даже если сейчас диалог ведёт кто-то другой. Ответы, отправленные не из LeadChat, а из приложения Авито, сюда не попадают: у них не записан автор. Карточка «Отвечено» наверху считает иначе — любой ответ оператора по текущему ответственному",
    sortable: true,
    numeric: true,
    render: (r) => formatNumber(r.answered),
    total: (t) => formatNumber(t.answered),
  },
  {
    key: "closed",
    label: "Закрыто",
    /*
     * ⚠ ОГОВОРКА ПРО ИТОГ ДОБАВЛЕНА 08.09 (аудит, H-02). У «Принято» она стояла
     * с 15 августа, у «Закрыто» — нет, хотя механизм тот же и расхождение тех
     * же размеров: аудитор увидел «Закрыто 10 970» при сумме столбца 16 245 и
     * прочитал это как ошибку счёта.
     */
    hint:
      "Диалоги, которые человек закрыл в выбранный период, — включая взятые до его начала и чужие. Закрывший и ведущий могут быть разными людьми. В ИТОГО диалог считается один раз, поэтому сумма столбца может быть больше итога",
    sortable: true,
    numeric: true,
    render: (r) => formatNumber(r.closed),
    total: (t) => formatNumber(t.closed),
  },
  {
    key: "frt_median_sec",
    label: "Первый ответ",
    hint:
      "Медиана времени от сообщения клиента до первого ответа оператора — за все часы суток. " +
      "Считается по диалогам, где известен автор первого ответа, поэтому ИТОГО не совпадает " +
      "с карточкой «Первый ответ» наверху: она берёт все ответы, включая отправленные из Авито",
    sortable: true,
    numeric: true,
    render: (r) => formatDuration(r.frt_median_sec),
    total: (t) => formatDuration(t.frt_median_sec),
  },
  {
    key: "frt_median_biz_sec",
    label: "В рабочие часы",
    hint: "То же время первого ответа, но считаются только рабочие часы: ночное ожидание в него не попадает",
    sortable: true,
    numeric: true,
    render: (r) => formatDuration(r.frt_median_biz_sec),
    total: (t) => formatDuration(t.frt_median_biz_sec),
  },
  {
    key: "messages_sent",
    label: "Сообщений",
    /*
     * ⚠ ЭТО НЕ ТО ЖЕ ЧИСЛО, ЧТО В ГРАФИКЕ «ИСХОДЯЩИЕ СООБЩЕНИЯ», и комментарий
     * у самого графика (`stats.py`, шаблон `messages_out`) обещал обратное:
     * «ровно то же определение, что и в колонке „отправлено“». Условия отбора
     * и правда те же, а вот собирается результат по-разному: график считает
     * все исходящие оператора, колонка группирует по автору — и сообщения без
     * автора (отправленные из приложения Авито) не попадают ни в чью строку.
     */
    hint:
      "Доставленные сообщения этого человека. Отправленные из приложения Авито сюда не попадают: " +
      "у них не записан автор — поэтому сумма столбца меньше графика «Исходящие сообщения»",
    sortable: true,
    numeric: true,
    render: (r) => formatNumber(r.messages_sent),
    total: (t) => formatNumber(t.messages_sent),
  },
];

/**
 * ПОЛОСКА НАГРУЗКИ ПОД ЧИСЛОМ СООБЩЕНИЙ.
 *
 * Разброс между первым и последним оператором смены — вопрос, который задают
 * этой таблице чаще прочих, а отвечали на него чтением тринадцати чисел подряд
 * и делением в уме. Форма отвечает быстрее.
 *
 * ⚠ ДОЛЯ СЧИТАЕТСЯ ОТ МАКСИМУМА ПО ПОКАЗАННЫМ СТРОКАМ, А НЕ ОТ ИТОГА, И ЭТО НЕ
 * ПРИДИРКА. На тринадцати сотрудниках итог примерно вдесятеро больше личного
 * счёта: лидер получил бы пятую часть ширины, отстающий — доли процента, и все
 * тринадцать полосок слились бы в одинаковые огрызки у левого края. От
 * максимума лидер даёт 100 %, а отстающий — свою честную треть.
 *
 * Итог сюда не годится и по второй причине: он приходит с сервера по ВСЕЙ
 * выборке, а строк на экране может быть меньше (фильтр по сотруднику).
 */
function долиНагрузки(rows: ManagerRow[]): Map<string, number> {
  const max = rows.reduce((m, r) => (r.messages_sent > m ? r.messages_sent : m), 0);
  const доли = new Map<string, number>();
  // Смена без единого сообщения — полосок нет вовсе: делить не на что, а
  // тринадцать пустых рамок читались бы как «данные не пришли».
  if (max <= 0) return доли;
  for (const r of rows) доли.set(r.manager_id, Math.round((r.messages_sent / max) * 100));
  return доли;
}

export interface ManagersTableProps {
  data?: ManagersResponse;
  isPending: boolean;
  isError: boolean;
  onRetry(): void;
  sort: ManagersSort;
  order: SortOrder;
  /** Клик по заголовку: та же колонка — разворот порядка, другая — новый sort. */
  onSortChange(next: ManagersSort): void;
  selectedManagerIds: string[];
  onSelectManager(id: string): void;
  /**
   * Снять сужение по сотрудникам целиком — единственное действие пустого
   * отчёта. Отдельным обработчиком, а не перебором `onSelectManager`: тот
   * ЗАМЕНЯЕТ выбор одним человеком, и на двух выбранных перебор оставил бы
   * последнего вместо того, чтобы очистить фильтр.
   */
  onClearManagers?(): void;
  onOpenChats(row: ManagerRow): void;
}

export function ManagersTable({
  data,
  isPending,
  isError,
  onRetry,
  sort,
  order,
  onSortChange,
  selectedManagerIds,
  onSelectManager,
  onClearManagers,
  onOpenChats,
}: ManagersTableProps) {
  if (isPending) {
    return (
      <section className="lc-card stats-panel" aria-label="Таблица менеджеров">
        <Text component="h2" className="stats-panel__title">
          Менеджеры
        </Text>
        <div className="stats-skeleton stats-skeleton--table" aria-hidden="true" />
      </section>
    );
  }

  if (isError) {
    return (
      <section className="lc-card stats-panel" aria-label="Таблица менеджеров">
        <Text component="h2" className="stats-panel__title">
          Менеджеры
        </Text>
        <div className="stats-error" role="alert">
          <Text fz="sm" c="var(--lc-text-2)">
            Не получилось загрузить таблицу
          </Text>
          <Button size="xs" variant="outline" onClick={onRetry}>
            Повторить
          </Button>
        </div>
      </section>
    );
  }

  const rows = data?.rows ?? [];
  const доли = долиНагрузки(rows);

  return (
    <section className="lc-card stats-panel" aria-label="Таблица менеджеров">
      {/*
        Свежесть подписана и здесь — теми же словами, что у групп карточек.
        До 09.09 подписывать было нечего: «Ответил первым» и обе медианы FRT
        приходили с витрины ВСЕГДА, а «Принято», «Закрыто» и «Сообщений» —
        живьём, и одна строка держала два разных момента времени. Теперь
        источник у всей строки один, и подпись называет какой.
      */}
      <Text component="h2" className="stats-panel__title">
        Менеджеры
        {data?.period_live !== undefined && (
          <span className="stats-group__hint">
            {data.period_live ? "на момент открытия страницы" : "по витрине статистики"}
          </span>
        )}
      </Text>
      {/*
        ⚠ ОДНА ОГОВОРКА НА ВСЮ ТАБЛИЦУ ВМЕСТО ПЯТИ ОТДЕЛЬНЫХ. Разбор 08.09
        нашёл три «расхождения» подряд — «Сообщений» против графика
        «Исходящие», «Первый ответ» против одноимённой карточки, «Ответил
        первым» против «Отвечено», — и у всех трёх корень ОДИН: таблица
        построчная, а строка бывает только у того, чьё имя записано. Ответ,
        отправленный не из LeadChat, а из приложения Авито, автора не имеет —
        он есть в карточках наверху и в графике, но ни в чьей строке.

        Три подсказки об одном и том же читатель складывает в «здесь всё
        как-то не сходится». Одна строка под заголовком отвечает сразу.
      */}
      <Text component="p" fz="xs" c="var(--lc-text-3)" className="managers__note">
        Таблица считает только то, что можно приписать человеку. Ответы, отправленные из приложения
        Авито, автора не имеют — они попадают в карточки и график наверху, но ни в чью строку.
      </Text>

      {rows.length === 0 ? (
        /*
         * ПУСТАЯ ТАБЛИЦА — ЭТО ОТВЕТ, А НЕ ИЗВИНЕНИЕ.
         *
         * Здесь стояла одна приглушённая строка «Нет данных за выбранный
         * период». На месте, где ждали тринадцать строк, серый шрифт читается
         * как «не доехало», и человек жмёт F5 вместо того, чтобы поменять срез.
         *
         * А срез чаще всего и виноват: клик по строке кладёт сотрудника в
         * фильтр (`selectedManagerIds`), и стоит выбрать период, в котором у
         * него не было работы, — таблица пустеет вся. Тогда рядом с фразой
         * стоит ровно одно действие, снимающее это сужение. Фильтра нет —
         * действия нет: кнопка, которая ничего не меняет, хуже её отсутствия.
         */
        <div className="stats-empty">
          <p className="stats-empty__title">Нет данных за выбранный период</p>
          {selectedManagerIds.length > 0 && onClearManagers ? (
            <>
              <Text fz="sm" c="var(--lc-text-3)">
                Отчёт сужен до выбранных сотрудников — у них за этот период работы не было
              </Text>
              <Button size="xs" variant="outline" onClick={onClearManagers}>
                Показать всех
              </Button>
            </>
          ) : (
            <Text fz="sm" c="var(--lc-text-3)">
              Ни один сотрудник не работал в эти даты — возьмите период пошире
            </Text>
          )}
        </div>
      ) : (
        <div className="managers-scroll">
          {/* На узком экране строка становится карточкой (lc-table-cards.css):
              семь столбцов чисел в 400 пикселей не помещаются, а прокрутка
              вбок прячет половину показателей — и человек делает выводы по
              тому, что случайно попало в кадр. */}
          <table className="lc-table managers lc-table--cards">
            <thead>
              <tr>
                {COLUMNS.map((col) => {
                  const active = col.sortable && col.key === sort;
                  return (
                    <th
                      key={col.key}
                      scope="col"
                      data-numeric={col.numeric || undefined}
                      aria-sort={active ? (order === "asc" ? "ascending" : "descending") : undefined}
                    >
                      {col.sortable ? (
                        /* Подсказка висит на КНОПКЕ сортировки, а не на голой
                           строке: Mantine требует элемент со ссылкой, и на
                           тексте она упала бы в рантайме. Свой span тоже не
                           годится — он сломал бы прижатие стрелки к правому
                           краю. */
                        <Tooltip label={col.hint} disabled={!col.hint} multiline w={280} withArrow>
                          <button
                            type="button"
                            className="managers__sort"
                            data-active={active || undefined}
                            onClick={() => onSortChange(col.key as ManagersSort)}
                          >
                            {col.label}
                            <span aria-hidden="true">{active ? (order === "asc" ? "▲" : "▼") : ""}</span>
                          </button>
                        </Tooltip>
                      ) : (
                        col.label
                      )}
                    </th>
                  );
                })}
                <th scope="col" className="managers__actions-col">
                  <span className="lc-visually-hidden">Действия</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.manager_id}
                  className="managers__row"
                  data-selected={selectedManagerIds.includes(row.manager_id) || undefined}
                  data-inactive={!row.is_active || undefined}
                  onClick={() => onSelectManager(row.manager_id)}
                >
                  {COLUMNS.map((col) => (
                    <td key={col.key} data-numeric={col.numeric || undefined} data-label={col.label}>
                      {col.key === "full_name" ? (
                        <button
                          type="button"
                          className="managers__name"
                          onClick={(e) => {
                            e.stopPropagation();
                            onSelectManager(row.manager_id);
                          }}
                          aria-label={`Показать статистику: ${row.full_name}`}
                        >
                          {row.full_name}
                          {!row.is_active && <span className="managers__badge">отключён</span>}
                        </button>
                      ) : col.key === "messages_sent" && доли.has(row.manager_id) ? (
                        <span className="managers__load">
                          {col.render(row)}
                          {/* Полоска — вторая подача ТОГО ЖЕ числа, читалке её
                              незачем: она уже произнесла «Сообщений 12 480». */}
                          <span className="managers__load-bar" aria-hidden="true">
                            <i style={{ width: `${доли.get(row.manager_id)}%` }} />
                          </span>
                        </span>
                      ) : (
                        col.render(row)
                      )}
                    </td>
                  ))}
                  <td className="managers__actions-col">
                    {/*
                      Подсказка называет, чего переход НЕ переносит (STATS-07,
                      SCEN-28): у рабочего списка чатов нет фильтра по датам, и
                      отчёт за прошлый месяц открывается текущим списком.
                      Закрытые при этом на месте — вкладка «Все» спрашивает
                      сервер без отбора по статусу (`tab=any`). Прежняя
                      подсказка отсылала к переключателю «Показывать закрытые»,
                      которого в списке нет с 17.08 (проверка 24.09).
                    */}
                    <Tooltip
                      label="Откроет диалоги сотрудника в чатах, вкладка «Все» — с закрытыми. Период отчёта там не действует"
                      withArrow
                      position="left"
                      multiline
                      w={260}
                    >
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        aria-label={`Диалоги: ${row.full_name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpenChats(row);
                        }}
                      >
                        Диалоги <IconForward size={13} />
                      </Button>
                    </Tooltip>
                  </td>
                </tr>
              ))}
            </tbody>
            {data?.totals && (
              <tfoot>
                <tr className="managers__totals">
                  {COLUMNS.map((col) => (
                    <td key={col.key} data-numeric={col.numeric || undefined} data-label={col.label}>
                      {col.total(data.totals)}
                    </td>
                  ))}
                  <td />
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}
    </section>
  );
}
