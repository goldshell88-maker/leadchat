import { buildBeepDataUri } from "@/shared/realtime/notify";
import { useChatUiStore } from "@/shared/stores/chatUiStore";

/**
 * Звук новой строки в очереди (7.1 п.6). Механизм переиспользован целиком —
 * тот же WAV из `notify.ts`, собранный в data-URI, тот же тумблер звука, тот же
 * порог троттлинга. Отличается рисунок: очередь звенит ДВАЖДЫ.
 *
 * Почему двойной, а не другая нота: новый диалог в очереди важнее очередного
 * сообщения, и оператор должен различать их не глядя на экран. Два коротких
 * удара слышны как «событие», один — как «капнуло». Второй файл ради этого не
 * нужен: повторный `play()` того же элемента ничего не весит в бандле.
 */

const REPEAT_DELAY_MS = 260;
const THROTTLE_MS = 2000;

let cachedAudio: HTMLAudioElement | null = null;
let lastAt = 0;
let pendingSecondStrike: ReturnType<typeof setTimeout> | undefined;

function strike(): void {
  const audio = cachedAudio;
  if (!audio) return;
  try {
    audio.currentTime = 0;
    const p = audio.play();
    // autoplay-policy до первого клика (03 §3.5) — молча глотаем
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch {
    // jsdom/старый браузер без Audio — звук просто не играет
  }
}

/** Два коротких сигнала на `inbox:new`; тумблер и троттлинг — как у сообщений. */
export function playInboxChime(): void {
  if (!useChatUiStore.getState().soundEnabled) return; // тумблер в /settings/profile (11 §4.3)
  const now = Date.now();
  if (now - lastAt < THROTTLE_MS) return;
  lastAt = now;
  try {
    cachedAudio ??= new Audio(buildBeepDataUri());
  } catch {
    return; // среды без конструктора Audio
  }
  clearTimeout(pendingSecondStrike); // второй удар всегда ровно один
  strike();
  pendingSecondStrike = setTimeout(strike, REPEAT_DELAY_MS);
}
