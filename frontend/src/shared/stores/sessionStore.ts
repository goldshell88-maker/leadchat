import { create } from "zustand";
import { getBridgeOrNull, isTauri } from "@/platform/bridge";
import { http, refreshSession } from "@/shared/api/http";
import type { LoginResponse, MeResponse, SessionUser } from "@/shared/api/types";
import type { Permission } from "@/shared/auth/usePermissions";

/**
 * Платформенный слой грузится ТОЛЬКО динамически: статический импорт замкнул бы
 * цикл `http → sessionStore → platform → applyWsEvent → http`, а в вебе ещё и
 * тянул бы десктоп-код в граф модулей раньше времени.
 */
function loadPlatform() {
  return import("@/platform").catch((e) => {
    console.warn("[session] платформенный слой недоступен:", e);
    return null;
  });
}

/** Access-JWT нужен Rust для `outbox_flush` (04 §8.1) — только в памяти процесса. */
function syncSessionToken(token: string | null): void {
  if (!isTauri()) return;
  void loadPlatform().then((p) => p?.pushSessionToken(token));
}

/**
 * Стереть локальный кэш и очередь отправки при уходе сессии.
 *
 * ⚠ БЕЗ ЭТОГО ЧУЖОЕ СООБЩЕНИЕ УХОДИТ ПОД ЧУЖИМ ИМЕНЕМ (28.08). Команда
 * `cache_clear` в Rust была написана, зарегистрирована и снабжена комментарием
 * «очередь принадлежит сессии, и оставлять чужие неотправленные сообщения
 * следующему пользователю на этой машине нельзя» — и не вызывалась НИОТКУДА.
 * Выход чистил только токен в памяти, база SQLite оставалась нетронутой, а
 * фоновый флаш раз в 30 секунд берёт ТЕКУЩИЙ токен.
 *
 * Диспетчер A на общем компьютере смены пишет клиенту при оборванной сети —
 * строка ложится в очередь. A выходит и уходит домой. Диспетчер B входит на том
 * же профиле — и через полминуты сообщение A уезжает клиенту под именем B: в
 * ленте, в журнале и в правиле «кто ответил, тот и ведёт» это сообщение B, а он
 * его не писал и нигде не видит. Вместе с очередью на диске оставался и кэш
 * диалогов A — чужая переписка на чужом экране.
 *
 * Зовётся из `clear()`, то есть покрывает и «Выход», и принудительный отзыв
 * сессии (`refresh` вернул неудачу): точка одна, забыть её на новом пути
 * невозможно.
 */
function wipeLocalCache(): void {
  /*
   * ⚠ ЗАСЛОН `if (!isTauri()) return` СНЯТ 07.09, И ЭТО ЧАСТЬ ТОГО ЖЕ ДОВОДА.
   * Он стоял здесь верно ровно до тех пор, пока веб ничего не хранил: с
   * появлением снимка списка в IndexedDB (`platform/снимокСписка.ts`) в
   * браузере лежит ровно то, ради чего этот вызов и написан — список диалогов
   * ушедшего. Веб-мост чистит своё сам; десктопный — как чистил.
   */
  // Мост берём напрямую: `@/platform` — точка сборки, и `convCache` там не
  // экспортирован; `@/platform/bridge` уже импортирован статически и цикла не
  // замыкает. До инициализации моста чистить нечего — тогда и кэша нет.
  void getBridgeOrNull()?.convCache.clear();
}

/**
 * Session state. Access token lives ONLY in memory (DESIGN §9): nothing here is
 * persisted — "remember me" works via the httpOnly refresh cookie, which
 * bootstrap() exchanges for a fresh access token on every app start.
 */
interface SessionState {
  user: SessionUser | null;
  permissions: Permission[];
  /**
   * Личные сочетания клавиш — ТОЛЬКО отличия от умолчаний (требование заказчика
   * 13 августа). Живут здесь, а не в отдельном запросе: обработчик клавиш читает их
   * на КАЖДОЕ нажатие, и лишний поход в кэш на каждую букву ни к чему.
   */
  hotkeys: Record<string, string[]>;
  accessToken: string | null;
  /** true once the silent start-up refresh finished (either way) — gates RequireAuth. */
  bootstrapped: boolean;

  setSession(accessToken: string, user: SessionUser): void;
  clear(): void;

  login(email: string, password: string, remember: boolean): Promise<void>;
  logout(): Promise<void>;
  refresh(): Promise<boolean>;
  bootstrap(): Promise<void>;
  /** Shared tail of login/invite-accept: store the session, then load permissions. */
  applySession(data: LoginResponse): Promise<void>;
  /**
   * Ответ `/auth/me` → стор. Входов три: вход, старт вкладки и полоса
   * «Восстанавливаем ваши права». Полоса писала только пользователя и права,
   * и личные сочетания клавиш не действовали до перезагрузки (проверка 24.09).
   */
  applyMe(me: MeResponse): void;
}

function toSessionUser(me: MeResponse): SessionUser {
  return {
    id: me.id,
    email: me.email,
    full_name: me.full_name,
    role: me.role,
    is_active: me.is_active,
    department: me.department,
  };
}

/**
 * Спросить сервер, «на месте» я или «отошёл», и отразить это на экране.
 *
 * ⚠ ОДНА ФУНКЦИЯ НА ДВА ВХОДА, И ЭТО НЕ КРАСОТА (боевой случай 28.08).
 *
 * Чтение стояло ТОЛЬКО во `applySession` — то есть срабатывало при ВХОДЕ.
 * Перезагрузка страницы идёт другой дорогой (`bootstrap`: тихий refresh по
 * куке плюс `/auth/me`), и на ней статус не спрашивал никто. Хранилище при
 * этом стартует со значения «на месте» — «состояние только что открытого
 * приложения».
 *
 * Получалось ровно то, на что пожаловался владелец: «слетает отошёл при
 * перезагрузке сайта». Весь интерфейс сообщает «На месте», переключатель и
 * трей стоят там же, а на сервере человек по-прежнему «отошёл» — и автораздача
 * продолжает его пропускать. Он сидит и ждёт обращений, которых ему не дадут,
 * и ни одна строчка на экране не говорит, почему.
 *
 * Отказ ничего не ломает: остаётся прежнее умолчание «на месте», следующий
 * вход спросит снова.
 */
