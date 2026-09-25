import { useContext, useEffect, useRef, useState } from "react";
import { AppShell } from "@mantine/core";
import { HotkeysModal } from "@/features/hotkeys/HotkeysModal";
import { useFocusBus } from "@/features/hotkeys/focusBus";
import {
  useClaimKeyGuard,
  useGlobalHotkeys,
} from "@/features/hotkeys/useChatHotkeys";
import { useAutoAway } from "@/features/presence/автоОтошёл";
import { CriticalBanners } from "@/features/notifications/CriticalBanners";
import { useNotificationsBootstrap } from "@/features/notifications/useNotifications";
import { UpdateBanner } from "@/platform/UpdateBanner";
import { usePermissions } from "@/shared/auth/usePermissions";
import { useRoleUiSync } from "@/shared/auth/useRoleUiSync";
import { setDocumentSection } from "@/shared/stores/badges";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import {
  RAIL_WIDTH_COLLAPSED,
  RAIL_WIDTH_EXPANDED,
  useRailExpanded,
} from "@/shared/stores/railStore";
import { useSessionStore } from "@/shared/stores/sessionStore";
import {
  Outlet,
  UNSAFE_DataRouterContext as DataRouterContext,
  useLocation,
  useNavigation,
} from "react-router-dom";
import { AppRail } from "./AppRail";
import { ServerVersionWatch } from "./ConnectionIndicator";
import { ErrorBoundary } from "./ErrorBoundary";
import { PermissionsBanner } from "./PermissionsBanner";
import { предзагрузитьВПростое, type LazyRoutePath } from "./lazyRoutes";
import "./app-layout.css";
import "./nav-pending.css";
import { IconAlert, IconCheck } from "@/shared/ui/Icon";

/** Полоса потери соединения (11 §8.1): жёлтая при reconnect, зелёная 2 с при восстановлении. */
function ConnectionBanner() {
  const status = useConnectionStore((s) => s.status);
  const prev = useRef(status);
  const [justRestored, setJustRestored] = useState(false);

  useEffect(() => {
    const was = prev.current;
    prev.current = status;
    if (was === "reconnecting" && status === "open") {
      setJustRestored(true);
      const t = window.setTimeout(() => setJustRestored(false), 2000);
      return () => window.clearTimeout(t);
    }
  }, [status]);

  if (status === "reconnecting") {
    return (
      <div className="lc-conn-banner lc-conn-banner--warn" role="status">
        <IconAlert size={14} /> Нет соединения — переподключаемся…
      </div>
    );
  }
  if (justRestored) {
    return (
      <div className="lc-conn-banner lc-conn-banner--ok" role="status">
        <IconCheck size={14} /> Соединение восстановлено
      </div>
    );
  }
  return null;
}

/**
 * Каркас на время тихого входа (`RequireAuth`, пока `bootstrapped` false).
 *
 * ЧТО БЫЛО. Полноэкранный `FullscreenLoader`: логотип и спиннер на пустом
 * поле, а следом рабочее место возникает целиком. Это заметный рывок при
 * каждом открытии вкладки — и он стал частым: полоса «Вышло обновление»
 * (SHELL-02) кончается перезагрузкой, то есть человек сам просит обновить и
 * в награду получает вспышку пустоты.
 *
 * ЧТО ЗДЕСЬ. Оболочка рисуется сразу и в тех же размерах, что настоящая:
 * шапка с логотипом, рельса, две панели. Подмена происходит внутри рамки, а
 * не вместо неё, — рамка не шелохнётся.
 *
 * Живого содержимого здесь быть не может: сессии ещё нет, права неизвестны,
 * состав рельсы зависит от роли. Поэтому значки рельсы не рисуются вовсе —
 * нарисовать не тот набор хуже, чем не рисовать: он моргнёт при подмене.
 */
