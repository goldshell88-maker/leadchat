import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Text } from "@mantine/core";

import { usePermissions } from "@/shared/auth/usePermissions";
import { http } from "@/shared/api/http";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";

/**
 * ПОДРОБНЫЙ СЛЕД ПУТИ СООБЩЕНИЯ — включается на время, прямо отсюда.
 *
 * ЗАЧЕМ ЗДЕСЬ. Лента отвечает на «что происходит», и когда ответ «непонятно
 * что», следующий вопрос — «запиши подробнее». Держать переключатель в другом
 * разделе значило бы заставить человека уйти с экрана, на котором он как раз
 * заметил странность.
 *
 * ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ САМОЙ ЛЕНТЫ. Лента — про события продукта («ответ не
 * доставлен», «диалог взят»), и её читает владелец. След — про путь внутри
 * системы: пришло по вебхуку, легло в базу, разбудило бота, встало в очередь
 * доставки. Его читают в журнале сервера при разборе поломки, и включён он
 * бывает час-другой, а не всегда: на боевом идут десятки тысяч сообщений в
 * сутки (разбор — в `app/core/trace.py`).
 *
 * ПРАВО ДРУГОЕ, ЧЕМ У ЛЕНТЫ. Ленту видит руководитель (`stats:all`), а трогать
 * настройки системы может только администратор (`settings:manage`). Поэтому
 * блок просто не рисуется тем, кто не может им пользоваться: запертая кнопка
 * без объяснения читается как поломка.
 */

interface TraceState {
  enabled: boolean;
  seconds_left: number;
  max_hours: number;
}

const KEY = ["settings", "trace"] as const;

/** «47 мин» — сколько ещё писать. Секунды здесь не нужны, часы слишком грубо. */
function left(seconds: number): string {
  if (seconds >= 3600) {
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return m ? `${h} ч ${m} мин` : `${h} ч`;
  }
  return `${Math.max(1, Math.round(seconds / 60))} мин`;
}

export function TraceSwitch() {
  const { can } = usePermissions();
  const allowed = can("settings:manage");
  const qc = useQueryClient();

  const state = useQuery({
    queryKey: KEY,
    queryFn: () => http.get<TraceState>("/settings/trace"),
    enabled: allowed,
    // Раз в полминуты: у следа есть срок, и «осталось 47 мин» обязано
    // уменьшаться само — иначе человек уйдёт, думая, что он ещё пишет.
    refetchInterval: 30_000,
  });

  const set = useMutation({
    mutationFn: (hours: number) => http.patch<TraceState>("/settings/trace", { hours }),
    onSuccess: (data) => qc.setQueryData(KEY, data),
    // Без этого отказ сервера выглядел как нажатие, которое ничего не сделало:
    // кнопка переставала крутиться, и администратор считал след включённым
    // или выключенным — как ему казалось (проверка 24.09).
    onError: (error, hours) =>
      showToast(
        describeError({
          where: hours > 0 ? "Включить подробный след" : "Выключить подробный след",
          error,
          fallback: "Попробуйте ещё раз",
        }),
      ),
  });

  if (!allowed || !state.data) return null;
  const { enabled, seconds_left } = state.data;

  return (
    <div className="lc-feed__trace">
      {enabled ? (
        <>
          <Text fz="xs" c="var(--lc-text-2)">
            Подробный след пишется — ещё {left(seconds_left)}
          </Text>
          <Button
            className="lc-btn"
            size="xs"
            variant="subtle"
            onClick={() => set.mutate(0)}
            loading={set.isPending}
          >
            Выключить
          </Button>
        </>
      ) : (
        <>
          <Text fz="xs" c="var(--lc-text-3)">
            Не сходится картина? Включите подробный след — он запишет путь каждого сообщения
            в журнал сервера
          </Text>
          <Button
            className="lc-btn"
            size="xs"
            variant="default"
            onClick={() => set.mutate(1)}
            loading={set.isPending}
          >
            {/* Час, а не «включить навсегда»: след гаснет сам, и забыть его
                включённым нельзя (`app/core/trace.py`). */}
            Записывать час
          </Button>
        </>
      )}
    </div>
  );
}
