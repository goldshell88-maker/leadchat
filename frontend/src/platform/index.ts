import type { NavigateFunction, To } from "react-router-dom";
import { applyOutboxReport, installOptimisticDedup } from "@/features/chats/hooks/outboxSync";
import { requestComposerFocus } from "@/features/hotkeys/focusBus";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { selectTotalUnread, useUnreadStore } from "@/shared/stores/unreadStore";
import { getBridgeOrNull, initBridge, type NotificationAction, type PlatformBridge } from "./bridge";
import { запуститьВыборы } from "./наЭкранеЛи";
import { включитьВоркер } from "./serviceWorker";
import { installCachePersist } from "./warmup";

/**
 * Композиционный корень платформенного слоя. Зовётся один раз из
 * `sessionStore.bootstrap()` — так десктоп-обвязка включается без единой
 * правки в App/AppLayout, а веб получает обычный веб-мост и ничего больше.
 *
 * Все подписки десктопа собраны здесь: бейдж трея, тосты, outbox,
 * автообновление. Запись кэша общая для веба и десктопа и живёт в
 * `initPlatform`. Ни один из этих модулей не попадает в веб-бандл — вход
 * только через динамический импорт в `bridge.ts`.
 */

let started = false;
const teardown: Array<() => void> = [];

export async function initPlatform(): Promise<PlatformBridge | null> {
  if (started) return getBridgeOrNull();
  started = true;
  try {
    const bridge = await initBridge();
    /*
     * Запись снимка списка — ОБЩАЯ для обеих платформ (04 §5.4 шаг 2). Куда
     * лягут строки, решает мост: SQLite в десктопе, IndexedDB в вебе. Второй
     * подписки под веб здесь нет намеренно — расходящиеся пути к одному полю
     * мы уже ловили на бою, и видны они только там.
     */
    teardown.push(installCachePersist());
    /*
     * ⚠ ПОДПИСКА НА НАЖАТИЕ ПО УВЕДОМЛЕНИЮ — ОБЩАЯ (07.09). Она стояла внутри
     * `wireDesktop`, и в вебе её не было вовсе: уведомление показывалось, клик
     * по нему поднимал вкладку и не открывал ничего. Переход `openConversation`
     * был написан и годился обеим платформам — его просто некому было позвать.
     * Здесь она ровно одна: две подписки открыли бы диалог дважды.
     */
    teardown.push(
      bridge.onNotificationAction((a) => {
        if (a.replyText) {
          // Текст уже лёг в outbox на стороне Rust — окно не выдёргиваем, только флашим.
          void bridge.offlineQueue.drain().then(applyOutboxReport);
          return;
        }
        void исполнить(a).catch((e) =>
          console.warn("[platform] действие из уведомления не выполнено:", a.action, e),
        );
      }),
    );
    if (bridge.kind === "web") {
      // Вкладки знакомятся заранее: к первому уведомлению поле известно, и
      // показывает его одна вкладка, а не каждая по разу (см. `наЭкранеЛи.ts`).
      teardown.push(запуститьВыборы());
      /*
       * ⚠ БЕЗ ЭТОЙ СТРОКИ КНОПОК В УВЕДОМЛЕНИИ НЕТ НИКОГДА, И МОЛЧА.
       * `показатьЧерезВоркер` судит по регистрации, которую заводит
       * `включитьВоркер`, — не позвав его, веб-мост всегда получает `false` и
       * показывает карточку через `new Notification`, у которого поля
       * `actions` не существует вовсе (браузер молча его игнорирует). При
       * этом написанный воркер, настроенный nginx и разложенные по местам
       * кнопки выглядели бы совершенно рабочими: ровно тот класс «написано,
       * но не подключено», который мы ловим в этом коде третий раз.
       *
       * Не ждём: регистрация — это поход в сеть, а старт приложения из-за
       * удобства задерживаться не должен. Первое уведомление, пришедшее
       * раньше готовности, выйдет без кнопок — и это честнее, чем задержка.
       */
      void включитьВоркер();
    }
    if (bridge.kind === "tauri") wireDesktop(bridge);
    return bridge;
  } catch (e) {
    // Мост — удобство, а не условие работы: веб-приложение живёт и без него.
    console.warn("[platform] мост не инициализирован:", e);
    return null;
  }
}

