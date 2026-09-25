import { useMutation } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { ApiError } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto } from "@/shared/api/types";
import { applyPinnedInLists } from "@/shared/realtime/applyWsEvent";
import { showToast } from "@/shared/ui/toast";
import { pinConversation, unpinConversation } from "../api";

/**
 * Закрепить/открепить у себя (требование от 7 августа).
 *
 * Оптимистично: закрепление — отметка человека в собственном списке, ждать
 * ответа сервера, чтобы нарисовать её, значит превратить мгновенное действие
 * в подтормаживающее. При отказе откатываем и объясняем причину.
 *
 * СПИСОК НЕ ПЕРЕЗАПРАШИВАЕТСЯ (разбор от 12 августа). Раньше на успех ручки
 * уходил `invalidateQueries` по корню списка — до пятидесяти строк заново с
 * сервера ради собственной галочки, и по разу на каждое нажатие. Довод был
 * «порядок считает сервер, повторять его на клиенте — завести второе место с
 * одним правилом», и он был верен ровно наполовину: второе место УЖЕ
 * существовало (кэш пересортировывался на каждое входящее сообщение) и
 * считало по своему правилу. Теперь правило одно —
 * `shared/lib/conversationOrder`, зеркало серверного `ORDER BY`, — и строка
 * встаёт на своё место без похода на сервер.
 */
export function useTogglePin(convId: string) {
  return useMutation<void, Error, boolean, { prev?: ConversationDetailDto; wasPinned: boolean }>({
    mutationFn: (next) => (next ? pinConversation(convId) : unpinConversation(convId)),
    onMutate: (next) => {
      const prev = queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail(convId));
      queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (old) =>
        old ? { ...old, pinned: next } : old,
      );
      // Строка в списке и её место в порядке — здесь же, а не по ответу
      // сервера: закреп нажимают, чтобы диалог поднялся сейчас.
      applyPinnedInLists(convId, next);
      return { prev, wasPinned: !next };
    },
    onError: (err, _next, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(qk.conversations.detail(convId), ctx.prev);
      // Откат строки списка обязателен: без него отказ сервера («уже максимум»)
      // оставлял бы диалог поднятым наверх — тост говорит одно, список другое.
      applyPinnedInLists(convId, ctx?.wasPinned ?? false);
      const reason = err instanceof ApiError ? err.details?.reason : undefined;
      showToast({
        title: "Не получилось закрепить",
        message:
          reason === "too_many_pins"
            ? "Закреплённых уже максимум — открепите что-нибудь"
            : reason === "not_mine"
              ? "Закреплять можно только свои диалоги"
              : "Попробуйте ещё раз",
        color: "red",
      });
    },
  });
}
