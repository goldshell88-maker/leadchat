import { useEffect, useRef, useState } from "react";
import { Popover, Text, UnstyledButton } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  requestHotkeysHelp,
  requestSearchFocus,
} from "@/features/hotkeys/focusBus";
import {
  useShortcutLabel,
  сПодписью,
} from "@/features/hotkeys/подписьСочетания";
import { NotificationBell } from "@/features/notifications/NotificationBell";
import {
  useSetPresence,
  usePresenceStore,
} from "@/features/presence/usePresence";
import { смыслОтошёл, useReleaseRule } from "@/features/presence/releaseRule";
import { MyTodayMenuItem } from "@/features/stats/MyTodayWidget";
import {
  selectHasUnseenUpdates,
  useUpdatesSeen,
} from "@/features/updates/seen";
import { queryClient } from "@/app/queryClient";
import { usePermissions, type Role } from "@/shared/auth/usePermissions";
import { MOBILE_QUERY } from "@/shared/lib/breakpoints";
import { formatChatsRailLabel } from "@/shared/stores/badges";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { selectInboxCount, useInboxStore } from "@/shared/stores/inboxStore";
import { useRailExpanded, useRailStore } from "@/shared/stores/railStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { selectTotalUnread, useUnreadStore } from "@/shared/stores/unreadStore";
import {
  IconBot,
  IconBolt,
  IconChart,
  IconChevronLeft,
  IconHelp,
  IconMessage,
  IconSearch,
  IconSettings,
  IconSound,
  IconSoundOff,
  IconTemplate,
} from "@/shared/ui/Icon";
import { LogoMark } from "@/shared/ui/LogoMark";
import { UserAvatar } from "@/shared/ui/UserAvatar";
import { ConnectionIndicator } from "./ConnectionIndicator";
import { preloadOnHover } from "./lazyRoutes";
import "./app-rail.css";

const ROLE_LABELS: Record<Role, string> = {
  admin: "Администратор",
  head: "Руководитель",
  manager: "Менеджер",
  observer: "Наблюдатель",
};

/**
 * Левая панель — теперь весь левый столбец, а не полоска значков (макет
 * владельца от 12 августа).
 *
 * ЧТО ИЗМЕНИЛОСЬ ПО СУТИ, А НЕ ПО ВИДУ. Раньше каркас был двухэтажным: шапка
 * во всю ширину (знак, связь, справка, колокольчик, меню сотрудника) и под
 * ней рельса в 56 пикселей с четырьмя безымянными значками. Разделы были
 * подписаны только подсказкой при наведении, а всё остальное — восемь
 * элементов управления — жило в полосе высотой 56 пикселей, где на 390px
 * половина уезжала за край экрана.
 *
 * Теперь всё, что относится к «я и приложение», собрано в одном столбце и
 * подписано словами. Шапке остаётся название раздела.
 *
 * ДВА СОСТОЯНИЯ, ДВА ПРИЗНАКА НА КОРНЕ. Ширина и подписи — `data-expanded`;
 * «отошёл» — `data-away` там же. Поэтому в состоянии компонента ровно два
 * булевых значения, а точка на аватаре, подпись под именем и янтарная полоса
 * слева меняются сами, из CSS.
 *
 * ПОДСКАЗКИ СВЁРНУТОГО ВИДА — из атрибута `data-tip`, а не компонентом
 * `Tooltip`. Свёрнутая панель это девять безымянных значков; девять порталов
 * Mantine ради подписи в одну строку — цена, которой можно не платить.
 *
 * (Объявление самой панели — ниже: перед ней стоит `useRailExpanded`, потому
 * что тем же ответом пользуется каркас.)
 */