function wireDesktop(bridge: PlatformBridge): void {
  // 0. База API — ПЕРВЫМ делом, до любого outbox_flush (04 §8.1). У ядра свой
  //    дефолт (прод-домен); бандл под стенд собирается с другим VITE_API_BASE,
  //    и без этой строки очередь уходила бы на чужой хост.
  const apiBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
  if (apiBase) void bridge.desktop?.setApiBase(apiBase);

  // 1. Бейдж непрочитанных: трей + overlay таскбара (04 §3.2).
  const pushBadge = (total: number) => void bridge.setBadge(total);
  pushBadge(selectTotalUnread(useUnreadStore.getState()));
  teardown.push(useUnreadStore.subscribe((s) => pushBadge(selectTotalUnread(s))));

  // 2. Клик по тосту и быстрый ответ (04 §4.2) живут в `initPlatform`: путь
  //    одинаков у обеих платформ, а вторая подписка здесь открыла бы диалог
  //    дважды на одно нажатие.

  // 3. Outbox: отчёты флаша, дедуп ⏳-строк, расписание повторов (04 §5.3).
  teardown.push(bridge.offlineQueue.onReport(applyOutboxReport));
  teardown.push(installOptimisticDedup());
  void import("./tauri/offline").then(({ startOutboxSchedule }) => {
    teardown.push(startOutboxSchedule(() => bridge.offlineQueue.drain().then(applyOutboxReport)));
  });

  // Реконнект WS — тоже повод прогнать очередь (04 §5.3 п.2).
  let prevStatus = useConnectionStore.getState().status;
  teardown.push(
    useConnectionStore.subscribe((s) => {
      if (s.status === "open" && prevStatus !== "open") {
        void bridge.offlineQueue.drain().then(applyOutboxReport);
      }
      prevStatus = s.status;
    }),
  );

  // 4. Автообновление: событие из Rust → неблокирующий баннер (04 §6.3).
  void import("./tauri/updater").then(({ startUpdater }) => teardown.push(startUpdater()));

  // 5. Rust просит свежий токен для outbox_flush (04 §8.2, ветка 401),
  //    трей меняет статус присутствия, и — последним! — «слушатели навешаны».
  void import("./tauri/ipc").then(({ listenSafe, emitSafe, invokeSafe }) => {
    teardown.push(
      listenSafe("session:refresh-needed", () => {
        void import("@/shared/stores/sessionStore").then(({ useSessionStore }) =>
          useSessionStore
            .getState()
            .refresh()
            .then((ok) => {
              if (ok) void bridge.offlineQueue.drain().then(applyOutboxReport);
            }),
        );
      }),
    );

    /*
     * Статус из меню трея (04 §3.2). Rust только сообщает выбор — реакция за фронтом.
     *
     * ⚠ РЕАКЦИЯ БЫЛА НЕПОЛНОЙ (найдено 27.08). Здесь стоял ровно
     * `setAwayMode(status === "away")`: тосты замолкали, а на сервер не уходило
     * ничего. Коллеги видели человека на месте, автораздача продолжала слать
     * ему обращения, значок в интерфейсе не менялся. Замысел записан в
     * tray.rs — «фронт делает PUT и возвращает подтверждённое значение», — и
     * половина его не выполнялась.
     */
    teardown.push(
      listenSafe<string>(PRESENCE_EVENT, (status) => {
        const выбор = status === "away" ? "away" : "online";
        void import("@/features/presence/usePresence")
          .then(async ({ pushPresence, usePresenceStore }) => {
            try {
              await pushPresence(выбор);
            } catch {
              /*
               * ⚠ ГАЛОЧКУ НАДО ВЕРНУТЬ РУКАМИ (28.08).
               *
               * Rust переставляет её ОПТИМИСТИЧНО, до похода в сеть
               * (`tray.rs::request_presence`), и здесь стоял пустой catch с
               * обещанием «подписка ниже вернёт галочку на подтверждённое
               * значение». Обещание неисполнимо: `pushPresence` при отказе
               * бросает ДО записи в хранилище, значение не меняется, подписка
               * не срабатывает — и `set_presence`, единственная команда,
               * переставляющая галку обратно, не вызывается никогда.
               *
               * Получалось: диспетчер уходит на обед, выбирает «Отошёл», сеть
               * отвалилась. В трее «○ Отошёл» и подсказка «LeadChat — отошёл»,
               * а на сервере он «на месте» — автораздача продолжает давать ему
               * обращения. Возвращается через сорок минут к очереди лидов,
               * уверенный, что выпал из раздачи.
               *
               * Возвращаем трей к тому, что подтверждено, и говорим вслух: у
               * трея места для сообщения нет, но тост есть у приложения.
               */
              const подтверждено = usePresenceStore.getState().status;
              void invokeSafe("set_presence", { status: подтверждено }, undefined);
              const { setAwayMode } = await import("./tauri/notifier");
              setAwayMode(подтверждено === "away");
              const { showToast } = await import("@/shared/ui/toast");
              showToast({
                title: "Состояние не сохранилось",
                message: подтверждено === "away" ? "Вы по-прежнему «отошли»" : "Вы по-прежнему на месте",
                color: "red",
              });
            }
          })
          .catch(() => {
            // Не загрузился сам модуль присутствия — трей уже переставлен, но
            // сделать с этим здесь нечего: показать сообщение тоже нечем.
          });
      }),
    );

    /*
     * ХРАНИЛИЩЕ — ЕДИНСТВЕННЫЙ ИСТОЧНИК ПРАВДЫ О СОСТОЯНИИ, а трей и тосты
     * следуют за ним. Отсюда обе недостающие связи:
     *
     *   • `setAwayMode` — «отошёл» глушит звук тостов (04 §4.3). Раньше его
     *     звал только трей, поэтому переключатель в интерфейсе тосты не глушил;
     *   • `set_presence` — галочка в меню трея. Её не переставлял никто: команда
     *     объявлена в Rust и во фронте не вызывалась ни разу.
     *
     * Подписка живёт до `teardown`, как и слушатели выше.
     */
    void import("@/features/presence/usePresence").then(({ usePresenceStore }) => {
      const применить = (status: string) => {
        void import("./tauri/notifier").then(({ setAwayMode }) => setAwayMode(status === "away"));
        void invokeSafe("set_presence", { status }, undefined);
      };
      применить(usePresenceStore.getState().status);
      teardown.push(usePresenceStore.subscribe((s) => применить(s.status)));
    });

    // ВАЖНО: только после этого Rust отдаёт отложенный deep-link холодного
    // старта (клик по тосту при выключенном приложении, lib.rs::queue_navigate).
    // Без `app:ready` маршрут остаётся в очереди навсегда.
    emitSafe(APP_READY_EVENT);
  });
}

