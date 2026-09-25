import { create } from "zustand";
import type { NotificationDto } from "@/shared/api/types";
import { SEVERITY_RANK, isUnread } from "./catalog";

/**
 * Живое состояние центра уведомлений (14 §3).
 *
 * Сервер — истина по счётчику непрочитанного (в базе их больше, чем помещается
 * в колокольчик): каждый ответ «прочитано» и «действие» приносит свежий `unread`,
 * им и правим бейдж. Стор держит последние строки и поддерживает их из WS.
 * Не персистится: после перезагрузки список и счётчик приходят с сервера.
 */

/** Сколько строк держим в памяти: колокольчик показывает 10, журнал ходит на сервер. */
export const RECENT_LIMIT = 50;

/** Сколько плашек критичного видно одновременно (14 §3: остальные — «и ещё N»). */
export const MAX_CRITICAL_BANNERS = 3;

interface NotificationState {
  items: NotificationDto[];
  unread: number;
  /**
   * Критичные, подтверждённые в этой сессии. Пометка прочитанным уходит на
   * сервер, но плашка должна исчезнуть мгновенно — не дожидаясь ответа.
   *
   * Сюда попадает ТОЛЬКО осознанное подтверждение конкретной строки: кнопка
   * «Подтвердить» на плашке, её же действие или нажатие на саму строку.
   * «Отметить все прочитанными» сюда НЕ пишет — почему, см. `markAllRead`.
   */
  acknowledged: string[];
  /**
   * Критичные, которые этот человек уже видел непрочитанными и ещё не
   * подтвердил лично.
   *
   * ЗАЧЕМ ОТДЕЛЬНЫЙ СПИСОК. Плашка критичного раньше держалась на признаке
   * «не прочитано», и этого хватало ровно до одного нажатия: «Отметить все
   * прочитанными» помечает прочитанным всё, в том числе на сервере, — и
   * «Канал отобрали: подписка пропала» вместе с «Приём сообщений остановился»
   * пропадали с экрана молча, без подтверждения и без отмены. Даже если не
   * гасить их локально, ответ сервера прилетит следом со своим `is_read`.
   *
   * Поэтому у плашки теперь свой признак жизни. «Прочитано» и «подтверждено» —
   * разные вещи: первое про журнал и счётчик, второе про то, что человек
   * увидел беду и берёт её на себя.
   *
   * Список живёт в памяти вкладки, а после перезагрузки плашку поднимает
   * непрочитанность: `read-all` критичные не трогает с 24.09 ни на сервере,
   * ни здесь — они гасятся только поимённо.
   */
  pinnedCritical: string[];
  /**
   * Чьё содержимое сейчас в сторе: `${userId}:${role}`. Уведомления адресные —
   * менеджеру нечего знать о заявках на сброс пароля, пришедших администратору.
   */
  owner: string | null;
  /**
   * Стартовая загрузка центра не удалась (NOTIF-01).
   *
   * Пустой стор и стор, который не смогли наполнить, — это РАЗНЫЕ вещи, а на
   * экране они выглядели одинаково: «Пока ничего не происходило». Ровно та же
   * надпись показывалась администратору, которому пришло «Канал отобрали:
   * подписка пропала», — сеть моргнула, запрос упал (и повторов тогда не было
   * ни одного), и человек прочитал, что всё спокойно. Отличить одно от
   * другого можно только признаком, и он живёт здесь: панель рисует
   * колокольчик, а знает про неудачу запрос в `useNotificationsBootstrap`.
   */
  loadFailed: boolean;

  seed(items: NotificationDto[], unread?: number): void;
  setUnread(n: number): void;
  setLoadFailed(failed: boolean): void;
  /** Новое уведомление из WS: подавление повторов по id. */
  push(n: NotificationDto): void;
  markRead(id: string): void;
  markAllRead(): void;
  /**
   * Вернуть строку в непрочитанное после неудачной пометки (NOTIF-02).
   * `prev` — снимок ДО оптимистичного обновления: без него откат либо поднимет
   * счётчик там, где его никто не опускал, либо снимет чужое подтверждение.
   */
  revertRead(id: string, prev: { wasUnread: boolean; wasAcknowledged: boolean }): void;
  /**
   * Откат массовой пометки: перечисленные строки снова непрочитаны (NOTIF-02).
   * `kept` — сколько критичных пометка оставила непрочитанными: всё сверх них в
   * счётчике натикало за время запроса.
   */
  revertAllRead(ids: string[], prevUnread: number, kept: number): void;
  acknowledge(id: string): void;
  /**
   * Отметить владельца содержимого. Сменился человек или его роль (на одном
   * компьютере работают по очереди — 11 §2.5) — центр обнуляется, и до ответа
   * сервера на экране не остаётся ни счётчика, ни плашек предыдущего сотрудника.
   */
  setOwner(identity: string | null): void;
  clear(): void;
}

