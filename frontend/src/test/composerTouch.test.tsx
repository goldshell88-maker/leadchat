import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "@/features/chats/components/composer/Composer";
import { queryClient } from "@/app/queryClient";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * НА ТЕЛЕФОНЕ «ВВОД» — ЭТО ПЕРЕНОС СТРОКИ.
 *
 * Правило «Enter отправляет, Shift+Enter переносит» родом с настольной
 * клавиатуры. На экранной второй половины правила не существует: Shift+Enter
 * набрать нечем. Значит на телефоне у человека отняты обе возможности сразу —
 * написать клиенту две строки и исправить опечатку до отправки: касание «ввода»
 * отправляет недописанное, а отозвать сообщение в Авито невозможно.
 *
 * Признак — грубый указатель (`pointer: coarse`), а не разбор строки браузера:
 * подключил человек к планшету настоящую клавиатуру — правило вернётся само.
 * Признак читается на каждое нажатие именно поэтому.
 */

/*
 * У каждого теста свой диалог, и это не гигиена ради гигиены: черновик поля
 * ввода хранится ПО ДИАЛОГУ и живёт дольше рендера (03 §2.2). С одним
 * идентификатором второй тест начинал бы с недописанной строки первого — и
 * падал бы, рассказывая про отправку, а не про черновики.
 */
let seq = 0;
const nextConvId = () => `77777777-8888-9999-aaaa-${String(++seq).padStart(12, "0")}`;

const conversation = (id: string) => ({
  id,
  channel: "avito",
  status: "in_progress",
  unread_count: 0,
  client: { id: "c1", name: "Клиент", channel: "avito" },
  account: { id: "a1", title: "Парт-7" },
});

/** Указатель: `true` — палец (телефон, планшет), `false` — мышь. */
function stubPointer(coarse: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("pointer: coarse") ? coarse : false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

function sentTexts(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .filter(([input, init]) => {
      const url = new URL(String(input), "http://localhost");
      return (init?.method ?? "GET") === "POST" && url.pathname.endsWith("/messages");
    })
    .map(([, init]) => String(JSON.parse(String(init?.body ?? "{}")).text ?? ""));
}

describe("Композер на телефоне", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "conversations:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if ((init?.method ?? "GET") === "POST" && url.pathname.endsWith("/messages")) {
        return jsonResponse(201, { id: "m1", direction: "out", body: "ок" });
      }
      const id = url.pathname.split("/").find((p) => p.startsWith("77777777")) ?? nextConvId();
      return jsonResponse(200, conversation(id));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("«ввод» с пальца не отправляет сообщение клиенту", async () => {
    stubPointer(true);
    const user = userEvent.setup();
    const convId = nextConvId();
    renderWithProviders(<Composer convId={convId} conversation={conversation(convId) as never} />);

    const поле = await screen.findByRole("textbox");
    await user.type(поле, "Здравствуйте{Enter}");

    expect(sentTexts(fetchMock)).toEqual([]);
    // И перенос действительно набран: иначе «не отправили» означало бы
    // «проглотили нажатие», а это другая поломка.
    expect((поле as HTMLTextAreaElement).value).toBe("Здравствуйте\n");
  });

  it("кнопка отправки на телефоне работает как обычно", async () => {
    stubPointer(true);
    const user = userEvent.setup();
    const convId = nextConvId();
    renderWithProviders(<Composer convId={convId} conversation={conversation(convId) as never} />);

    await user.type(await screen.findByRole("textbox"), "Выезд бесплатный");
    await user.click(screen.getByRole("button", { name: /Отправить/i }));

    expect(sentTexts(fetchMock)).toEqual(["Выезд бесплатный"]);
  });

  it("с мышью правило прежнее: «ввод» отправляет", async () => {
    stubPointer(false);
    const user = userEvent.setup();
    const convId = nextConvId();
    renderWithProviders(<Composer convId={convId} conversation={conversation(convId) as never} />);

    await user.type(await screen.findByRole("textbox"), "Готово{Enter}");

    expect(sentTexts(fetchMock)).toEqual(["Готово"]);
  });
});
