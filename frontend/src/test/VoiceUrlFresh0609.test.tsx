import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import { VoicePlayer } from "@/features/chats/components/thread/VoicePlayer";
import type { MessageDto } from "@/shared/api/types";
import { renderWithProviders } from "./render";

/**
 * ССЫЛКА НА ЗАПИСЬ СВЕЖАЯ В МОМЕНТ ИГРЫ, А НЕ В МОМЕНТ ПОЯВЛЕНИЯ ПУЗЫРЯ (06.09).
 *
 * Скриншот владельца: оператор пишет клиенту «аудио не грузит, можете
 * написать пожалуйста». Так выглядел `<audio>`, упавший на мёртвой ссылке:
 * ссылку спрашивали при появлении пузыря, а «Слушать» нажимали через
 * полчаса. Замер 06.09: сама запись у Авито живёт ≥29 дней, протухает только
 * подписанная ссылка files.avito.ru — то есть свежий запрос лечит всё.
 *
 * Три правила, и каждое под своим сторожем:
 *  1. свежая ссылка — играем сразу, без лишнего похода на сервер;
 *  2. ссылка старше порога — сначала свежая, ПОТОМ `play()`;
 *  3. запись не открылась — один перезапрос и повтор без участия человека;
 *     не помогло — честная подпись про файл, а не про «устаревшую ссылку»,
 *     которую только что обновили.
 *
 * ⚠ ВРЕМЯ ПОДМЕНЯЕТСЯ ТОЛЬКО У `Date`. Таймеры остаются настоящими: на них
 * живут и таймаут `http`, и опрос `waitFor`; замороженные — они бы повесили
 * проверку на ожидании, которое никогда не наступит.
 */

const СВЕЖАЯ = "https://files.avito.example/voice.mp3?token=first";
const НОВАЯ = "https://files.avito.example/voice.mp3?token=second";

function голосовое(): MessageDto {
  return {
    id: "m-voice-fresh",
    conversation_id: "conv-fresh",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: null,
    attachments: [
      {
        media_id: "avito_voice_fresh",
        kind: "file",
        name: "Голосовое сообщение",
        size: null,
        avito_type: "voice",
      },
    ],
    delivery_status: "delivered",
    created_at: "2026-09-06T09:00:00Z",
    voice_transcript_status: null,
  };
}

/** Порядок событий: кто раньше — запрос ссылки или `play()`. */
let журнал: string[];
/**
 * Адрес в `<audio src>` В МОМЕНТ `play()`, а не после всех ожиданий.
 *
 * ⚠ Итоговый `src` врёт. Без `flushSync` в `обновитьСсылку` React донёс бы
 * свежий адрес до элемента кадром позже: `play()` ушёл бы по мёртвому, а к
 * проверке `src` уже стоял бы новый. Диверсия 06.09 (снят `flushSync`):
 * сторож по итоговому `src` оставался ЗЕЛЁНЫМ — потому здесь пишется адрес
 * ровно в тот момент, когда элемент начинает играть.
 */
let игралиПо: string[];

/**
 * ⚠ JSDOM НЕ СБРАСЫВАЕТ `window.event` ПОСЛЕ DISPATCH, И ЭТО ПРЯЧЕТ ДЕФЕКТ.
 *
 * React по `window.event` решает, срочное ли обновление. В jsdom после
 * `fireEvent.click` там навсегда остаётся «click», и любой `setState` из
 * обещания рисуется микрозадачей — раньше, чем `play()` доберётся до
 * элемента. В браузере снаружи обработчика `window.event` — `undefined`,
 * обновление уходит в планировщик кадром позже, и без `flushSync` в
 * `обновитьСсылку` `play()` уходит по мёртвому адресу.
 *
 * Диверсия 06.09 (снят `flushSync`): без этой заглушки `игралиПо` оставался
 * ЗЕЛЁНЫМ даже у плеера без родителя; с ней — красный. Гасим на время
 * ожидания и возвращаем как было.
 */
async function какВБраузереВнеОбработчика<T>(тело: () => Promise<T>): Promise<T> {
  const было = Object.getOwnPropertyDescriptor(window, "event");
  Object.defineProperty(window, "event", { configurable: true, get: () => undefined });
  try {
    return await тело();
  } finally {
    if (было) Object.defineProperty(window, "event", было);
    else Reflect.deleteProperty(window, "event");
  }
}

function протезМедиа() {
  const play = vi
    .spyOn(HTMLMediaElement.prototype, "play")
    .mockImplementation(function (this: HTMLMediaElement) {
      журнал.push("play");
      игралиПо.push(this.getAttribute("src") ?? "");
      return Promise.resolve();
    });
  const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  const load = vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  return { play, pause, load };
}

