import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { ApiError } from "@/shared/api/http";
import { qk } from "@/shared/api/queryKeys";
import type { NotificationActionResult, NotificationDto } from "@/shared/api/types";
import { usePermissions } from "@/shared/auth/usePermissions";
import { useSessionStore } from "@/shared/stores/sessionStore";
import {
  fetchRecentNotifications,
  fetchUnreadCount,
  fetchUnreadCriticalNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  runNotificationAction,
} from "./api";
import { NOTIFICATIONS_PERMISSION, isUnread } from "./catalog";
import { useNotificationStore } from "./store";
import { showToast } from "@/shared/ui/toast";

/** Как часто перепроверяем счётчик, если WS молчал (страховка, не основной путь). */
const UNREAD_POLL_MS = 60_000;

/**
 * Сколько раз перепробовать стартовую загрузку центра (NOTIF-01).
 *
 * Было `retry: 0` — одна попытка и тишина. Самые частые причины отказа здесь
 * временные: перезапуск api отдаёт 502 несколько секунд, вайфай моргает на
 * пути в переговорную. Две повторные попытки закрывают их молча и бесплатно;
 * то, что не закрылось, показывается человеком читаемой ошибкой.
 */
const LOAD_RETRIES = 2;

/**
 * Пауза между попытками: полсекунды, потом секунда.
 *
 * Умолчание библиотеки — секунда и две, то есть о неудаче человек узнаёт через
 * три секунды после открытия приложения. Для колокольчика это долго: всё это
 * время он показывает ноль, а ноль читается как «новостей нет». Полторы
 * секунды хватает пережить перезапуск api и не заставляет ждать при настоящем
 * отказе.
 */
const RETRY_DELAY = (attempt: number) => Math.min(500 * 2 ** attempt, 2000);

/**
 * Колокольчик есть у тех, кому уведомления вообще приходят. Право — то же, что
 * проверяет сервер на всех пяти ручках центра: наблюдатель получает 403, и
 * рисовать ему вечный ноль незачем.
 */
export function useNotificationsEnabled(): boolean {
  const { can } = usePermissions();
  return can(NOTIFICATIONS_PERMISSION);
}

/**
 * Стартовая загрузка центра уведомлений: последние строки для колокольчика и
 * счётчик непрочитанного. Дальше состояние живёт от WS-события `notify`;
 * опрос счётчика раз в минуту — страховка на случай пропущенного кадра.
 */
