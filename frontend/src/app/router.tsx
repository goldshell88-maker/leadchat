/* eslint-disable react-refresh/only-export-components -- guards live next to the route map (03 §6) */
import { Button, Stack, Text, Title } from "@mantine/core";
import { Link, Navigate, Outlet, createBrowserRouter, useLocation } from "react-router-dom";
import { LoginPage } from "@/features/auth/LoginPage";
import { SettingsLayout } from "@/features/settings/SettingsLayout";
import { ChatsPage } from "@/features/chats/ChatsPage";
import { usePermissions, type Permission } from "@/shared/auth/usePermissions";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { AppChromeSkeleton, AppLayout } from "./AppLayout";
import { RouteErrorScreen } from "./ErrorBoundary";
import { loadChunk } from "./lazyRoutes";

/**
 * UI Kit (docs/16 §8) — отдельным чанком. Страница нужна раз в спринт тому,
 * кто правит дизайн, и в стартовом бандле оператора ей делать нечего.
 */

/** Таблица диалогов — отдельным чанком: экран руководителя, не оператора. */

/** «Что нового» — отдельным чанком: читают раз в неделю, в стартовом бандле не нужна. */

/**
 * Guard 1 (03 §6): session. Waits for the silent cookie-refresh, then gates.
 *
 * Экспортируется ради теста (`test/bootSkeleton.test.tsx`), и это не «ради
 * удобства». Проверять каркас входа на настоящем `createBrowserRouter` не
 * выходит: react-router на переходе собирает `Request`, а undici в jsdom
 * бракует его `AbortSignal` — прогон получает необработанное отклонение и
 * предупреждение «может давать ложные срабатывания». Guard на memory-роутере
 * проверяется без этого, а то, что авторизованная ветка настоящей карты стоит
 * именно на нём, тест сверяет отдельно.
 */
export function RequireAuth() {
  const user = useSessionStore((s) => s.user);
  const bootstrapped = useSessionStore((s) => s.bootstrapped);
  const loc = useLocation();
  // Скелет оболочки, а не спиннер на весь экран: рамка появляется сразу и
  // не двигается, когда приезжают данные (см. AppChromeSkeleton).
  if (!bootstrapped) return <AppChromeSkeleton />;
  if (!user) return <Navigate to="/login" state={{ from: loc }} replace />;
  return <Outlet />;
}

/**
 * Guard 2 (03 §6): permissions. Not a 403 page but a redirect to /chats —
 * the user never saw the menu item, they only get here via a direct link.
 */
function RequirePermission({ anyOf }: { anyOf: Permission[] }) {
  const { canAny } = usePermissions();
  return canAny(...anyOf) ? <Outlet /> : <Navigate to="/chats" replace />;
}

/**
 * Неизвестный адрес (аудит SHELL-09).
 *
 * ЧТО БЫЛО. Маршрут `*` стоял на верхнем уровне — вне `RequireAuth` и вне
 * `AppLayout`. Залогиненный человек, попавший на несуществующий адрес
 * (опечатка, устаревшая ссылка от коллеги, `/settings/что-угодно`), терял
 * оболочку целиком: ни шапки, ни рельсы разделов, ни колокольчика. Экран
 * выглядел как «приложение сломалось», а выходом была одна кнопка посреди
 * пустоты.
 *
 * ЧТО ЗДЕСЬ. Тот же маршрут — ребёнок `AppLayout`: рабочее место остаётся на
 * месте, гаснет только содержимое `Outlet`, и уйти можно куда угодно одним
 * нажатием по рельсе. Неавторизованного по-прежнему уводит на вход: `*` теперь
 * под `RequireAuth`, а тот на пустой сессии делает `Navigate` на `/login`.
 *
 * ВТОРОЙ ТАКОЙ ЖЕ МАРШРУТ СТОИТ ВНУТРИ НАСТРОЕК, И ЭТО НЕ ДУБЛЬ. Лечили мы
 * потерю оболочки, а внутри «Настроек» оболочек ДВЕ: общая (шапка и рельса) и
 * своя — колонка разделов «Профиль · Каналы · Быстрые ответы · Команда…».
 * Один `*` на уровне `AppLayout` сохранял только первую: `/settings/опечатка`
 * показывал «Страница не найдена» с шапкой, но без меню настроек, то есть без
 * единственного способа перейти в соседний раздел. Человек попадает сюда как
 * раз оттуда — по старой ссылке коллеги или по переименованному разделу, — и
 * возвращать его «К диалогам» из настроек значит выбрасывать из того места,
 * куда он шёл.
 *
 * Компонент один на оба места намеренно: два разных текста «не найдено»
 * разъехались бы, и человек решил бы, что это две разные поломки.
 *
 * Высота — `100%`, а НЕ `100dvh`. Внутри `Outlet` сверху уже 56 пикселей
 * шапки: `100dvh` сделал бы документ выше окна, и на неизвестном адресе
 * вылезала бы полоса прокрутки всей страницы — та же беда, что была у
 * полноэкранного лоадера на переходах.
 */
