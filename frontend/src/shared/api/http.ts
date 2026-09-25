import { useSessionStore } from "@/shared/stores/sessionStore";
import type { HealthDto, LoginResponse } from "./types";

const ROOT_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
/** Base URL per 01 §1.1; VITE_API_BASE is set only by the desktop build (04 §1.1). */
const API_BASE = `${ROOT_BASE}/api/v1`;

/**
 * Адрес сервера для `src`/`href` вложения (проверка 24.09). API отдаёт
 * ОТНОСИТЕЛЬНЫЕ подписанные ссылки (`/api/v1/media/…?sig=`, `/api/v1/avito-img/…`):
 * в вебе они ведут на тот же сервер, а в сборке для Windows страница живёт на
 * `tauri.localhost`, и фото, голосовые и файлы грузились бы оттуда. В вебе
 * `ROOT_BASE` пуст — адрес не меняется.
 */
export function withApiRoot(url: string): string {
  return ROOT_BASE && url.startsWith("/") && !url.startsWith("//") ? `${ROOT_BASE}${url}` : url;
}

/** Synthetic code for fetch-level failures (no HTTP response at all). */
export const NETWORK_ERROR = "network_error";

/**
 * СЕРВЕР ЗАМОЛЧАЛ — ЭТО ТОЖЕ ОТКАЗ (ACC-06, DIALOGS-13).
 *
 * ЧТО БЫЛО. Ни у одного запроса не было потолка ожидания: `fetch` вызывался
 * без `AbortSignal` и без таймера. Оборванную сеть браузер замечает сам и
 * отбивает запрос ошибкой; а вот случай «соединение приняли и не ответили»
 * браузер не отбивает НИКОГДА — промис `fetch` просто не выполняется. Для
 * человека это не ошибка, а вечная загрузка: на «Разборе диалогов» бесконечно
 * крутится спиннер, на настройках — скелетоны, и ни кнопки «Повторить», ни
 * слова о том, что случилось. Отличить это от «медленно грузится» нельзя
 * ничем, поэтому люди сидят и ждут.
 *
 * И это не редкость. Каждый выкат перезапускает контейнер `api`, а запрос
 * таблицы считает метрики по 454 тысячам диалогов — обе ситуации штатные, и
 * обе кончаются молчащим соединением.
 *
 * ЧТО ЗДЕСЬ. Свой таймер и свой `AbortController` на каждый запрос. Отдельный
 * код отказа нужен потому, что говорить надо РАЗНОЕ: при оборванной сети
 * человеку чинить интернет, при молчащем сервере — просто повторить, интернет
 * у него в порядке. Один текст на оба случая половину людей отправил бы
 * дёргать роутер впустую.
 */
export const TIMEOUT_ERROR = "timeout";

/**
 * Потолок ожидания обычного запроса.
 *
 * Двадцать секунд — не «на глаз»: самая тяжёлая ручка продукта (таблица
 * разбора с сортировкой по метрике) на боевой базе отвечает за единицы секунд,
 * а nginx перед приложением рвёт связь на своих шестидесяти. Между этими
 * числами и живёт порог: короче — резали бы честные медленные ответы, длиннее —
 * человек полминуты смотрел бы на крутилку, ничего не узнав.
 */
export const REQUEST_TIMEOUT_MS = 20_000;

/**
 * Потолок для вложений, которые уходят на сервер.
 *
 * Здесь ждать приходится не сервер, а канал: фотография с телефона диспетчера
 * идёт по нему минуту, и это НОРМАЛЬНО. Общий двадцатисекундный потолок
 * обрывал бы такие передачи на середине — то есть чинил бы вечное ожидание
 * ценой сломанной отправки фото клиенту. Выгрузки вниз с 24.09 приходят
 * подписанной ссылкой и этим путём не идут.
 */
export const TRANSFER_TIMEOUT_MS = 120_000;

interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
    request_id?: string;
  };
}

/** Error-envelope of 01 §1.3, thrown for every non-2xx response. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: Record<string, unknown>,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Отказ nginx по частоте запросов (`error_page 429` в leadchat.conf.template).
 * У отказов самого приложения по лимиту код другой — `rate_limited`.
 */
export const TOO_MANY_REQUESTS = "too_many_requests";
const TOO_MANY_REQUESTS_MESSAGE = "Слишком много запросов подряд — подождите пару секунд";

