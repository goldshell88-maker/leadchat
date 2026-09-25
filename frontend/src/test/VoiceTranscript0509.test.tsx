// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в VoicePlayer0509.test.tsx: сторож только читает файл стилей.
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { VoicePlayer } from "@/features/chats/components/thread/VoicePlayer";

/**
 * МАШИННАЯ РАСШИФРОВКА ГОЛОСОВОГО ПОД ПРОИГРЫВАТЕЛЕМ.
 *
 * Владелец выбирал между облаком и своим сервером и выбрал свой: «давай свой
 * Whisper». Замер боя: 597 голосовых за 60 дней — в голосовом клиент обычно и
 * рассказывает суть заказа, и прочитать его глазами сегодня нельзя никак.
 *
 * ⚠ ЧТО ИМЕННО ЗДЕСЬ ОХРАНЯЕТСЯ. Не «текст показан» — а то, что его нельзя
 * принять за стенограмму. Распознавание врёт: в замере на живой русской речи
 * «стоить ремонт и когда» слиплось в «стоить ремонта когда», и это ещё
 * безобидный случай — врёт оно и на адресах, которые диспетчер диктует мастеру
 * вслух. Поэтому запись остаётся на месте, а текст подписан машинным.
 */

const VOICE_CSS = "src/features/chats/components/thread/voice-player.css";

const РЕЧЬ =
  "Здравствуйте, меня зовут Сергей. Сломался телевизор Samsung, " +
  "адрес Санкт-Петербург, улица Рябиновая, дом 17.";

function протезМедиа() {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
}