/** Сервер отдаёт ссылки по очереди: первая при появлении, дальше — свежие. */
function сервер(ссылки: string[]) {
  let номер = 0;
  const fetch = vi.fn(async () => {
    журнал.push("ссылка");
    const url = ссылки[Math.min(номер, ссылки.length - 1)];
    номер += 1;
    return new Response(JSON.stringify({ url }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function нарисоватьПузырь() {
  const итог = renderWithProviders(
    <MessageBubble msg={голосовое()} prev={null} clientId="cl-1" clientName="Иван" />,
  );
  const аудио = await waitFor(() => {
    const el = итог.container.querySelector("audio");
    expect(el).not.toBeNull();
    return el as HTMLAudioElement;
  });
  return { ...итог, аудио };
}

beforeEach(() => {
  журнал = [];
  игралиПо = [];
  queryClient.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-09-06T12:00:00Z"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("свежая ссылка при нажатии «Слушать»", () => {
  it("ссылка моложе порога — играем сразу, сервер не дёргаем", async () => {
    const { play } = протезМедиа();
    const fetch = сервер([СВЕЖАЯ]);
    const { аудио } = await нарисоватьПузырь();
    expect(fetch).toHaveBeenCalledTimes(1);

    vi.setSystemTime(Date.now() + 10_000);
    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));

    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    // Лишний запрос на каждое нажатие — это и есть «дёргать Авито за все
    // голосовые разом», от чего уходили; порог существует ради этого.
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(аудио.getAttribute("src")).toBe(СВЕЖАЯ);
  });

  it("ссылка старше порога — сначала свежая с сервера, потом play()", async () => {
    /*
     * ⚠ ДИВЕРСИЯ: убрать проверку свежести в `включить` (условие
     * `протухла || упал(el)`) — `play()` уходит по старой ссылке, второй
     * запрос не делается: краснеют счётчик и порядок. Проверено 06.09: красный.
     */
    const { play } = протезМедиа();
    const fetch = сервер([СВЕЖАЯ, НОВАЯ]);
    const { аудио } = await нарисоватьПузырь();

    // Полчаса спустя — как на скриншоте владельца.
    vi.setSystemTime(Date.now() + 30 * 60_000);
    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));

    await какВБраузереВнеОбработчика(() => waitFor(() => expect(play).toHaveBeenCalledTimes(1)));
    expect(fetch).toHaveBeenCalledTimes(2);
    // Порядок — суть правки: свежая ссылка ДО игры, а не рядом с ней.
    expect(журнал).toEqual(["ссылка", "ссылка", "play"]);
    // И играет `<audio>` уже по ней: перезапрос, которым не воспользовались,
    // ничего бы не починил.
    expect(аудио.getAttribute("src")).toBe(НОВАЯ);
    /*
     * ⚠ ДИВЕРСИЯ: снять `flushSync` в `обновитьСсылку` — строка выше остаётся
     * зелёной (React догоняет кадром позже), а `play()` уходит по СВЕЖАЯ.
     * Краснеет только эта проверка, и только под `какВБраузереВнеОбработчика`.
     * Проверено 06.09: красный.
     */
    expect(игралиПо).toEqual([НОВАЯ]);
    expect(screen.queryByText(/не открылась/i)).toBeNull();
  });

  it("свежая ссылка ложится в общий кэш: следующее нажатие сервер не дёргает", async () => {
    const { play } = протезМедиа();
    const fetch = сервер([СВЕЖАЯ, НОВАЯ]);
    await нарисоватьПузырь();

    vi.setSystemTime(Date.now() + 30 * 60_000);
    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledTimes(2);

    // Второе нажатие через десять секунд: ссылке десять секунд, она свежая.
    // (`play()` протезирован и события `play` не даёт, так что кнопка так и
    // осталась «Слушать» — нажимаем её же.)
    vi.setSystemTime(Date.now() + 10_000);
    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(2));
    expect(fetch).toHaveBeenCalledTimes(2);

    /*
     * Тот же пузырь после прокрутки туда-сюда: лента виртуализована, пузырь
     * уходит с экрана и возвращается, и вернуться он обязан со свежей ссылкой
     * ИЗ КЭША — не спросив сервер и не откатившись на мёртвую.
     *
     * ⚠ ДИВЕРСИЯ: пустить `refreshUrl` в `MessageBubble` мимо `fetchQuery`
     * (голый `voiceUrl(msg.id)`) — два нажатия выше остаются зелёными: свежесть
     * держится на собственном состоянии плеера, кэш ни при чём. Краснеет
     * только возврат пузыря: кэш помнит мёртвую и просит сервер третий раз.
     * Проверено 06.09: красный.
     */
    cleanup();
    const { аудио: снова } = await нарисоватьПузырь();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(снова.getAttribute("src")).toBe(НОВАЯ);
  });
});

