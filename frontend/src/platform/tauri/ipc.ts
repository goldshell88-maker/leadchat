/**
 * Тонкий слой IPC к ядру Tauri 2.
 *
 * ПОЧЕМУ НЕ `@tauri-apps/api`: требование 03 §7 — веб-бандл не должен тащить
 * ни байта Tauri. Пакеты `@tauri-apps/*` — это обёртки над одним и тем же
 * инжектируемым объектом `window.__TAURI_INTERNALS__`; вызывая его напрямую,
 * мы получаем нулевую зависимость в package.json, честно пустой веб-бандл и
 * ровно то же поведение внутри WebView. Плагины дергаются по их собственным
 * командам (`plugin:autostart|enable` и т.п.) — capabilities те же (04 §8.3).
 *
 * // CHECK: имена внутренних команд ядра (`plugin:event|listen`) фиксировать по
 * версии Tauri 2.x на момент старта — API стабилен внутри мажора.
 */

interface TauriInternals {
  invoke<T>(cmd: string, args?: Record<string, unknown>, options?: unknown): Promise<T>;
  transformCallback(cb: (payload: unknown) => void, once?: boolean): number;
}

declare global {
  interface Window {
    __TAURI_INTERNALS__?: TauriInternals;
  }
}

function internals(): TauriInternals {
  const api = typeof window !== "undefined" ? window.__TAURI_INTERNALS__ : undefined;
  if (!api) throw new Error("Tauri IPC недоступен: приложение запущено не в WebView Tauri");
  return api;
}

/** Вызов `#[tauri::command]` (04 §8.1). Ключи args — camelCase, Tauri сам приводит к snake_case. */
export function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  return internals().invoke<T>(cmd, args ?? {});
}

/** Вызов, который не должен ронять UI: логируем и отдаём fallback. */
export async function invokeSafe<T>(cmd: string, args: Record<string, unknown>, fallback: T): Promise<T> {
  try {
    return await invoke<T>(cmd, args);
  } catch (e) {
    console.warn(`[platform] ${cmd} не выполнена:`, e);
    return fallback;
  }
}

interface EventEnvelope<T> {
  event: string;
  id: number;
  payload: T;
}

export type Unlisten = () => void;

/**
 * Подписка на событие из Rust (`app.emit(...)`, 04 §4.2/§8.2).
 * Эквивалент `@tauri-apps/api/event.listen` — тот же `plugin:event|listen`.
 */
export function listen<T>(event: string, handler: (payload: T) => void): Promise<Unlisten> {
  const api = internals();
  const cbId = api.transformCallback((raw) => {
    const env = raw as EventEnvelope<T>;
    handler(env?.payload as T);
  });
  return api
    .invoke<number>("plugin:event|listen", { event, target: { kind: "Any" }, handler: cbId })
    .then((eventId) => () => {
      void api.invoke("plugin:event|unlisten", { event, eventId }).catch(() => {});
    });
}

/**
 * Фронт → Rust: событие для `app.listen(...)` на стороне ядра.
 * Эквивалент `@tauri-apps/api/event.emit` — тот же `plugin:event|emit`
 * (разрешение `core:event:allow-emit` уже в capabilities/main.json).
 */
export function emit(event: string, payload?: unknown): Promise<void> {
  return internals().invoke<void>("plugin:event|emit", { event, payload: payload ?? null });
}

/** Эмит, который не должен ронять старт приложения. */
export function emitSafe(event: string, payload?: unknown): void {
  try {
    void emit(event, payload).catch((e) => console.warn(`[platform] событие «${event}» не отправлено:`, e));
  } catch (e) {
    console.warn(`[platform] событие «${event}» не отправлено:`, e);
  }
}

/** Подписка, переживающая отсутствие ядра (тесты, ранний старт): не бросает. */
export function listenSafe<T>(event: string, handler: (payload: T) => void): Unlisten {
  let unlisten: Unlisten | null = null;
  let cancelled = false;
  /*
   * ⚠ ПОДПИСЬ «не бросает» БЫЛА НЕПРАВДОЙ (07.09). `listen` проверяет
   * `window.__TAURI_INTERNALS__` ДО первого `await` (`internals()`), то есть
   * исключение вылетает из самого `listenSafe`, мимо `.catch` ниже — тот
   * ловит только отказ уже созданного обещания. Соседний `emitSafe` закрыт
   * try/catch с самого начала; здесь его просто забыли.
   *
   * Цена промаха не теоретическая. Обе подписки `wireDesktop` (§8.2 «дай
   * токен» и статус в трее) стоят в ОДНОЙ цепочке с `emitSafe(APP_READY_EVENT)`,
   * и первый же бросок уносит вместе с ними «слушатели навешаны»: отложенный
   * переход по клику на тост при выключенном приложении остаётся в очереди
   * Rust навсегда (`lib.rs::queue_navigate`). Видно это было бы как «нажал на
   * уведомление, приложение открылось, а диалог не тот».
   */
  try {
    void listen<T>(event, handler)
      .then((u) => {
        if (cancelled) u();
        else unlisten = u;
      })
      .catch((e) => console.warn(`[platform] подписка на «${event}» не удалась:`, e));
  } catch (e) {
    console.warn(`[platform] подписка на «${event}» не удалась:`, e);
  }
  return () => {
    cancelled = true;
    unlisten?.();
    unlisten = null;
  };
}
