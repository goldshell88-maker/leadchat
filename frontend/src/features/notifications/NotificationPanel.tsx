import { useId } from "react";
import { Button, Text, VisuallyHidden } from "@mantine/core";
import { useNavigate } from "react-router-dom";
import type { NotificationDto } from "@/shared/api/types";
import { RECENT_PAGE_SIZE } from "./api";
import { SEVERITY_LABELS, entityLink, isUnread, kindIcon, severityDotClass } from "./catalog";
import {
  selectHasUnconfirmedCritical,
  selectLoadFailed,
  selectUnread,
  useNotificationStore,
} from "./store";
import { formatNotificationTime, formatRepeat } from "./time";
import {
  retryNotificationsLoad,
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
} from "./useNotifications";
import { выбралСамПоАдресу } from "@/features/chats/выборДиалога";
import { толькоВнутренний } from "@/shared/lib/внутреннийАдрес";

/**
 * Содержимое выпадающего списка колокольчика (14 §3): последние уведомления,
 * важность цветом и словом, время по-человечески, ссылка на связанную сущность.
 *
 * Кнопок действий здесь нет намеренно: одно нажатие с «Переподключить» живёт
 * в плашке критичного и в журнале `/notifications`, а список — быстрый просмотр.
 */

export function NotificationRow({
  notification,
  onNavigate,
}: {
  notification: NotificationDto;
  onNavigate?: () => void;
}) {
  const navigate = useNavigate();
  const markRead = useMarkNotificationRead();
  const link = entityLink(notification);
  const repeat = formatRepeat(notification.repeat_count);

  const handleClick = () => {
    if (isUnread(notification)) markRead.mutate(notification.id);
    onNavigate?.();
    // Адрес приходит с сервера. Сегодня он строится из закрытого списка путей
    // (`catalog.ts:entityLink`), но проверка стоит здесь, а не в доверии к
    // соседнему файлу: открытый редирект в `react-router` 6.x уводит наружу
    // ровно через такой адрес. Разбор — в `shared/lib/внутреннийАдрес.ts`.
    const куда = link && толькоВнутренний(link.to);
    if (куда) {
      // Нажатие по уведомлению — осознанный выбор: замок обязан его пропустить.
      выбралСамПоАдресу(куда);
      navigate(куда);
    }
  };

  return (
    <button
      type="button"
      className="lc-notify-row"
      data-severity={notification.severity}
      data-unread={isUnread(notification) || undefined}
      onClick={handleClick}
    >
      <span className="lc-notify-row__icon" aria-hidden="true">
        {kindIcon(notification.kind)}
      </span>
      <span className="lc-notify-row__body">
        <span className="lc-notify-row__head">
          <span className={severityDotClass(notification.severity)}>
            {SEVERITY_LABELS[notification.severity]}
          </span>
          <span className="lc-notify-row__title">{notification.title}</span>
          {/* Вес шрифта скринридер не озвучивает — признак нужен словами. */}
          {isUnread(notification) && <VisuallyHidden>не прочитано</VisuallyHidden>}
        </span>
        {notification.body && <span className="lc-notify-row__text">{notification.body}</span>}
        <span className="lc-notify-row__meta">
          {formatNotificationTime(notification.last_seen_at || notification.created_at)}
          {repeat ? ` · ${repeat}` : ""}
          {link ? ` · ${link.label.toLowerCase()}` : ""}
        </span>
      </span>
    </button>
  );
}