/** Фронт → Rust: «слушатели навешаны, отдавай отложенную навигацию» (lib.rs EVENT_APP_READY). */
export const APP_READY_EVENT = "app:ready";
/**
 * Rust → фронт: пользователь выбрал статус в меню трея (`online` | `away`).
 *
 * ⚠ ЭТОТ ТЕКСТ УСТАРЕЛ И ВВОДИЛ В ЗАБЛУЖДЕНИЕ (выправлено 28.08). Здесь стояло
 * «серверного PUT /api/v1/presence в API нет», хотя ручка есть
 * (`app/api/routes/presence.py`) и код десятью строками выше её зовёт. Статус —
 * не локальная настройка звука: от него зависит автораздача, и коллеги видят
 * его в списках.
 *
 * Выбор в трее делает ровно то же, что переключатель в интерфейсе: PUT на
 * сервер, запись в хранилище, а за хранилищем следуют и галочка в меню, и
 * глушение звука тостов (04 §4.1, строка «статус „отошёл“»). Персональный
 * тумблер звука в профиле при этом не трогаем — это отдельная настройка.
 */
export const PRESENCE_EVENT = "presence:set";

/**
 * Что делать с нажатием в уведомлении.
 *
 * ⚠ КНОПКА ОБЯЗАНА ДЕЛАТЬ ТО, ЧТО НА НЕЙ НАПИСАНО. До 07.09 любое нажатие —
 * и по телу карточки, и по кнопке «Принять» — вело в одно и то же: открыть
 * диалог. Кнопка была нарисована, действие доезжало до вкладки и молча
 * терялось. Это хуже отсутствия кнопки: человек считает диалог принятым и
 * уходит, а диалог остаётся в очереди и достаётся коллеге через минуту.
 *
 * ⚠ ВСЕ ДЕЙСТВИЯ ИДУТ ТЕМ ЖЕ ПУТЁМ, ЧТО И КНОПКИ НА ЭКРАНЕ. Приём — через
 * `claimFromQueue` (он же под клавишей и под кнопкой очереди): там разобраны
 * 409 «уже забрал коллега» и 409 «это твой же диалог», порядок строки в
 * списке и фокус в поле ввода. Отказ от очереди — через `declineFromQueue`,
 * у которого обвязка успеха и разбор отказа общие с мутацией экрана, поэтому
 * и «Вернуть» после него есть. Решение по передаче — через шину в ту самую
 * панель, что рисует кнопки: у неё правка кэша детали, строки и счётчиков
 * вкладок на шести разобранных боевых случаях. Своей копии ни того, ни
 * другого здесь нет намеренно — расходящиеся пути к одному полю мы ловим в
 * этом проекте регулярно, и расходятся они молча.
 *
 * Импорты динамические: платформенный слой не имеет права затащить в свой
 * чанк половину дерева страниц (тем же приёмом взят роутер ниже).
 */
