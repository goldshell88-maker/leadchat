import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { MessageDto } from "@/shared/api/types";
import { queryClient } from "@/app/queryClient";
import { renderWithProviders } from "./render";

/**
 * Голосовое сообщение можно послушать (жалоба владельца 19.08: «не грузятся
 * голосовые сообщения»).
 *
 * ЧТО БЫЛО. Авито присылает голосовое ОДНИМ ИДЕНТИФИКАТОРОМ — самой записи в
 * сообщении нет, за ней надо идти отдельным запросом, которого мы не делали.
 * В ленте была строка «Голосовое сообщение» без ссылки: клиент говорит, а мы
 * не слышим. На бою таких сообщений 174, и в голосовом обычно и есть суть
 * заказа.
 */

function голосовое(): MessageDto {
  return {
    id: "m-voice",
    conversation_id: "c-1",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: null,
    attachments: [
      {
        media_id: "avito_voice_2229d5a7",
        kind: "file",
        name: "Голосовое сообщение",
        size: null,
        avito_type: "voice",
      },
    ],
    delivery_status: "delivered",
    created_at: "2026-08-19T09:00:00Z",
  };
}

describe("Голосовое сообщение в ленте", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    /*
     * ⚠ КЭШ ЗАПРОСОВ ЧИСТИМ МЕЖДУ ПРОВЕРКАМИ. С 05.09 ссылка на запись
     * спрашивается сама и кладётся в общий кэш по идентификатору сообщения —
     * а идентификатор у обеих проверок один. Без очистки вторая (про отказ
     * Авито) брала УСПЕШНЫЙ ответ первой и рисовала проигрыватель вместо
     * причины: тест падал, обвиняя невиновный код.
     */
    queryClient.clear();
  });

  it("спрашивает запись сама и включает проигрыватель", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ url: "https://avito.example/voice.mp3" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const { container } = renderWithProviders(
      <MessageBubble msg={голосовое()} prev={null} clientId="cl-1" clientName="Иван" />,
    );

    /*
     * ⚠ БЫЛО «ПОКА НЕ НАЖАЛИ — ПРОИГРЫВАТЕЛЯ НЕТ», СТАЛО НАОБОРОТ (05.09,
     * просьба владельца: «я хочу, чтобы он сразу отображался»).
     *
     * Прежний довод — «идти в Авито за каждой записью при открытии диалога
     * значит дёргать площадку за все голосовые разом» — верен только для
     * НЕВИРТУАЛИЗОВАННОЙ ленты. Лента виртуализована: на экране живут лишь
     * видимые сообщения, и «сразу» здесь значит «когда сообщение попало на
     * экран». Замер боя: 597 голосовых за 60 дней на все диалоги — около
     * десяти в сутки на всю смену.
     */
    const player = await vi.waitFor(() => {
      const el = container.querySelector("audio");
      expect(el).not.toBeNull();
      return el as HTMLAudioElement;
    });
    expect(player.getAttribute("src")).toBe("https://avito.example/voice.mp3");
  });

  it("Авито не отдал запись — человек видит причину, а не молчащий плеер", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({ error: { code: "not_found", message: "Авито не отдал запись" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const { container } = renderWithProviders(
      <MessageBubble msg={голосовое()} prev={null} clientId="cl-1" clientName="Иван" />,
    );
    // Причина показывается сама — идти за ней нажатием больше не нужно; рядом
    // остаётся кнопка «попробовать ещё раз» на случай, если Авито ответит.
    expect(await screen.findByText(/не отдал запись/i)).toBeTruthy();
    expect(container.querySelector("audio")).toBeNull();
  });
});
