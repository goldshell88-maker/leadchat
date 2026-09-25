import { http } from "@/shared/api/http";
import type { ExportJob, ExportJobCreated } from "@/shared/api/types";
import type { ConversationStatus } from "@/shared/lib/conversationStatus";

/** Строка таблицы диалогов (план 7.3) — метрики считает сервер. */
export interface TableRow {
  id: string;
  // Свой литеральный тип отсюда убран (docs/38 §0): он был четвёртой
  // копией словаря и разъезжался бы молча — два одинаковых объединения
  // TypeScript считает совместимыми, пока они одинаковые.
  status: ConversationStatus;
  client_name: string | null;
  client_phone: string | null;
  /**
   * Кто вёл диалог. Идентификатор нужен рядом с именем, потому что фильтр
   * «Оператор» добирает из строк тех, кого нет в справочнике: справочник
   * отдаёт только тех, кому можно ДАВАТЬ диалоги, а в колонке стоят и те,
   * кому давать уже нельзя (уволен, снят с диалогов, роль head). Без id их
   * пришлось бы класть в фильтр по имени — и первые же два однофамильца
   * превратили бы фильтр в ложь.
   */
  assignee_id: string | null;
  assignee_name: string | null;
  /**
   * Отдел ответственного — подписью «(ОКК)» в колонке «Оператор» (04.09).
   *
   * Отдельным полем, а не вклеенным в `assignee_name`: имя уходит ещё и в
   * выгрузку CSV, а там скобки сделали бы столбец непригодным для фильтра
   * Excel по точному имени оператора.
   */
  assignee_department?: string | null;
  item_title: string | null;
  account_id: string;
  /** Название канала: в таблице показывается оно, а не uuid аккаунта. */
  account_title: string | null;
  tags: string[];
  bot_active: boolean;
  unread_count: number;
  last_message_at: string | null;
  messages_count: number;
  /**
   * Секунды от первого сообщения клиента до первого ответа оператора.
   *
   * `null` — «ещё не ответили», и это НЕ ноль. Ноль читался бы как «ответили
   * мгновенно», то есть отчёт показывал бы идеальную скорость ровно там, где
   * клиента не обслужили вовсе.
   */
  first_response_sec: number | null;
  /**
   * Длительность переписки: от первого сообщения до последнего.
   *
   * Здесь ноль честен — в отличие от времени ответа: диалог из одного
   * сообщения действительно длился нисколько.
   */
  duration_sec: number | null;
}

export interface TablePage {
  items: TableRow[];
  page: { limit: number; offset: number; total: number };
}

export interface TableQuery {
  /** Поиск по имени клиента и ТЕКСТУ СООБЩЕНИЙ — сервер ищет полнотекстово. */
  q?: string;
  status?: string;
  accountId?: string;
  assigneeId?: string;
  tag?: string;
  botActive?: boolean;
  dateFrom?: string;
  dateTo?: string;
  sort: string;
  direction: "asc" | "desc";
  offset: number;
}

/**
 * Параметры выборки — одни на экран и на выгрузку.
 *
 * Собираются ОДНОЙ функцией намеренно: выгрузка обязана отдать ровно то, что
 * человек видит. Две сборки параметров разъехались бы на первом же новом
 * фильтре, и файл тихо перестал бы совпадать с экраном.
 */
function searchParams(qy: TableQuery, { withPaging }: { withPaging: boolean }): URLSearchParams {
  const p = new URLSearchParams();
  // Поиск — первым: он сужает выборку сильнее всех остальных фильтров, и в
  // адресной строке его удобнее видеть сразу.
  if (qy.q) p.set("q", qy.q);
  if (qy.status) p.set("status", qy.status);
  if (qy.accountId) p.set("account_id", qy.accountId);
  if (qy.assigneeId) p.set("assignee_id", qy.assigneeId);
  if (qy.tag) p.set("tag", qy.tag);
  if (qy.botActive !== undefined) p.set("bot_active", String(qy.botActive));
  if (qy.dateFrom) p.set("date_from", qy.dateFrom);
  if (qy.dateTo) p.set("date_to", qy.dateTo);
  p.set("sort", qy.sort);
  p.set("direction", qy.direction);
  // Страница у выгрузки своя — она берёт всю выборку целиком.
  if (withPaging) p.set("offset", String(qy.offset));
  return p;
}