export function AppChromeSkeleton() {
  /*
   * ⚠ РЕЛЬС У СКЕЛЕТА БЫЛ ШИРИНОЙ 72 ВСЕГДА (замер на стенде 05.09).
   *
   * `.lc-boot__rail` берёт ширину из `--lc-rail-w`, но настоящее число
   * приезжает инлайновым стилем на корень `AppShell` (ниже), а скелет
   * рисуется ВМЕСТО него — то есть переменной взять неоткуда, и в дело шло
   * запасное значение из `lc-vars.css`, равное свёрнутому рельсу.
   *
   * По умолчанию же панель РАЗВЁРНУТА (railStore: «команда переезжает с Jivo,
   * где разделы подписаны словами»), то есть у большинства она 218. Замер на
   * стенде при 1440: колонка списка у скелета начиналась на 96 пикселе, у
   * рабочего места — на 234. Вся работа прыгала вправо на 146 пикселей ровно
   * в тот кадр, ради неподвижности которого скелет и написан.
   *
   * Ответ берётся из того же `useRailExpanded`, что и у каркаса: две копии
   * одного решения разошлись бы на первой правке порога.
   */
  const railWidth = useRailExpanded()
    ? RAIL_WIDTH_EXPANDED
    : RAIL_WIDTH_COLLAPSED;

  return (
    <div
      className="lc-boot"
      role="status"
      aria-busy="true"
      aria-label="Загружаем рабочее место"
      style={{ "--lc-rail-w": `${railWidth}px` } as React.CSSProperties}
    >
      {/*
        ⚠ ШАПКИ ЗДЕСЬ БОЛЬШЕ НЕТ (разбор дизайна 28.08).
        Скелет рисовал полосу с логотипом и надписью «LeadChat», а у настоящего
        рабочего места такой полосы нет: `--lc-header-h` равен нулю. Правило
        `.lc-boot__header` брало высоту оттуда же, получало 0px — и логотип с
        надписью ложились ПОВЕРХ тела скелета. Скелет обязан повторять
        настоящую раскладку, а не отменённую: иначе он обещает экран, которого
        через мгновение не окажется.
      */}
      <div className="lc-boot__body">
        <div className="lc-boot__rail" />
        <div className="lc-boot__panes">
          <div className="lc-boot__pane lc-boot__pane--list" />
          <div className="lc-boot__pane lc-boot__pane--thread" />
          {/*
            ⚠ ТРЕТЬЯ ПАНЕЛЬ — КАРТОЧКА КЛИЕНТА (замер на стенде 05.09).
            От 1360 пикселей она стоит в потоке ВСЕГДА, даже когда диалог не
            выбран (ChatsPage.tsx). Скелет её не рисовал — и обещал две
            колонки там, где через мгновение появлялись три: рамка дёргалась
            ровно в том кадре, ради неподвижности которого скелет и заведён.
            Прячет её на узких экранах CSS, а не условие в разметке: порог
            один и живёт там же, где остальные (app-layout.css).
          */}
          <div className="lc-boot__pane lc-boot__pane--card" />
        </div>
      </div>
    </div>
  );
}

/**
 * Каркас рабочего места: левая панель во всю высоту и шапка с названием
 * раздела (макет владельца от 12 августа).
 *
 * ЧТО ПЕРЕЕХАЛО И ПОЧЕМУ. Шапка была двухэтажной конструкцией: знак, состояние
 * связи, справка, колокольчик и меню сотрудника в полосе высотой 56 пикселей,
 * а под ней — рельса в 56 пикселей с четырьмя безымянными значками. На 390px
 * содержимое шапки уезжало за край экрана; разделы были подписаны только
 * подсказкой при наведении. Всё это собрано в левый столбец
 * (`AppRail`) и подписано словами, а шапке осталось единственное, чего в
 * столбце быть не может, — где мы сейчас.
 */
/**
 * Имя раздела по пути — слова те же, что на рейке (`AppRail.tsx`): вкладку
 * ищут по тому слову, которым раздел называется в самой системе. Порядок
 * проверок — от частного к общему; `null` на незнакомом пути честнее догадки.
 */
function sectionTitle(pathname: string): string | null {
  if (pathname.startsWith("/chats")) return "Чаты";
  if (pathname.startsWith("/dialogs")) return "Разбор диалогов";
  if (pathname.startsWith("/bot-dialogs")) return "Диалоги бота";
  if (pathname.startsWith("/stats")) return "Статистика";
  if (pathname.startsWith("/notifications")) return "Уведомления";
  if (pathname.startsWith("/feed")) return "Живая лента";
  if (pathname.startsWith("/settings")) return "Настройки";
  if (pathname.startsWith("/updates")) return "Обновления";
  return null;
}