function нарисовать(props: Partial<Parameters<typeof VoicePlayer>[0]> = {}) {
  протезМедиа();
  return render(
    <VoicePlayer url="https://avito.example/voice.mp3" duration={19} {...props} />,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Расшифровка голосового: подсказка, а не замена записи", () => {
  it("готовый текст виден и подписан машинным", () => {
    /*
     * ⚠ ДИВЕРСИЯ: убрать строку с подписью из `Расшифровка` — тест краснеет на
     * `/машинная/i`. Без подписи текст читается как слова клиента, и ошибка
     * распознавания в адресе уходит дальше уже как факт.
     */
    нарисовать({ transcript: РЕЧЬ, transcriptStatus: "done" });

    expect(screen.getByText(РЕЧЬ)).toBeTruthy();
    expect(screen.getByText(/машинная расшифровка/i)).toBeTruthy();
    expect(screen.getByText(/может ошибаться/i)).toBeTruthy();
  });

  it("запись остаётся на месте: текст добавлен ПОД проигрывателем, а не вместо", () => {
    /*
     * ⚠ САМОЕ ВАЖНОЕ ТРЕБОВАНИЕ ЗАДАЧИ. Заменить запись текстом — значит
     * лишить диспетчера возможности переслушать спорное место, а спорных мест
     * в машинном тексте хватает.
     *
     * ⚠ ДИВЕРСИЯ: отрисовать расшифровку вместо `<audio>`/кнопки — краснеет
     * первая же проверка.
     */
    const { container } = нарисовать({ transcript: РЕЧЬ, transcriptStatus: "done" });

    expect(container.querySelector("audio")).toBeTruthy();
    expect(screen.getByRole("slider")).toBeTruthy();
    expect(screen.getByRole("button", { name: /слушать|пауза/i })).toBeTruthy();

    // Порядок в документе: сначала полоса перемотки, потом текст.
    const полоса = screen.getByRole("slider");
    const текст = screen.getByText(РЕЧЬ);
    expect(полоса.compareDocumentPosition(текст) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("у не начатых входящих под проигрывателем — «в очереди», у исходящих пусто", () => {
    /*
     * ⚠ ПЕРЕПИСАНО 06.09, И ПРИЧИНА — ЗАМЕР, А НЕ ВКУС. Здесь стояло «у старых
     * записей пусто: расшифровать их нечем, ссылки уже не действуют». Проба
     * на живом Авито (getVoiceFiles на возрастах от 0,3 ч до 29 дней) выдала
     * ссылку на ВСЕ — протухает подписанная ссылка, а не запись. Значит «не
     * начинали» — временное состояние: досчёт на сервере берёт такие записи
     * сам, и человеку об этом честно сказано. Для исходящих очереди нет
     * (расшифровываем только клиента) — там по-прежнему пусто.
     *
     * ⚠ ДИВЕРСИЯ: убрать `В_ОЧЕРЕДИ` из выбора подписи — краснеет первая
     * половина; сделать подпись безусловной — краснеет вторая.
     */
    нарисовать({ transcript: null, transcriptStatus: null });
    expect(screen.getByRole("status").textContent).toMatch(/в очереди/i);
    cleanup();

    const { container } = нарисовать({
      transcript: null,
      transcriptStatus: null,
      transcriptQueued: false,
    });
    expect(container.querySelector(".voice-player__transcript")).toBeNull();
    expect(container.textContent).not.toMatch(/расшифров/i);
  });

  it("«готовится» обещает, что текст появится сам — и не зовёт переоткрывать диалог", () => {
    /*
     * Раньше подпись честно говорила «появится при следующем открытии
     * диалога»: кадра WS у расшифровки не было. С 06.09 воркер публикует
     * `message:transcript` после commit, и лента патчится на месте
     * (VoiceTranscriptLive0609). Старая подпись стала бы ложью наоборот —
     * звала бы человека делать лишнее действие.
     *
     * ⚠ ДИВЕРСИЯ: вернуть в подпись «при следующем открытии диалога» —
     * краснеет.
     */
    нарисовать({ transcriptStatus: "running" });

    const строка = screen.getByRole("status");
    expect(строка.textContent).toMatch(/готовится/i);
    expect(строка.textContent).toMatch(/появится здесь сам/i);
    expect(строка.textContent).not.toMatch(/открытии диалога/i);
    // Предупреждать «может ошибаться» не о чем: текста ещё нет.
    expect(screen.queryByText(/может ошибаться/i)).toBeNull();
  });

  it("неудача и слишком длинная запись — разные строки, и вторая не зовёт повторять", () => {
    /*
     * ⚠ ПОЧЕМУ ДВА РАЗНЫХ СОСТОЯНИЯ, А НЕ ОДНО. «Не удалось» человек читает
     * как «попробуйте ещё раз». Для записи, которая длиннее порога, это ложный
     * совет: повтор упрётся в тот же порог и потратит те же минуты процессора.
     *
     * ⚠ ДИВЕРСИЯ: свести `too_long` к `failed` — краснеет проверка длины.
     */
    const { unmount } = нарисовать({ transcriptStatus: "failed" });
    expect(screen.getByRole("status").textContent).toMatch(/не удалось/i);
    unmount();

    нарисовать({ transcriptStatus: "too_long" });
    const длинная = screen.getByRole("status").textContent ?? "";
    expect(длинная).toMatch(/длинная/i);
    expect(длинная).not.toMatch(/не удалось/i);
    /*
     * ⚠ ПОРОГ НЕ НАЗВАН ЧИСЛОМ НАМЕРЕННО: он живёт настройкой
     * WHISPER_MAX_AUDIO_SECONDS, и «длиннее пяти минут» стало бы враньём на
     * следующий день после её правки. Сторож держит это правило: подпись без
     * числа переживает изменение настройки, подпись с числом — нет.
     */
    expect(длинная).not.toMatch(/\d/);
  });

  it("готовая, но пустая расшифровка не выдаётся за слова клиента", () => {
    /*
     * Отсев тишины (VAD) на записи из одного шороха оставляет пустоту. Пустой
     * пузырь под подписью «расшифровка» человек читает как «клиент промолчал»,
     * хотя клиент говорил — просто разобрать не вышло.
     */
    нарисовать({ transcript: "   ", transcriptStatus: "done" });

    const строка = screen.getByText(/ни слова/i);
    expect(строка.getAttribute("data-empty")).toBe("true");
  });

  it("расшифровка красится currentColor: пузырей четыре, цвет один", () => {
    /*
     * ⚠ ПРОВЕРКА ПО ТЕКСТУ CSS — ПО ТОЙ ЖЕ ПРИЧИНЕ, ЧТО И В СОСЕДНЕМ ФАЙЛЕ: в
     * vitest стоит `css: false`, в jsdom стилей нет и померить цвет нечем.
     *
     * Почему это важно именно здесь: плеер живёт внутри четырёх разных пузырей
     * (входящий, исходящий с заливкой акцентом, заметка, бот). Возьми
     * расшифровка `--lc-text-3` — на исходящем она слилась бы с фоном совсем.
     */
    const css = readFileSync(VOICE_CSS, "utf-8") as string;
    const блок = css.slice(css.indexOf(".voice-player__transcript"));
    expect(блок).toMatch(/currentColor/);
    expect(блок).not.toMatch(/--lc-(text|accent|danger)/);
    // Обе строки занимают всю ширину сетки: иначе первая строка текста была бы
    // уже остальных на ширину кнопки «Слушать».
    expect(блок).toMatch(/grid-column:\s*1\s*\/\s*-1/);
  });
});