export function fetchTable(qy: TableQuery): Promise<TablePage> {
  return http.get<TablePage>(`/conversations/table?${searchParams(qy, { withPaging: true })}`);
}

/**
 * Выгрузка текущей выборки в CSV — фоном (проверка 24.09).
 *
 * Файл собирал запрос с потолком 10 000 строк, а это меньше любого готового
 * периода: на бою «30 дней» — 44 052 строки. Теперь его пишет воркер, как у
 * выгрузки статистики: ответ — номер задачи, файл — по подписанной ссылке.
 * Параметры те же, что у экрана, одной функцией: выгрузка обязана повторять
 * увиденное.
 */
export function startTableExport(qy: TableQuery): Promise<ExportJobCreated> {
  return http.post<ExportJobCreated>(
    `/conversations/table/export?${searchParams(qy, { withPaging: false })}`,
  );
}

export function fetchTableExportJob(jobId: string): Promise<ExportJob> {
  return http.get<ExportJob>(`/conversations/table/export/${encodeURIComponent(jobId)}`);
}

/** Оператор для фильтра «Оператор»: только то, что нужно выпадающему списку. */
export interface TableOperator {
  id: string;
  full_name: string;
  /** Отдел — подписью «(ОКК)» в фильтре «Оператор». `null` — не заполнен. */
  department?: string | null;
  /** `false` — отключён; такого помечаем в подписи, но из списка не убираем. */
  is_active: boolean;
}

export interface TableOperatorsResponse {
  items: TableOperator[];
}

/**
 * Справочник операторов ДЛЯ РАЗБОРА, а не для назначения.
 *
 * ЧЕМ ОТЛИЧАЕТСЯ ОТ ОБЩЕГО `useAssignableUsersQuery`. Тот спрашивает «кому
 * можно отдать диалог» и по контракту (01 §3.1) отдаёт только активных. Для
 * окна «Передать диалог» это ровно то, что нужно. Для фильтра отчёта — нет:
 * здесь ищут как раз тех, кому отдавать уже нельзя. Уволенный оператор
 * остаётся ответственным в сотнях строк (деактивация диалоги не
 * переназначает, 01 §3.5), и «покажи всё, что осталось от Петра» — первый же
 * вопрос руководителя после увольнения. В общем справочнике этого человека
 * нет, и фильтр молча делает вид, что таких строк не бывает.
 *
 * Поэтому отдельный запрос с `include_inactive=true` и отдельный ключ кэша:
 * это ДРУГИЕ данные, а не те же самые. Один ключ на два разных ответа
 * означал бы, что порядок открытия экранов решает, увидит ли окно передачи
 * уволенных в списке «кому передать».
 */
export function fetchTableOperators(): Promise<TableOperatorsResponse> {
  return http.get<TableOperatorsResponse>("/users/assignable?include_inactive=true");
}

/**
 * Колонки, по которым сервер разрешает сортировать. Список продублирован
 * здесь НЕ от лени: интерфейс обязан знать, у каких заголовков рисовать
 * стрелку, а у каких нет, — иначе человек кликает и получает ошибку вместо
 * сортировки. Расхождение с сервером ловится тестом.
 */
export const SORTABLE = new Set([
  "last_message_at",
  "updated_at",
  "status",
  "unread_count",
  // Дорогая: порядок считается по всей выборке, а не по видимой странице.
  // Сервер ограничивает её ширину и на широкой отвечает отказом словами — его
  // и показываем (см. `errorText` в TablePage), своей копии потолка тут нет.
  "first_response_sec",
]);
