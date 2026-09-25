// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it } from "vitest";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import { appendMessage, upsertMessage } from "@/shared/realtime/applyWsEvent";
import type { MessageDto, MessagesPage } from "@/shared/api/types";

/**
 * ДОГОН ПОСЛЕ ОБРЫВА ВЫБРАСЫВАЛ СВЕЖИЙ СТАТУС ДОСТАВКИ.
 *
 * Хвост ленты догонялся через `appendMessage`, а тот на первой же строке
 * отбивает всё, что уже лежит в кэше — по id. Для живого кадра `message:new`
 * это верно: одно и то же сообщение приходит и по WS, и догоном, дубль не
 * нужен. Но курсор хвоста запоминается при ЗАГРУЗКЕ ленты, то есть ДО
 * отправки, и сервер отдаёт всё, что после него, — включая наше собственное
 * сообщение, уже с настоящим `delivery_status`.
 *
 * Единственный, кто правит статус на живой связи, — кадр `message:status`, а он
 * и потерян: хаб событий не буферизирует, всё уехавшее в Pub/Sub при мёртвом
 * сокете пропадает. То есть догон отбрасывал ЕДИНСТВЕННУЮ уцелевшую копию этой
 * правды.
 *
 * Как это выглядело: оператор отправляет ответ, видит часики; в эту секунду
 * перезапускается api на выкатке, сокеты рвутся; воркер доставки исчерпывает
 * попытки и помечает сообщение `failed`; сокет возвращается, догон забирает
 * хвост — и выбрасывает его. Пузырь навсегда остаётся «Отправляется», кнопки
 * «Повторить» нет (она нарисована строго по `failed`), а строка слева краснеет.
 * Ленту больше ничто не освежает: ни `refetchInterval`, ни
 * `refetchOnWindowFocus`.
 *
 * ЧТО ЛОМАЛИ: вернули `appendMessage` в цикле догона — падает «свежий статус
 * доезжает до уже загруженного сообщения».
 */

const CONV = "conv-catchup";

function сообщение(over: Partial<MessageDto> = {}): MessageDto {
  return {
    id: "srv-1",
    conversation_id: CONV,
    direction: "out",
    sender_type: "operator",
    sender: { id: "u-1", full_name: "Анна Смирнова" },
    body: "Приедем завтра с 10 до 12",
    attachments: [],
    delivery_status: "pending",
    client_message_id: "temp-1",
    created_at: "2026-08-28T10:12:00Z",
    ...over,
  };
}

function лента(items: MessageDto[]): void {
  queryClient.setQueryData(qk.messages.list(CONV), {
    pages: [
      {
        items,
        page: {
          prev_cursor: null,
          next_cursor: null,
          has_more_before: false,
          has_more_after: false,
        },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

function строки(): MessageDto[] {
  const data = queryClient.getQueryData<{ pages: MessagesPage[] }>(qk.messages.list(CONV));
  return data?.pages.flatMap((p) => p.items) ?? [];
}

describe("Догон ленты после обрыва", () => {
  beforeEach(() => queryClient.clear());

  it("свежий статус доезжает до уже загруженного сообщения", () => {
    лента([сообщение({ delivery_status: "pending" })]);

    // Хвост с сервера: то же сообщение, но воркер уже отчитался о провале.
    upsertMessage(CONV, сообщение({ delivery_status: "failed" }));

    const [msg] = строки();
    expect(строки()).toHaveLength(1); // строка одна, не задвоилась
    expect(msg.delivery_status).toBe("failed");
  });

  it("новое сообщение из хвоста добавляется, как и раньше", () => {
    лента([сообщение()]);

    upsertMessage(CONV, сообщение({ id: "srv-2", body: "и ещё вопрос", direction: "in" }));

    expect(строки().map((m) => m.id)).toEqual(["srv-1", "srv-2"]);
  });

  it("живой кадр по-прежнему НЕ переписывает ленту: дубль отбивается по id", () => {
    лента([сообщение({ delivery_status: "delivered" })]);

    // `appendMessage` остаётся дедупликатором для WS — его поведение не меняли.
    appendMessage(CONV, сообщение({ delivery_status: "pending" }));

    expect(строки()).toHaveLength(1);
    expect(строки()[0].delivery_status).toBe("delivered");
  });

  it("ленты нет в кэше — кэш не создаём ни тем, ни другим путём", () => {
    upsertMessage(CONV, сообщение());
    expect(queryClient.getQueryData(qk.messages.list(CONV))).toBeUndefined();
  });

  it("сам догон зовёт именно upsert, а не append", () => {
    /*
     * ⚠ БЕЗ ЭТОЙ ПРОВЕРКИ ВСЕ ОСТАЛЬНЫЕ ЗЕЛЕНЕЮТ ВПУСТУЮ. Они проверяют
     * функцию, а не то, что её кто-то зовёт: вернуть в цикле догона
     * `appendMessage` — и дефект возвращается при полностью зелёном файле.
     * Ровно на такой проводке этот проект уже попадался.
     */
    const исходник = (readFileSync("src/shared/realtime/applyWsEvent.ts", "utf-8") as string)
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/\/\/[^\n]*/g, " ");
    const начало = исходник.indexOf("export async function catchUpAfterReconnect");
    expect(начало, "функция догона исчезла — проверять нечего").toBeGreaterThan(-1);
    const тело = исходник.slice(начало);
    const цикл = тело.slice(тело.indexOf("for (const m of tail.items)"));
    expect(цикл.slice(0, 120)).toContain("upsertMessage");
  });
});