/**
 * Полоса «раздел едет» у верхнего края рабочей области (жалоба владельца
 * 06.09: «задержки»).
 *
 * `lazy` намеренно держит прежний экран на месте, пока грузится чанк (довод у
 * `/dialogs` в router.tsx), и без полосы нажатие на медленной сети секунду-две
 * не имело никакого следа. Пункт, к которому идём, подсвечивает сам `NavLink`
 * (класс `pending`, nav-pending.css), а полоса нужна тем переходам, где
 * `NavLink` нет: клавиатура, меню сотрудника, ссылка из уведомления.
 *
 * `role="progressbar"` без значения — «неопределённый» прогресс: сколько
 * осталось, неизвестно ни нам, ни браузеру.
 */
function NavProgress() {
  const navigation = useNavigation();
  if (navigation.state !== "loading") return null;
  return (
    <div
      className="lc-nav-progress"
      role="progressbar"
      aria-label="Загружаем раздел"
    />
  );
}

/**
 * Через сколько после первого кадра просыпается прогрев разделов.
 *
 * ЧИСЛО ВЫБРАНО ПО ЗАМЕРАМ, А НЕ НА ГЛАЗ. В первые секунды после кадра канал
 * занят собственным стартом рабочего места: «кто я», список диалогов (p50 39 /
 * p95 114 мс), значки, уведомления, здоровье, поднятие сокета — а следом
 * первым же действием человек открывает диалог, и это ещё деталь плюс лента.
 * При офисном круге ~120 мс всё это укладывается в три секунды. Раньше —
 * встанем в очередь позади собственного старта и задержим ровно то, ради чего
 * человек открыл вкладку. Сильно позже — проигрываем гонку с его первым
 * переходом, а после выкатки этот переход ловит 404 на исчезнувшем чанке
 * (разбор у `предзагрузитьВПростое`).
 *
 * Пауза — это не «столько длится простой»: простой сверх неё ждёт сам браузер
 * (`requestIdleCallback`). Здесь только момент, раньше которого спрашивать
 * простоя не стоит.
 */
export const ПАУЗА_ДО_ПРОГРЕВА_МС = 3000;

/**
 * Что греем и в каком порядке.
 *
 * ⚠ ПРАВИЛА ЗДЕСЬ — НЕ НОВЫЕ, А ТЕ ЖЕ, ЧТО У ПУНКТОВ МЕНЮ. Разделы рельсы
 * закрыты условиями из `AppRail.tsx`, разделы настроек — из
 * `SettingsLayout.tsx`, а сами права берутся из единственного их источника
 * (`usePermissions` → `GET /auth/me`). Разойдись эти условия с меню — очередь
 * начнёт греть то, чего человек не видит и открыть не может, то есть тратить
 * его канал впустую. Сторож `IdlePreload0709` сверяет список с пунктами,
 * которые при тех же правах реально нарисованы.
 *
 * Порядок — как человек ходит: сначала разделы рельсы сверху вниз, потом
 * настройки. `/chats` в списке нет: он жадный и приезжает с бандлом.
 *
 * НЕ ГРЕЕМ то, что не в рельсе и не в меню настроек: «Что нового» и журнал
 * уведомлений открывают раз в неделю (у первого есть прогрев по наведению),
 * `/ui-kit` — раз в спринт, `/settings/bots/:id` открывают ИЗ «Ботов», то есть
 * его чанк успевает приехать, пока человек выбирает бота, а `/invite/:token`
 * достаётся тому, у кого сессии ещё нет.
 */
function путиПрогрева({
  can,
  canAny,
}: ReturnType<typeof usePermissions>): LazyRoutePath[] {
  const пути: LazyRoutePath[] = [];

  if (can("dialogs:read")) пути.push("/dialogs");
  if (can("bots:manage")) пути.push("/bot-dialogs");
  if (can("stats:all")) пути.push("/feed", "/stats");
  // «Настройки» в рельсе ведут в профиль, и пункт есть у всех ролей.
  пути.push("/settings/profile");

  if (can("accounts:read"))
    пути.push("/settings/accounts", "/settings/accounts/operators");
  if (can("templates:own") || can("templates:shared"))
    пути.push("/settings/templates");
  if (can("bots:manage")) пути.push("/settings/bots", "/settings/leadbot");
  if (can("settings:manage"))
    пути.push("/settings/leads", "/settings/distribution", "/settings/apis");
  if (canAny("users:manage", "audit:read")) пути.push("/settings/team");

  return пути;
}