describe("запись не открылась", () => {
  it("один автоматический перезапрос ссылки и повтор play() — без человека", async () => {
    /*
     * ⚠ ДИВЕРСИЯ: в `неОткрылась` заменить перезапрос с повтором на
     * немедленную подпись — второго запроса и второго `play()` нет, краснеют
     * оба счётчика. Проверено 06.09: красный.
     */
    const { play } = протезМедиа();
    const fetch = сервер([СВЕЖАЯ, НОВАЯ]);
    const { аудио } = await нарисоватьПузырь();

    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledTimes(1);

    // Свежая по времени ссылка оказалась мёртвой: Авито ответил 403 на файл.
    fireEvent.error(аудио);

    await какВБраузереВнеОбработчика(() => waitFor(() => expect(play).toHaveBeenCalledTimes(2)));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(журнал).toEqual(["ссылка", "play", "ссылка", "play"]);
    expect(аудио.getAttribute("src")).toBe(НОВАЯ);
    // Повтор — по НОВОЙ ссылке в момент `play()`, а не по той же мёртвой.
    expect(игралиПо).toEqual([СВЕЖАЯ, НОВАЯ]);
    // Повтор ещё может выиграть — подписи о поломке рано.
    expect(screen.queryByText(/не открылась/i)).toBeNull();
  });

  it("второй отказ подряд — подпись про файл, а не про «устаревшую ссылку»", async () => {
    const { play } = протезМедиа();
    const fetch = сервер([СВЕЖАЯ, НОВАЯ]);
    const { аудио } = await нарисоватьПузырь();

    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    fireEvent.error(аудио);
    await waitFor(() => expect(play).toHaveBeenCalledTimes(2));

    fireEvent.error(аудио);

    const подпись = await screen.findByText(/не открылась/i);
    // Ссылку только что обновили: винить её — врать. Файл не отдал Авито.
    expect(подпись.textContent).toMatch(/Авито не отдал файл/i);
    expect(подпись.textContent).not.toMatch(/устарела/i);
    expect(подпись.textContent).toMatch(/приложении Авито/i);
    // Повтор ровно один: третьего похода на сервер нет.
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(play).toHaveBeenCalledTimes(2);
  });

  it("сдвоенный сигнал об одном сбое (error + отказ play) даёт ОДИН повтор", async () => {
    /*
     * Об одном и том же сбое `<audio>` сообщает дважды: событием `error` и
     * отказом обещания `play()`. Второй сигнал не должен ни начинать второй
     * перезапрос, ни выдавать подпись, пока первый повтор ещё идёт.
     */
    const { play } = протезМедиа();
    play.mockImplementationOnce(() => {
      журнал.push("play");
      return Promise.reject(new DOMException("no source", "NotSupportedError"));
    });
    const fetch = сервер([СВЕЖАЯ, НОВАЯ]);
    const { аудио } = await нарисоватьПузырь();

    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    fireEvent.error(аудио);

    await waitFor(() => expect(play).toHaveBeenCalledTimes(2));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/не открылась/i)).toBeNull();
  });

  it("сбой при появлении пузыря (метаданные) без нажатия ничего не запускает", async () => {
    /*
     * Лечение сбоя — это игра, а играть без нажатия нельзя (05.09: серия из
     * трёх голосовых заговорила бы разом). Нажмут — перезапросим тогда.
     */
    протезМедиа();
    const fetch = сервер([СВЕЖАЯ, НОВАЯ]);
    const { аудио } = await нарисоватьПузырь();

    fireEvent.error(аудио);
    await new Promise((r) => setTimeout(r, 20));

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(журнал).toEqual(["ссылка"]);
    expect(screen.queryByText(/не открылась/i)).toBeNull();
  });
});