export function AppRail() {
  const expanded = useRailExpanded();
  const toggleRail = useRailStore((s) => s.toggle);
  const isMobile = useMediaQuery(MOBILE_QUERY, false, {
    getInitialValueInEffect: false,
  });
  const away = usePresenceStore((s) => s.status === "away");

  const location = useLocation();
  const navigate = useNavigate();
  const { can } = usePermissions();

  const totalUnread = useUnreadStore(selectTotalUnread);
  const inboxCount = useInboxStore(selectInboxCount);
  const soundEnabled = useChatUiStore((s) => s.soundEnabled);
  const setSoundEnabled = useChatUiStore((s) => s.setSoundEnabled);

  // Кружки на значке `aria-hidden` — обе величины уходят в подпись словами (7.1 п.5).
  const chatsLabel = formatChatsRailLabel({
    unread: totalUnread,
    queue: inboxCount,
  });
  const chatsActive = location.pathname.startsWith("/chats");

  /*
   * Поиск живёт в списке диалогов, а здесь только кнопка к нему. Поэтому
   * сначала уводим на «Чаты» и лишь потом просим фокус: на экране статистики
   * фокусировать нечего, и без перехода нажатие молча ничего не делало бы.
   * Счётчик шины растёт после перехода — поле успевает смонтироваться.
   */
  /* Подпись сочетания — из того же источника, что и разбор нажатия. */
  const поискСочетание = useShortcutLabel("search");

  const goSearch = () => {
    if (!chatsActive) navigate("/chats");
    requestSearchFocus();
  };

  return (
    <nav
      className="lc-rail"
      data-expanded={expanded || undefined}
      data-away={away || undefined}
      aria-label="Основная навигация"
    >
      {/*
        Знак — он же переключатель ширины. Отдельная кнопка «свернуть» заняла
        бы ещё одну строку в столбце, который и так борется за высоту, а
        обещание «нажми на знак — вернёшься на главную» здесь давать нечем:
        главной страницы у рабочего места нет, есть последний открытый раздел.
      */}
      {/* На телефоне ширина не наша, а экрана — там знак просто знак: кнопка,
          которая ничего не меняет, читается как поломка. */}
      {isMobile ? (
        <div className="lc-rail__logo">
          <LogoMark size={30} />
        </div>
      ) : (
        <UnstyledButton
          className="lc-rail__logo"
          onClick={toggleRail}
          aria-expanded={expanded}
          aria-label={expanded ? "Свернуть панель" : "Развернуть панель"}
          data-tip="Развернуть панель"
        >
          <LogoMark size={30} />
          <span className="lc-rail__wordmark">LeadChat</span>
          <IconChevronLeft size={16} className="lc-rail__collapse" />
        </UnstyledButton>
      )}

      {/*
        У КАЖДОЙ СТРОКИ ЕСТЬ `aria-label`, И ЭТО НЕ ПЕРЕСТРАХОВКА. В свёрнутом
        виде подпись спрятана `display: none`, а спрятанный так текст не
        участвует в вычислении доступного имени: строка остаётся кнопкой БЕЗ
        НАЗВАНИЯ. Проверено деревом доступности прямо на стенде — «Чаты»
        читались (у них подпись была своя), а «Разбор диалогов», «Статистика»
        и «Настройки» шли безымянными ссылками.
      */}
      {/*
        Ленивые разделы предзагружаются по наведению и фокусу — ДО нажатия
        (`lazyRoutes.ts`); «Чаты» жадные, им нечего грузить. Класс `pending`
        на время загрузки вешает сам `NavLink` (nav-pending.css).
      */}
      <div className="lc-rail__section">Разделы</div>
      <div className="lc-rail__zone">
        <NavLink
          to="/chats"
          className="lc-rail__item"
          data-active={chatsActive || undefined}
          data-tip="Чаты"
          aria-label={chatsLabel}
        >
          <IconMessage size={20} />
          <span className="lc-rail__label">Чаты</span>
          {/*
            Бейджи гаснут, КОГДА ОПЕРАТОР УЖЕ В ЧАТАХ (UX-аудит, docs/17): там
            те же числа стоят крупнее и подписанными, на вкладках «Входящие» и
            «Мои». На других разделах бейдж — единственный способ узнать, что
            очередь растёт.

            Очередь и непрочитанное разведены цветом и порядком: очередь
            первой, потому что с неё начинается смена.
          */}
          {!chatsActive && (inboxCount > 0 || totalUnread > 0) && (
            <span className="lc-rail__badges" aria-hidden="true">
              {inboxCount > 0 && (
                <span className="lc-rail__badge lc-rail__badge--queue">
                  {inboxCount > 99 ? "99+" : inboxCount}
                </span>
              )}
              {totalUnread > 0 && (
                <span className="lc-rail__badge">
                  {totalUnread > 99 ? "99+" : totalUnread}
                </span>
              )}
            </span>
          )}
        </NavLink>

        {/* Таблица диалогов (план 7.3) — рядом со статистикой: это два взгляда
            на одно, цифрами и построчно. Право с 27.08 своё и есть у всех
            ролей: владелец просил открыть разбор менеджерам, а статистику и
            ленту оставить руководителям. */}
        {can("dialogs:read") && (
          <NavLink
            to="/dialogs"
            {...preloadOnHover("/dialogs")}
            className="lc-rail__item"
            data-active={location.pathname.startsWith("/dialogs") || undefined}
            data-tip="Разбор диалогов"
            aria-label="Разбор диалогов"
            title="Разбор диалогов"
          >
            <IconTemplate size={20} />
            <span className="lc-rail__label">Разборы</span>
          </NavLink>
        )}

        {/*
          Надзор за ботом (docs/45) — сразу под «Разбором диалогов»: это тот же
          разбор, только про одного участника.

          ПУНКТ АДМИНСКИЙ, и право уже такое: `bots:manage` есть только у роли
          `admin`. Руководителю его не дали намеренно — он смотрит за людьми, а
          за ботом смотрит тот, кто им управляет; увидеть сбой и не иметь
          возможности его починить — худший из видов надзора.
        */}
        {can("bots:manage") && (
          <NavLink
            to="/bot-dialogs"
            {...preloadOnHover("/bot-dialogs")}
            className="lc-rail__item"
            data-active={
              location.pathname.startsWith("/bot-dialogs") || undefined
            }
            data-tip="Диалоги бота"
            aria-label="Диалоги бота"
            title="Диалоги бота"
          >
            <IconBot size={20} />
            <span className="lc-rail__label">Бот</span>
          </NavLink>
        )}

        {/*
          Живая лента — рядом со статистикой и по её же праву (`stats:all`).

          ПОЧЕМУ ЗДЕСЬ, А НЕ У «ЧАТОВ». Владелец просил «смотреть логи работы в
          реальном времени»: это надзор, а не работа с клиентом. Тот же вопрос,
          что и у статистики, — «как идут дела», — только не за неделю, а за
          последние десять минут; поэтому и место, и право у них общие.

          ОПЕРАТОРУ ПУНКТА НЕТ намеренно. Всё, что лента показала бы ему, у него
          и так на экране: своя очередь, свои диалоги, судьба своих ответов.
          Отдельный экран добавил бы ему не сведений, а поводов отвлечься —
          бегущая строка притягивает взгляд сильнее, чем ждущий клиент.
        */}
        {can("stats:all") && (
          <NavLink
            to="/feed"
            {...preloadOnHover("/feed")}
            className="lc-rail__item"
            data-active={location.pathname.startsWith("/feed") || undefined}
            data-tip="Живая лента"
            aria-label="Живая лента"
          >
            <IconBolt size={20} />
            <span className="lc-rail__label">Живая лента</span>
          </NavLink>
        )}

        {/* Статистика — только при `stats:all` (03 §5.2): у менеджера вместо
            экрана виджет в подвале списка. */}
        {can("stats:all") && (
          <NavLink
            to="/stats"
            {...preloadOnHover("/stats")}
            className="lc-rail__item"
            data-active={location.pathname.startsWith("/stats") || undefined}
            data-tip="Статистика"
            aria-label="Статистика"
          >
            <IconChart size={20} />
            <span className="lc-rail__label">Статистика</span>
          </NavLink>
        )}

        {/* Настройки видны всем — внутри у роли своё наполнение (11 §4) */}
        <NavLink
          to="/settings/profile"
          {...preloadOnHover("/settings/profile")}
          className="lc-rail__item"
          data-active={location.pathname.startsWith("/settings") || undefined}
          data-tip="Настройки"
          aria-label="Настройки"
        >
          <IconSettings size={20} />
          <span className="lc-rail__label">Настройки</span>
        </NavLink>
      </div>

      {/* Воздух — МЕЖДУ разделами и «Под рукой», а не под ней (аудит 15.09:
          300 px пустоты посреди меню): поиск, звук и клавиши стоят у
          уведомлений и профиля, к которым и относятся. */}
      <div className="lc-rail__spacer" />

      <div className="lc-rail__divider" />
      <div className="lc-rail__section">Под рукой</div>
      <div className="lc-rail__zone">
        {/*
          ⚠ ПОДСКАЗКА КОРОЧЕ ПОЛЯ, В КОТОРОЕ ОНА РИСУЕТСЯ (замер 05.09).
          Здесь стояло «Поиск по имени, телефону или сообщению — Ctrl+K»: 327
          пикселей при потолке подсказки в 240 (`app-rail.css`). Многоточие
          съедало ровно тот хвост, ради которого подсказку и читают, —
          сочетание клавиш; на экране оставалось «…или соо…». Теперь подпись
          та же, что и доступное имя: одно место, одна формулировка, и
          помещается целиком (185 пикселей).
        */}
        {/*
          ⚠ СОЧЕТАНИЕ СПРАШИВАЕТСЯ, А НЕ ЗАШИВАЕТСЯ (аудит 08.09, H-06). Здесь
          в трёх местах стояло «Ctrl+K» строкой, а действие `search` объявлено
          `offByDefault: true` — по умолчанию оно не привязано ни к чему и не
          работает. Значок обещал клавишу, которой нет.
        */}
        <button
          type="button"
          className="lc-rail__item"
          onClick={goSearch}
          data-tip={сПодписью("Поиск по диалогам", поискСочетание)}
          aria-label={сПодписью("Поиск по диалогам", поискСочетание)}
        >
          <IconSearch size={20} />
          <span className="lc-rail__label">Поиск</span>
          {поискСочетание && (
            <span className="lc-rail__key">{поискСочетание}</span>
          )}
        </button>

        {/*
          Звук вынесен из настроек НАРУЖУ намеренно. Его выключают не «однажды
          и навсегда», а на время: пришёл человек, идёт разговор по телефону.
          Три нажатия до профиля ради этого не делают — работают с включённым
          звуком и раздражаются, либо выключают и забывают вернуть. Здесь
          состояние подписано словом и снимается тем же нажатием.
        */}
        <button
          type="button"
          className="lc-rail__item"
          onClick={() => setSoundEnabled(!soundEnabled)}
          aria-pressed={soundEnabled}
          data-tip={
            soundEnabled
              ? "Звук новых сообщений включён"
              : "Звук новых сообщений выключен"
          }
          aria-label={
            soundEnabled
              ? "Звук новых сообщений включён"
              : "Звук новых сообщений выключен"
          }
        >
          {soundEnabled ? <IconSound size={20} /> : <IconSoundOff size={20} />}
          {/* «Звук новых сообщений» из макета не помещался в 218 пикселей и
              ехал во вторую строку, ломая ритм столбца. Слово «новых» тут
              лишнее: старые сообщения звуком и не сопровождаются. Полная
              подпись осталась в подсказке. */}
          <span className="lc-rail__label">Звук</span>
          {/* Состояние — переключателем с видимым положением, а не самым тусклым
              словом меню («выкл» 2,71:1 — аудит интерфейса 15.09). */}
          <span
            className="lc-rail__toggle"
            data-on={soundEnabled || undefined}
            aria-hidden="true"
          />
        </button>

        <button
          type="button"
          className="lc-rail__item"
          onClick={requestHotkeysHelp}
          data-tip="Горячие клавиши"
          aria-label="Горячие клавиши"
          title="Горячие клавиши"
        >
          <IconHelp size={20} />
          <span className="lc-rail__label">Клавиши</span>
          <span className="lc-rail__key lc-rail__kbd">?</span>
        </button>
      </div>

      <div className="lc-rail__zone">
        <NotificationBell placement="rail" />
        <MeButton />
      </div>
    </nav>
  );
}