async function исполнить(a: NotificationAction): Promise<void> {
  switch (a.action) {
    case "claim": {
      const [{ claimFromQueue }, { router }] = await Promise.all([
        import("@/features/chats/inbox/claimFromQueue"),
        import("@/app/router"),
      ]);
      // `claimFromQueue` сам решает, когда открыть диалог: сначала ответ
      // сервера, потом переход — иначе человек увидел бы чужой принятый
      // диалог с плашкой «Диалог принял Иван» и решил, что нажал не туда.
      await claimFromQueue(a.conversationId, переход(router));
      return;
    }
    /*
     * ⚠ ОТКАЗ ОТ ОЧЕРЕДИ, А НЕ ОТ ПЕРЕДАЧИ, И ИМЯ У НЕГО СВОЁ. Ниже в этом же
     * `switch` живёт `decline` — отказ от предложенной передачи, и это другая
     * ручка сервера с другими последствиями. Склеить их одним именем значит
     * однажды отказаться не от того: диалог из очереди уехал бы обратно
     * передавшему, которого нет, а предложение передачи — сгинуло из очереди.
     *
     * Путь тот же, что у кнопки на экране и у Ctrl+Backspace: `declineFromQueue`
     * зовёт общую с мутацией обвязку (`отклоненоУспешно`), поэтому «Вернуть»
     * есть и здесь — промахнуться кнопкой в карточке не труднее, чем клавишей.
     */
    case "inbox-decline": {
      const [{ declineFromQueue }, { router }] = await Promise.all([
        import("@/features/chats/inbox/claimFromQueue"),
        import("@/app/router"),
      ]);
      await declineFromQueue(a.conversationId, переход(router));
      return;
    }
    case "accept":
    case "decline": {
      const { requestTransferDecision } = await import("@/features/hotkeys/actionBus");
      // Просьба ПЕРЕД переходом: панель передачи родится уже после него и
      // обязана застать её на месте (тот же приём, что у закрытия Ctrl+D).
      requestTransferDecision(a.conversationId, a.action);
      await openConversation(a.conversationId);
      return;
    }
    default:
      // Нажали по телу карточки — открыть диалог и есть всё действие.
      await openConversation(a.conversationId);
  }
}

/**
 * Переход в виде, который ждут функции экрана (`NavigateFunction`).
 *
 * Роутер отдаёт обещание, а `NavigateFunction` объявлена синхронной и
 * перегружена шагом истории числом; переходник нужен ровно поэтому, а не
 * ради удобства.
 */
type Роутер = { navigate: (to: string) => Promise<void> };
function переход(router: Роутер): NavigateFunction {
  const идти = (to: To | number) => {
    // Шаг истории числом уведомлению не нужен и не приходит: переход зовут
    // адресом. Молча выходим, а не бросаем — уведомление не имеет права
    // ронять приём диалога.
    if (typeof to !== "string") return;
    void router.navigate(to).catch((e) => console.warn("[platform] переход не удался:", to, e));
  };
  // Приведение неизбежно: `NavigateFunction` — перегруженный тип (адрес либо
  // шаг истории), а перегрузки в стрелочной функции не выражаются.
  return идти as NavigateFunction;
}

/** Открыть диалог из уведомления: роутер + фокус в поле ввода (04 §4.2). */
async function openConversation(conversationId: string): Promise<void> {
  useChatUiStore.getState().setActive(conversationId);
  // Динамический импорт роутера — иначе платформенный слой затянет всё дерево страниц.
  const { router } = await import("@/app/router");
  await router.navigate(`/chats/${conversationId}`);
  requestComposerFocus();
}

/** Отдать Rust текущий access-JWT (04 §8.1 `set_session_token`). */
export function pushSessionToken(token: string | null): void {
  void getBridgeOrNull()?.desktop?.setSessionToken(token);
}

export { warmupFromCache } from "./warmup";

/** Только для тестов: снять все подписки и разрешить повторный init. */
export function __resetPlatform(): void {
  for (const off of teardown.splice(0)) {
    try {
      off();
    } catch {
      /* отписка не должна ронять тест */
    }
  }
  started = false;
}
