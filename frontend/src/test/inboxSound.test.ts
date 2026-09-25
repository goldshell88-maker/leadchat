import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { playInboxChime } from "@/features/chats/inbox/sound";
import { useChatUiStore } from "@/shared/stores/chatUiStore";

/**
 * Звук новой строки в очереди (7.1 п.6).
 *
 * Проверяется свойство, ради которого сигнал вообще заведён отдельно: очередь
 * звучит ИНАЧЕ, чем обычное сообщение. Оператор на девяти каналах не смотрит в
 * экран постоянно, и «клиент ждёт, чтобы его взяли» он должен отличать от
 * «пришло сообщение в мой диалог» на слух.
 *
 * Троттлинг живёт в модуле и переживает тесты, поэтому каждый тест начинается
 * в заведомо новом времени: иначе «звука не было» проходило бы само собой, и
 * тест ничего бы не доказывал.
 */
describe("Сигнал очереди", () => {
  let plays: number;
  let clock = Date.parse("2026-08-06T10:00:00Z");

  beforeEach(() => {
    vi.useFakeTimers();
    clock += 60_000; // заведомо за порогом троттлинга от прошлого теста
    vi.setSystemTime(clock);
    plays = 0;
    useChatUiStore.setState({ soundEnabled: true });
    // Audio в jsdom нет — подменяем минимальным двойником, считающим play().
    vi.stubGlobal(
      "Audio",
      class {
        currentTime = 0;
        play() {
          plays += 1;
          return Promise.resolve();
        }
      },
    );
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("бьёт дважды: два коротких удара слышны как событие, один — как «капнуло»", () => {
    playInboxChime();
    expect(plays).toBe(1);

    vi.advanceTimersByTime(300); // второй удар отложен, но обязателен
    expect(plays).toBe(2);
  });

  it("подряд идущие диалоги не превращаются в трель", () => {
    playInboxChime();
    vi.advanceTimersByTime(300);
    expect(plays).toBe(2); // первый диалог отзвучал полностью

    playInboxChime(); // второй в пределах двух секунд — молчим
    vi.advanceTimersByTime(300);
    expect(plays).toBe(2);

    vi.advanceTimersByTime(2_000); // порог прошёл — снова слышно
    playInboxChime();
    vi.advanceTimersByTime(300);
    expect(plays).toBe(4);
  });

  it("выключенный звук выключен и для очереди", () => {
    useChatUiStore.setState({ soundEnabled: false });

    playInboxChime();
    vi.advanceTimersByTime(300);
    expect(plays).toBe(0);

    // И это именно тумблер, а не троттлинг: включили — зазвучало сразу.
    useChatUiStore.setState({ soundEnabled: true });
    playInboxChime();
    expect(plays).toBe(1);
  });
});
