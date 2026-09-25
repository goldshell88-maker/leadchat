import { beforeEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { queryClient } from "@/app/queryClient";
import { applyInboxClaimed } from "@/shared/realtime/applyWsEvent";
import { CONVERSATIONS_LIST_KEY } from "@/shared/api/queryKeys";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { fakeUser, resetSessionStore } from "./helpers";

/**
 * КАДР «ДИАЛОГ УШЁЛ ИЗ ОЧЕРЕДИ» БЕЗ ПРИНЯВШЕГО (аудит 30.08).
 *
 * ⚠ ЭТО НЕ РЕДКАЯ ВЕТКА. `inbox:claimed` шлётся не только на «человек нажал
 * Принять»: `inbound` публикует тот же кадр, когда диалог ушёл из очереди БЕЗ
 * принявшего — коллега ответил клиенту прямо из приложения Авито. По замерам
 * прода это ОСНОВНОЙ путь ответов: из LeadChat в тот день не отправили ни
 * одного сообщения.
 *
 * Тип кадра обещал `UserRef`, а обработчик читал `claimedBy.full_name` — и
 * падал с TypeError ровно у того, у кого этот диалог открыт на экране. Дальше
 * не выполнялось ничего: ни сверка списка, ни снятие строки.
 */

const CONV = "cccccccc-0000-0000-0000-000000000001";

describe("inbox:claimed без принявшего", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: CONV });
    useInboxStore.setState({ ids: { [CONV]: true } } as never);
  });

  it("не роняет обработчик у того, кто смотрит этот диалог", () => {
    expect(() =>
      applyInboxClaimed(CONV, null, { in_inbox: false, status: "in_progress" }, false),
    ).not.toThrow();
  });

  it("строка уходит из очереди, как и должна", () => {
    applyInboxClaimed(CONV, null, { in_inbox: false, status: "in_progress" }, false);
    expect(
      useInboxStore.getState().ids[CONV],
      "строка осталась в очереди — её возьмут вторым голосом",
    ).toBeFalsy();
  });

  it("плашку «Диалог принял …» не показываем: называть некого", () => {
    const показать = vi.spyOn(useInboxStore.getState(), "showClaimed");
    applyInboxClaimed(CONV, null, { in_inbox: false }, false);
    expect(показать, "плашка назвала принявшим никого").not.toHaveBeenCalled();
    показать.mockRestore();
  });

  it("ответственного из ниоткуда не выдумываем", () => {
    /*
     * Записать `null` ответственным значило бы стереть уже известного:
     * диалог мог вестись человеком, а кадр говорит лишь «в очереди больше нет».
     */
    queryClient.setQueryData([...CONVERSATIONS_LIST_KEY, "тест"], {
      pages: [
        {
          items: [
            {
              id: CONV,
              assignee: { id: "u-1", full_name: "Анна" },
              unread_count: 0,
              tags: [],
            },
          ],
          page: { limit: 50, has_more: false, next_cursor: null },
        },
      ],
      pageParams: [null],
    });

    applyInboxClaimed(CONV, null, { in_inbox: false }, false);

    const строка = queryClient
      .getQueryData<{ pages: { items: { id: string; assignee: unknown }[] }[] }>([
        ...CONVERSATIONS_LIST_KEY,
        "тест",
      ])
      ?.pages[0].items[0];
    expect(строка?.assignee, "известный ответственный стёрт кадром без имени").toEqual({
      id: "u-1",
      full_name: "Анна",
    });
  });

  it("с принявшим всё работает как раньше", () => {
    const показать = vi.spyOn(useInboxStore.getState(), "showClaimed");
    applyInboxClaimed(CONV, { id: "u-2", full_name: "Иван" }, { in_inbox: false }, false);
    expect(показать, "плашка о принявшем пропала").toHaveBeenCalledWith(CONV, "Иван");
    показать.mockRestore();
  });

  it("свой же приём плашкой не объявляем", () => {
    const показать = vi.spyOn(useInboxStore.getState(), "showClaimed");
    applyInboxClaimed(CONV, { id: fakeUser.id, full_name: fakeUser.full_name }, {}, true);
    expect(показать).not.toHaveBeenCalled();
    показать.mockRestore();
  });
});

describe("Тост отказа называет исход правильно", () => {
  it("сообщение привязано к флагу круга, а не к счётчику очереди", () => {
    /*
     * ⚠ Сервер шлёт два похожих поля и прямо предупреждает «не путать»:
     * `escalated` — ЧИСЛО брошенных в моей очереди, `escalated_now` — «этим
     * отказом закрылся круг». Фронт брал число: пока в очереди висел хоть один
     * брошенный, КАЖДЫЙ отказ печатал «От него отказались все» — ложь про
     * только что отклонённый. А когда круг действительно замыкался и других
     * брошенных не было — молчали.
     */
    const исходник = readFileSync(
      "src/features/chats/inbox/useInbox.ts",
      "utf-8",
    ) as string;
    expect(исходник, "тост снова читает счётчик вместо флага").toMatch(
      /message: res\.escalated_now \?/,
    );
  });
});
