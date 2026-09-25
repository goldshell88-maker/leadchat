import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { useQuery, type InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import { VoicePlayer } from "@/features/chats/components/thread/VoicePlayer";
import { qk } from "@/shared/api/queryKeys";
import type { MessageDto, MessagesPage } from "@/shared/api/types";
import { applyWsEvent } from "@/shared/realtime/applyWsEvent";
import { renderWithProviders, wrap } from "./render";

/**
 * РАСШИФРОВКА ГОЛОСОВОГО ПРИЕЗЖАЕТ В ПУЗЫРЬ САМА (06.09).
 *
 * Скриншот владельца: закрытый диалог, два голосовых по 0:14 и 0:18, ни
 * строки текста под ними — «не понимаю, где находится расшифровка». Текст
 * ехал только вместе с сообщением, кадра у него не было, а подпись под
 * записью звала открыть диалог заново.
 *
 * ⚠ ДОВОД «У СТАРЫХ ЗАПИСЕЙ ССЫЛКИ ПРОТУХЛИ, РАСШИФРОВАТЬ НЕЧЕМ» ОКАЗАЛСЯ
 * НЕВЕРНЫМ. Замер 06.09 через боевой клиент: Авито отдаёт ссылку на запись
 * для сообщений возрастом от 0,3 ч до 29 дней — все «есть». Протухает только
 * подписанная ссылка, а её сервер берёт заново. Значит 458 голосовых без
 * расшифровки за 30 дней — очередь, а не приговор, и пустота под ними была
 * враньём умолчанием.
 *
 * Здесь два сторожа: кадр `message:transcript` кладёт текст в кэш ленты тем же
 * механизмом, что и статус доставки, и подписи под записью обещают ровно то,
 * что этот кадр делает.
 */

const CONV = "conv-voice-live";
const MSG = "m-voice-live";
const РЕЧЬ = "Здравствуйте, сломался телевизор Samsung, адрес Рябиновая 17.";

function голосовое(over: Partial<MessageDto> = {}): MessageDto {
  return {
    id: MSG,
    conversation_id: CONV,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: null,
    attachments: [
      {
        media_id: "avito_voice_live",
        kind: "file",
        name: "Голосовое сообщение",
        size: null,
        avito_type: "voice",
      },
    ],
    delivery_status: "delivered",
    created_at: "2026-09-06T09:00:00Z",
    voice_transcript: null,
    voice_transcript_status: "running",
    ...over,
  };
}

function засеятьЛенту(items: MessageDto[]): void {
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(CONV), {
    pages: [
      {
        items,
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      },
    ],
    pageParams: [null],
  });
}

function изКэша(): MessageDto | undefined {
  const data = queryClient.getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(CONV));
  return data?.pages.flatMap((p) => p.items).find((m) => m.id === MSG);
}

function кадр(over: Partial<{ voice_transcript: string | null; voice_transcript_status: "done" | "failed" }> = {}) {
  applyWsEvent({
    type: "message:transcript",
    ts: "2026-09-06T09:00:07Z",
    data: {
      conversation_id: CONV,
      message_id: MSG,
      voice_transcript: РЕЧЬ,
      voice_transcript_status: "done",
      ...over,
    },
  });
}

/**
 * Лента так, как её видит экран: пузыри читаются ИЗ КЭША, а не из пропов
 * теста. Иначе проверка показала бы, что пузырь умеет рисовать текст, а не
 * что кадр доносит его до пузыря без единого перечитывания.
 */
function ThreadFromCache() {
  const лента = useQuery<InfiniteData<MessagesPage>>({
    queryKey: qk.messages.list(CONV),
    queryFn: () => Promise.reject(new Error("лента засеяна, спрашивать сервер нечего")),
    enabled: false,
  });
  const сообщения = лента.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      {сообщения.map((m) => (
        <MessageBubble key={m.id} msg={m} prev={null} clientId="cl-1" clientName="Иван" />
      ))}
    </>
  );
}

function протезМедиа() {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
}