async function toApiError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as ErrorEnvelope;
    if (body?.error?.code) {
      return new ApiError(
        res.status,
        body.error.code,
        body.error.message,
        body.error.details,
        body.error.request_id,
      );
    }
  } catch {
    // Non-JSON body (e.g. nginx 502) — fall through to the generic error.
  }
  // 429 без конверта — страница nginx старого образца: человеку нужно «подождите»,
  // а не «Запрос завершился с кодом 429».
  if (res.status === 429) return new ApiError(429, TOO_MANY_REQUESTS, TOO_MANY_REQUESTS_MESSAGE);
  return new ApiError(
    res.status,
    "internal_error",
    `Запрос завершился с кодом ${res.status}`,
  );
}

/**
 * `fetch` с потолком ожидания (см. `TIMEOUT_ERROR`).
 *
 * Свой `AbortController` вместо `AbortSignal.timeout()` — потому что сигнал
 * вызывающего надо СЛОЖИТЬ с таймером, а `AbortSignal.any()` есть не везде,
 * куда мы ставимся (десктопная сборка живёт в системном WebView). Сложение
 * руками работает одинаково всюду.
 *
 * Флаг `timedOut` обязателен: браузер отбивает и наш таймер, и отмену
 * вызывающего одинаковым `AbortError`, а различать их надо. Отмена — это
 * «ответ больше не нужен» (человек ушёл с экрана), и говорить про неё нечего;
 * таймаут — это отказ, о котором человеку сказать НАДО.
 */
async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<Response> {
  const ctl = new AbortController();
  let timedOut = false;
  const onOuterAbort = () => ctl.abort();
  if (signal?.aborted) ctl.abort();
  else signal?.addEventListener("abort", onOuterAbort, { once: true });
  const timer = setTimeout(() => {
    timedOut = true;
    ctl.abort();
  }, timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctl.signal });
  } catch (e) {
    if (timedOut) {
      throw new ApiError(
        0,
        TIMEOUT_ERROR,
        `Сервер не ответил за ${Math.round(timeoutMs / 1000)} с. Попробуйте ещё раз`,
      );
    }
    throw e;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onOuterAbort);
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  /** false — do not attach the access token and never attempt refresh (auth/* endpoints). */
  auth?: boolean;
  signal?: AbortSignal;
  /** Потолок ожидания; по умолчанию `REQUEST_TIMEOUT_MS`. Файлам нужен больший. */
  timeoutMs?: number;
}

async function send(
  path: string,
  {
    method = "GET",
    body,
    auth = true,
    signal,
    timeoutMs = REQUEST_TIMEOUT_MS,
  }: RequestOptions,
): Promise<Response> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = useSessionStore.getState().accessToken;
  if (auth && token) headers.Authorization = `Bearer ${token}`;
  try {
    return await fetchWithTimeout(
      `${API_BASE}${path}`,
      {
        method,
        headers,
        credentials: "include", // refresh cookie lc_refresh (httpOnly) travels with auth requests
        body: body !== undefined ? JSON.stringify(body) : undefined,
      },
      timeoutMs,
      signal,
    );
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    // Таймаут приходит сюда уже готовым `ApiError` и обязан дойти до человека
    // КАК ЕСТЬ. Раньше на его месте был бы «Сервер недоступен. Проверьте
    // соединение» — то есть молчащий сервер отправлял бы диспетчера чинить
    // интернет, с которым всё в порядке.
    if (e instanceof ApiError) throw e;
    throw new ApiError(
      0,
      NETWORK_ERROR,
      "Сервер недоступен. Проверьте соединение",
    );
  }
}

/* -------------------------------------------------------------------------- *
 *  Повтор запроса при временной беде
 * -------------------------------------------------------------------------- */

/**
 * Какие запросы МОЖНО повторить. Список закрытый, и это главное в нём.
 *
 * ЗАЧЕМ ВООБЩЕ. Отметка прочтения (`POST /conversations/{id}/read`) уходит при
 * каждом открытии диалога, а её единственный результат — погасший бейдж. Не
 * дошла — бейдж висит, и человеку не сказано ничего: вызывающий код глушит
 * ошибку намеренно (иначе всплывашка «не удалось» на каждое открытие диалога).
 * Тихая потеря плюс отсутствие повтора и дают ту самую жалобу «непрочитанные
 * висят без причины».
 *
 * ПОЧЕМУ ЭТО НЕ ТЕОРИЯ. Каждый выкат перезапускает контейнер `api`, и nginx
 * в эти секунды НЕ повторяет POST за нас: повтор на его стороне
 * (`proxy_next_upstream`, docker/nginx/templates/leadchat.conf.template) по
 * умолчанию не распространяется на неидемпотентные методы — для этого нужен
 * явный `non_idempotent`, которого там нет. GET'ы окно выката переживают, а
 * все /read, попавшие в него, умирают молча.
 *
 * ПОЧЕМУ СПИСОК, А НЕ «ПОВТОРЯТЬ ВСЕ POST». Повторить отправку сообщения
 * клиенту — это отправить его дважды. В список попадает только то, о чём
 * ДОКАЗАНО, что повтор ничего не меняет. Для /read это так по построению:
 * сервер двигает маркер только вперёд (app/services/read_markers.set_marker),
 * поэтому второй такой же запрос — пустая операция.
 */