/**
 * Запомнить критичные, которые пришли непрочитанными. Ссылка на массив не
 * меняется, если ничего нового нет: список читает компонент плашек, и новый
 * массив на каждый кадр WS заставлял бы его перерисовываться впустую.
 */
function pinCriticals(pinned: string[], items: NotificationDto[]): string[] {
  const fresh = items
    .filter((n) => n.severity === "critical" && isUnread(n) && !pinned.includes(n.id))
    .map((n) => n.id);
  return fresh.length === 0 ? pinned : [...pinned, ...fresh];
}

/** Свежие сверху; при равном времени критичные выше (14 §3). */
function compareNotifications(a: NotificationDto, b: NotificationDto): number {
  const ta = a.last_seen_at || a.created_at;
  const tb = b.last_seen_at || b.created_at;
  if (ta !== tb) return ta < tb ? 1 : -1;
  return SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
}

export function sortNotifications(items: NotificationDto[]): NotificationDto[] {
  return [...items].sort(compareNotifications);
}

/** Строка, из-за которой висит красная плашка: критичное, ещё не подтверждённое лично. */
function isPendingCritical(
  n: NotificationDto,
  acknowledged: string[],
  pinned: string[],
): boolean {
  return (
    n.severity === "critical" &&
    (isUnread(n) || pinned.includes(n.id)) &&
    !acknowledged.includes(n.id)
  );
}

/**
 * Обрезать список до `RECENT_LIMIT`, НЕ ВЫБРАСЫВАЯ неподтверждённое критичное.
 *
 * ЗАЧЕМ. Стор держит полсотни последних строк, и до 12 августа обрезка была
 * тупой: `slice(0, RECENT_LIMIT)` по времени. Красная плашка рисуется по тому,
 * что лежит в сторе, — значит любое событие, умеющее повторяться сотнями,
 * выдавливало критичное с экрана просто своей свежестью. Так и вышло: с 6 по
 * 11 августа «Приём сообщений остановился» дал 112 строк, а два настоящих
 * «Резервное копирование не выполнилось» (ночи 6 и 8 августа) не увидел никто.
 * Копий за те ночи нет.
 *
 * Список после обрезки может оказаться ДЛИННЕЕ `RECENT_LIMIT`, и это осознанно:
 * предел памяти придуман, чтобы колокольчик не разрастался, а не чтобы гасить
 * тревогу. Спасаются только строки, которые прямо сейчас держат плашку, —
 * подтверждённое и прочитанное уезжает как раньше.
 */
export function trimRecent(
  items: NotificationDto[],
  acknowledged: string[],
  pinned: string[],
): NotificationDto[] {
  if (items.length <= RECENT_LIMIT) return items;
  const head = items.slice(0, RECENT_LIMIT);
  const inHead = new Set(head.map((i) => i.id));
  const rescued = items
    .slice(RECENT_LIMIT)
    .filter((n) => !inHead.has(n.id) && isPendingCritical(n, acknowledged, pinned));
  return rescued.length === 0 ? head : sortNotifications([...head, ...rescued]);
}

/** Слить страницы сервера в один список без повторов по id (свежая копия строки — главная). */
export function dedupeById(items: NotificationDto[]): NotificationDto[] {
  const byId = new Map<string, NotificationDto>();
  for (const n of items) byId.set(n.id, n);
  return [...byId.values()];
}

/**
 * Слить пришедшую строку в список. Подавление повторов (14 §4) делает сервер:
 * повтор обновляет ТУ ЖЕ запись и приезжает с тем же `id` — здесь мы его
 * узнаём и обновляем строку вместо второй такой же. Возвращает и список,
 * и признак «строка новая» — по нему растёт счётчик непрочитанного.
 */