/**
 * Прогрев разделов в простое (07.09).
 *
 * ЧТО БЫЛО. Чанк раздела грелся только по наведению или фокусу на пункте
 * (`preloadOnHover`, 06.09). Это закрывает мышь и клавиатурный обход рельсы —
 * и не закрывает ничего больше: переход сочетанием клавиш, касание на
 * планшете, back/forward, `navigate()` из кода и заход по прямой ссылке
 * по-прежнему платят круг до сервера целиком.
 *
 * ЧТО ЗДЕСЬ. Каркас решает только КОГДА будить очередь, а как она себя ведёт —
 * её собственное дело (`предзагрузитьВПростое`). Три условия на запуск:
 *
 *  1) ПОСЛЕ ПЕРВОГО ПОЛЕЗНОГО КАДРА, а не на монтировании: заводится таймер на
 *     три секунды, и это единственное, что прогрев делает в кадре, ради
 *     скорости которого он и написан;
 *  2) ЖИВАЯ СЕССИЯ. До ответа «кто я» права пусты, и список вышел бы куцым —
 *     прогрели бы не то. Заодно это отсекает экран входа: там ленивых разделов
 *     нет вовсе;
 *  3) ВИДИМАЯ ВКЛАДКА — за это отвечает сама очередь, ей же следить за
 *     сворачиванием посреди прогрева.
 *
 * Уход с экрана снимает и таймер, и очередь: пережить каркас она не должна.
 *
 * Имя латиницей, как у соседних крючков (`useAutoAway`, `useRoleUiSync`):
 * правило `react-hooks/rules-of-hooks` узнаёт крючок по `use` + ЗАГЛАВНАЯ
 * латинская, и на кириллице ругается на каждый вызов внутри.
 */
function useIdlePreload(): void {
  const права = usePermissions();
  const bootstrapped = useSessionStore((s) => s.bootstrapped);
  const естьСессия = useSessionStore((s) => s.user !== null);

  useEffect(() => {
    if (!bootstrapped || !естьСессия) return;
    const пути = путиПрогрева(права);

    let отменитьОчередь: (() => void) | null = null;
    const таймер = window.setTimeout(() => {
      отменитьОчередь = предзагрузитьВПростое(пути);
    }, ПАУЗА_ДО_ПРОГРЕВА_МС);

    return () => {
      window.clearTimeout(таймер);
      отменитьОчередь?.();
    };
  }, [bootstrapped, естьСессия, права]);
}

