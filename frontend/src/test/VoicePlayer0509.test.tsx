// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts и shellFrame0509.test.ts: сторож только читает файл.
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { VoicePlayer } from "@/features/chats/components/thread/VoicePlayer";

/**
 * СВОЙ ПРОИГРЫВАТЕЛЬ ГОЛОСОВОГО (просьба владельца 05.09 со скриншотом:
 * «сделай качественный визуал для аудио»).
 *
 * ЧТО БЫЛО. Голое `<audio controls autoPlay>`: панель рисует браузер — своим
 * шрифтом, своими значками и своей шириной (замер 28.08: 300px по умолчанию,
 * плеер торчал на 92px за скруглённый край пузыря). Внутри переписки это
 * выглядело вставкой из чужой программы.
 *
 * Замер боя за 60 дней: голосовых 597 — в них клиент обычно и рассказывает
 * суть заказа. Видео при этом НОЛЬ, и проигрывателя под него здесь нет
 * намеренно: Авито видео не отдаёт вовсе.
 *
 * ⚠ ПОЧЕМУ ЗДЕСЬ ЕСТЬ ПРОВЕРКИ ПО ТЕКСТУ CSS. В vitest стоит `css: false`: в
 * jsdom стилей нет, ширины и цвета нулевые, померить нечем. Поэтому вид
 * проверяется единственным проверяемым способом — что правило написано; всё
 * остальное проверяется поведением.
 */

const VOICE_CSS = "src/features/chats/components/thread/voice-player.css";

/**
 * `play`/`pause` в jsdom не реализованы: вызов пишет «Not implemented» в
 * консоль и не возвращает промиса. Подменяем на прототипе — так же видно
 * ЧЕЙ элемент попросили остановить (`mock.instances`), а это половина
 * проверок ниже.
 */
function протезМедиа() {
  const play = vi
    .spyOn(HTMLMediaElement.prototype, "play")
    .mockImplementation(() => Promise.resolve());
  const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  return { play, pause };
}

/** Запись на 23 секунды: длина известна сразу, метаданные ждать не надо. */
function нарисовать(duration: number | null = 23) {
  const итог = render(<VoicePlayer url="https://avito.example/voice.mp3" duration={duration} />);
  const аудио = итог.container.querySelector("audio") as HTMLAudioElement;
  const полоса = screen.getByRole("slider");
  return { ...итог, аудио, полоса };
}

/** Полоса шириной 200px от левого края: в jsdom раскладки нет, задаём сами. */
function ширинаПолосы(полоса: HTMLElement, width = 200) {
  полоса.getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: width, bottom: 24, width, height: 24, x: 0, y: 0 }) as DOMRect;
}

afterEach(() => {
  // ⚠ СНАЧАЛА РАЗМОНТИРОВАТЬ, ПОТОМ СНЯТЬ ПРОТЕЗЫ. Общий `cleanup` из
  // setup.ts выполняется ПОСЛЕ этого хука, и без явного вызова здесь плеер
  // закрывался бы с уже восстановленным `pause` — то есть настоящим методом
  // jsdom, которого нет. Прогон при этом зелёный, но каждая проверка пишет в
  // консоль «Not implemented», и настоящую ошибку в этом шуме не увидеть.
  cleanup();
  vi.restoreAllMocks();
});