describe("проигрыватель сам по себе", () => {
  it("свежий адрес стоит в <audio> в момент play(), а не кадром позже", async () => {
    /*
     * Без пузыря и без TanStack между нажатием и игрой: единственное, что
     * может донести адрес до элемента раньше `play()`, — `flushSync` в
     * `обновитьСсылку`. В пузыре его подстраховал бы перерисовавшийся
     * родитель, и сторож выше не отличил бы страховку от самого правила.
     *
     * ⚠ ДИВЕРСИЯ: снять `flushSync` — `игралиПо` = [СВЕЖАЯ]. Проверено
     * 06.09: красный (только под `какВБраузереВнеОбработчика`, см. её).
     */
    const { play } = протезМедиа();
    const refreshUrl = vi.fn(async () => НОВАЯ);
    render(
      <VoicePlayer
        url={СВЕЖАЯ}
        urlUpdatedAt={Date.now() - 30 * 60_000}
        refreshUrl={refreshUrl}
        duration={14}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    await какВБраузереВнеОбработчика(() => waitFor(() => expect(play).toHaveBeenCalledTimes(1)));

    expect(refreshUrl).toHaveBeenCalledTimes(1);
    expect(игралиПо).toEqual([НОВАЯ]);
  });

  it("отказ политики автозапуска после ожидания ссылки — пауза, а не подпись о поломке", async () => {
    /*
     * Между нажатием и `play()` теперь бывает запрос к серверу; WebKit может
     * счесть жест истёкшим и ответить NotAllowedError. Это не «Авито не отдал
     * файл»: ссылка свежая, второе нажатие играет без ожидания.
     */
    const { play } = протезМедиа();
    play.mockImplementationOnce(() => Promise.reject(new DOMException("gesture", "NotAllowedError")));
    const refreshUrl = vi.fn(async () => НОВАЯ);
    render(
      <VoicePlayer
        url={СВЕЖАЯ}
        urlUpdatedAt={Date.now() - 30 * 60_000}
        refreshUrl={refreshUrl}
        duration={14}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    expect(refreshUrl).toHaveBeenCalledTimes(1);

    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText(/не открылась/i)).toBeNull();
    expect(screen.getByRole("button", { name: "Слушать" })).toBeTruthy();
  });

  it("пауза до того, как запись догрузилась (AbortError), — не сбой: ни перезапроса, ни повтора", async () => {
    /*
     * `pause()` под ещё не начавшимся `play()` отклоняет его обещание с
     * AbortError — это человек передумал, а не Авито не отдал файл. Считай
     * плеер это сбоем — он перезапросил бы ссылку и ВОЗОБНОВИЛ игру, которую
     * только что остановили нажатием.
     *
     * ⚠ ДИВЕРСИЯ: убрать "AbortError" из `НЕ_СБОЙ` — краснеет: `refreshUrl`
     * вызван, `play()` второй раз. Проверено 06.09: красный.
     */
    const { play } = протезМедиа();
    play.mockImplementationOnce(() => {
      журнал.push("play");
      return Promise.reject(new DOMException("interrupted by a call to pause()", "AbortError"));
    });
    const refreshUrl = vi.fn(async () => НОВАЯ);
    render(
      <VoicePlayer url={СВЕЖАЯ} urlUpdatedAt={Date.now()} refreshUrl={refreshUrl} duration={14} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 20));

    expect(refreshUrl).not.toHaveBeenCalled();
    expect(play).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/не открылась/i)).toBeNull();
  });

  it("без способа обновить ссылку сбой сразу даёт подпись — обновлять нечем", async () => {
    протезМедиа();
    const { container } = render(<VoicePlayer url={СВЕЖАЯ} duration={14} />);
    fireEvent.click(screen.getByRole("button", { name: "Слушать" }));
    fireEvent.error(container.querySelector("audio") as HTMLAudioElement);
    expect(await screen.findByText(/Авито не отдал файл/i)).toBeTruthy();
  });

  it("пока ждём свежую ссылку, кнопка занята и второе нажатие не начинает вторую игру", async () => {
    const { play } = протезМедиа();
    let отдать: (url: string) => void = () => {};
    const refreshUrl = vi.fn(() => new Promise<string>((resolve) => (отдать = resolve)));
    render(
      <VoicePlayer
        url={СВЕЖАЯ}
        urlUpdatedAt={Date.now() - 30 * 60_000}
        refreshUrl={refreshUrl}
        duration={14}
      />,
    );

    const кнопка = screen.getByRole("button", { name: "Слушать" });
    fireEvent.click(кнопка);
    await waitFor(() => expect(кнопка.getAttribute("aria-busy")).toBe("true"));
    fireEvent.click(кнопка);
    expect(refreshUrl).toHaveBeenCalledTimes(1);

    отдать(НОВАЯ);
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    expect(кнопка.hasAttribute("aria-busy")).toBe(false);
  });
});