export function useNotificationsBootstrap(): void {
  const enabled = useNotificationsEnabled();
  const seed = useNotificationStore((s) => s.seed);
  const setUnread = useNotificationStore((s) => s.setUnread);
  const setOwner = useNotificationStore((s) => s.setOwner);
  const clear = useNotificationStore((s) => s.clear);

  /**
   * Кому принадлежит содержимое центра. Проверяем ДО загрузки: на одном
   * компьютере люди работают по очереди (11 §2.5, как `useRoleUiSync`), и стор
   * переживает выход — иначе пришедший менеджер до ответа сервера увидит
   * счётчик и красные плашки администратора, а если запрос не удастся — так с
   * ними и останется (попытки кончатся, а чужое на экране нет).
   */
  const identity = useSessionStore((s) => (s.user ? `${s.user.id}:${s.user.role}` : null));
  useEffect(() => {
    setOwner(identity);
  }, [identity, setOwner]);

  const recent = useQuery({
    queryKey: qk.notifications.recent,
    queryFn: fetchRecentNotifications,
    enabled,
    staleTime: UNREAD_POLL_MS,
    retry: LOAD_RETRIES,
    retryDelay: RETRY_DELAY,
  });

  const count = useQuery({
    queryKey: qk.notifications.unreadCount,
    queryFn: fetchUnreadCount,
    enabled,
    refetchInterval: UNREAD_POLL_MS,
    staleTime: 0,
    retry: LOAD_RETRIES,
    retryDelay: RETRY_DELAY,
  });

  /*
   * ВТОРОЙ ЗАПРОС — ЗА НЕПРОЧИТАННЫМ КРИТИЧНЫМ, и он не роскошь.
   *
   * Колокольчик грузит десять ПОСЛЕДНИХ строк, красная плашка рисуется по
   * тому, что доехало. С 6 по 11 августа «Приём сообщений остановился» дал
   * 112 строк, и два настоящих «Резервное копирование не выполнилось» (ночи
   * 6 и 8 августа) в эту десятку не попали — их не увидел никто, а копий за
   * те ночи нет. Шумный вид с тех пор понижен до «важно», но полагаться на
   * это одно нельзя: следующий шумный вид ещё не написан, а плашка должна
   * выживать при любом соседе.
   */
  const criticals = useQuery({
    queryKey: qk.notifications.unreadCritical,
    queryFn: fetchUnreadCriticalNotifications,
    enabled,
    staleTime: UNREAD_POLL_MS,
    retry: LOAD_RETRIES,
    retryDelay: RETRY_DELAY,
  });

  const recentData = recent.data;
  const criticalData = criticals.data;
  useEffect(() => {
    if (!recentData && !criticalData) return;
    // Порядок и повторы разбирает стор (`seed` → `dedupeById` → сортировка):
    // одна и та же строка законно приходит обоими запросами.
    seed(
      [...(recentData?.items ?? []), ...(criticalData?.items ?? [])],
      recentData?.unread ?? criticalData?.unread,
    );
  }, [recentData, criticalData, seed]);

  const unread = count.data?.unread;
  useEffect(() => {
    if (typeof unread === "number") setUnread(unread);
  }, [unread, setUnread]);

  /*
   * НЕУДАЧА ЗАГРУЗКИ ОБЯЗАНА ДОЙТИ ДО ЭКРАНА (NOTIF-01).
   *
   * Раньше её не видел никто: стор оставался пустым, панель писала «Пока
   * ничего не происходило», а бейдж показывал ноль — и то и другое звучало
   * как «всё спокойно». Администратору в этот момент могло лежать «Канал
   * отобрали: подписка пропала».
   *
   * Оба запроса считаются одним признаком: список без счётчика неполон, а
   * счётчик без списка нечем открыть. Признак живёт в сторе, потому что
   * читает его панель колокольчика, а запрос делает каркас приложения.
   */
  const loadFailed = recent.isError || count.isError;
  const setLoadFailed = useNotificationStore((s) => s.setLoadFailed);
  useEffect(() => {
    if (enabled) setLoadFailed(loadFailed);
  }, [enabled, loadFailed, setLoadFailed]);

  // Роль без уведомлений (наблюдатель) или выход — в сторе не должно остаться чужого.
  useEffect(() => {
    if (!enabled) clear();
  }, [enabled, clear]);
}

/**
 * Повторить стартовую загрузку центра — кнопка «Повторить» в панели (NOTIF-01).
 * Функция, а не хук: её зовут из обработчика нажатия, и лишний хук ради этого
 * пришлось бы протаскивать через все места, где рисуется панель.
 */
export function retryNotificationsLoad(): void {
  void queryClient.refetchQueries({ queryKey: qk.notifications.root });
}