describe("Голосовое: проигрыватель наш, а не браузерный", () => {
  it("панель браузера не рисуется, а кнопка и полоса — наши", () => {
    протезМедиа();
    const { аудио } = нарисовать();
    // `controls` — это и есть та самая чужая серая панель.
    expect(аудио.hasAttribute("controls"), "вернулась панель браузера").toBe(false);
    expect(screen.getByRole("button", { name: "Слушать" })).toBeTruthy();
    expect(screen.getByRole("slider", { name: "Перемотка записи" })).toBeTruthy();
    // Подпись кнопки называет ДЕЙСТВИЕ, а не состояние: заигравшая запись
    // предлагает паузу, иначе человек нажимает «Слушать» и получает тишину.
    fireEvent.play(аудио);
    expect(screen.getByRole("button", { name: "Пауза" })).toBeTruthy();
  });

  it("время показано «0:07 / 0:23» и моноширинными цифрами", () => {
    протезМедиа();
    const { аудио } = нарисовать();
    fireEvent.timeUpdate(аудио, { target: { currentTime: 7 } });
    expect(screen.getByText("0:07 / 0:23")).toBeTruthy();

    // Ширина цифры — свойство шрифта, в jsdom его нет; проверяем правило.
    const css = readFileSync(VOICE_CSS, "utf-8") as string;
    const правило = css.slice(css.indexOf(".voice-player__time"));
    expect(правило.slice(0, правило.indexOf("}")), "цифры времени поедут по ширине").toContain(
      "font-variant-numeric: var(--lc-num)",
    );
  });

  /*
   * ⚠ ШИРИНА — ЧИСЛОМ, А НЕ `min()` С ПРОЦЕНТОМ. Найдено замером на стенде уже
   * после того, как компонент был написан: с `width: min(280px, 100%)` пузырь
   * выходил 134px, проигрыватель 102px, а полоса перемотки — 58px. Причина в
   * расчёте вклада: процент внутри `min()` неразрешим, всё выражение считается
   * `auto`, и число 280 не участвует. После правки: 294 / 262 / 218.
   *
   * Приём с `min()` в проекте есть и он верен — у снимка (`.msg__image`), где
   * у `<img>` своя внутренняя ширина. Отсюда и соблазн повторить его здесь.
   */
  it("ширина проигрывателя задана числом, а потолок — отдельно", () => {
    const css = readFileSync(VOICE_CSS, "utf-8") as string;
    const правило = css.slice(css.indexOf("\n.voice-player {"));
    const тело = правило.slice(0, правило.indexOf("}"));
    expect(тело, "ширина вернулась к min() с процентом — полоса схлопнется").not.toMatch(
      /width:\s*min\(/,
    );
    expect(тело).toContain("width: 280px");
    expect(тело, "без потолка проигрыватель вылезет за край узкого пузыря").toContain(
      "max-width: 100%",
    );
  });

  it("длина ещё не известна — границы ползунка не выдумываются", () => {
    протезМедиа();
    const { полоса } = нарисовать(null);
    // `aria-valuemax="0"` читалка произнесла бы как «запись нулевой длины».
    expect(полоса.hasAttribute("aria-valuemax")).toBe(false);
    expect(полоса.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText("0:00 / —:—")).toBeTruthy();
  });
});

describe("Голосовое: запись НЕ включается сама", () => {
  /*
   * ⚠ ЭТО ПРАВИЛО ПЕРЕВЕРНУЛОСЬ В ТОТ ЖЕ ДЕНЬ, И ВОТ ПОЧЕМУ.
   *
   * Утром проигрыватель появлялся РОВНО в ответ на нажатие по «Голосовому
   * сообщению» — ссылку Авито отдаёт только отдельным запросом. Раз человек
   * уже сказал «хочу послушать», просить второе нажатие было бы 597 лишних
   * нажатий за 60 дней, и запись начинала играть сама.
   *
   * Вечером владелец попросил открывать запись БЕЗ нажатия («я хочу, чтобы он
   * сразу отображался»). Основание самозапуска исчезло вместе с нажатием:
   * теперь проигрыватель появляется у каждого голосового, попавшего на экран,
   * а голосовые приходят сериями по две-три. Диалог заговорил бы всеми разом —
   * в открытом кабинете это громче любой ошибки вёрстки.
   *
   * Атрибут `autoPlay` запрещён по-прежнему и отдельно: он включает запись при
   * ЛЮБОМ появлении элемента, и почему она заиграла, из кода не видно.
   */
  it("не играет с появления и не несёт атрибута autoplay", () => {
    const { play } = протезМедиа();
    const { аудио } = нарисовать();
    expect(play, "запись начала играть сама").not.toHaveBeenCalled();
    expect(аудио.hasAttribute("autoplay"), "вернулся автозапуск атрибутом").toBe(false);
  });

  it("на экране кнопка «Слушать», а не молчащий плеер без органов управления", async () => {
    протезМедиа();
    нарисовать();
    await act(async () => {});
    expect(screen.getByRole("button", { name: "Слушать" })).toBeTruthy();
    expect(screen.queryByText(/не открылась/i), "пауза показана как поломка").toBeNull();
  });

  it("размонтирование останавливает запись", () => {
    const { pause } = протезМедиа();
    const { аудио, unmount } = нарисовать();
    unmount();
    // Оторванный от документа `<audio>` продолжает играть: человек слышал бы
    // голос без единого плеера на экране и не смог бы его выключить.
    expect(pause.mock.instances, "запись играет после закрытия плеера").toContain(аудио);
  });
});

describe("Голосовое: перемотка", () => {
  it("нажатием по полосе — в ту точку, куда нажали", () => {
    протезМедиа();
    const { аудио, полоса } = нарисовать();
    ширинаПолосы(полоса);
    // PointerEvent в jsdom нет; MouseEvent с типом pointerdown React разбирает
    // так же и несёт clientX, который здесь и проверяется.
    fireEvent(полоса, new MouseEvent("pointerdown", { bubbles: true, clientX: 100 }));
    expect(аудио.currentTime, "нажатие в середину не перемотало в середину").toBe(11.5);
    expect(screen.getByText("0:11 / 0:23")).toBeTruthy();
  });

  it("перетаскиванием — пока держат кнопку, и не после того, как отпустили", () => {
    протезМедиа();
    const { аудио, полоса } = нарисовать();
    ширинаПолосы(полоса);
    fireEvent(полоса, new MouseEvent("pointerdown", { bubbles: true, clientX: 50 }));
    fireEvent(полоса, new MouseEvent("pointermove", { bubbles: true, clientX: 150 }));
    expect(аудио.currentTime, "запись не следует за пальцем").toBe(17.25);
    fireEvent(полоса, new MouseEvent("pointerup", { bubbles: true, clientX: 150 }));
    fireEvent(полоса, new MouseEvent("pointermove", { bubbles: true, clientX: 50 }));
    expect(аудио.currentTime, "полоса тянется за курсором с отпущенной кнопкой").toBe(17.25);
  });

  it("нулевая ширина полосы не перематывает в конец", () => {
    протезМедиа();
    const { аудио, полоса } = нарисовать();
    // Свёрнутая вкладка, `display: none`: делить на ноль нельзя, а `Infinity`
    // отправил бы запись в конец от любого нажатия.
    ширинаПолосы(полоса, 0);
    fireEvent(полоса, new MouseEvent("pointerdown", { bubbles: true, clientX: 100 }));
    expect(аудио.currentTime).toBe(0);
  });

  it("стрелками, Home и End — и лента при этом не уезжает", () => {
    протезМедиа();
    const { аудио, полоса } = нарисовать();
    // `false` из fireEvent означает «событие отменено»: без preventDefault
    // стрелка прокручивает переписку ровно в момент перемотки.
    expect(fireEvent.keyDown(полоса, { key: "ArrowRight" }), "лента уедет под рукой").toBe(false);
    expect(аудио.currentTime).toBe(5);
    fireEvent.keyDown(полоса, { key: "ArrowLeft" });
    expect(аудио.currentTime).toBe(0);
    fireEvent.keyDown(полоса, { key: "End" });
    expect(аудио.currentTime).toBe(23);
    fireEvent.keyDown(полоса, { key: "Home" });
    expect(аудио.currentTime).toBe(0);
  });

  it("ползунок называет читалке время, а не голое число секунд", () => {
    протезМедиа();
    const { аудио, полоса } = нарисовать();
    fireEvent.timeUpdate(аудио, { target: { currentTime: 7 } });
    expect(полоса.getAttribute("aria-valuenow")).toBe("7");
    expect(полоса.getAttribute("aria-valuemax")).toBe("23");
    expect(полоса.getAttribute("aria-valuetext")).toBe("0:07 из 0:23");
  });
});

describe("Голосовое: сбои и соседи", () => {
  /*
   * ⚠ СТОРОЖ «ЗАПИСЬ НЕ ОТКРЫЛАСЬ — ЧЕЛОВЕК ЧИТАЕТ ПРИЧИНУ» ПЕРЕЕХАЛ (06.09) в
   * VoiceUrlFresh0509/0609: сбой <audio> теперь сначала лечится сам — один
   * перезапрос ссылки и повтор play() без участия человека, — и только после
   * второго отказа появляется подпись. Причина тоже сменилась: «ссылка Авито
   * устарела» была бы ложью сразу после её обновления. Прежняя проверка
   * (ошибка без нажатия → подпись про устаревшую ссылку) стерегла поведение,
   * которого больше нет.
   */
  it("говорит только одна запись на всю консоль", () => {
    const { pause } = протезМедиа();
    const { container } = render(
      <>
        <VoicePlayer url="https://avito.example/a.mp3" duration={10} />
        <VoicePlayer url="https://avito.example/b.mp3" duration={10} />
      </>,
    );
    const [первая, вторая] = [...container.querySelectorAll("audio")] as HTMLAudioElement[];
    fireEvent.play(первая);
    pause.mockClear();
    fireEvent.play(вторая);
    // Иначе два голоса звучат разом, и человек не понимает, который слушает.
    expect(pause.mock.instances, "вторая запись не остановила первую").toContain(первая);
  });
});