/*
 * ⚠ КЕГЛЬ ЗАГОЛОВКА — ТОКЕНОМ, А НЕ ЧИСЛОМ (разбор дизайна 05.09).
 *
 * Здесь стояло `fz={24}` — число мимо шкалы. Оно стоило дважды. Во-первых,
 * 24 не совпадает ни с одной ступенью (`--lc-fz-page` 28, `--lc-fz-metric`
 * 24, `--lc-fz-section` 20), то есть на экране жил десятый кегль. Во-вторых —
 * и это важнее — от 1800 пикселей шкала поднимается целиком (lc-vars.css,
 * жалоба владельца про 27 дюймов), а зашитое число не поднималось: на самом
 * большом мониторе заголовок оказывался МЕЛЬЧЕ, чем на ноутбуке.
 *
 * Заодно экран перестал извиняться. Пустое состояние — это не «простите,
 * ничего нет», а «вот что случилось и вот единственный шаг»: заголовок в
 * полный кегль страницы, объяснение тише его на две ступени, одно действие
 * заливкой, а не обводкой. Воздуха больше: здесь читают, а не сравнивают.
 */
function NotFoundPage() {
  return (
    <Stack
      align="center"
      justify="center"
      gap="var(--lc-space-4)"
      style={{ height: "100%", padding: "var(--lc-space-8) var(--lc-space-5)" }}
    >
      <Title order={1} fz="var(--lc-fz-page)" c="var(--lc-text-1)" ta="center">
        Страница не найдена
      </Title>
      <Text fz="var(--lc-fz-body)" c="var(--lc-text-3)" ta="center" maw={380}>
        Такой страницы нет или она переехала
      </Text>
      <Button component={Link} to="/chats">
        К диалогам
      </Button>
    </Stack>
  );
}

/*
 * ЭКРАН ОТКАЗА НА МАРШРУТАХ (аудит SHELL-01, SHELL-02).
 *
 * `errorElement` стоит в двух ярусах, и это не перестраховка.
 *
 * ВНУТРЕННИЙ — на каждом ленивом разделе. Ошибка всплывает до ближайшего
 * маршрута с `errorElement`, а рисуется он НА МЕСТЕ СВОЕГО МАРШРУТА, то есть
 * внутри `Outlet` у `AppLayout`. Поэтому шапка, рельса, колокольчик и плашки
 * критичного остаются на экране, и человек уходит в другой раздел одним
 * нажатием. Повесить один `errorElement` на общего родителя было бы короче на
 * девять строк и уносило бы рабочее место целиком — ровно то, от чего лечимся.
 * Ленивые разделы названы поимённо потому, что беда у них своя: после каждой
 * выкатки старый чанк исчезает с сервера, и `import()` во вкладке, открытой до
 * выкатки, отдаёт 404. Граница React такое не ловит — исключения там нет,
 * есть отклонённый промис маршрута.
 *
 * ВНЕШНИЙ — на самом верху авторизованной части. Ловит падение `AppLayout` и
 * обоих guard'ов: чинить в этом случае нечего, шапки всё равно нет.
 *
 * Обычные исключения отрисовки экранов ловит не он, а `ErrorBoundary` внутри
 * `AppLayout` (он ближе), — там же и сброс при переходе в другой раздел.
 */
