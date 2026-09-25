/**
 * Ctrl+D у ГОСТЯ — это выход, а не закрытие (решение владельца 04.09).
 *
 * ⚠ ДОСЛОВНО: «мне не нужна лишняя кнопка „Выйти", для себя я нажму Ctrl+D и
 * выйду — у меня чат пропадёт, а у другого человека, у которого он был, он
 * останется, если он сам не вышел».
 *
 * ⚠ ПОЧЕМУ ЭТО НЕ ОБХОД ЗАПРЕТА. Гостю сервер закрыть чужой диалог и не даст
 * (403 `guest_cannot_close`), и пункта «Закрыть диалог» у него нет. Сочетание
 * значит одно и то же на обеих сторонах — «убери этот диалог у меня», — и
 * различается только способ: хозяину убрать можно закрытием, гостю выходом.
 *
 * ⚠ ПРОВЕРЯЕТСЯ ПРОВОДКА, А НЕ ФУНКЦИЯ. Признак гостя живёт отдельной функцией
 * (`lib/participation.ts`), и проверить её саму было бы легко и бесполезно:
 * в этом проекте уже дважды случалось «написано, но не подключено». Поэтому
 * здесь монтируется настоящая панель и дёргается настоящая шина действий.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { act } from "react";
import { queryClient } from "@/app/queryClient";
import { ChatThreadPane } from "@/features/chats/components/thread/ChatThreadPane";
import { useActionBus } from "@/features/hotkeys/actionBus";
import { qk } from "@/shared/api/queryKeys";
import type { ConversationDetailDto, MessagesPage } from "@/shared/api/types";
import { DEFAULT_FILTERS, useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => vi.fn() };
});

const ХОЗЯИН = { id: "u-denis", full_name: "Зуев Денис" };

function деталь(over: Partial<ConversationDetailDto> = {}): ConversationDetailDto {
  return {
    ...makeConversation(),
    status: "in_progress",
    assignee: ХОЗЯИН,
    participants: [],
    ...over,
  } as ConversationDetailDto;
}

function положить(conv: ConversationDetailDto) {
  queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
  queryClient.setQueryData(qk.messages.list(CONV_ID), {
    pages: [
      {
        items: [],
        page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
      } satisfies MessagesPage,
    ],
    pageParams: [null],
  });
}

describe("Ctrl+D в чужом диалоге", () => {
  let запросы: Array<{ url: string; method: string }> = [];
  /*
   * ⚠ ЗАГЛУШКА ОБЯЗАНА ОТВЕЧАТЬ ФОРМОЙ ДЕТАЛИ. И выход, и закрытие перезапрашивают
   * диалог; ответь заглушка списком, панель получила бы объект без `client` и
   * упала бы УЖЕ ПОСЛЕ утверждений — прогон остаётся зелёным, а строка «Errors 1»
   * заваливает выкатку.
   */
  let текущий: ConversationDetailDto;

  beforeEach(() => {
    запросы = [];
    queryClient.clear();
    useChatUiStore.setState({ filters: DEFAULT_FILTERS, drafts: {} });
    useActionBus.setState({ closeRequest: null });
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        запросы.push({ url, method });
        if (url.includes("/messages")) {
          return jsonResponse(200, {
            items: [],
            page: { prev_cursor: null, next_cursor: null, has_more_before: false, has_more_after: false },
          });
        }
        if (url.includes(`/conversations/${CONV_ID}`) && !url.includes("/participants")) {
          return jsonResponse(200, текущий);
        }
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }),
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  async function нажать(convId: string = CONV_ID) {
    await act(async () => {
      useActionBus.getState().requestClose(convId);
    });
  }

  it("гость выходит, а диалог остаётся в работе у хозяина", async () => {
    текущий = деталь({
      participants: [
        { id: fakeUser.id, full_name: "Я", reason: null, invited_at: null, kind: "self" },
      ],
    });
    положить(текущий);
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await нажать();

    await waitFor(() =>
      expect(
        запросы.some((r) => r.method === "DELETE" && r.url.includes(`/participants/${fakeUser.id}`)),
      ).toBe(true),
    );
    // Главное: статус диалога никто не трогал — у хозяина он остался в работе.
    expect(запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"))).toBe(false);
  });

  it("хозяину Ctrl+D по-прежнему закрывает диалог", async () => {
    текущий = деталь({ assignee: { id: fakeUser.id, full_name: "Я" } });
    положить(текущий);
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await нажать();

    await waitFor(() =>
      expect(запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"))).toBe(true),
    );
    expect(запросы.some((r) => r.method === "DELETE" && r.url.includes("/participants/"))).toBe(
      false,
    );
  });

  it("позванному Ctrl+D закрывает: он не гость", async () => {
    /*
     * ⚠ ГРАНИЦА ПРАВИЛА. Позванного позвали помогать, права он получил
     * осознанно — и до сих пор закрывал диалог. Не различай мы два вида
     * участия, правка тихо отняла бы у него это.
     */
    текущий = деталь({
      participants: [
        { id: fakeUser.id, full_name: "Я", reason: "нужен мастер", invited_at: null, kind: "invited" },
      ],
    });
    положить(текущий);
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);
    await нажать();

    await waitFor(() =>
      expect(запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"))).toBe(true),
    );
  });

  /*
   * ⚠ КЛАВИША «СЛЕТАЛА» ДВУМЯ РАЗНЫМИ СПОСОБАМИ (жалоба владельца 07.09: «не
   * всегда получается быстро закрывать диалоги через комбинацию клавиш, иногда
   * она слетает и приходится снова кликать на диалог»). Оба ниже.
   */

  /*
   * СПОСОБ ПЕРВЫЙ: КАРТОЧКА ЕЩЁ НЕ ПРИЕХАЛА.
   *
   * После закрытия человек оказывается в следующем диалоге мгновенно (правка
   * 06.09), а его карточка едет с сервера ещё десятки миллисекунд. Прежний
   * обработчик в этот момент выходил, УЖЕ отметив просьбу выполненной, — и
   * второе быстрое нажатие пропадало навсегда.
   *
   * ⚠ ДИВЕРСИЯ: перенести `closeHandled(closeReq)` ВЫШЕ проверки
   * `if (!current) return;` — проверка краснеет: закрытия не происходит.
   */
  it("нажатие до приезда карточки не теряется, а ждёт данных", async () => {
    текущий = деталь({ assignee: { id: fakeUser.id, full_name: "Я" } });
    // Карточки в кэше нет: запрос ещё летит.
    queryClient.removeQueries({ queryKey: qk.conversations.detail(CONV_ID) });
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    await нажать();
    expect(запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"))).toBe(false);

    // Карточка приехала — просьба обязана исполниться сама, без второго нажатия.
    await act(async () => {
      положить(текущий);
    });
    await waitFor(() =>
      expect(запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"))).toBe(true),
    );
  });

  /*
   * СПОСОБ ВТОРОЙ: НАЖАТИЕ В ПРОМЕЖУТКЕ МЕЖДУ ДИАЛОГАМИ.
   *
   * Панель ленты рождается заново на каждый диалог (`<ThreadView key={convId} />`)
   * и раньше объявляла все прошлые просьбы виденными прямо при рождении.
   * Нажатие, попавшее между уходом старой панели и рождением новой, поднимало
   * счётчик в никуда.
   *
   * ⚠ ДИВЕРСИЯ: объявить просьбу исполненной при рождении панели (снять её с
   * шины в `useEffect(..., [])`) — краснеет: просьба, сделанная до рождения
   * панели, не исполняется никогда.
   */
  it("нажатие, сделанное до рождения панели, всё равно исполняется", async () => {
    текущий = деталь({ assignee: { id: fakeUser.id, full_name: "Я" } });
    положить(текущий);

    // Человек нажал, пока новой панели ещё не было.
    await act(async () => {
      useActionBus.getState().requestClose(CONV_ID);
    });
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    await waitFor(() =>
      expect(запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"))).toBe(true),
    );
  });

  /*
   * А ЧУЖУЮ ПРОСЬБУ БРАТЬ НЕЛЬЗЯ. Адрес у просьбы затем и появился: без него
   * панель нового диалога исполнила бы нажатие, сделанное для предыдущего, —
   * то есть закрыла бы не тот диалог.
   *
   * ⚠ ДИВЕРСИЯ: убрать проверку `closeReq.convId !== convId` — краснеет.
   */
  it("просьба, адресованная другому диалогу, не исполняется", async () => {
    текущий = деталь({ assignee: { id: fakeUser.id, full_name: "Я" } });
    положить(текущий);
    renderWithProviders(<ChatThreadPane convId={CONV_ID} />);

    await нажать("другой-диалог");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(запросы.some((r) => r.method === "PATCH" && r.url.includes("/status"))).toBe(false);
  });
});
