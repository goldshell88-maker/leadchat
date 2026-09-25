import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { useAutoAway } from "@/features/presence/автоОтошёл";
import { usePresenceStore } from "@/features/presence/usePresence";

/**
 * «ОТОШЁЛ» СТАВИТСЯ САМ.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 31.08: «пишется, что два сотрудника онлайн, а их даже нет
 * — просто включены ПК».
 *
 * Присутствие на сервере означает «приложение открыто и отвечает на пинги»:
 * ключ живёт минутами и продлевается каждым пингом сокета. Пинги шлёт вкладка,
 * а не человек — значит включённый компьютер честно докладывал «на месте» хоть
 * всю ночь. Статус «отошёл» существовал, но выставлялся только руками, а руками
 * его не ставит никто: уходя со смены, человек закрывает не вкладку, а глаза.
 */

const запросы: { status: string }[] = [];

vi.mock("@/shared/api/http", () => ({
  http: {
    put: async (_url: string, body: { status: string }) => {
      запросы.push(body);
      return body;
    },
  },
}));

/**
 * ⚠ ПОРОГ ПОДНЯТ ДО 30 МИНУТ 01.09 ПО РЕШЕНИЮ ВЛАДЕЛЬЦА («если нет активности
 * через 30 минут, ставился статус отошёл»). Времена в этом наборе пересчитаны
 * вместе с ним, а не подогнаны: «дольше порога» стало 31 минутой, «обычная
 * пауза в работе» — двадцатью минутами. Тридцать МЯГЧЕ десяти, и это
 * осознанно: диспетчер, который полчаса ведёт переписку в самом Авито и нашу
 * вкладку не трогает, при десяти минутах числился бы отошедшим зря.
 */
const ПОРОГ_МИН = 30;

describe("Присутствие считается по действиям, а не по открытой вкладке", () => {
  beforeEach(() => {
    запросы.length = 0;
    vi.useFakeTimers();
    usePresenceStore.getState().set("online");
    // Отметка активности и последний отправленный статус теперь ОБЩИЕ на все
    // вкладки и живут в localStorage. Без очистки соседний тест унаследовал бы
    // чужую отметку, и порог считался бы не от нуля.
    localStorage.clear();
  });

  it("⚠ молчание дольше порога переводит в «отошёл»", () => {
    renderHook(() => useAutoAway());

    vi.advanceTimersByTime((ПОРОГ_МИН + 1) * 60 * 1000);

    expect(запросы.at(-1)?.status, "человек ушёл, а система держит его «на месте»").toBe("away");
  });

  it("короткая пауза статус не трогает", () => {
    // Отойти налить чай — не «нет на работе».
    renderHook(() => useAutoAway());
    vi.advanceTimersByTime(20 * 60 * 1000);
    expect(запросы, "статус дёрнули на обычной паузе в работе").toHaveLength(0);
  });

  it("действие возвращает «на месте»", () => {
    renderHook(() => useAutoAway());
    vi.advanceTimersByTime((ПОРОГ_МИН + 1) * 60 * 1000);
    expect(запросы.at(-1)?.status).toBe("away");
    usePresenceStore.getState().set("away");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));

    expect(запросы.at(-1)?.status, "человек вернулся и работает, а числится ушедшим").toBe("online");
  });

  it("не шлёт на каждое нажатие: одно подтверждение, дальше тишина", () => {
    renderHook(() => useAutoAway());
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "b" }));
    vi.advanceTimersByTime(60 * 1000);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "c" }));
    expect(запросы, "шквал запросов на каждое нажатие").toHaveLength(1);
  });

  it("работающее устройство напоминает серверу «на месте» раз в пять минут", () => {
    /*
     * Сервер не даёт простаивающей вкладке поставить «отошёл», пока видел
     * человека активным последние полчаса, — а видит он его только по этому
     * запросу. Телефон, отправивший «на месте» один раз, через полчаса
     * проигрывал компьютеру, и сторож забирал диалоги работающего человека.
     */
    renderHook(() => useAutoAway());
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    vi.advanceTimersByTime(6 * 60 * 1000);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "b" }));
    expect(запросы.map((r) => r.status)).toEqual(["online", "online"]);
  });

  it("заслон клавиши приёма висит на всём приложении, а не на экране чатов", () => {
    /*
     * ⚠ ТРЕТЬЕ ВОЗВРАЩЕНИЕ ЖАЛОБЫ «Ctrl+R так и перезагружает страницу».
     * Заслон был, работал и доехал в боевую сборку — но жил внутри
     * `useChatHotkeys`, а тот монтируется ТОЛЬКО на `/chats`. Стоило открыть
     * настройки или статистику — и клавиша снова доставалась браузеру.
     */
    const раскладка = readFileSync("src/app/AppLayout.tsx", "utf-8") as string;
    expect(раскладка, "заслон снова не подключён к раскладке приложения").toMatch(
      /useClaimKeyGuard\(\)/,
    );
    const хук = readFileSync("src/features/hotkeys/useChatHotkeys.ts", "utf-8") as string;
    expect(хук, "заслон перестал работать на фазе перехвата").toMatch(
      /addEventListener\("keydown", наПерехвате, true\)/,
    );
  });
});