/*
 * ⚠ ПОЧЕМУ МЫ ЕЩЁ НА `react-router` 6, ХОТЯ В НЁМ ЧИСЛИТСЯ УЯЗВИМОСТЬ (01.09).
 *
 * В 6.x есть открытый редирект: обратный слэш в адресе, переданном в `<Link>`
 * или `navigate()`, уводит браузер наружу. Исправлено только в ветке 7.
 *
 * Обновление ПРОВЕРЕНО НА ДЕЛЕ, а не отвергнуто с ходу: `7.18.3` ставится,
 * `npm audit` становится чистым, типы и линтер молчат. Упали ровно два теста —
 * и показали настоящую перемену поведения: в седьмой ветке ленивому маршруту
 * нужен `HydrateFallback`, иначе при ПЕРВОЙ загрузке роутер не рисует ничего.
 *
 * Ленивых маршрутов здесь без малого два десятка (все — в карте
 * `lazyRoutes.ts`). Значит человек, открывший закладку прямо на статистику,
 * разбор или настройки, увидел бы белый экран. Это не «поднять
 * версию», а решение о том, ЧТО показывать во время загрузки, — и оно спорит с
 * доводом у `lazy` ниже, где полноэкранный загрузчик отвергнут осознанно.
 *
 * Сама уязвимость при этом закрыта иначе и надёжнее: переход разрешён только
 * внутрь приложения (`shared/lib/внутреннийАдрес.ts`), проверка стоит там, где
 * адрес приходит с сервера. Замер показал, что свободного адреса в переход не
 * попадает нигде и сегодня, — проверка нужна, чтобы так осталось завтра.
 *
 * ПЕРЕХОД НА 7 — ОТДЕЛЬНАЯ РАБОТА. Её первый шаг: решить, что рисовать вместо
 * ленивого раздела при первой загрузке.
 */
