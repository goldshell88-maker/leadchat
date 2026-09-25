import type { UpdateInfo } from "../bridge";
import { useUpdateStore } from "../updateStore";
import { invoke, listenSafe, type Unlisten } from "./ipc";

/**
 * Автообновление, клиентская часть (04 §6.3).
 *
 * Проверку/скачивание/подпись делает Rust (`tauri-plugin-updater`): весь
 * Windows-специфичный код остаётся на своей стороне, а фронт лишь слушает
 * событие «есть версия» и рисует НЕБЛОКИРУЮЩИЙ баннер. Ставится обновление
 * только по клику — у оператора может быть недописанный ответ.
 */

/** Rust: `app.emit("update:available", { version, notes, pub_date })`. */
export const UPDATE_AVAILABLE_EVENT = "update:available";

/** Первая проверка — через 30 с после старта, чтобы не мешать синку (04 §6.3). */
export const FIRST_CHECK_DELAY_MS = 30_000;
export const CHECK_INTERVAL_MS = 4 * 60 * 60_000;

interface UpdateRaw {
  version?: string;
  notes?: string | null;
  body?: string | null;
  pub_date?: string | null;
  available?: boolean;
}

export function toUpdateInfo(raw: UpdateRaw | null | undefined): UpdateInfo | null {
  if (!raw?.version) return null;
  if (raw.available === false) return null;
  return { version: raw.version, notes: raw.notes ?? raw.body ?? null, pubDate: raw.pub_date ?? null };
}

/**
 * Проверка обновлений: `null` — обновления нет, сбой — исключение с причиной.
 *
 * ⚠ СБОЙ НЕ РАВЕН «ОБНОВЛЕНИЯ НЕТ» (проверка 24.09). Здесь стоял `invokeSafe`
 * с запасным `null`, и нет сети, недоступный сервер обновлений или несошедшаяся
 * подпись читались «У вас последняя версия» — человек оставался на старой
 * сборке. Плановые проверки ошибку глотают сами (`startUpdater`): о пропуске
 * фоновой проверки человеку знать незачем.
 */
export async function checkForUpdates(): Promise<UpdateInfo | null> {
  const store = useUpdateStore.getState();
  store.setChecking(true);
  try {
    let raw: UpdateRaw | null;
    try {
      raw = await invoke<UpdateRaw | null>("updater_check", {});
    } catch (e) {
      // Команды Tauri отдают ошибку строкой, а не `Error`.
      throw new Error(
        typeof e === "string" && e ? e : e instanceof Error && e.message ? e.message : "проверка не состоялась",
      );
    }
    const info = toUpdateInfo(raw);
    store.announce(info);
    return info;
  } finally {
    useUpdateStore.getState().setChecking(false);
  }
}

function checkInBackground(): void {
  checkForUpdates().catch((e: unknown) => console.warn("[platform] фоновая проверка обновлений не удалась:", e));
}

/** Скачать + проверить minisign-подпись + поставить + перезапустить — всё в Rust. */
export async function installUpdate(): Promise<void> {
  const store = useUpdateStore.getState();
  store.setApplying(true);
  try {
    await invoke("updater_install", {});
  } catch (e) {
    store.setError(e instanceof Error ? e.message : "Не удалось установить обновление");
    throw e;
  }
}

/** Подписка на событие + расписание проверок; возвращает отписку. */
export function startUpdater(): Unlisten {
  const off = listenSafe<UpdateRaw>(UPDATE_AVAILABLE_EVENT, (raw) => {
    const info = toUpdateInfo(raw);
    if (info) useUpdateStore.getState().announce(info);
  });

  const first = setTimeout(checkInBackground, FIRST_CHECK_DELAY_MS);
  const timer = setInterval(checkInBackground, CHECK_INTERVAL_MS);

  return () => {
    off();
    clearTimeout(first);
    clearInterval(timer);
  };
}
