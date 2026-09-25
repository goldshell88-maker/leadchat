import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { ApiError, http } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, ConversationDto, ConversationStatus } from "@/shared/api/types";
import {
  appendMessage,
  applyConversationPatch,
  cachedConversationRow,
} from "@/shared/realtime/applyWsEvent";
import {
  выдачаПрячетЗакрытые,
  вернутьСтроку,
  патчСтрок,
  убратьСтроку,
  type СнимокСтроки,
} from "@/shared/realtime/listCache";
import {
  подписьСотрудника,
  type ЧеловекСОтделом,
} from "@/shared/lib/подписьСотрудника";
import {
  assignConversation,
  fetchAssignableUsers,
  fetchClientHistory,
  fetchConversation,
  fetchMessages,
  inviteParticipant,
  nextConversationId,
  patchConversationStatus,
  removeParticipant,
} from "../api";
import { describeError } from "@/shared/ui/errorToast";
import { showToast } from "@/shared/ui/toast";
import { useNavigate } from "react-router-dom";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { inboxRows, insertInboxRow, nextInboxId, removeInboxRow } from "@/features/chats/inbox/api";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { refetchListsNow } from "@/shared/realtime/listRefetch";
import { пересобратьПорядок } from "@/features/chats/components/list/listOrder";

/**
 * Деталь диалога (01 §5.2) — шапка ленты и правая карточка берут её по одному
 * ключу: второй потребитель получает кэш, лишнего запроса нет (03 §2.1).
 * Deep-link работает без списка.
 */
/**
 * Войти в чужой диалог — нажатием (просьба владельца 04.09: «сделай кнопку
 * войти в диалог»).
 *
 * ⚠ ЗДЕСЬ БЫЛ АВТОМАТИЧЕСКИЙ ВХОД ЧЕРЕЗ ДВЕ СЕКУНДЫ ЧТЕНИЯ. Он выполнял ту же
 * просьбу («чтобы чужой диалог появлялся в „Моих"»), но решение за человека
 * принимала машина: открыл посмотреть — и диалог уже в списке. Задержка была
 * защитой от того, чтобы прокрутка стрелками не собирала историю просмотров, —
 * то есть заплаткой на решении, которого человек не принимал.
 *
 * Кнопка возвращает решение туда, где ему место. Заплатка вместе с ней уходит:
 * листать список можно сколько угодно, «Мои» от этого не растут.
 *
 * Тихо на сервере: ни уведомления, ни строки в ленте (см. ручку `/enter`).
 */
export function useEnterDialog(convId: string | null) {
  return useMutation({
    mutationFn: () => http.post(`/conversations/${encodeURIComponent(convId as string)}/enter`, {}),
    onSuccess: () => {
      // Строки в «Моих» ещё нет — патч её не создаст, нужен перезапрос.
      void queryClient.invalidateQueries({ queryKey: qk.conversations.detail(convId ?? "") });
      refetchListsNow();
      if (convId) пересобратьПорядок(convId);
    },
    onError: (error) =>
      showToast({ ...describeError({ where: "входе в диалог", error }), color: "red" }),
  });
}

/** Выйти из чужого диалога: та же ручка, что убирает позванного. */
export function useLeaveDialog(convId: string | null, userId: string | null) {
  return useMutation({
    mutationFn: () =>
      http.del(
        `/conversations/${encodeURIComponent(convId as string)}/participants/${encodeURIComponent(
          userId as string,
        )}`,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.conversations.detail(convId ?? "") });
      refetchListsNow();
    },
    onError: (error) =>
      showToast({ ...describeError({ where: "выходе из диалога", error }), color: "red" }),
  });
}

export function useConversationDetail(convId: string | null) {
  return useQuery({
    queryKey: qk.conversations.detail(convId ?? ""),
    queryFn: () => fetchConversation(convId as string),
    enabled: Boolean(convId),
    // Своего правила повторов здесь больше нет: 404 (как и всякий 4xx) не
    // переспрашивается общим правилом в `queryClient.ts`. Точечное исключение
    // на один код осталось бы вторым местом про одно и то же — и второй такой
    // случай (400 на кривом deep-link) в него бы уже не попал.
  });
}