export const router = createBrowserRouter([
  /*
   * ⚠ ЖАДНЫМИ ОСТАЛИСЬ ДВА, И ЭТО РЕШЕНИЕ, А НЕ НЕДОСМОТР (разбор 03.09).
   *
   * `LoginPage` — первый экран для невошедшего: сделать его ленивым значит
   * показать пустоту на время лишнего похода в сеть ровно тому, у кого ещё
   * ничего не открыто. `ChatsPage` — посадочный экран, на него ведёт «/»;
   * ленивая загрузка добавила бы поход в сеть на самый частый путь системы.
   *
   * Ленивыми стали четыре: страницы настроек и приглашение открывают редко,
   * а весят много — одна `AccountsPage` 63,6 КБ исходника. А вот
   * `SettingsLayout` осталась жадной, и это тоже замер: 3,1 КБ исходника
   * против лишнего похода в сеть ПЕРЕД любой страницей настроек. Плюс она
   * держит колонку разделов на неизвестном адресе внутри настроек (SHELL-09,
   * `notFoundInShell.test.tsx`) — ленивая обёртка ломала это на первый кадр. Каждому
   * ленивому обязателен свой `errorElement`: после выкатки старый чанк
   * исчезает, `import()` отдаёт 404, и это отклонённый промис, а не
   * исключение React (разбор в комментарии ниже).
   */
  { path: "/login", element: <LoginPage />, errorElement: <RouteErrorScreen /> },
  { path: "/invite/:token", lazy: async () => ({ Component: (await loadChunk("/invite/:token")).InvitePage }), errorElement: <RouteErrorScreen /> },
  {
    element: <RequireAuth />,
    errorElement: <RouteErrorScreen />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <Navigate to="/chats" replace /> },
          { path: "/chats", element: <ChatsPage /> },
          // Неизвестный адрес — ВНУТРИ оболочки (SHELL-09, см. NotFoundPage).
          // Статические пути выигрывают у `*` по специфичности, а не по месту
          // в списке, поэтому соседям он не мешает.
          { path: "*", element: <NotFoundPage /> },
          {
            // ⚠ 27.08: своё право вместо `stats:all` — разбор открыт всем
            // ролям, а статистика и лента остались за руководителями.
            element: <RequirePermission anyOf={["dialogs:read"]} />,
            children: [
              {
                /*
                 * `lazy` вместо Suspense с полноэкранным лоадером — как у
                 * шести соседних маршрутов.
                 *
                 * ЧТО БЫЛО. При каждом переходе экран целиком заменялся
                 * спиннером с логотипом. Это главный источник ощущения
                 * медленной работы: даже когда чанк приходит за сотню
                 * миллисекунд, глаз успевает увидеть, как всё исчезло и
                 * появилось заново. Плюс сам лоадер брал 100dvh внутри
                 * области, у которой сверху уже 56 пикселей шапки, — документ
                 * оказывался выше окна, и на каждом первом переходе мигала
                 * полоса прокрутки.
                 *
                 * С `lazy` фолбэк не рендерится вовсе: на месте остаётся
                 * ПРЕДЫДУЩИЙ экран, пока не приедет новый. Шапка и рельса
                 * никуда не деваются — они и так снаружи.
                 */
                path: "/dialogs",
                lazy: async () => ({
                  Component: (await loadChunk("/dialogs")).TablePage,
                }),
                errorElement: <RouteErrorScreen />,
              },
            ],
          },
          {
            /*
             * Надзор за ботом (docs/45). Право `bots:manage` есть ТОЛЬКО у
             * администратора — владелец просил админскую вкладку, и отдельной
             * сущности для этого заводить не пришлось.
             *
             * Ленивая загрузка, как у соседей: экран открывают раз в день, в
             * стартовом бандле рабочего места ему делать нечего.
             */
            element: <RequirePermission anyOf={["bots:manage"]} />,
            children: [
              {
                path: "/bot-dialogs",
                lazy: async () => ({
                  Component: (await loadChunk("/bot-dialogs"))
                    .BotDialogsPage,
                }),
                errorElement: <RouteErrorScreen />,
              },
            ],
          },
          {
            path: "/updates",
            lazy: async () => ({
              Component: (await loadChunk("/updates")).UpdatesPage,
            }),
            errorElement: <RouteErrorScreen />,
          },
          /*
           * ⚠ ВИТРИНА КОМПОНЕНТОВ — ТОЛЬКО В РАЗРАБОТКЕ (чистка 08.09, аудит
           * S-03). Раздел заводился для сверки вида и открывается «раз в
           * спринт» — то есть никогда за смену. В бою он был доступен каждому,
           * кто наберёт адрес: страница с образцами всех состояний, включая
           * выдуманные диалоги и телефоны. Ни ссылки на неё, ни пункта меню
           * нет — значит и вреда от изъятия нет, а `import.meta.env.DEV`
           * выбрасывает и сам чанк из боевой сборки, а не только маршрут.
           */
          ...(import.meta.env.DEV
            ? [
                {
                  path: "/ui-kit",
                  lazy: async () => ({
                    Component: (await loadChunk("/ui-kit")).UiKitPage,
                  }),
                  errorElement: <RouteErrorScreen />,
                },
              ]
            : []),
          { path: "/chats/:id", element: <ChatsPage /> }, // тот же компонент — deep-link (03 §6)
          {
            // Журнал уведомлений (14 §3). Guard по правам не ставим: своего
            // права у центра нет, а роль без уведомлений видит на странице
            // честное «вашей роли уведомления не приходят» вместо редиректа.
            // Ленивая загрузка: фильтры с датами и таблица журнала в стартовом
            // бандле рабочего места не нужны (03 §7).
            path: "/notifications",
            lazy: async () => ({
              Component: (await loadChunk("/notifications")).NotificationsPage,
            }),
            errorElement: <RouteErrorScreen />,
          },
          {
            /*
             * Живая лента (`/feed`) — право то же, что у статистики: это
             * управленческий взгляд на ту же работу, только за последние
             * десять минут, а не за неделю.
             *
             * Ленивый кусок, хотя копить события лента начинает с момента
             * входа. Противоречия нет: копит хранилище (`features/feed/store`),
             * которое приезжает вместе с рабочим местом и весит десяток строк,
             * а отдельным куском едет ЭКРАН — отборы, раскладка, стили. Их в
             * стартовом бандле оператора быть не должно, и открывает их
             * руководитель не каждый день.
             */
            element: <RequirePermission anyOf={["stats:all"]} />,
            children: [
              {
                path: "/feed",
                lazy: async () => ({
                  Component: (await loadChunk("/feed")).FeedPage,
                }),
                errorElement: <RouteErrorScreen />,
              },
            ],
          },
          {
            // Статистика по всем сотрудникам — admin и head (11 §6, 01 §13).
            element: <RequirePermission anyOf={["stats:all"]} />,
            // Ленивая загрузка: экран статистики (график, тепловая карта, date-picker)
            // нужен двум ролям — держать его в стартовом бандле рабочего места незачем.
            children: [
              {
                path: "/stats",
                lazy: async () => ({ Component: (await loadChunk("/stats")).StatsPage }),
                errorElement: <RouteErrorScreen />,
              },
            ],
          },
          {
            element: <SettingsLayout />,
            children: [
              { path: "/settings", element: <Navigate to="/settings/profile" replace /> },
              // Неизвестный адрес ВНУТРИ настроек — с колонкой разделов
              // (см. `NotFoundPage`). Специфичность у `/settings/*` выше, чем
              // у верхнего `*`, поэтому соседним статическим путям он не мешает
              // и перехватывает ровно то, что иначе ушло бы наверх без меню.
              { path: "/settings/*", element: <NotFoundPage /> },
              { path: "/settings/profile", lazy: async () => ({ Component: (await loadChunk("/settings/profile")).ProfilePage }), errorElement: <RouteErrorScreen /> }, // все роли (11 §4.3)
              {
                element: <RequirePermission anyOf={["accounts:read", "accounts:manage"]} />,
                children: [
                  { path: "/settings/accounts", lazy: async () => ({ Component: (await loadChunk("/settings/accounts")).AccountsPage }), errorElement: <RouteErrorScreen /> },
                  // Решётка «люди × каналы» (просьба владельца 04.09: «я вручную
                  // по 30 раз захожу и тыкаю»). Право то же, что у списка
                  // каналов: смотреть вправе руководитель, менять — только
                  // администратор, и это решает сам экран.
                  { path: "/settings/accounts/operators", lazy: async () => ({ Component: (await loadChunk("/settings/accounts/operators")).OperatorsGridPage }), errorElement: <RouteErrorScreen /> },
                ],
              },
              {
                // ⚠ ПО `templates:own`, А НЕ ПО `shared` (разбор 03.09).
                //
                // Раздел называется «Быстрые ответы» и закрывался правом,
                // которого у диспетчера нет. То есть человек, который
                // пользуется ими каждый день (17 % его исходящих — дословно
                // заготовки), не мог открыть экран с их названием: личные
                // лежали вкладкой в «Профиле», а кнопка «Управлять» из пикера
                // высаживала его на «Учётную запись».
                //
                // Цена была видна в данных: личные заготовки завели 2 человека
                // из 57. Внутри раздела `personalOnly` схлопывает интерфейс до
                // своих — общие менеджер по-прежнему не правит.
                element: <RequirePermission anyOf={["templates:own", "templates:shared"]} />,
                children: [{ path: "/settings/templates", lazy: async () => ({ Component: (await loadChunk("/settings/templates")).TemplatesPage }), errorElement: <RouteErrorScreen /> }],
              },
              {
                // Боты — только admin (право `bots:manage`, 01 §8 / 11 §5).
                // Ленивая загрузка: редактор сценариев с формами девяти типов
                // шагов и песочницей нужен одной роли и одному экрану —
                // в стартовом бандле рабочего места ему делать нечего (03 §7).
                element: <RequirePermission anyOf={["bots:manage"]} />,
                children: [
                  {
                    path: "/settings/bots",
                    lazy: async () => ({ Component: (await loadChunk("/settings/bots")).BotsPage }),
                    errorElement: <RouteErrorScreen />,
                  },
                  {
                    path: "/settings/bots/:id",
                    lazy: async () => ({ Component: (await loadChunk("/settings/bots/:id")).BotEditor }),
                    errorElement: <RouteErrorScreen />,
                  },
                  {
                    // Лид-бот: право то же (`bots:manage`), потому что здесь
                    // включается автоответ живым клиентам и меняется адрес
                    // чужого сервиса, которому уходит переписка.
                    path: "/settings/leadbot",
                    lazy: async () => ({
                      Component: (await loadChunk("/settings/leadbot")).LeadbotTab,
                    }),
                    errorElement: <RouteErrorScreen />,
                  },
                  {
                    // Автозаявки: право `settings:manage` — здесь выпускается
                    // токен, открывающий наружу телефоны клиентов.
                    path: "/settings/leads",
                    lazy: async () => ({
                      Component: (await loadChunk("/settings/leads")).LeadsTab,
                    }),
                    errorElement: <RouteErrorScreen />,
                  },
                ],
              },
              {
                // Настройки распределения: право `settings:manage` (только
                // admin). Отдельное от `users:manage` намеренно — заводить
                // сотрудников и решать, как между ними раздаются диалоги,
                // разные полномочия.
                element: <RequirePermission anyOf={["settings:manage"]} />,
                children: [
                  {
                    path: "/settings/distribution",
                    lazy: async () => ({
                      Component: (await loadChunk("/settings/distribution"))
                        .DistributionTab,
                    }),
                    errorElement: <RouteErrorScreen />,
                  },
                  {
                    // Монитор внешних сервисов (16.09): ключи, потолки, расход,
                    // проверка доступности с сервера — то же право, что у
                    // остальных настроек системы.
                    path: "/settings/apis",
                    lazy: async () => ({
                      Component: (await loadChunk("/settings/apis")).ApisPage,
                    }),
                    errorElement: <RouteErrorScreen />,
                  },
                ],
              },
              {
                // 11 §4.2: guard `anyOf ["users:manage","audit:read"]` — head заходит
                // ради вкладки журнала, вкладки внутри фильтрует сама страница.
                element: <RequirePermission anyOf={["users:manage", "audit:read"]} />,
                children: [
                  {
                    path: "/settings/team",
                    lazy: async () => ({ Component: (await loadChunk("/settings/team")).TeamPage }),
                    errorElement: <RouteErrorScreen />,
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
  },
]);
