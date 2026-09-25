import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InfiniteData } from "@tanstack/react-query";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { MessagesPage } from "@/shared/api/types";
import { catchUpAfterReconnect } from "@/shared/realtime/applyWsEvent";
import { useConnectionStore } from "@/shared/realtime/connectionStore";
import { clearCursors, lastKnownCursor, rememberCursor } from "@/shared/realtime/cursorRegistry";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID } from "./render";

/**
 * ВОЗВРАТ СВЯЗИ НЕ ТЕРЯЕТ СООБЩЕНИЯ (03 §3.2, 01 §11.7).
 *
 * Хаб события не буферизирует: всё, что пришло клиенту, пока сокет был мёртв,
 * существует только в базе. Догон — единственный способ это забрать, и три
 * его дыры стоили ровно того, ради чего он написан.
 *
 * Дыра 1. `lastEventAt` ставится ТОЛЬКО пришедшим кадром. Тихое утро — он
 *   null, и догон выходил, не сделав ничего: обрыв в 9:20, клиент написал в
 *   9:20, оператор не узнал об этом до перезагрузки вкладки.
 * Дыра 2. Отказ дифф-запроса перезапрашивал только список. А отказывает он
 *   именно на выкатке — api перезапускается, сокет обрывается, первый же REST
 *   после возврата попадает в те же секунды 502. Список подтягивался, открытая
 *   лента — нет.
 * Дыра 3. Диалог открыт, а курсора нет (реестр в памяти модуля, пуст после
 *   перезагрузки вкладки) — хвост не докачивался вовсе и молча.
 * Дыра 4. Хвост забирался ОДНОЙ страницей на 50 сообщений, `has_more_after`
 *   не смотрели. Остальное оставалось в базе, а курсор сдвигался на конец
 *   полученной страницы — пропуск закреплялся навсегда.
 */

/** Сообщение клиента — минимальный валидный кадр для ленты. */
function msg(id: string) {
  return {
    id,
    conversation_id: CONV_ID,
    direction: "in",
    sender_type: "client",
    sender: null,
    body: `сообщение ${id}`,
    attachments: [],
    delivery_status: "delivered",
    client_message_id: null,
    created_at: "2026-08-11T10:05:00Z",
  };
}

/** Пустая, но существующая лента: appendMessage не создаёт кэш с нуля. */
function seedThread() {
  queryClient.setQueryData<InfiniteData<MessagesPage>>(qk.messages.list(CONV_ID), {
    pages: [
      {
        items: [],
        page: {
          prev_cursor: null,
          next_cursor: "cur-42",
          has_more_before: false,
          has_more_after: false,
        },
      },
    ],
    pageParams: [null],
  });
}

/** Идентификаторы сообщений, лежащих сейчас в ленте открытого диалога. */
function threadIds(): string[] {
  const data = queryClient.getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(CONV_ID));
  return (data?.pages ?? []).flatMap((p) => p.items.map((m) => m.id));
}

/** Ключи, которые догон пометил протухшими за прогон. */
function trackInvalidations(): unknown[][] {
  const seen: unknown[][] = [];
  vi.spyOn(queryClient, "invalidateQueries").mockImplementation(async (filters) => {
    seen.push((filters?.queryKey ?? []) as unknown[]);
  });
  return seen;
}

const hasKey = (seen: unknown[][], key: readonly unknown[]) =>
  seen.some((k) => JSON.stringify(k) === JSON.stringify(key));

/**
 * Сплошная сверка «всё, что на экране» ключа не имеет вовсе.
 *
 * ⚠ ПРАВИЛО ПЕРЕВЁРНУТО 31.08. Догон после обрыва перечислял два корня —
 * диалоги и сообщения, — а устареть после разрыва могло что угодно: карточка
 * клиента, настройки, счётчики, статистика. Ни одно из этого не сверялось, и
 * единственным способом узнать правду оставалась перезагрузка страницы.
 */
// Сборщик выше подменяет отсутствующий ключ пустым массивом — по нему
// сплошную сверку и узнаём: осмысленных вызовов с пустым ключом нет.
const естьСплошная = (seen: unknown[][]) => seen.some((k) => Array.isArray(k) && k.length === 0);