/**
 * Кнопка сотрудника и всплывающее окно под ней.
 *
 * ЗДЕСЬ ЖЕ ПЕРЕКЛЮЧАЕТСЯ «НА МЕСТЕ / ОТОШЁЛ» — и это возврат к тому, что
 * 11 августа отсюда убрали. Тогда владелец попросил перенести переключение «в
 * поле, где настройки», и оно уехало в профиль; макет от 12 августа приносит
 * его обратно, но уже иначе. Разница существенная: раньше состояние
 * СУЩЕСТВОВАЛО только внутри закрытого меню — не видя его, человек не знал,
 * что выключен из автораздачи. Теперь оно видно ВСЕГДА: янтарная полоса на
 * панели, точка на аватаре и слово под именем.
 *
 * Блок в профиле остаётся: оба управляют одним и тем же состоянием на
 * сервере (`useSetPresence`), так что разойтись им нечем.
 */
function MeButton() {
  const user = useSessionStore((s) => s.user);
  const logout = useSessionStore((s) => s.logout);
  const { can, role } = usePermissions();
  const isMobile = useMediaQuery(MOBILE_QUERY, false, {
    getInitialValueInEffect: false,
  });
  const showMyToday = isMobile && role === "manager" && can("stats:own");
  const away = usePresenceStore((s) => s.status === "away");
  const setPresence = useSetPresence();
  const освобождение = useReleaseRule();
  const hasUnseenUpdates = useUpdatesSeen(selectHasUnseenUpdates);
  const navigate = useNavigate();
  const [opened, setOpened] = useState(false);

  // Переход в другой раздел закрывает окно: иначе оно висит поверх нового
  // экрана и перекрывает то, ради чего туда шли.
  const path = useLocation().pathname;
  const seen = useRef(path);
  useEffect(() => {
    if (seen.current !== path) {
      seen.current = path;
      setOpened(false);
    }
  }, [path]);

  if (!user) return null;

  const go = (to: string) => {
    setOpened(false);
    navigate(to);
  };

  const handleLogout = async () => {
    setOpened(false);
    await logout();
    // 03 §2.3: смена пользователя — полная очистка кэша (+ клиентские счётчики).
    queryClient.clear();
    useUnreadStore.getState().clear();
    navigate("/login", { replace: true });
  };

  return (
    <Popover
      position="right-end"
      offset={10}
      width={288}
      opened={opened}
      onChange={setOpened}
      trapFocus
      returnFocus
      shadow="md"
      withinPortal
    >
      {/*
        ⚠ В ПОДСКАЗКЕ СОСТОЯНИЕ ИДЁТ ПЕРВЫМ, ИМЯ ВТОРЫМ (замер 05.09).

        Было наоборот: имя, тире, состояние. Подсказка свёрнутого рельса
        обрезана потолком в 240 пикселей (`app-rail.css`), а имена сотрудников
        заводит администратор по паспорту — «Станислав Александрович
        Гончаренко — на месте» просит 323. Многоточие съедало хвост, то есть
        ровно состояние: единственное, чего в свёрнутом виде узнать больше
        неоткуда — подпись под именем там спрятана.

        Имя в этой подсказке самое избыточное: человек знает, под кем сидит.
        Поэтому вперёд выходит то, что проверяют.
      */}
      <Popover.Target>
        <UnstyledButton
          className="lc-rail__me"
          aria-expanded={opened}
          aria-haspopup="dialog"
          aria-label={`${user.full_name} — ${away ? "вы отошли" : "вы на месте"}`}
          data-tip={`${away ? "Отошёл" : "На месте"} · ${user.full_name}`}
          onClick={() => setOpened((o) => !o)}
        >
          <span className="lc-rail__ava">
            <UserAvatar name={user.full_name} size={36} />
            <i className="lc-rail__state" aria-hidden="true" />
          </span>
          <span className="lc-rail__who">
            {user.full_name}
            <em>{away ? "Отошёл" : "На месте"}</em>
          </span>
        </UnstyledButton>
      </Popover.Target>

      <Popover.Dropdown className="lc-me-pop">
        <Text fz="sm" fw={600} c="var(--lc-text-1)" lh={1.25}>
          {user.full_name}
        </Text>
        <Text
          fz="xs"
          c="var(--lc-text-3)"
          lh={1.3}
          style={{ overflowWrap: "anywhere" }}
        >
          {user.email} · {ROLE_LABELS[user.role]}
        </Text>

        {/*
          Два состояния переключателем, а не галкой: у галки «выключено» — это
          отсутствие пометки, то есть состояние без имени. Здесь оба названы, и
          нажатое видно и цветом, и `aria-pressed`.
        */}
        <div
          className="lc-me-pop__seg"
          role="group"
          aria-label="Ваше состояние"
        >
          <button
            type="button"
            aria-pressed={!away}
            onClick={() => setPresence.mutate("online")}
            disabled={setPresence.isPending}
          >
            <u aria-hidden="true" />
            На месте
          </button>
          <button
            type="button"
            className="is-away"
            aria-pressed={away}
            onClick={() => setPresence.mutate("away")}
            disabled={setPresence.isPending}
          >
            <u aria-hidden="true" />
            Отошёл
          </button>
        </div>

        {/* Что именно делает «отошёл» для этого человека (правило освобождения). */}
        <p className="lc-me-pop__hint">{смыслОтошёл(освобождение)}</p>

        <div className="lc-me-pop__menu">
          {/* «Моя статистика» — только менеджеру и только на телефоне: на
              широком экране те же цифры стоят в подвале списка диалогов
              (11 §6.4), а на `/chats/:id` левой колонки на экране нет. */}
          {showMyToday && <MyTodayMenuItem />}
          {/* Пункты меню ведут в ленивые разделы — предзагрузка та же, что у рельсы. */}
          <button
            type="button"
            onClick={() => go("/settings/profile")}
            {...preloadOnHover("/settings/profile")}
          >
            Профиль
          </button>
          <button
            type="button"
            onClick={() => go("/updates")}
            {...preloadOnHover("/updates")}
          >
            Что нового
            {hasUnseenUpdates && (
              <span className="lc-menu-dot" aria-label="есть непрочитанное" />
            )}
          </button>
          <button
            type="button"
            className="is-exit"
            onClick={() => void handleLogout()}
          >
            Выйти
          </button>
        </div>

        {/* Связь с сервером — здесь, а не в шапке: шапки во всю ширину больше
            нет. Молчащий канал при этом виден и без раскрытия окна, полосой
            над рабочим местом (`ConnectionBanner`). */}
        <div className="lc-me-pop__conn">
          <ConnectionIndicator />
        </div>
      </Popover.Dropdown>
    </Popover>
  );
}