async function подтянутьСвоёСостояние(): Promise<void> {
  try {
    const { usePresenceStore } = await import("@/features/presence/usePresence");
    const своё = await http.get<{ status: string }>("/presence");
    if (своё.status === "away" || своё.status === "online") {
      usePresenceStore.getState().set(своё.status);
    }
  } catch {
    // Сеть моргнула — остаёмся на умолчании; следующий вход спросит снова.
  }
}

export const useSessionStore = create<SessionState>()((set, get) => ({
  user: null,
  permissions: [],
  hotkeys: {},
  accessToken: null,
  bootstrapped: false,

  setSession: (accessToken, user) => {
    set({ accessToken, user });
    syncSessionToken(accessToken);
  },

  clear: () => {
    set({ user: null, permissions: [], hotkeys: {}, accessToken: null });
    syncSessionToken(null);
    wipeLocalCache();
  },

  applyMe(me) {
    set({ user: toSessionUser(me), permissions: me.permissions, hotkeys: me.hotkeys ?? {} });
  },

  async applySession(data) {
    get().setSession(data.access_token, data.user);
    try {
      get().applyMe(await http.get<MeResponse>("/auth/me"));
    } catch {
      // /auth/me failed (network blip): keep the session, UI degrades to no-permission chrome.
      set({ permissions: [] });
    }
    /*
     * ⚠ СВОЁ СОСТОЯНИЕ СПРАШИВАЕМ У СЕРВЕРА, А НЕ ПРЕДПОЛАГАЕМ (28.08).
     *
     * Хранилище присутствия стартует со значения «на месте» — «состояние
     * только что открытого приложения». Но статус переживает перезагрузку
     * страницы: он лежит в том же ключе, что и присутствие, тот живёт минутами,
     * и переподключение сокета текущее значение СОХРАНЯЕТ.
     *
     * Получалось так: диспетчер поставил «отошёл», нажал F5 — и весь интерфейс
     * сообщает «На месте», переключатель стоит в положении «на месте», трей
     * тоже. Автораздача при этом продолжает его пропускать, потому что на
     * сервере он «отошёл». Человек сидит и ждёт обращений, которых ему не
     * дадут, и ни одна строчка на экране не говорит, почему.
     *
     * Отказ здесь ничего не ломает: остаётся прежнее умолчание «на месте».
     */
    await подтянутьСвоёСостояние();
  },

  async login(email, password, remember) {
    const data = await http.post<LoginResponse>("/auth/login", { email, password, remember }, { auth: false });
    await get().applySession(data);
  },

  async logout() {
    try {
      await http.post<void>("/auth/logout");
    } catch {
      // Logout must always succeed locally; the refresh cookie dies server-side on next rotation.
    }
    get().clear();
  },

  async refresh() {
    const итог = await refreshSession();
    // Гасим сессию только на явный отказ сервера. Сбой связи проходит сам, а
    // выход стоит человеку набранного ответа и места в ленте.
    if (итог === "отозван") get().clear();
    return итог === "ok";
  },

  async bootstrap() {
    // Мост платформы поднимается ДО сессии: в десктопе он нужен уже для
    // мгновенной отрисовки из локального кэша, в вебе это дешёвая заглушка.
    const platform = await loadPlatform();
    await platform?.initPlatform();

    // Silent POST /auth/refresh by cookie (03 §6 «Логин»): success → the user
    // lands straight in the app, no localStorage tokens involved.
    /*
     * ⚠ СРАВНИВАЕМ СО ЗНАЧЕНИЕМ, А НЕ ПРОВЕРЯЕМ НА ИСТИННОСТЬ. `refreshSession`
     * теперь возвращает не «да/нет», а три исхода, и `if (итог)` было бы верно
     * ВСЕГДА — включая «отозван» и «нет-связи». Типы такую подмену пропускают
     * молча: строка — законное условие.
     */
    const итог = await refreshSession();
    if (итог === "ok") {
      /*
       * Снапшот последних 200 диалогов рисуется до первого ответа API (04 §5.4).
       *
       * ⚠ РЯДОМ С `/auth/me`, А НЕ ПЕРЕД НИМ (правка 07.09). Пока веб-мост
       * возвращал `null` мгновенно, порядок ничего не стоил. С приходом снимка
       * на IndexedDB (07.09) это стало походом в базу с потолком 300 мс — и
       * последовательное ожидание ДОБАВЛЯЛО их к первому кадру на медленной
       * или заблокированной базе. Ускорение, которое умеет замедлить, — не
       * ускорение. Два вызова просят разного и не зависят друг от друга.
       */
      const снимок = platform?.warmupFromCache().catch(() => false);
      try {
        get().applyMe(await http.get<MeResponse>("/auth/me"));
      } catch {
        set({ permissions: [] });
      }
      // Снимок дождаться всё-таки надо: `bootstrapped` ниже открывает экран, и
      // строки, приехавшие после него, дали бы прыжок вместо готового списка.
      await снимок;
      // Тем же вызовом, что и при входе: разъедься эти два пути, и «отошёл»
      // снова терялся бы ровно на одном из них — как и терялся до 28.08.
      await подтянутьСвоёСостояние();
    }
    set({ bootstrapped: true });
  },
}));