/**
 * Лента диалога (01 §6.1). Один и тот же ключ у центра и у карточки клиента —
 * TanStack Query отдаёт общий кэш, второго запроса не будет (03 §2.1).
 */
export function useThreadMessages(convId: string) {
  return useInfiniteQuery({
    queryKey: qk.messages.list(convId),
    queryFn: ({ pageParam }) => fetchMessages(convId, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: () => null, // вниз ничего не догружаем — низ живой (WS)
    getPreviousPageParam: (first) => (first.page.has_more_before ? first.page.prev_cursor : null),
  });
}

/**
 * Смена статуса (01 §5.4): оптимистично в детали, при ошибке — откат + тост
 * (10 §4.5). Сервер всё равно пришлёт conversation:updated — обработчик идемпотентен.
 */
/**
 * Смена статуса. Объектом, а не голой строкой, оставлено намеренно: рядом со
 * `status` жили `outcome` и `amountRub` (результат обращения и сумма), снятые
 * 12 августа. Все вызовы уже написаны под объект, и разворачивать их обратно в
 * строку значило бы тронуть каждый ради нуля пользы.
 */
export interface StatusChange {
  status: ConversationStatus;
  /**
   * Статус ДО нажатия — его говорит тот, кто нажал.
   *
   * ⚠ ПОЧЕМУ НЕ ИЗ КЭША. Прежний статус лежит в кэше детали, но опираться на
   * него нельзя: кэш может быть холодным (пришли по прямой ссылке, деталь ещё
   * летит), и тогда «вернул из закрытого» молча превратилось бы в «обычная
   * смена статуса» — то есть перезапрос списков не сработал бы ровно в том
   * случае, ради которого написан. Нажавший знает точно: он видит кнопку
   * «Вернуть в работу» только у закрытого диалога.
   */
  прежний?: ConversationStatus;
}

/**
 * Текст отказа сервера ЧЕЛОВЕКУ — по машинному `reason` (docs/38 §3).
 *
 * Каждый запрет матрицы переходов приходит со своей причиной, и общий текст
 * «не получилось» вместо неё оператор читает как поломку системы: он видит
 * пункт в списке, выбирает его и получает ошибку без объяснений. Причины
 * ровно те, которые сервер и умеет вернуть, — выдумывать сюда своих нельзя.
 */
function statusErrorToast(err: unknown): { title: string; message: string } {
  const reason = err instanceof ApiError ? err.details?.reason : undefined;
  switch (reason) {
    case "same_status":
      return { title: "Статус не изменился", message: "Этот статус уже стоит" };
    case "already_claimed": {
      // Гонка «двое взяли одновременно». Имя победителя приходит в теле 409 —
      // тем же полем, что и у кнопки «Принять» в очереди, и обработчик у них
      // общий по смыслу: человек должен узнать, КТО успел, а не что «не вышло».
      const by = (err as ApiError).details?.claimed_by as ЧеловекСОтделом | undefined;
      const кто = подписьСотрудника(by);
      return {
        title: "Диалог уже занят",
        message: кто ? `Диалог уже взял ${кто}` : "Диалог успел взять коллега",
      };
    }
    case "guest_cannot_close":
      // Человек зашёл в чужой диалог сам и жмёт «Закрыть», имея в виду «убрать
      // у себя». Сервер называет хозяина и подсказывает кнопку, которая
      // действительно убирает у себя, — показываем его слова, а не свои.
      return {
        title: "Диалог ведёт коллега",
        message:
          err instanceof ApiError && err.message
            ? err.message
            : "Чтобы убрать диалог из своих, нажмите «Выйти»",
      };
    case "no_reply_yet":
      return {
        title: "Ещё рано",
        message: "Клиенту никто не ответил — ждать ему нечего",
      };
    case "reopen_to_progress_only":
      return {
        title: "Сначала верните в работу",
        message: "Закрытый диалог возвращают в работу, и уже потом решают, чего ждать",
      };
    case "manual_reopen_forbidden":
      return {
        title: "Так вернуть нельзя",
        message: "Закрытый диалог возвращается только в работу — или сам, когда клиент напишет",
      };
    case "queued_close_forbidden":
      // Обращение ещё никто не взял: закрытие убрало бы его из очереди
      // насовсем, а клиент ждёт. Оператору нужен «Отклонить» — диалог уйдёт с
      // его глаз на три минуты и вернётся к коллегам (решение владельца 22.08).
      return {
        title: "Это обращение ещё никто не взял",
        message:
          "Нажмите «Отклонить» — оно вернётся в очередь к коллегам. " +
          "Закрыть необработанное может только администратор",
      };
    case "transfer_pending":
      // Закрытие молча отменило бы передачу, а получатель остался бы с «Принять».
      return {
        title: "Диалог ждёт ответа на передачу",
        message: "Отмените передачу или дождитесь ответа коллеги — закрыть можно после",
      };
    default:
      break;
  }
  /*
   * ⚠ ОБЩИЙ ТЕКСТ БОЛЬШЕ НЕ ПРЯЧЕТ ПРИЧИНУ (жалоба владельца 22.08: три
   * одинаковых «Статус не изменился — попробуйте ещё раз» при закрытии пачкой).
   *
   * `reason` читается только у ApiError, то есть у отказа, оформленного самим
   * приложением. А при закрытии пачкой прилетает то, что приложение не
   * оформляло: 429 от nginx (лимит 30 запросов в секунду на адрес), 502 при
   * перезапуске, обрыв сети. Все три попадали в один безликий текст, и владелец
   * читал его как «система сломалась», не понимая, что делать.
   *
   * Теперь каждый случай называет себя и говорит, что делать дальше.
   */
  const код = err instanceof ApiError ? err.status : undefined;
  if (код === 429) {
    return {
      title: "Слишком часто",
      message: "Закрываете быстрее, чем сервер успевает. Подождите пару секунд и продолжайте",
    };
  }
  if (код !== undefined && код >= 500) {
    return {
      title: "Сервер не ответил",
      message: `Ошибка ${код}. Диалог не закрыт — попробуйте ещё раз через минуту`,
    };
  }
  if (код === undefined) {
    return {
      title: "Нет связи с сервером",
      message: "Диалог не закрыт. Проверьте соединение и повторите",
    };
  }
  return {
    title: "Статус не изменился",
    message: `Сервер отказал (${код}) и причины не назвал — повторите или позовите администратора`,
  };
}

/** Что запомнили в момент нажатия — чтобы откатить, если сервер откажет. */
interface КонтекстСтатуса {
  prev?: ConversationDetailDto;
  /** Строка до перекраски — статус и признак очереди, которые мы тронули. */
  прежняя: Pick<ConversationDto, "status" | "in_inbox"> | null;
  /** Откуда убрали строку при закрытии; `null` — ниоткуда (списка не было). */
  снимок: СнимокСтроки | null;
  /** Строка стояла в очереди — при откате вернуть и туда. */
  строкаОчереди: ConversationDto | null;
  /** Куда увели человека до ответа сервера; `null` — остались на месте. */
  ушлиК: string | null;
}

export function useChangeStatus(convId: string) {
  const navigate = useNavigate();
  return useMutation<ConversationDetailDto, Error, StatusChange, КонтекстСтатуса>({
    /*
     * ⚠ КЛЮЧ С ДИАЛОГОМ ВНУТРИ — ЭТО ЗАЩИТА, А НЕ УКРАШЕНИЕ (06.09).
     *
     * Закрытие уводит человека к следующему диалогу ДО ответа сервера (ниже,
     * в `onMutate`). `ChatThreadPane` при этом не перемонтируется — ему
     * приходит новый `convId`, и хук перерисовывается с новыми замыканиями.
     * TanStack Query у ВИСЯЩЕЙ мутации подменяет колбэки на свежие при каждой
     * перерисовке наблюдателя (`MutationObserver.setOptions`): без ключа ответ
     * на закрытие старого диалога прилетел бы в `onSuccess` с `convId`
     * НОВОГО — и «закрытым» в кэше оказался бы тот, что человек только что
     * открыл. Проверено на установленном query-core 5.101.
     *
     * Смена ключа заставляет наблюдателя отпустить висящую мутацию: она
     * дорабатывает со своими прежними замыканиями, а `isPending` у нового
     * диалога сразу `false` — кнопка «Закрыть» и Ctrl+D не ждут чужой ответ.
     */
    mutationKey: ["conversations", "status", convId],
    mutationFn: ({ status }) => patchConversationStatus(convId, status),
    onMutate: ({ status }) => {
      const prev = queryClient.getQueryData<ConversationDetailDto>(qk.conversations.detail(convId));
      queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (old) =>
        old ? { ...old, status } : old,
      );
      const ctx: КонтекстСтатуса = {
        prev,
        прежняя: null,
        снимок: null,
        строкаОчереди: null,
        ушлиК: null,
      };
      if (status !== "closed") return ctx;

      /*
       * ⚠ ЗАКРЫТИЕ — ВСЁ ВИДИМОЕ ДЕЛАЕТСЯ ДО ОТВЕТА СЕРВЕРА (замер 06.09: 377
       * закрытий за смену, p50 35 мс). Раньше оптимистично правилась только
       * деталь; строка списка и переход к следующему ждали `onSuccess`, а уход
       * строки из «Моих» — ещё круг `refetchListsNow`. Человек ждал RTT + 35 мс
       * до перехода и ещё ~50 мс, пока строка исчезнет; при 429 или обрыве —
       * тост и никакого перехода.
       *
       * Порядок ниже важен: следующего считаем ПО ТОМУ СПИСКУ, ЧТО ЕЩЁ СО
       * СТРОКОЙ, — после удаления соседа не найти (тот же довод, что у отказа
       * в очереди). Всё, что тронули, запоминаем в `ctx`: отказ сервера обязан
       * вернуть экран байт в байт, включая гостя с 403 `guest_cannot_close`.
       */
      const ui = useChatUiStore.getState();
      const next = ui.inboxOpen
        ? nextInboxId(convId)
        : ui.filters.tab === "mine"
          ? nextConversationId(convId, ui.filters)
          : null;

      const строка = cachedConversationRow(convId);
      ctx.прежняя = строка ? { status: строка.status, in_inbox: строка.in_inbox } : null;
      ctx.строкаОчереди = inboxRows().find((r) => r.id === convId) ?? null;

      // «Мои» закрытых не показывают — оттуда строка уходит; «Все» держат её и
      // после закрытия — там она перекрашивается. Сначала убрать, потом
      // перекрасить: так снимок хранит строку нетронутой, а откат ниже
      // перекрашивает назад ровно те строки, что никуда не уходили («Все»).
      // Числа над вкладкой НЕ спрашиваем: перезапрос, ушедший вместе с PATCH,
      // привёз бы список с ещё живой строкой. Сервер спросим после ответа.
      ctx.снимок = убратьСтроку(convId, выдачаПрячетЗакрытые);
      патчСтрок(
        (r) => r.id === convId,
        (r) => ({ ...r, status: "closed", in_inbox: false }),
      );
      /*
       * ⚠ ЗАКРЫЛ — СТРОКА УХОДИТ ИЗ ОЧЕРЕДИ СРАЗУ (жалоба владельца 28.08: «не
       * моментально уходит диалог из входящих при принятии и закрытии»).
       *
       * Очередь живёт в своём кэше (`qk.inbox.list`) со своим составом
       * строки, и патч списков его не трогает вовсе. Убираем строку и снимаем
       * диалог со счётчика очереди (он там идемпотентный) — тем же движением,
       * что и принятие (`inbox/useInbox.ts`, `принятоУспешно`).
       */
      removeInboxRow(convId);
      useInboxStore.getState().remove(convId);

      /*
       * Закрыл — переходим к следующему (UX-аудит, docs/17 §С2 + замечание
       * заказчика от 7 августа).
       *
       * Раньше оператор оставался в закрытом диалоге с надписью «Диалог
       * закрыт» и сам ехал в список за следующим — клик и пересканирование
       * колонки на каждом закрытии.
       *
       * ГДЕ ПЕРЕХОДИМ, А ГДЕ НЕТ. Сначала автопереход стоял только в очереди:
       * там конвейер очевиден. Но заказчик показал, что то же самое верно для
       * «Моих»: «закрываешь диалог, а окно остаётся, и приходится постоянно
       * перекликивать на новый». «Мои» — это своя нагрузка, её закрывают
       * подряд, а не между делом.
       *
       * А вот на «Всех» перехода нет намеренно: это витрина, куда заходят
       * посмотреть чужой диалог или найти старый. Унести человека оттуда в
       * соседнюю строку значит потерять то, что он читал.
       *
       * ⚠ С НЕДОПИСАННЫМ ОТВЕТОМ НИКУДА НЕ УВОДИМ (30.08). Закрытие висит на
       * `Mod+Shift+Enter`, а это промах мимо `Shift+Enter` («перенос строки»)
       * у человека, чей палец уже лежит на Ctrl по привычке «Ctrl+Enter —
       * отправить». Диалог закрывался, экран сам уезжал к следующему клиенту,
       * а набранное оставалось в брошенной переписке — и `replace: true` не
       * давал вернуться назад браузером.
       *
       * Само закрытие не трогаем: человек мог и правда закрыть диалог, не
       * дописав. Отнят только переход — он и был тем «самовольным
       * переключением на другого клиента», о котором говорил владелец.
       */
      const набранное = (ui.drafts[convId]?.text ?? "").trim();
      if (next && !набранное) navigate(`/chats/${next}`, { replace: true });
      // Куда увели — чтобы откат вернул назад; не уводили — возвращать некуда.
      ctx.ушлиК = next && !набранное ? next : null;
      return ctx;
    },
    onError: (err, _status, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(qk.conversations.detail(convId), ctx.prev);
      /*
       * Откат закрытия — в обратном порядке тому, что сделали в `onMutate`:
       * строка перекрашивается назад, встаёт на прежние места в списках и в
       * очереди, а человека возвращаем в диалог, из которого увели. Тост —
       * словами сервера (`statusErrorToast`): гость, закрывающий чужой диалог,
       * должен прочитать «Диалог ведёт коллега», а не «что-то не вышло».
       */
      if (ctx?.снимок) вернутьСтроку(ctx.снимок);
      if (ctx?.прежняя) {
        const { status, in_inbox } = ctx.прежняя;
        патчСтрок(
          (r) => r.id === convId,
          (r) => ({ ...r, status, in_inbox }),
        );
      }
      if (ctx?.строкаОчереди) {
        insertInboxRow(ctx.строкаОчереди);
        useInboxStore.getState().add(convId);
      }
      /*
       * Назад — только если человек ещё там, куда мы его увели (ревью 06.09).
       * Пока ответ ехал, он мог сам открыть другой диалог (закрыть следующий,
       * пройти по списку) — увести его второй раз значит то самое
       * «самовольное переключение», от которого отняли переход при
       * недописанном ответе. Строка уже вернулась в список, тост назвал
       * причину. `null` — панель не сообщила, где он, — ведём назад, как и
       * при попадании точно в уведённый диалог.
       */
      if (ctx?.ушлиК) {
        const где = useChatUiStore.getState().activeConversationId;
        if (где === null || где === ctx.ушлиК) navigate(`/chats/${convId}`, { replace: true });
      }
      showToast({ ...statusErrorToast(err), color: "red" });
    },
    onSuccess: (conv, vars, ctx) => {
      applyConversationPatch(convId, {
        status: conv.status,
        status_since: conv.status_since,
        assignee: conv.assignee,
        // Закрытый диалог в очереди не стоит: её условие требует
        // `status != closed`. Без этого поля строка оставалась во «Входящих» до
        // следующей полной загрузки.
        ...(conv.status === "closed" ? { in_inbox: false } : null),
      });

      /*
       * Строка и очередь поправлены ещё в `onMutate`; здесь — сверка с
       * сервером, и СРАЗУ, а не в хвосте чужой пачки: своё нажатие человек
       * ждёт глазами. Не `await`: ответ уже на экране, круг идёт в фоне.
       */
      if (conv.status === "closed") {
        refetchListsNow();
      }

      /*
       * ⚠ ВЕРНУЛ ИЗ «ЗАКРЫТ» — СТРОКА ОБЯЗАНА ПОЯВИТЬСЯ В «МОИХ» СРАЗУ (вторая
       * жалоба владельца 03.09: «взял в работу и снова так же в „Мои" диалог не
       * появился»).
       *
       * Первая её половина — серверная: переоткрытие теперь отдаёт диалог
       * нажавшему. Но одного сервера мало, и вот почему: `applyConversationPatch`
       * правит строку ТАМ, ГДЕ ОНА УЖЕ ЕСТЬ, а в «Моих» закрытого диалога нет —
       * вкладка их исключает. Патчить нечего, создать строку патч не умеет.
       * Обычный перезапрос схлопывается (полсекунды, потолок три), и человек
       * читает это как «опять не появился».
       *
       * Свой перезапрос, а не расширение ветки закрытия: там снимают строку из
       * ОЧЕРЕДИ, чего здесь делать нельзя — переоткрытый диалог во «Входящих» и
       * не стоял.
       */
      const былЗакрыт = (vars.прежний ?? ctx?.prev?.status) === "closed";
      if (былЗакрыт && conv.status !== "closed") {
        refetchListsNow();
        // ⚠ И ПОМЕТКА «СВОЯ», ИНАЧЕ СТРОКА ПРИЕДЕТ В ХВОСТ. В «Моих» этого
        // диалога до сих пор не было, значит для заморозки порядка он новый —
        // а новые встают в конец, чтобы не прыгать под рукой. Та же половина,
        // что и у приёма из очереди.
        пересобратьПорядок(convId);
      }
      // Переход к следующему после закрытия сделан в `onMutate`, до ответа
      // сервера, — здесь его больше нет намеренно.
    },
  });
}

