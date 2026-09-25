import { http } from "@/shared/api/http";
import { useQuery } from "@tanstack/react-query";
import { qk, type TemplateScope } from "@/shared/api/queryKeys";
import { fetchTemplateFolders, fetchTemplates } from "./api";

/**
 * Быстрые ответы (01 §7.1). Список короткий (десятки) — грузим целиком и
 * фильтруем локально (11 §3.1).
 *
 * ЗДЕСЬ БЫЛА НЕПРАВДА: комментарий обещал, что при более чем 200 записях
 * подключается серверный поиск по `q`. Он не подключён нигде — ни в `api.ts`,
 * ни в вызывающих (TPL-05). То есть с 201-й записи часть библиотеки просто
 * переставала существовать для экрана, а поиск по ней ничего не находил и
 * молчал об этом. Раз механизма нет, обещать его нельзя; вместо обещания экран
 * теперь ЧЕСТНО говорит, что показаны не все, — см. `TemplatesManager`.
 */
export function useTemplates(scope: TemplateScope, enabled = true) {
  return useQuery({
    queryKey: qk.templates.list(scope),
    queryFn: () => fetchTemplates(scope),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export function useTemplateFolders(enabled = true) {
  return useQuery({
    queryKey: qk.templates.folders,
    queryFn: fetchTemplateFolders,
    enabled,
    staleTime: 5 * 60_000,
  });
}

/**
 * Отметить, что заготовку применили. Вдогонку, без ожидания.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 29.08: «сделай популярность ответов, когда пишешь для
 * быстрых ответов». Заготовок 39 общих плюс личные, а подсказка показывает
 * пять: какие пять — вопрос не вкуса, и отвечать на него должен живой сигнал.
 *
 * ⚠ РЕЗУЛЬТАТА НЕ ЖДЁМ И ОТКАЗ ГЛОТАЕМ, И ЭТО РЕДКИЙ СЛУЧАЙ, КОГДА ТАК МОЖНО.
 * Заготовка к этому моменту уже вставлена в поле — работа сделана. Провалившийся
 * счётчик стоит одной позиции в порядке подсказки, а тост про него посреди
 * набора сообщения отвлекал бы от разговора с клиентом ради пустяка. Молчим
 * только здесь и только потому, что терять нечего.
 */
export function отметитьПрименение(id: string): void {
  void http.post(`/templates/${encodeURIComponent(id)}/used`, {}).catch(() => {});
}