export function mergeNotification(
  items: NotificationDto[],
  incoming: NotificationDto,
  acknowledged: string[],
  pinned: string[],
): { items: NotificationDto[]; added: boolean } {
  const byId = items.findIndex((i) => i.id === incoming.id);
  if (byId !== -1) {
    const prev = items[byId];
    const next = items.slice();
    next[byId] = incoming;
    // Прочитанное снова стало непрочитанным (повтор события) — это «плюс один».
    return { items: sortNotifications(next), added: !isUnread(prev) && isUnread(incoming) };
  }

  // Обрезка знает про подтверждение: новая строка не имеет права вытолкнуть с
  // экрана красную плашку (см. `trimRecent`). Списки аргументами, а не со
  // значением по умолчанию, — забытый вызов должен ломать сборку, а не тихо
  // возвращать прежнее поведение.
  return {
    items: trimRecent(sortNotifications([incoming, ...items]), acknowledged, pinned),
    added: isUnread(incoming),
  };
}

export const useNotificationStore = create<NotificationState>()((set) => ({
  items: [],
  unread: 0,
  acknowledged: [],
  pinnedCritical: [],
  owner: null,
  loadFailed: false,

  seed: (items, unread) =>
    set((s) => {
      const next = trimRecent(
        sortNotifications(dedupeById(items)),
        s.acknowledged,
        s.pinnedCritical,
      );
      return {
        items: next,
        unread: unread ?? s.unread,
        pinnedCritical: pinCriticals(s.pinnedCritical, next),
        // Данные доехали — прежняя неудача больше не про этот экран.
        loadFailed: false,
      };
    }),

  setUnread: (n) => set({ unread: Math.max(0, n) }),

  setLoadFailed: (failed) => set({ loadFailed: failed }),

  push: (n) =>
    set((s) => {
      const { items, added } = mergeNotification(s.items, n, s.acknowledged, s.pinnedCritical);
      return {
        items,
        unread: added ? s.unread + 1 : s.unread,
        pinnedCritical: pinCriticals(s.pinnedCritical, [n]),
      };
    }),

  markRead: (id) =>
    set((s) => {
      const target = s.items.find((i) => i.id === id);
      const wasUnread = target ? isUnread(target) : false;
      return {
        items: s.items.map((i) => (i.id === id ? { ...i, is_read: true } : i)),
        unread: wasUnread ? Math.max(0, s.unread - 1) : s.unread,
        acknowledged: s.acknowledged.includes(id) ? s.acknowledged : [...s.acknowledged, id],
      };
    }),

  /**
   * «Отметить все прочитанными» — про журнал и счётчик, не про подтверждение.
   *
   * ЧТО БЫЛО. Сюда же клались идентификаторы ВСЕХ строк хранилища в
   * `acknowledged`, а плашки критичного фильтруются именно по нему. Кнопка
   * стоит в двух местах и включена всегда, когда есть непрочитанное, — и
   * администратор, разгребающий колокольчик, одним нажатием убирал с экрана
   * «Канал отобрали: подписка пропала» и «Приём сообщений остановился».
   * Ни подтверждения, ни отмены. Беда при этом никуда не девалась: канал
   * оставался отобранным, приём — остановленным, просто напоминать об этом
   * стало нечему.
   *
   * ТЕПЕРЬ. `acknowledged` не трогаем вовсе — подтверждение бывает только
   * поимённым. Критичные остаются непрочитанными (сервер с 24.09 делает ровно
   * это), остальное помечается прочитанным.
   */
  markAllRead: () =>
    set((s) => ({
      items: s.items.map((i) =>
        isUnread(i) && i.severity !== "critical" ? { ...i, is_read: true } : i,
      ),
      unread: s.items.filter((i) => isUnread(i) && i.severity === "critical").length,
    })),

  /**
   * ОТКАТ ОДНОЙ ПОМЕТКИ (NOTIF-02).
   *
   * ЧТО БЫЛО. Пометка прочитанным шла оптимистично и без `onError`. Нажатие
   * «Подтвердить» на красной плашке «Канал отобрали: подписка пропала»
   * гасило её мгновенно и клало id в `acknowledged`. Если запрос не прошёл
   * (сеть моргнула, 502 при рестарте), человеку не говорили ничего, а плашка
   * не возвращалась уже никогда: `seed()` перезаписывает `items` и `unread`,
   * но `acknowledged` не трогает — а фильтр плашек смотрит именно на него.
   * На сервере уведомление при этом осталось непрочитанным, и через секунду
   * на колокольчике снова загоралось число — без всякого объяснения.
   *
   * Возвращаем ровно то, что сами меняли: снятие подтверждения (только если
   * его не было ДО нажатия — иначе отберём чужое), признак непрочитанного и
   * счётчик. Строки может уже не быть в списке (WS вытеснил) — тогда правим
   * только счётчик и подтверждение.
   */
  revertRead: (id, prev) =>
    set((s) => ({
      // Непрочитанным строку возвращаем ТОЛЬКО если сами её прочитали. Строка
      // могла быть прочитана и раньше (массовой пометкой), а плашка держаться
      // на `pinnedCritical` — тогда для её возврата достаточно снять
      // подтверждение, и трогать `is_read` значило бы врать про журнал.
      items: prev.wasUnread
        ? s.items.map((i) => (i.id === id ? { ...i, is_read: false } : i))
        : s.items,
      unread: prev.wasUnread ? s.unread + 1 : s.unread,
      acknowledged: prev.wasAcknowledged ? s.acknowledged : s.acknowledged.filter((x) => x !== id),
    })),

  /**
   * Откат массовой пометки (NOTIF-02). Счётчик возвращаем числом, а не
   * приращением: между нажатием и отказом сервера могло прийти новое
   * уведомление, и `unread` уже не тот, из которого мы делали ноль.
   */
  revertAllRead: (ids, prevUnread, kept) =>
    set((s) => {
      const back = new Set(ids);
      const arrived = Math.max(0, s.unread - kept); // натикало после массовой пометки
      return {
        items: s.items.map((i) => (back.has(i.id) ? { ...i, is_read: false } : i)),
        unread: prevUnread + arrived,
      };
    }),

  acknowledge: (id) =>
    set((s) => ({
      acknowledged: s.acknowledged.includes(id) ? s.acknowledged : [...s.acknowledged, id],
    })),

  setOwner: (identity) =>
    set((s) =>
      s.owner === identity
        ? {}
        : {
            items: [],
            unread: 0,
            acknowledged: [],
            pinnedCritical: [],
            owner: identity,
            loadFailed: false,
          },
    ),

  clear: () =>
    set({
      items: [],
      unread: 0,
      acknowledged: [],
      pinnedCritical: [],
      owner: null,
      loadFailed: false,
    }),
}));