// ЗДЕСЬ ЖИЛ `useTakeConversation` — «Взять в работу» из карточки клиента.
// Снят 23.08: кнопку, ради которой он писался, убрали перекройкой 12 августа
// (действие переехало в нижнюю плашку ленты), и с тех пор хук не звал никто —
// а тесты `ThreadFooterClaim` и `CardNoDuplicates` прямо ЗАПРЕЩАЮТ появление
// кнопки «Взять в работу» в карточке.
//
// ЧТО СТОИТ ЗНАТЬ, ЕСЛИ КНОПКУ ЗАХОТЯТ ВЕРНУТЬ. Ручка одна и та же —
// `POST /conversations/{id}/claim`, — но живой `useClaimConversation`
// (`inbox/useInbox.ts`) после успеха УВОДИТ человека к следующему диалогу
// очереди, а снятый хук намеренно не уводил: оператор читает переписку, и
// унести его оттуда значило бы потерять прочитанное. Разница между двумя
// поведениями и была всем содержимым снятого кода.


// ЗДЕСЬ ЖИЛИ `useSnoozeConversation` И `useUnsnoozeConversation` — «Отложить
// до…» и «Вернуть сейчас». Убраны 12 августа вместе со всей отложкой:
// ручек `POST`/`DELETE /conversations/{id}/snooze` на сервере больше нет.