describe("Догон после переподключения", () => {
  beforeEach(() => {
    queryClient.clear();
    clearCursors();
    // Права без `messages:send`: refreshInboxCount выходит сразу и не мешает
    // считать запросы — очередь проверяется своими тестами.
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null });
    useConnectionStore.setState({ status: "open", lastEventAt: null });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe("связь вернулась, а событий до обрыва не было", () => {
    it("открытые экраны всё равно перезапрашиваются", async () => {
      const seen = trackInvalidations();
      const fetchSpy = vi.fn(async () => jsonResponse(200, { items: [] }));
      vi.stubGlobal("fetch", fetchSpy);

      await catchUpAfterReconnect(true);

      expect(
        естьСплошная(seen),
        "после обрыва сверяется не всё видимое — часть экранов останется вчерашней",
      ).toBe(true);
    });

    it("и очередь тоже: у неё свой ключ, в conversations.root она не попадает", async () => {
      /*
       * ⚠ ПОЛОВИНЧАТЫЙ ДОГОН. `refetchOpenScreens` обновляет `conversations` и
       * `messages`, а очередь живёт на СВОЁМ корне `["inbox","list"]`. Бейдж
       * сверялся выше по функции и показывал правду, а список под ним оставался
       * вчерашним — со строками, которые коллеги давно забрали: оператор жал
       * «Принять» и получал отказ, а число над вкладкой с содержимым не сходилось.
       */
      const seen = trackInvalidations();
      vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [] })));

      await catchUpAfterReconnect(true);

      expect(hasKey(seen, qk.inbox.list)).toBe(true);
    });

    it("а на ПЕРВОМ подключении — нет: догонять нечего", async () => {
      // Кэш только что собран обычными REST-запросами; лишний перезапрос на
      // каждом входе — это удвоенная нагрузка на api от тринадцати вкладок.
      const seen = trackInvalidations();
      vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [] })));

      await catchUpAfterReconnect(false);

      expect(hasKey(seen, qk.conversations.root)).toBe(false);
      expect(hasKey(seen, qk.messages.root)).toBe(false);
    });
  });

  describe("дифф-запрос не удался (api перезапускается на выкатке)", () => {
    it("перезапрашивается не только список, но и открытая лента", async () => {
      useConnectionStore.setState({ lastEventAt: new Date(Date.now() - 60_000).toISOString() });
      const seen = trackInvalidations();
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => jsonResponse(502, { error: { code: "bad_gateway", message: "502" } })),
      );

      await catchUpAfterReconnect(true);

      expect(естьСплошная(seen), "дифф не удался, а экраны не сверились").toBe(true);
    });
  });

  describe("открытый диалог", () => {
    beforeEach(() => {
      useChatUiStore.setState({ activeConversationId: CONV_ID });
      useConnectionStore.setState({ lastEventAt: new Date(Date.now() - 60_000).toISOString() });
    });

    it("без запомненного курсора — лента перезапрашивается по своему ключу", async () => {
      const seen = trackInvalidations();
      vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [] })));

      await catchUpAfterReconnect(true);

      expect(hasKey(seen, qk.messages.list(CONV_ID))).toBe(true);
    });

    it("с курсором — хвост докачивается и ложится в ленту", async () => {
      // Половина, которую чинить не надо: она обязана продолжать работать,
      // иначе «починка» свелась бы к перезапросу всего подряд.
      rememberCursor(CONV_ID, "cur-42");
      seedThread();

      const urls: string[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const url = String(input);
          urls.push(url);
          if (url.includes("/messages?after=")) {
            return jsonResponse(200, {
              items: [
                {
                  id: "m-new",
                  conversation_id: CONV_ID,
                  direction: "in",
                  sender_type: "client",
                  sender: null,
                  body: "Мастер приедет сегодня?",
                  attachments: [],
                  delivery_status: "delivered",
                  client_message_id: null,
                  created_at: "2026-08-11T10:05:00Z",
                },
              ],
              page: { prev_cursor: null, next_cursor: "cur-43", has_more_before: false, has_more_after: false },
            });
          }
          return jsonResponse(200, { items: [] });
        }),
      );

      await catchUpAfterReconnect(true);

      expect(urls.some((u) => u.includes(`/conversations/${CONV_ID}/messages?after=cur-42`))).toBe(true);
      const data = queryClient.getQueryData<InfiniteData<MessagesPage>>(qk.messages.list(CONV_ID));
      expect(data?.pages.at(-1)?.items.map((m) => m.id)).toEqual(["m-new"]);
    });

    it("длинный хвост добирается до конца, а не первой страницей", async () => {
      /*
       * Полминуты обрыва на девяти каналах — это легко больше пятидесяти
       * строк: пишет клиент, отвечает бот, ложатся системные записи о
       * передаче. Раньше бралась ровно одна страница, а курсор сдвигался на
       * её конец — то есть пропуск не просто случался, он закреплялся.
       */
      rememberCursor(CONV_ID, "cur-1");
      seedThread();

      const pages: Record<string, { ids: string[]; next: string | null; more: boolean }> = {
        "cur-1": { ids: ["m-1", "m-2"], next: "cur-2", more: true },
        "cur-2": { ids: ["m-3", "m-4"], next: "cur-3", more: true },
        "cur-3": { ids: ["m-5"], next: "cur-4", more: false },
      };

      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const url = String(input);
          const at = /messages\?after=([^&]+)/.exec(url)?.[1];
          if (at) {
            const p = pages[at];
            return jsonResponse(200, {
              items: p.ids.map(msg),
              page: {
                prev_cursor: null,
                next_cursor: p.next,
                has_more_before: false,
                has_more_after: p.more,
              },
            });
          }
          return jsonResponse(200, { items: [] });
        }),
      );

      await catchUpAfterReconnect(true);

      expect(threadIds()).toEqual(["m-1", "m-2", "m-3", "m-4", "m-5"]);
      // Курсор запомнен последний — следующий догон начнёт с конца хвоста.
      expect(lastKnownCursor(CONV_ID)).toBe("cur-4");
    });

    it("бесконечный хвост не крутит запросы по кругу, а сдаётся в перезапрос", async () => {
      // Сервер отдаёт `has_more_after` вечно (баг на той стороне или курсор,
      // который не двигается). Вкладка не имеет права уйти в бесконечный
      // цикл запросов: тринадцать таких вкладок кладут api надёжнее выкатки.
      rememberCursor(CONV_ID, "cur-1");
      seedThread();
      const seen = trackInvalidations();

      let calls = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const url = String(input);
          if (url.includes("/messages?after=")) {
            calls += 1;
            return jsonResponse(200, {
              items: [msg(`m-${calls}`)],
              page: {
                prev_cursor: null,
                next_cursor: `cur-${calls + 1}`,
                has_more_before: false,
                has_more_after: true,
              },
            });
          }
          return jsonResponse(200, { items: [] });
        }),
      );

      await catchUpAfterReconnect(true);

      expect(calls).toBeLessThanOrEqual(10);
      // Сдались — но не молча: лента перезапрашивается целиком.
      expect(hasKey(seen, qk.messages.list(CONV_ID))).toBe(true);
    });

    it("курсор, который не двигается, обрывает добор сразу", async () => {
      // Тот же `next_cursor` в ответе означает «идти дальше некуда»: повтор
      // того же запроса вернёт то же самое.
      rememberCursor(CONV_ID, "cur-1");
      seedThread();

      let calls = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const url = String(input);
          if (url.includes("/messages?after=")) {
            calls += 1;
            return jsonResponse(200, {
              items: [msg(`m-${calls}`)],
              page: {
                prev_cursor: null,
                next_cursor: "cur-1",
                has_more_before: false,
                has_more_after: true,
              },
            });
          }
          return jsonResponse(200, { items: [] });
        }),
      );

      await catchUpAfterReconnect(true);

      expect(calls).toBe(1);
    });
  });
});