/**
 * Критичные, которые администратор ещё не подтвердил (14 §3).
 * Не селектор zustand: результат — новый массив, и подписка на него ломала бы
 * кэш снимка (useSyncExternalStore). Компонент берёт items/acknowledged/pinned
 * по отдельности и зовёт эту функцию в useMemo.
 *
 * Условие держится на двух признаках, и второй здесь не для красоты: пометка
 * прочитанным приходит и от массовой кнопки, и с сервера следующим ответом, —
 * без `pinned` плашка гасла бы от обоих (см. `pinnedCritical`).
 *
 * Третий довод обязателен намеренно: со значением по умолчанию забытый вызов
 * молча вернул бы прежнее поведение — плашку, гаснущую от массовой пометки.
 */
export function criticalBanners(
  items: NotificationDto[],
  acknowledged: string[],
  pinned: string[],
): NotificationDto[] {
  return items.filter((n) => isPendingCritical(n, acknowledged, pinned));
}

/**
 * Есть ли на экране критичное, ждущее личного подтверждения. Нужен обеим
 * кнопкам «Отметить все прочитанными»: они честно предупреждают, что красные
 * плашки останутся, — иначе человек нажмёт и решит, что кнопка не сработала.
 */
export function selectHasUnconfirmedCritical(s: NotificationState): boolean {
  return criticalBanners(s.items, s.acknowledged, s.pinnedCritical).length > 0;
}

export function selectUnread(s: NotificationState): number {
  return s.unread;
}

/** Не удалось загрузить центр (NOTIF-01): панель обязана сказать это словами. */
export function selectLoadFailed(s: NotificationState): boolean {
  return s.loadFailed;
}

export function selectRecent(s: NotificationState): NotificationDto[] {
  return s.items;
}