/** GET /users/assignable (01 §3.1) — список для TransferDialog. */
export function useAssignableUsers(enabled: boolean) {
  return useQuery({
    queryKey: qk.users.assignable,
    queryFn: fetchAssignableUsers,
    enabled,
    staleTime: 60_000,
  });
}

/** POST /conversations/{id}/assign (01 §5.5): комментарий уходит системным сообщением в ленту. */
export function useAssignConversation(convId: string) {
  return useMutation({
    mutationFn: (v: { assigneeId: string | null; comment?: string }) =>
      assignConversation(convId, v.assigneeId, v.comment),
    onSuccess: (res, v) => {
      queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), res.conversation);
      appendMessage(convId, res.system_message);
      applyConversationPatch(convId, {
        status: res.conversation.status,
        assignee: res.conversation.assignee,
      });
      /*
       * ⚠ ПЕРЕДАЧА КОЛЛЕГЕ — ЭТО ПРЕДЛОЖЕНИЕ, А НЕ СВЕРШИВШИЙСЯ ФАКТ.
       *
       * Сервер в этой ветке зовёт `transfer.offer` и выходит ДО смены
       * ответственного — «предложить диалог коллеге, ответственный НЕ
       * меняется» (docstring `offer`). А сообщение печаталось утверждением:
       * «Диалог передан · Ответственный: …», и имя бралось из ответа, где
       * ответственный ПРЕЖНИЙ. То есть оператор читал имя того, у кого диалог
       * остался, и уходил, считая дело сделанным.
       *
       * Различаем по факту: совпал ли ответственный в ответе с тем, кому
       * передавали. Совпал — назначение состоялось (себе или через снятие);
       * не совпал — коллеге отправлено предложение, и он ещё может отказаться.
       */
      const назначен = res.conversation.assignee?.id === v.assigneeId;
      if (v.assigneeId && !назначен) {
        showToast({
          title: "Предложение отправлено",
          message: "Диалог останется за вами, пока коллега не примет его",
          color: "lp",
        });
        return;
      }
      showToast({
        title: "Диалог передан",
        message: res.conversation.assignee
          ? `Ответственный: ${подписьСотрудника(res.conversation.assignee)}`
          : "Диалог вернулся в «Новые»",
        color: "lp",
      });
    },
    onError: (err) => {
      // Причину называет сервер: «коллега не в сети», «диалог закрыт»… Общее
      // «Попробуйте ещё раз» прятало её, и человек повторял то же самое.
      showToast(describeError({ where: "передать диалог", error: err }));
    },
  });
}

