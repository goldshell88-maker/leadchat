import { useMemo } from "react";
import { Button } from "@mantine/core";
import { useNavigate } from "react-router-dom";
import type { NotificationDto } from "@/shared/api/types";
import { kindIcon, resolveAction } from "./catalog";
import { criticalBanners, MAX_CRITICAL_BANNERS, useNotificationStore } from "./store";
import { formatRepeat } from "./time";
import { useMarkNotificationRead, useNotificationAction, useNotificationsEnabled } from "./useNotifications";
import "./notifications.css";

/**
 * Плашки критичного (14 §3). Не строка в списке, а полоса поверх экрана: висит,
 * пока администратор не подтвердит. Одновременно видно не больше трёх —
 * дальше «и ещё N», иначе в плохой день плашки съедают рабочее место целиком.
 *
 * «Подтвердит» — значит именно эту плашку и именно руками. Массовое «Отметить
 * все прочитанными» её не гасит: раньше гасило, и «Канал отобрали» уходил с
 * экрана заодно с прочитанной мелочью (см. `store.markAllRead`).
 */

function CriticalBanner({ notification }: { notification: NotificationDto }) {
  const navigate = useNavigate();
  const markRead = useMarkNotificationRead();
  const action = resolveAction(notification);
  const runAction = useNotificationAction();
  const repeat = formatRepeat(notification.repeat_count);

  const acknowledge = () => markRead.mutate(notification.id);

  const handleAction = () => {
    if (!action) return;
    if (action.mode === "link") {
      acknowledge();
      navigate(action.to);
      return;
    }
    if (action.mode === "request") {
      runAction.mutate({ notification, code: action.code });
      return;
    }
    acknowledge();
  };

  return (
    <div className="lc-critical__item" role="alert">
      <span className="lc-critical__icon" aria-hidden="true">
        {kindIcon(notification.kind)}
      </span>
      <div className="lc-critical__text">
        <strong className="lc-critical__title">{notification.title}</strong>
        <span className="lc-critical__body">
          {notification.body ?? ""}
          {repeat ? ` · ${repeat}` : ""}
        </span>
      </div>
      {/*
        У «Подтвердить» ТОЖЕ есть признак ожидания (NOTIF-02).

        Соседняя кнопка действия его имела, а эта — нет, и разница выходила
        боком: пометка прочитанным уходит на сервер, плашка гаснет мгновенно, и
        при неудаче человек узнавал об этом только по числу, вернувшемуся на
        колокольчик. Теперь отказ виден тостом и плашка возвращается
        (`useMarkNotificationRead.onError`), а пока запрос идёт — нажатие
        показывает, что оно принято.
      */}
      <div className="lc-critical__actions">
        {action && (
          <Button
            size="xs"
            color="red"
            loading={runAction.isPending || (action.mode !== "request" && markRead.isPending)}
            onClick={handleAction}
          >
            {action.label}
          </Button>
        )}
        {action?.mode !== "ack" && (
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            loading={markRead.isPending}
            onClick={acknowledge}
          >
            Подтвердить
          </Button>
        )}
      </div>
    </div>
  );
}

export function CriticalBanners() {
  const enabled = useNotificationsEnabled();
  const items = useNotificationStore((s) => s.items);
  const acknowledged = useNotificationStore((s) => s.acknowledged);
  const pinned = useNotificationStore((s) => s.pinnedCritical);
  const navigate = useNavigate();

  const banners = useMemo(
    () => criticalBanners(items, acknowledged, pinned),
    [items, acknowledged, pinned],
  );

  if (!enabled || banners.length === 0) return null;

  const visible = banners.slice(0, MAX_CRITICAL_BANNERS);
  const rest = banners.length - visible.length;

  return (
    <section className="lc-critical" aria-label="Критичные уведомления">
      {visible.map((n) => (
        <CriticalBanner key={n.id} notification={n} />
      ))}
      {rest > 0 && (
        <button type="button" className="lc-critical__more" onClick={() => navigate("/notifications")}>
          и ещё {rest}
        </button>
      )}
    </section>
  );
}