export function NotificationPanel({ items, onClose }: { items: NotificationDto[]; onClose?: () => void }) {
  const navigate = useNavigate();
  const markAll = useMarkAllNotificationsRead();
  // Истина по непрочитанному — счётчик колокольчика, а не десять видимых строк:
  // непрочитанное старше их попадает в бейдж, но не в список, и кнопка,
  // отключённая «потому что здесь всё прочитано», оставляла бы человека с
  // числом на колокольчике, которое нечем погасить.
  const unread = useNotificationStore(selectUnread);
  const hasUnread = unread > 0 || items.some(isUnread);
  const hasUnconfirmedCritical = useNotificationStore(selectHasUnconfirmedCritical);
  const loadFailed = useNotificationStore(selectLoadFailed);
  // Заголовок панели — видимый и связанный по id. `aria-label` на голом <div>
  // без роли скринридером не экспонируется (NOTIF-07), то есть панель для него
  // была безымянной.
  const headingId = useId();

  /*
   * ПОКАЗЫВАЕМ РОВНО ТО, ЧТО ОБЕЩАНО, — ПОСЛЕДНИЕ ДЕСЯТЬ (NOTIF-06).
   *
   * С сервера просится `RECENT_PAGE_SIZE` строк, но стор копит до
   * `RECENT_LIMIT` за счёт приходящих по WS, а панель рисовала его целиком.
   * За смену выпадающий список превращался в прокручиваемую ленту на полсотни
   * строк — «последние уведомления» переставали быть последними, а нижние
   * читать было уже некому.
   */
  const visible = items.slice(0, RECENT_PAGE_SIZE);
  const hidden = items.length - visible.length;

  return (
    <div className="lc-notify-panel" role="group" aria-labelledby={headingId}>
      <div className="lc-notify-panel__head">
        <Text id={headingId} fz="sm" fw={600} c="var(--lc-text-1)">
          Уведомления
        </Text>
        <Button
          size="compact-xs"
          variant="subtle"
          disabled={!hasUnread || markAll.isPending}
          loading={markAll.isPending}
          onClick={() => markAll.mutate()}
        >
          Отметить все прочитанными
        </Button>
      </div>

      {/* Кнопка выше больше не гасит красные плашки (см. store.markAllRead).
          Об этом надо сказать заранее: иначе человек нажмёт, увидит, что
          плашки на месте, и решит, что кнопка не работает. */}
      {hasUnconfirmedCritical && (
        <Text fz="xs" c="var(--lc-text-3)" className="lc-notify-panel__note">
          Критичные останутся на экране, пока не подтвердите каждое
        </Text>
      )}

      {/*
        ОШИБКА ЗАГРУЗКИ — НЕ ПУСТОТА (NOTIF-01).

        Пока этой ветки не было, упавший запрос выглядел как «Пока ничего не
        происходило»: администратор с непрочитанным «Канал отобрали: подписка
        пропала» читал, что всё спокойно, и закрывал панель. Поэтому ветка
        стоит ПЕРВОЙ — даже если в сторе что-то есть от WS, показанное неполно,
        и знать об этом важнее, чем видеть часть.
      */}
      {loadFailed ? (
        <div className="lc-notify-panel__empty" role="alert">
          {/* Кегль и цвет — классом, а не пропами: главная строка пустоты
              набирается тем же рецептом, что `.lc-empty__title` у остальных
              пустых состояний продукта (разбор в notifications.css). */}
          <Text className="lc-notify-panel__empty-title">Не удалось загрузить уведомления</Text>
          <Text fz="xs" c="var(--lc-text-3)" mb="var(--lc-space-2)">
            Список может быть неполным — здесь показано не всё, что вам пришло
          </Text>
          <Button size="compact-xs" variant="outline" onClick={retryNotificationsLoad}>
            Повторить
          </Button>
        </div>
      ) : items.length === 0 ? (
        <div className="lc-notify-panel__empty">
          <Text className="lc-notify-panel__empty-title">Пока тихо — уведомлений нет</Text>
          <Text fz="xs" c="var(--lc-text-3)">
            Здесь появится всё важное: аккаунты Авито, приём сообщений, диалоги без ответа
          </Text>
        </div>
      ) : (
        <div className="lc-notify-panel__list">
          {visible.map((n) => (
            <NotificationRow key={n.id} notification={n} onNavigate={onClose} />
          ))}
        </div>
      )}

      <div className="lc-notify-panel__foot">
        {hidden > 0 && (
          <Text fz="xs" c="var(--lc-text-3)">
            Показаны последние {RECENT_PAGE_SIZE}
          </Text>
        )}
        <Button
          size="compact-xs"
          variant="subtle"
          onClick={() => {
            onClose?.();
            navigate("/notifications");
          }}
        >
          Показать все
        </Button>
      </div>
    </div>
  );
}
