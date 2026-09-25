import { useState } from "react";
import { Popover, Tooltip, UnstyledButton } from "@mantine/core";
import { NotificationPanel } from "./NotificationPanel";
import { selectLoadFailed, selectRecent, selectUnread, useNotificationStore } from "./store";
import { useNotificationsEnabled } from "./useNotifications";
import "./notifications.css";
import { IconBell } from "@/shared/ui/Icon";

/**
 * Колокольчик в шапке (14 §3). Показывается только ролям, которым уведомления
 * вообще приходят: наблюдателю бейдж с нулём не нужен — он его никогда не увидит
 * отличным от нуля.
 *
 * ПОЧЕМУ POPOVER, А НЕ MENU (NOTIF-07). `Menu.Dropdown` в Mantine жёстко
 * ставит `role="menu"`, а внутри панели лежат обычные кнопки — не `Menu.Item`
 * и без `role="menuitem"`. Скринридер честно повторял разметку: «меню, 0
 * пунктов», и стрелки ↑/↓ (обработчик Mantine ищет `[data-menu-item]`) не
 * двигались ни на одну строку. Переписывать панель под пункты меню было бы
 * неверно: внутри не команды меню, а список событий с кнопками. Popover не
 * навязывает роль вовсе, и обычные кнопки в нём корректны.
 *
 * `trapFocus` — не украшение: выпадающий список живёт в портале в конце
 * документа, и без переноса фокуса добраться до строк клавишей Tab из шапки
 * было невозможно. Закрытие возвращает фокус на сам колокольчик.
 */
/**
 * Где стоит колокольчик.
 *
 * `rail` — в левой панели, строкой со словом «Уведомления»: там он и живёт
 * после переезда шапки (макет от 12 августа). `header` остаётся ради экранов,
 * где полоса сверху ещё своя, и ради тестов, написанных на прежнюю разметку.
 */
type Placement = "header" | "rail";

export function NotificationBell({ placement = "header" }: { placement?: Placement } = {}) {
  const enabled = useNotificationsEnabled();
  const unread = useNotificationStore(selectUnread);
  const items = useNotificationStore(selectRecent);
  const loadFailed = useNotificationStore(selectLoadFailed);
  const [opened, setOpened] = useState(false);

  if (!enabled) return null;

  /*
   * Подпись колокольчика говорит и о беде (NOTIF-01). Когда загрузка не
   * удалась, бейджа нет и счётчик равен нулю — для того, кто не видит экрана,
   * «Уведомления» звучало бы как «всё спокойно», хотя на деле мы просто не
   * знаем, что там.
   */
  const label = loadFailed
    ? "Уведомления — не загрузились"
    : unread > 0
      ? `Уведомления — непрочитанных: ${unread}`
      : "Уведомления";

  const inRail = placement === "rail";

  /* Точка вместо числа, когда числа мы не знаем: молчаливый ноль на упавшей
     загрузке читался как «новостей нет». */
  const badge = loadFailed ? (
    <span className="lc-bell__badge lc-bell__badge--unknown" aria-hidden="true">
      !
    </span>
  ) : unread > 0 ? (
    <span className="lc-bell__badge" aria-hidden="true">
      {unread > 99 ? "99+" : unread}
    </span>
  ) : null;

  const trigger = (
    <UnstyledButton
      className={inRail ? "lc-rail__item lc-rail__item--bell" : "lc-bell"}
      aria-label={label}
      aria-expanded={opened}
      aria-haspopup="dialog"
      // В свёрнутой панели подпись рисует CSS из этого атрибута; в шапке
      // подсказку по-прежнему даёт Mantine (см. ниже).
      data-tip={inRail ? "Уведомления" : undefined}
      onClick={() => setOpened((o) => !o)}
    >
      <IconBell size={inRail ? 20 : 18} />
      {inRail && <span className="lc-rail__label">Уведомления</span>}
      {badge}
    </UnstyledButton>
  );

  return (
    <Popover
      position={inRail ? "right-end" : "bottom-end"}
      withArrow={!inRail}
      offset={inRail ? 10 : undefined}
      /*
       * ⚠ 380 БЫЛО ЧИСЛОМ, А ЭКРАН БЫВАЕТ УЖЕ (замер 05.09).
       *
       * `width={380}` кладётся в `--popover-width` и держится любой ширины
       * окна. На телефоне 375 панель шире экрана: floating-ui сдвигает её к
       * краю, но не сжимает, и правый столбец — время, «4 раза», подпись
       * ссылки — уезжает за границу. Ровно этим столбцом строка и отличается
       * от соседней.
       *
       * `min()` вместо порога в медиазапросе: у панели нет своего CSS-файла на
       * этот узел (её рисует портал Mantine), и лишний порог был бы четвёртым
       * числом «телефона» в участке. Запас `--lc-space-4` с каждой стороны —
       * чтобы панель не прилипала к краям экрана.
       */
      width="min(380px, calc(100vw - var(--lc-space-4) * 2))"
      opened={opened}
      onChange={setOpened}
      trapFocus
      returnFocus
      shadow="md"
      withinPortal
    >
      <Popover.Target>
        {inRail ? (
          trigger
        ) : (
          <Tooltip label="Уведомления" disabled={opened}>
            {trigger}
          </Tooltip>
        )}
      </Popover.Target>
      <Popover.Dropdown p={0}>
        <NotificationPanel items={items} onClose={() => setOpened(false)} />
      </Popover.Dropdown>
    </Popover>
  );
}