/** Пометить прочитанным: в интерфейсе — сразу, счётчик — из ответа сервера. */
export function useMarkNotificationRead() {
  const qc = useQueryClient();
  const markRead = useNotificationStore((s) => s.markRead);
  const revertRead = useNotificationStore((s) => s.revertRead);
  const setUnread = useNotificationStore((s) => s.setUnread);
  return useMutation({
    mutationFn: (id: string) => markNotificationRead(id),
    onMutate: (id: string) => {
      // Снимок ДО правки: по нему откатываемся, если сервер откажет (NOTIF-02).
      const before = useNotificationStore.getState();
      const target = before.items.find((i) => i.id === id);
      markRead(id);
      return {
        wasUnread: target ? isUnread(target) : false,
        wasAcknowledged: before.acknowledged.includes(id),
      };
    },
    onSuccess: (result) => {
      if (typeof result?.unread === "number") setUnread(result.unread);
    },
    /*
     * ОТКАТ И ЧЕСТНОЕ СЛОВО (NOTIF-02).
     *
     * Без этой ветки нажатие «Подтвердить» на красной плашке было необратимым
     * даже при полном отказе сервера: плашка гасла, id уходил в
     * `acknowledged`, и обратно её не поднимал никто — `seed()` этот список не
     * трогает. Беда оставалась (канал отобран, приём стоит), а напоминать о
     * ней стало нечему.
     */
    onError: (err, _id, ctx) => {
      if (ctx) revertRead(_id, ctx);
      showToast({
        title: "Не отметилось прочитанным",
        message:
          err instanceof ApiError && err.message
            ? err.message
            : "Уведомление осталось непрочитанным. Попробуйте ещё раз",
        tone: "danger",
      });
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.notifications.root });
    },
  });
}

export function useMarkAllNotificationsRead() {
  const qc = useQueryClient();
  const markAllRead = useNotificationStore((s) => s.markAllRead);
  const revertAllRead = useNotificationStore((s) => s.revertAllRead);
  const setUnread = useNotificationStore((s) => s.setUnread);
  return useMutation({
    mutationFn: () => markAllNotificationsRead(),
    onMutate: () => {
      const before = useNotificationStore.getState();
      // Критичные пометка не трогает: они гасятся только поимённо.
      const flipped = before.items
        .filter((i) => isUnread(i) && i.severity !== "critical")
        .map((i) => i.id);
      const kept = before.items.filter((i) => isUnread(i) && i.severity === "critical").length;
      markAllRead();
      return { flipped, kept, prevUnread: before.unread };
    },
    onSuccess: (result) => {
      if (typeof result?.unread === "number") setUnread(result.unread);
    },
    onError: (err, _v, ctx) => {
      if (ctx) revertAllRead(ctx.flipped, ctx.prevUnread, ctx.kept);
      showToast({
        title: "Не отметилось прочитанным",
        message:
          err instanceof ApiError && err.message
            ? err.message
            : "Уведомления остались непрочитанными. Попробуйте ещё раз",
        tone: "danger",
      });
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.notifications.root });
    },
  });
}

/**
 * Действие по уведомлению (14 §4). Что вернулось — решает само действие:
 *  - `url` — уводим браузер (OAuth переподключения аккаунта);
 *  - `invite_url` — одноразовая ссылка, её показывает вызывающий экран.
 * После успеха уведомление считается обработанным и гасится.
 */
export function useNotificationAction(onLink?: (result: NotificationActionResult) => void) {
  const qc = useQueryClient();
  const markRead = useNotificationStore((s) => s.markRead);
  const setUnread = useNotificationStore((s) => s.setUnread);
  return useMutation({
    mutationFn: (v: { notification: NotificationDto; code?: string }) =>
      runNotificationAction(v.notification.id, v.code ?? v.notification.action?.code),
    onSuccess: (response, v) => {
      markRead(v.notification.id);
      if (typeof response?.unread === "number") setUnread(response.unread);
      const result = response?.result ?? null;
      if (result?.invite_url) {
        onLink?.(response);
      } else if (result?.url) {
        window.location.href = result.url;
      } else {
        showToast({
          title: "Готово",
          message: result?.message || "Действие выполнено",
          tone: "success",
        });
      }
    },
    onError: (err) => {
      // Действие может быть ещё не написано на сервере — там честный 503
      // с человеческим текстом. Показываем его, а не «что-то пошло не так».
      showToast({
        title: "Действие не выполнено",
        message: err instanceof ApiError && err.message ? err.message : "Попробуйте ещё раз",
        tone: "danger",
      });
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.notifications.root });
    },
  });
}
