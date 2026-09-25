/**
 * СКЕЛЕТ ЗАНИМАЕТ РОВНО СТОЛЬКО, СКОЛЬКО ЗАЙМЁТ ПЕРЕПИСКА (05.09).
 *
 * ЧТО БЫЛО. Скелет жил своим ритмом: пузырь 52 px плюс зазор 12 — 64 px на
 * строку. Настоящая строка ленты — 76 px (медиана 73,5, среднее 77,0; то же
 * число стоит у виртуализатора в `estimateSize`). Пять строк скелета занимали
 * 332 px, те же пять строк переписки — 380. Полсотни пикселей лента отыгрывала
 * рывком ровно в тот момент, когда приходил ответ сервера, — то самое «диалог
 * собирается на глазах», против которого писалась шапка по строке списка
 * (22.08).
 *
 * ЧТО СТАЛО. Число одно — `ВЫСОТА_СТРОКИ`, и от него считают оба: виртуализатор
 * и скелет. Сторож держит обе половины связи, потому что порознь каждая
 * бесполезна:
 *  · «строка ленты стоит ВЫСОТА_СТРОКИ» — иначе константа может остаться в коде
 *    декорацией, пока `estimateSize` живёт своим числом;
 *  · «пузырь скелета ростом со строку» — иначе скелет вернётся к своим 52 px, и
 *    заметить это можно будет только глазом на живом сервере.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import {
  ChatThreadPane,
  ВЫСОТА_СТРОКИ,
} from "@/features/chats/components/thread/ChatThreadPane";
import { qk } from "@/shared/api/queryKeys";
import type { MessageDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * Зазор перед пузырём — `--lc-space-2`, он же `padding-top` у `.msg`.
 * Числом, а не токеном: в проверках CSS отключён (`css: false`), а поменяется
 * зазор в вёрстке — это как раз повод прийти сюда и подумать.
 */
const ЗАЗОР = 8;

function сообщение(i: number): MessageDto {
  return {
    id: `m${i}`,
    conversation_id: CONV_ID,
    direction: i % 2 ? "out" : "in",
    body: "Здравствуйте, подскажите стоимость ремонта",
    created_at: new Date(Date.UTC(2026, 8, 5, 8, 0, i)).toISOString(),
    delivery_status: "delivered",
    attachments: [],
    sender: null,
    sender_type: i % 2 ? "user" : "client",
    reply_to: null,
  } as unknown as MessageDto;
}

function положитьЛенту(сколько: number): void {
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [
      {
        items: Array.from({ length: сколько }, (_, i) => сообщение(i)),
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

/** Высота, которую лента объявила для своего содержимого. */
async function высотаЛенты(сколько: number): Promise<number> {
  положитьЛенту(сколько);
  const r = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
  await act(async () => {
    await new Promise((res) => setTimeout(res, 30));
  });
  const узел = r.container.querySelector(".thread-virtual") as HTMLElement;
  const h = parseFloat(узел.style.height);
  r.unmount();
  queryClient.removeQueries({ queryKey: qk.messages.list(CONV_ID) });
  return h;
}

describe("скелет ленты той же высоты, что содержимое", () => {
  beforeEach(() => {
    queryClient.clear();
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, activeConversationId: CONV_ID });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  /**
   * Меряем ПРИРОСТ, а не саму высоту: видимую часть ленты виртуализатор
   * измеряет по-настоящему (в проверках это нули — раскладки нет), и на общую
   * сумму эти строки не влияют одинаково в обоих замерах. Разница же между
   * «пятьдесят сообщений» и «шестьдесят» — чистая оценка размера строки.
   */
  it("каждое сообщение прибавляет ленте ровно ВЫСОТА_СТРОКИ", async () => {
    const пятьдесят = await высотаЛенты(50);
    const шестьдесят = await высотаЛенты(60);

    expect(шестьдесят - пятьдесят).toBe(10 * ВЫСОТА_СТРОКИ);
  });

  it("пузырь скелета ростом со строку ленты, а не сам по себе", () => {
    queryClient.removeQueries({ queryKey: qk.messages.list(CONV_ID) });
    const { container } = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    const пузыри = Array.from(
      container.querySelectorAll<HTMLElement>(".thread-skeleton__bubble"),
    );
    expect(пузыри.length).toBeGreaterThan(0);
    for (const п of пузыри) {
      expect(parseFloat(п.style.height) + ЗАЗОР).toBe(ВЫСОТА_СТРОКИ);
    }
  });

  /**
   * Четыре пузыря — медианная длина диалога (тот же замер, что дал 76 px на
   * строку). Пообещать местом больше, чем придёт, — тот же рывок, только в
   * другую сторону.
   */
  it("пузырей столько, сколько сообщений в медианном диалоге", () => {
    queryClient.removeQueries({ queryKey: qk.messages.list(CONV_ID) });
    const { container } = renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    expect(container.querySelectorAll(".thread-skeleton__bubble")).toHaveLength(4);
  });
});