function ответСсылкой() {
  return new Response(JSON.stringify({ url: "https://files.avito.example/voice.mp3?token=a" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  queryClient.clear();
  протезМедиа();
  vi.stubGlobal("fetch", vi.fn(async () => ответСсылкой()));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("кадр message:transcript", () => {
  it("кладёт текст и состояние в сообщение кэша ленты — точечно, без перечитывания", () => {
    /*
     * ⚠ ДИВЕРСИЯ: убрать `case "message:transcript"` из `applyWsEvent` — кадр
     * уходит в `default`, сообщение в кэше остаётся `running` без текста,
     * краснеют обе проверки ниже. Проверено 06.09: красный.
     */
    const сосед = голосовое({ id: "m-other", voice_transcript_status: null });
    засеятьЛенту([сосед, голосовое()]);

    кадр();

    const после = изКэша();
    expect(после?.voice_transcript_status).toBe("done");
    expect(после?.voice_transcript).toBe(РЕЧЬ);
    // Соседний пузырь кадр не трогает: патч адресный.
    const другой = queryClient
      .getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(CONV))
      ?.pages.flatMap((p) => p.items)
      .find((m) => m.id === "m-other");
    expect(другой?.voice_transcript_status).toBeNull();
  });

  it("неудача едет тем же кадром: состояние failed, текста нет", () => {
    засеятьЛенту([голосовое()]);
    кадр({ voice_transcript: null, voice_transcript_status: "failed" });
    expect(изКэша()?.voice_transcript_status).toBe("failed");
    expect(изКэша()?.voice_transcript).toBeNull();
  });

  it("на закрытую ленту (кэша нет) кадр не падает и кэш не создаёт", () => {
    expect(() => кадр()).not.toThrow();
    expect(queryClient.getQueryData(qk.messages.list(CONV))).toBeUndefined();
  });

  it("текст появляется под записью на экране — без открытия диалога заново", async () => {
    /*
     * Это и есть обещание подписи «текст появится здесь сам». Пузырь читает
     * сообщение из кэша, кадр правит кэш — и подпись «готовится» сменяется
     * словами клиента с пометкой «машинная».
     */
    засеятьЛенту([голосовое()]);
    render(wrap(<ThreadFromCache />));

    await screen.findByRole("button", { name: "Слушать" });
    expect(screen.getByText(/готовится/i)).toBeTruthy();
    expect(screen.queryByText(РЕЧЬ)).toBeNull();

    act(() => кадр());

    expect(await screen.findByText(РЕЧЬ)).toBeTruthy();
    expect(screen.getByText(/машинная расшифровка/i)).toBeTruthy();
    expect(screen.queryByText(/готовится/i)).toBeNull();
  });
});

describe("подписи под записью обещают то, что делает кадр", () => {
  function нарисовать(props: Partial<Parameters<typeof VoicePlayer>[0]> = {}) {
    return render(<VoicePlayer url="https://files.avito.example/voice.mp3" duration={14} {...props} />);
  }

  it("running: «появится здесь сам», а не «при следующем открытии диалога»", () => {
    /*
     * ⚠ ДИВЕРСИЯ: вернуть прежнюю подпись про открытие диалога — краснеет
     * вторая проверка. Прежняя подпись звала человека делать то, что теперь
     * делает кадр, и молчала бы о тексте, который приехал бы у него на глазах.
     */
    нарисовать({ transcriptStatus: "running" });
    const строка = screen.getByRole("status").textContent ?? "";
    expect(строка).toMatch(/готовится/i);
    expect(строка).toMatch(/появится здесь сам/i);
    expect(строка).not.toMatch(/открыти/i);
  });

  it("не начинали (NULL): приглушённая «в очереди — появится сама», а не пустота", () => {
    /*
     * ⚠ ДИВЕРСИЯ: вернуть `if (!состояние) return null` — краснеет: под записью
     * снова пусто, ровно то, что владелец не смог прочитать на скриншоте.
     */
    const { container } = нарисовать({ transcript: null, transcriptStatus: null });
    const строка = screen.getByRole("status");
    expect(строка.textContent).toMatch(/в очереди/i);
    expect(строка.textContent).toMatch(/появится сама/i);
    expect(строка.className).toContain("voice-player__transcript-note");
    // Служебная строка, а не текст: пометки «машинная» над ней быть не должно.
    expect(container.querySelector(".voice-player__transcript")).toBeNull();
  });

  it("под исходящим голосовым очереди не обещаем: расшифровывается только речь клиента", () => {
    const { container } = нарисовать({ transcriptStatus: null, transcriptQueued: false });
    expect(container.querySelector(".voice-player__transcript-note")).toBeNull();
    expect(container.textContent).not.toMatch(/расшифров/i);
  });

  it("пузырь исходящего передаёт проигрывателю «очереди нет»", async () => {
    // Через `MessageBubble`, а не через проп напрямую: важно, что направление
    // сообщения доезжает до подписи, а не что у подписи есть выключатель.
    renderWithProviders(
      <MessageBubble
        msg={голосовое({ direction: "out", sender_type: "operator", voice_transcript_status: null })}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
      />,
    );
    await screen.findByRole("button", { name: "Слушать" });
    expect(screen.queryByText(/в очереди/i)).toBeNull();
  });

  it("done: текст с пометкой «машинная», без служебных строк", () => {
    нарисовать({ transcript: РЕЧЬ, transcriptStatus: "done" });
    expect(screen.getByText(РЕЧЬ)).toBeTruthy();
    expect(screen.getByText(/может ошибаться/i)).toBeTruthy();
    expect(screen.queryByText(/в очереди|готовится/i)).toBeNull();
  });
});