describe("Закрытые за время обрыва", () => {
  beforeEach(() => {
    queryClient.clear();
    clearCursors();
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
    useChatUiStore.setState({ activeConversationId: null });
    useConnectionStore.setState({ status: "open", lastEventAt: null });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("дифф просит вкладку «везде, включая закрытые»", async () => {
    /*
     * ⚠ БЕЗ ВКЛАДКИ СЕРВЕР БЕРЁТ УМОЛЧАНИЕ `all`, А ОНО ИСКЛЮЧАЕТ ЗАКРЫТЫЕ
     * (`services/conversations.py`). Диалог, который коллега закрыл, пока у меня
     * рвалась связь, в дифф не приезжал вовсе — и оставался на экране «в
     * работе». Оператор открывал его, дописывал клиенту и узнавал о закрытии
     * по 422 «Диалог закрыт», уже потратив время на ответ.
     */
    useConnectionStore.setState({ lastEventAt: new Date(Date.now() - 60_000).toISOString() });
    const урлы: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        урлы.push(String(input));
        return jsonResponse(200, { items: [], page: { limit: 200, offset: 0, total: 0 } });
      }),
    );

    await catchUpAfterReconnect(true);

    const дифф = урлы.find((u) => u.includes("updated_since="));
    expect(дифф, "дифф-запрос не ушёл вовсе").toBeTruthy();
    expect(дифф).toContain("tab=any");
  });
});