export function AppLayout() {
  /*
   * Справка по сочетаниям живёт ЗДЕСЬ, а не на экране чатов: «?» работает из
   * любого раздела, и окно обязано быть там же, где каркас.
   */
  const [helpOpen, setHelpOpen] = useState(false);
  const helpNonce = useFocusBus((s) => s.helpNonce);
  useEffect(() => {
    if (helpNonce > 0) setHelpOpen(true);
  }, [helpNonce]);

  const location = useLocation();
  // Раздел — в заголовок вкладки: «Статистика · LeadChat» находится глазами
  // среди десятка вкладок, «LeadChat» ×10 — нет. Имена — те же, что в рейке:
  // вкладка обязана называться тем словом, по которому раздел искали.
  useEffect(() => {
    setDocumentSection(sectionTitle(location.pathname));
  }, [location.pathname]);
  useGlobalHotkeys(); // Ctrl+K работает с любого экрана (11 §8.4)
  /*
   * ⚠ ЗАСЛОН КЛАВИШИ ПРИЁМА — НА ВЕСЬ ПРИЛОЖЕНИЕ, А НЕ НА ЭКРАН ЧАТОВ.
   *
   * Он существовал и работал, но жил внутри `useChatHotkeys`, который
   * монтируется только на `/chats`. Стоило открыть настройки, статистику или
   * разбор диалогов — и `Ctrl+R` снова доставался браузеру: перезагрузка
   * рабочего места и потерянный набранный ответ. Жалоба владельца повторялась
   * трижды именно поэтому.
   *
   * Здесь же исполняется и просьба «принимать диалоги из любой вкладки, не
   * заходя во Входящие»: с любого экрана клавиша берёт следующего ждущего.
   */
  useClaimKeyGuard();
  /*
   * «Отошёл» ставится сам: присутствие теперь считается по действиям человека,
   * а не по открытой вкладке (жалоба 31.08 «онлайн, а их нет — просто включены
   * ПК»). Разбор — в `features/presence/автоОтошёл.ts`.
   */
  useAutoAway();
  useRoleUiSync(); // смена пользователя/роли сбрасывает UI-состояние чатов
  useNotificationsBootstrap(); // колокольчик и плашки критичного (14 §3)
  useIdlePreload(); // чанки разделов греются в простое, а не по нажатию

  const railExpanded = useRailExpanded();
  const railWidth = railExpanded ? RAIL_WIDTH_EXPANDED : RAIL_WIDTH_COLLAPSED;
  /*
   * ⚠ `useNavigation` БРОСАЕТ ВНЕ DATA-РОУТЕРА. В бою каркас стоит под
   * `createBrowserRouter`, но сторожа `staleBuild` и `ErrorBoundary` поднимают
   * его под обычным `MemoryRouter` — там состояния перехода просто нет. Полоса
   * вынесена в свой компонент и монтируется, только когда ей есть откуда взять
   * состояние.
   */
  const hasNavigationState = useContext(DataRouterContext) !== null;

  return (
    <AppShell
      navbar={{ width: railWidth, breakpoint: 0 }}
      padding={0}
      /* Ширина столбца нужна и собственному CSS — списку и ленте, которые
         считают от неё свои левые отступы. Одно число из одного места. */
      style={{ "--lc-rail-w": `${railWidth}px` } as React.CSSProperties}
    >
      <AppShell.Navbar>
        <AppRail />
      </AppShell.Navbar>

      {/* Опрос здоровья сервера держится здесь, а не там, где его показывают:
          на том же ответе висит сверка версии сборки (SHELL-02), а показ
          переехал внутрь закрытого по умолчанию окна. */}
      <ServerVersionWatch />

      {/*
        `lc-main` — колонка, а НЕ просто обёртка.

        Полоса критичного (`CriticalBanners`) стоит в потоке: она обязана
        отодвигать работу, а не накрывать её собой — под ней «Аккаунт Авито
        требует переподключения», и закрытая кнопка «Переподключить» стоит
        дороже сдвинутого списка. Но экраны считали свою высоту от окна
        (`100dvh`), то есть полоса ВЫТАЛКИВАЛА их за нижний край: замер на
        стенде — документ 976 при окне 900, поле ввода ответа под краем экрана
        и полоса прокрутки у всей страницы.

        Теперь высоту отдаёт колонка: полоса занимает столько, сколько ей
        нужно, экран забирает остаток.
      */}
      <AppShell.Main className="lc-main">
        {/*
          Три полосы — в общей стопке В ПОТОКЕ колонки (см. app-layout.css,
          `.lc-banners`): они отодвигают работу вниз, а не накрывают её. Здесь
          стоял оверлей, и он прятал управление на верхней кромке страницы —
          «Выгрузить CSV» на «Диалогах», пункт «Аккаунты Авито» в настройках
          (аудит 08.09, H-04).

          Порядок в стопке — по срочности: «нет соединения» важнее, чем
          «вышло обновление», и стоит выше.
        */}
        {hasNavigationState && <NavProgress />}
        <div className="lc-banners">
          <ConnectionBanner />
          {/* «Кто я» не ответил — права пусты, разделы исчезли (SHELL-04).
              Выше «обновления» и ниже «нет соединения»: пропавший доступ
              срочнее выкатки, но если связи нет вовсе, объяснять надо это. */}
          <PermissionsBanner />
          {/* Обновление: на десктопе — новая версия приложения (04 §6.3),
              в вебе — новая сборка фронта на сервере (SHELL-02). */}
          <UpdateBanner />
        </div>
        {/* Критичные уведомления висят поверх экрана, пока их не подтвердят (14 §3). */}
        <CriticalBanners />
        {/*
          Граница отказа ВНУТРИ каркаса, а не вокруг него (аудит SHELL-01).
          Исключение в отрисовке любого экрана раньше уносило рабочее место
          целиком — вместе с шапкой, рельсой, колокольчиком и плашками
          критичного, потому что они тоже внутри маршрутного дерева. Здесь
          гаснет только содержимое `Outlet`: человек видит, что случилось,
          и уходит в другой раздел одним нажатием по рельсе.

          `resetKey` — путь маршрута: без него один сбой запирал бы человека
          на экране отказа навсегда. Граница React сама себя не чинит, а
          переход в другой раздел меняет только содержимое `Outlet`, то есть
          с точки зрения границы ничего не происходит.
        */}
        <ErrorBoundary where="screen" resetKey={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </AppShell.Main>

      <HotkeysModal opened={helpOpen} onClose={() => setHelpOpen(false)} />
    </AppShell>
  );
}