/**
 * Позвать коллегу в диалог (docs/19). Ответственный НЕ меняется — поэтому
 * здесь нет `applyConversationPatch`: в строке списка меняться нечему.
 *
 * Состав кладётся в кэш детали из ответа, а не перезапросом: ручка вернула
 * ровно то, что нужно, и лишний GET показал бы карточку без только что
 * позванного человека на те доли секунды, пока он летит.
 */
export function useInviteParticipant(convId: string) {
  return useMutation({
    mutationFn: (v: { userId: string; reason?: string }) =>
      inviteParticipant(convId, v.userId, v.reason),
    onSuccess: (res, v) => {
      queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (prev) =>
        prev ? { ...prev, participants: res.participants } : prev,
      );
      // Системная запись про приглашение придёт кадром message:new — своей
      // ленте её не дописываем, иначе у автора она задвоится.
      const who = res.participants.find((p) => p.id === v.userId);
      showToast({
        title: "Позвали в диалог",
        message: who
          ? `${подписьСотрудника(who)} получит уведомление`
          : "Коллега получит уведомление",
        color: "lp",
      });
    },
    onError: (err) => {
      const reason = err instanceof ApiError ? err.details?.reason : undefined;
      showToast({
        title: "Не получилось позвать",
        message:
          reason === "already_assignee"
            ? "Этот сотрудник и так ведёт диалог"
            : "Попробуйте ещё раз",
        color: "red",
      });
    },
  });
}

/** Убрать позванного — сам вышел или его убрали (docs/19). */
export function useRemoveParticipant(convId: string) {
  return useMutation({
    mutationFn: (userId: string) => removeParticipant(convId, userId),
    onSuccess: (res) => {
      queryClient.setQueryData<ConversationDetailDto>(qk.conversations.detail(convId), (prev) =>
        prev ? { ...prev, participants: res.participants } : prev,
      );
    },
    onError: (err) =>
        showToast(describeError({ where: "Убрать", error: err, fallback: "Попробуйте ещё раз" })),
  });
}

/** GET /conversations/{id}/client-history (01 §5.6) — прошлые диалоги клиента. */
export function useClientHistory(convId: string, enabled = true) {
  return useQuery({
    queryKey: qk.clients.history(convId),
    queryFn: () => fetchClientHistory(convId),
    enabled: enabled && Boolean(convId),
    staleTime: 5 * 60_000,
  });
}
