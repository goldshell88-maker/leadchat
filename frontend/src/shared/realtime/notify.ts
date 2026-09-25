import { toastForMessage } from "@/platform/toast";
import type { MessageDto } from "@/shared/api/types";
import { useChatUiStore } from "@/shared/stores/chatUiStore";

/**
 * Звук нового сообщения (03 §3.5 + правила 10 §5.4):
 *  1) только входящие от клиента (direction=in, sender_type=client) — бот,
 *     коллеги и эхо своих беззвучны;
 *  2) открытый активный диалог в сфокусированной вкладке не звучит — это
 *     решает applyNewMessage ДО вызова notifyNewMessage;
 *  3) троттлинг 2 с — очередь входящих не строчит.
 * Файла в public/ нет — короткий beep собирается в data-URI (WAV) на лету.
 * Тумблер muted появится в профиле позже; каркас уже готов к нему.
 */

let lastSoundAt = 0;
let cachedAudio: HTMLAudioElement | null = null;

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
}

/**
 * Мягкий сигнал вместо писка (обратная связь диспетчера 02.09).
 *
 * ⚠ ЖАЛОБА ДОСЛОВНО: «звук выключил, потому что он режет сильно слух… пищит
 * просто так самым омерзительным звуком, который я слышал». Человек работать
 * не может: без звука не реагирует на сообщения, со звуком не может сидеть.
 *
 * ЧТО БЫЛО НЕ ТАК В САМОМ СИГНАЛЕ — три вещи, и все слышны:
 *
 *  1. ВОСЕМЬ БИТ ПРИ 8 кГц. Телефонное качество: у восьмибитного звука шаг
 *     квантования слышен как песок поверх тона, и на чистой синусоиде он
 *     заметнее всего. Теперь 16 бит при 44,1 кГц — это ничего не стоит, звук
 *     собирается в памяти, а не лежит в сборке.
 *  2. РЕЗКИЙ СТАРТ. Нарастания не было вовсе: сигнал начинался с полной
 *     громкости, и это щелчок. Ухо слышит щелчок как удар, а не как сигнал.
 *     Теперь восемь миллисекунд нарастания — не слышно как задержку, но
 *     щелчка нет.
 *  3. ЧИСТАЯ СИНУСОИДА 880 Гц. Ровно та область, где ухо чувствительнее
 *     всего, и без обертонов она звучит как медицинский прибор. Теперь два
 *     тона мягче (587 и 880 Гц, второй тише) и затухание по экспоненте — это
 *     ближе к удару по чему-то, а не к писку.
 *
 * Громкость снижена вдвое: сигнал должен звать, а не вздрагивать.
 */
export function buildBeepDataUri(): string {
  const sampleRate = 44100;
  const duration = 0.18;
  const samples = Math.floor(sampleRate * duration);
  const bytes = samples * 2; // 16 бит
  const buffer = new ArrayBuffer(44 + bytes);
  const v = new DataView(buffer);

  writeAscii(v, 0, "RIFF");
  v.setUint32(4, 36 + bytes, true);
  writeAscii(v, 8, "WAVE");
  writeAscii(v, 12, "fmt ");
  v.setUint32(16, 16, true); // PCM chunk size
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true); // byte rate (16-бит моно)
  v.setUint16(32, 2, true); // block align
  v.setUint16(34, 16, true); // bits per sample
  writeAscii(v, 36, "data");
  v.setUint32(40, bytes, true);

  const ATTACK = Math.floor(sampleRate * 0.008); // 8 мс — снимает щелчок
  for (let i = 0; i < samples; i++) {
    const t = i / sampleRate;
    // Нарастание, затем экспоненциальное затухание — как у удара, а не у писка.
    const rise = i < ATTACK ? i / ATTACK : 1;
    const decay = Math.exp(-4.5 * (i / samples));
    const tone = Math.sin(2 * Math.PI * 587 * t) + 0.35 * Math.sin(2 * Math.PI * 880 * t);
    const value = (tone / 1.35) * rise * decay * 0.28;
    v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, value)) * 32767, true);
  }

  const raw = new Uint8Array(buffer);
  let binary = "";
  // Кусками: `String.fromCharCode(...массив)` на сорока тысячах байт роняет
  // стек (Maximum call stack size exceeded), а посимвольно — заметно медленнее.
  const CHUNK = 8192;
  for (let i = 0; i < raw.length; i += CHUNK) {
    binary += String.fromCharCode(...raw.subarray(i, i + CHUNK));
  }
  return `data:audio/wav;base64,${btoa(binary)}`;
}

function beep(): void {
  if (!useChatUiStore.getState().soundEnabled) return; // тумблер в /settings/profile (11 §4.3)
  try {
    cachedAudio ??= new Audio(buildBeepDataUri());
    const p = cachedAudio.play();
    // autoplay-policy до первого клика (03 §3.5) — молча глотаем
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch {
    // jsdom/старый браузер без Audio — звук просто не играет
  }
}

export function notifyNewMessage(msg: MessageDto): void {
  if (msg.direction !== "in" || msg.sender_type !== "client") return; // 10 §5.4 п.1

  // Окно платформы: нативный тост Windows в десктопе, карточка браузера в вебе
  // (04 §4.1). Свой троттлинг у него другой (Rust, сводный режим), поэтому
  // 2-секундный порог звука на него не распространяем. Без выданного
  // разрешения и без моста функция просто выходит.
  //
  // ⚠ ЧЬЁ ЭТО СООБЩЕНИЕ, ЗДЕСЬ УЖЕ НЕ СПРАШИВАЮТ. Отбор «мой диалог» стоит
  // ВЫШЕ по потоку, в `applyNewMessage`: там есть `assignee_id` из кадра, а
  // здесь — только само сообщение. Вторая проверка по кэшу разошлась бы с
  // первой (правило в шапке `platform/toast.ts`), а цена ошибки — 4705
  // входящих от клиентов в сутки, о которых уведомлять нельзя.
  toastForMessage(msg);

  const now = Date.now();
  if (now - lastSoundAt < 2000) return; // троттлинг 2 с
  lastSoundAt = now;
  beep();
}

/**
 * Звук при передаче диалога (11 §2.4: «у получателя — звук, ⚑ и тост»).
 * Тумблер звука общий; троттлинг тот же — передача редкое событие, но
 * совпасть с потоком входящих может.
 */
export function notifyAssignedToMe(): void {
  const now = Date.now();
  if (now - lastSoundAt < 2000) return;
  lastSoundAt = now;
  beep();
}
