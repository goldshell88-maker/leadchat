/**
 * Голосовое открывается САМО, без нажатия на «Голосовое сообщение».
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 05.09: «всё так же нужно нажимать на „Голосовое
 * сообщение", я хочу чтобы он сразу отображался красиво, как мы сделали».
 *
 * ПОЧЕМУ РАНЬШЕ БЫЛО ПО НАЖАТИЮ. Ссылку на запись Авито отдаёт только отдельным
 * запросом и ненадолго; рисовать проигрыватель сразу означало дёргать Авито за
 * все голосовые переписки разом.
 *
 * ПОЧЕМУ ТЕПЕРЬ МОЖНО. Лента виртуализована — на экране живут только видимые
 * сообщения, поэтому «сразу» значит «когда сообщение попало на экран». Замер:
 * 597 голосовых за 60 дней на все диалоги, около десяти в сутки на смену.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";
import type { MessageDto } from "@/shared/api/types";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

function голосовое(): MessageDto {
  return {
    id: "m-voice-1",
    conversation_id: "conv-1",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "",
    created_at: "2026-09-05T13:16:00Z",
    delivery_status: "delivered",
    attachments: [
      {
        media_id: "att-1",
        kind: "file",
        avito_type: "voice",
        name: "Голосовое сообщение",
        url: null,
        size: null,
      },
    ],
  } as MessageDto;
}

describe("Голосовое сообщение в ленте", () => {
  let спрошено = 0;

  beforeEach(() => {
    спрошено = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/voice-url") || url.includes("/voice")) {
          спрошено += 1;
          return jsonResponse(200, { url: "https://avito.example/voice.mp3" });
        }
        return jsonResponse(200, {});
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it("ссылка спрашивается сама, без единого нажатия", async () => {
    renderWithProviders(
      <MessageBubble msg={голосовое()} prev={null} clientId="client-1" clientName="Ольга" />,
    );
    await waitFor(() => expect(спрошено).toBeGreaterThan(0));
  });

  it("после ответа на экране проигрыватель, а не строка со ссылкой", async () => {
    renderWithProviders(
      <MessageBubble msg={голосовое()} prev={null} clientId="client-1" clientName="Ольга" />,
    );
    // У проигрывателя есть кнопка воспроизведения и полоса с ролью slider —
    // ни того, ни другого у прежней строки-ссылки не было.
    await waitFor(() => expect(screen.getByRole("slider")).toBeInTheDocument());
  });

  it("сама запись НЕ начинает играть", async () => {
    /*
     * ⚠ САМАЯ ДОРОГАЯ ОШИБКА ЭТОЙ ПРАВКИ, И ОНА ЧУТЬ НЕ УЕХАЛА В БОЙ.
     * Проигрыватель вызывал `play()` при появлении — верно, пока он появлялся
     * ТОЛЬКО после нажатия. Как только запись стала открываться сама, диалог с
     * тремя голосовыми заговорил бы всеми тремя разом, а голосовые именно
     * сериями и приходят. В открытом кабинете это громче любой ошибки вёрстки.
     */
    const play = vi.fn(() => Promise.resolve());
    (window.HTMLMediaElement.prototype as unknown as { play: unknown }).play = play;

    renderWithProviders(
      <MessageBubble msg={голосовое()} prev={null} clientId="client-1" clientName="Ольга" />,
    );
    await waitFor(() => expect(screen.getByRole("slider")).toBeInTheDocument());
    expect(play, "запись включилась сама").not.toHaveBeenCalled();
  });
});