const RETRYABLE: ReadonlyArray<{ method: string; path: RegExp }> = [
  { method: "POST", path: /^\/conversations\/[^/]+\/read$/ },
];

/**
 * Коды, после которых повторять осмысленно: беда снаружи и, скорее всего,
 * временная. 5xx приложения (500) сюда НЕ входит — это наша ошибка, и повтор
 * даст ту же ошибку три раза вместо одной. 429 тоже нет: там свой протокол с
 * `Retry-After`, и ломиться в закрытую дверь чаще — ровно то, от чего лимит и
 * поставлен. Отказ nginx по частоте ждёт свой срок отдельно — `sendPatiently`.
 */
const RETRY_STATUSES = new Set([502, 503, 504]);

/** Попыток всего (включая первую) и шаг нарастающей паузы. */
const RETRY_ATTEMPTS = 3;
const RETRY_BASE_DELAY_MS = 300;

function isRetryable(path: string, method: string): boolean {
  // Хвост запроса (`?...`) в правиле не участвует: у наших повторяемых ручек
  // его не бывает, а забытый `split` однажды тихо выключил бы повтор целиком.
  const clean = path.split("?")[0];
  return RETRYABLE.some(
    (rule) => rule.method === method && rule.path.test(clean),
  );
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Запрос с повтором и НАРАСТАЮЩЕЙ паузой (300 мс, 600 мс) — для запросов из
 * `RETRYABLE`. Пауза растёт, потому что беда обычно длится дольше одного
 * мгновения: выкат — это секунды, и три попытки подряд без пауз попали бы в
 * одно и то же окно.
 *
 * Повтор ТИХИЙ: ни всплывашек, ни записи в консоль. Человеку тут сказать
 * нечего — он не делал ничего, что можно было бы переделать.
 */
async function sendWithRetry(
  path: string,
  options: RequestOptions,
): Promise<Response> {
  const attempts = isRetryable(path, options.method ?? "GET")
    ? RETRY_ATTEMPTS
    : 1;
  for (let attempt = 1; ; attempt++) {
    const last = attempt >= attempts;
    try {
      const res = await send(path, options);
      if (last || !RETRY_STATUSES.has(res.status)) return res;
    } catch (e) {
      // Отмена — не беда сети: запрос больше никому не нужен.
      if (last || (e instanceof DOMException && e.name === "AbortError"))
        throw e;
      if (!(e instanceof ApiError && e.code === NETWORK_ERROR)) throw e;
    }
    await sleep(RETRY_BASE_DELAY_MS * attempt, options.signal);
  }
}

/**
 * 429 ОТ NGINX — «ПОДОЖДИТЕ СЕКУНДУ», А НЕ ОТКАЗ (проверка 24.09).
 *
 * Личная планка nginx (30 запросов в секунду, запас 60) отбивает запрос ДО
 * приложения: в журнале у таких ответов пустой upstream. Значит, повтор
 * безопасен для ЛЮБОГО метода, включая «Принять»: первая попытка до сервера не
 * дошла. Раньше такой ответ становился тостом «Запрос завершился с кодом 429»,
 * а открытый диалог — «Диалог недоступен» (5 583 отказа за 08.09–24.09).
 *
 * Отказ самого приложения по лимиту (дневной лимит выгрузок, поддержка) несёт
 * свой конверт с кодом `rate_limited` и сроком в часы — его не повторяем.
 */
const EDGE_LIMIT_RETRIES = 2;
const EDGE_LIMIT_WAIT_MS = 1_000;
const EDGE_LIMIT_MAX_WAIT_MS = 5_000;

function edgeLimitWaitMs(res: Response): number {
  const seconds = Number(res.headers.get("Retry-After"));
  if (!Number.isFinite(seconds) || seconds <= 0) return EDGE_LIMIT_WAIT_MS;
  return Math.min(seconds * 1000, EDGE_LIMIT_MAX_WAIT_MS);
}

/**
 * `sendWithRetry`, который переживает отказ nginx по частоте (разбор выше).
 * Любой 429 разбирается здесь же и бросается готовой ошибкой: тело ответа
 * читается один раз, а 429 успехом не бывает.
 */
async function sendPatiently(path: string, options: RequestOptions): Promise<Response> {
  for (let waited = 0; ; waited++) {
    const res = await sendWithRetry(path, options);
    if (res.status !== 429) return res;
    const error = await toApiError(res);
    if (error.code !== TOO_MANY_REQUESTS || waited >= EDGE_LIMIT_RETRIES) throw error;
    await sleep(edgeLimitWaitMs(res), options.signal);
  }
}

/**
 * Чем закончилась попытка обновить токен доступа.
 *
 * ⚠ ЗАЧЕМ ТРИ ИСХОДА, А НЕ ДВА (аудит экранов 31.08, «выкидывает из работы»).
 *
 * Здесь стоял `boolean`, и все пять мест читали `false` одинаково: «сессия
 * мертва — гасим и отправляем на вход». Но `false` возвращался ЛЮБОЙ бедой:
 * оборванным туннелем, потолком ожидания, 502 от nginx во время выкатки. То
 * есть моргнувшая на секунду сеть выбрасывала диспетчера из работающего
 * приложения — вместе с набранным ответом клиенту, — а экран входа объяснял
 * это «долгим перерывом или отключением учётной записи», чего не было.
 *
 * Разница между «нам отказали» и «мы не дозвонились» есть на проводе: отзыв
 * refresh-cookie сервер сообщает кодом 401/403, всё остальное — молчание,
 * таймаут или пятисотка. Первое необратимо, второе проходит само.
 */
export type ИтогОбновления =
  /** Токен обновлён, запрос можно повторять. */
  | "ok"
  /** Сервер сказал «нет»: refresh-cookie отозван или протух. Сессии конец. */
  | "отозван"
  /** До сервера не достучались. Сессию НЕ трогаем: связь вернётся. */
  | "нет-связи";

let refreshPromise: Promise<ИтогОбновления> | null = null;

/**
 * Single-flight refresh: any number of concurrent 401s awaits ONE
 * POST /auth/refresh — this promise is the queue of waiting requests.
 * On success the rotated access token lands in the session store and
 * every queued request retries with it.
 */
export function refreshSession(): Promise<ИтогОбновления> {
  if (!refreshPromise) {
    refreshPromise = подЗамкомВкладок(doRefresh).finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

/**
 * Обновление — ПО ОДНОЙ ВКЛАДКЕ ЗА РАЗ на весь браузер (замер 15.09: 52 отказа
 * `/auth/refresh` за сутки, пачками по 4–6 в минуту по утрам).
 *
 * Refresh-cookie одна на все вкладки, а сервер её РОТИРУЕТ: старый токен
 * гасится, повторное предъявление читается как кража и обрывает всю цепочку
 * (`security.rotate_refresh_token`). У диспетчера 11–13 вкладок; две из них,
 * проснувшись разом, шлют обновление с одной и той же cookie — первая
 * побеждает, вторая приносит уже погашенный токен, и человека выкидывает из
 * ВСЕХ вкладок. Замок Web Locks общий для вкладок одного домена: вторая
 * дожидается первой и идёт уже со свежей cookie. Где замков нет (старый
 * WebView) — как раньше.
 */
function подЗамкомВкладок<T>(работа: () => Promise<T>): Promise<T> {
  const locks = (globalThis.navigator as Navigator | undefined)?.locks;
  if (!locks?.request) return работа();
  return locks.request("lc-auth-refresh", () => работа()) as Promise<T>;
}

/* -------------------------------------------------------------------------- *
 *  Упреждающее обновление токена
 * -------------------------------------------------------------------------- */

/**
 * За сколько до истечения токена обновлять его самим (аудит 06.09).
 *
 * ЧТО БЫЛО. Токен доступа живёт 900 секунд, а обновлялся ТОЛЬКО по 401 на
 * живом запросе: `expires_in` сервер шлёт в каждом ответе входа и обновления
 * (`types.ts:LoginResponse`), но фронт его не читал. Замер: 1 440 ответов 401
 * в сутки — по одному на каждый истёкший токен каждой вкладки, — и каждый
 * стоил лишних два круга (0,27 с p50, до 1,4 с) ровно тому запросу, на
 * котором истёк: 0,4 % отправок и 0,74 % открытий диалога.
 *
 * Минута запаса покрывает разбег часов и медленный ответ обновления; сам
 * путь по 401 остаётся запасным — если таймер не успел, токен обновится как
 * раньше, только с теми же двумя кругами.
 */
export const REFRESH_AHEAD_MS = 60_000;

let таймерОбновления: ReturnType<typeof setTimeout> | null = null;

/**
 * Поставить таймер на `expires_in − 60 с`. Зовётся на каждую ВЫДАЧУ токена —
 * вход, принятие приглашения, обновление — и заменяет прежний таймер.
 *
 * ⚠ ОБНОВЛЯЕТ ТОЛЬКО ВИДИМАЯ ВКЛАДКА. У диспетчера 11–13 вкладок за смену, и
 * почти все скрыты; обновлять токен в каждой значило бы тринадцать
 * `POST /auth/refresh` каждые четверть часа впустую — и все на один офисный
 * адрес с пределом 30 запросов в секунду. Скрытая вкладка при первом же
 * своём запросе пойдёт прежним путём по 401 — там ей и место.
 *
 * Не число или короче минуты — таймера нет: старый сервер без поля или
 * тестовый токен на секунды обслуживаются путём по 401.
 *
 * Отказ сервера здесь сессию НЕ гасит: токен ещё минуту действителен, и
 * первый рабочий запрос выяснит правду сам (`неПолучилосьОбновить`) — там
 * человека выгоняют только за настоящий отзыв, а не за моргнувшую сеть.
 */
export function запланироватьОбновление(expiresIn: unknown): void {
  if (таймерОбновления !== null) {
    clearTimeout(таймерОбновления);
    таймерОбновления = null;
  }
  if (typeof expiresIn !== "number" || !Number.isFinite(expiresIn)) return;
  const через = expiresIn * 1000 - REFRESH_AHEAD_MS;
  if (через <= 0) return;
  таймерОбновления = setTimeout(() => {
    таймерОбновления = null;
    if (!useSessionStore.getState().accessToken) return; // вышли — обновлять нечего
    if (
      typeof document !== "undefined" &&
      document.visibilityState !== "visible"
    )
      return;
    void refreshSession();
  }, через);
}

/** Ответ с токеном — вход, приглашение, обновление: отсюда отсчёт до следующего. */
function учестьВыдачуТокена(body: unknown): void {
  if (!body || typeof body !== "object" || !("access_token" in body)) return;
  запланироватьОбновление((body as { expires_in?: unknown }).expires_in);
}

async function doRefresh(): Promise<ИтогОбновления> {
  let res: Response;
  try {
    /*
     * ПОТОЛОК ЗДЕСЬ ВАЖНЕЕ, ЧЕМ ГДЕ БЫ ТО НИ БЫЛО. Этот запрос стоит первым при
     * каждом открытии вкладки (`sessionStore.bootstrap`), и до его ответа
     * `bootstrapped` остаётся false, то есть на экране висит скелет оболочки.
     * Молчащий сервер означал бы не «медленный вход», а скелет НАВСЕГДА — без
     * формы входа, без кнопки, без слова. Отказ по таймеру честнее: человек
     * попадает на экран входа и видит, что связи нет.
     */
    res = await fetchWithTimeout(
      `${API_BASE}/auth/refresh`,
      { method: "POST", credentials: "include" },
      REQUEST_TIMEOUT_MS,
    );
  } catch {
    // Сеть, таймаут, обрыв туннеля — сервер своего слова не сказал.
    return "нет-связи";
  }
  // Только явный отказ означает конец сессии. 500/502/504 — это упавший или
  // перезапускаемый сервер, и выгонять из-за него человека не за что.
  if (res.status === 401 || res.status === 403) return "отозван";
  if (!res.ok) return "нет-связи";
  try {
    const data = (await res.json()) as LoginResponse;
    useSessionStore.getState().setSession(data.access_token, data.user);
    учестьВыдачуТокена(data);
    return "ok";
  } catch {
    // Двухсотка с мусором вместо тела: сервер жив, но ответ невнятен. Считаем
    // это сбоем связи, а не отзывом, — цена ошибки здесь несимметрична.
    return "нет-связи";
  }
}

/**
 * Общий разбор неудачного обновления для трёх мест, где ходит 401 (обычный
 * запрос, загрузка файла, скачивание). Гасит сессию только при настоящем
 * отзыве; при сбое связи бросает понятную сетевую ошибку и ОСТАВЛЯЕТ человека
 * в приложении — там его ждёт открытый диалог и набранный текст.
 */
async function неПолучилосьОбновить(
  итог: ИтогОбновления,
  res: Response,
): Promise<never> {
  if (итог === "отозван") {
    useSessionStore.getState().clear();
    throw await toApiError(res);
  }
  throw new ApiError(
    0,
    NETWORK_ERROR,
    "Сервер недоступен. Проверьте соединение",
  );
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  let res = await sendPatiently(path, options);

  // 401 → one silent refresh, then a single retry (01 §1.2). auth:false
  // endpoints (login, invite) surface their 401 as-is.
  if (res.status === 401 && options.auth !== false) {
    const итог = await refreshSession();
    if (итог !== "ok") await неПолучилосьОбновить(итог, res);
    res = await sendPatiently(path, options);
  }

  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  const data = (await res.json()) as T;
  // Вход и принятие приглашения — единственные `auth: false` ручки с токеном
  // в ответе; отсчёт до обновления начинается здесь, а не в сторе сессии,
  // потому что здесь проходят ВСЕ выдачи токена, включая `doRefresh` выше.
  if (options.auth === false) учестьВыдачуТокена(data);
  return data;
}

/**
 * Multipart-запрос (POST /media, 01 §6.5): те же правила, что и у JSON —
 * Bearer, refresh-cookie, единый error-envelope и одна тихая попытка refresh.
 * Content-Type НЕ ставим руками: boundary проставляет браузер.
 */
export async function requestForm<T>(
  path: string,
  form: FormData,
  signal?: AbortSignal,
): Promise<T> {
  const call = async (): Promise<Response> => {
    const headers: Record<string, string> = {};
    const token = useSessionStore.getState().accessToken;
    if (token) headers.Authorization = `Bearer ${token}`;
    try {
      // Потолок — файловый (`TRANSFER_TIMEOUT_MS`), а не общий: фотография с
      // телефона диспетчера по мобильному каналу идёт минуту, и это не отказ.
      return await fetchWithTimeout(
        `${API_BASE}${path}`,
        { method: "POST", headers, credentials: "include", body: form },
        TRANSFER_TIMEOUT_MS,
        signal,
      );
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") throw e;
      if (e instanceof ApiError) throw e;
      throw new ApiError(
        0,
        NETWORK_ERROR,
        "Сервер недоступен. Проверьте соединение",
      );
    }
  };

  let res = await call();
  if (res.status === 401) {
    const итог = await refreshSession();
    if (итог !== "ok") await неПолучилосьОбновить(итог, res);
    res = await call();
  }
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as T;
}

export const http = {
  get: <T>(path: string, opts?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...opts, method: "GET" }),
  post: <T>(
    path: string,
    body?: unknown,
    opts?: Omit<RequestOptions, "method" | "body">,
  ) => request<T>(path, { ...opts, method: "POST", body }),
  patch: <T>(
    path: string,
    body?: unknown,
    opts?: Omit<RequestOptions, "method" | "body">,
  ) => request<T>(path, { ...opts, method: "PATCH", body }),
  // PUT появился ради PUT /presence (#34): контракт 01 §11.6 называет именно
  // его, и подменять глагол на POST ради удобства клиента значит разойтись с
  // документом, по которому пишут обе стороны.
  put: <T>(
    path: string,
    body?: unknown,
    opts?: Omit<RequestOptions, "method" | "body">,
  ) => request<T>(path, { ...opts, method: "PUT", body }),
  del: <T>(path: string, opts?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...opts, method: "DELETE" }),
};

/** GET /api/health lives OUTSIDE /api/v1 (01 §1.1) and needs no auth. */
export async function fetchHealth(): Promise<HealthDto> {
  let res: Response;
  try {
    // Точка связи в шапке обязана рано или поздно почернеть. Без потолка
    // молчащий сервер оставлял бы её в состоянии «Подключение…» навсегда — то
    // есть индикатор связи первым переставал бы говорить правду о связи.
    res = await fetchWithTimeout(
      `${ROOT_BASE}/api/health`,
      {},
      REQUEST_TIMEOUT_MS,
    );
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(0, NETWORK_ERROR, "Сервер недоступен");
  }
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as HealthDto;
}
