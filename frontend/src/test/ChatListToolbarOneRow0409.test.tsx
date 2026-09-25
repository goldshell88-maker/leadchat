/**
 * Шапка списка — ОДНА строка (просьба владельца 04.09 со скриншотом-образцом).
 *
 * ⚠ ДОСЛОВНО: «сделай мой по аналогии в одну строчку, а не как сейчас». На
 * образце вкладки и значок сужений стоят одним рядом; у нас вкладки занимали
 * строку, а долг и кнопка «Фильтры» — следующую.
 *
 * ⚠ ПОЧЕМУ СТОРОЖ, А НЕ ОДНА ПРАВКА CSS. Ряд держится не стилем, а РАЗМЕТКОЙ:
 * все три контрола обязаны быть детьми одного контейнера. Верни кто-нибудь
 * кнопку в собственный блок — стиль промолчит, а строк снова станет две.
 * Поэтому проверяется родство, а не внешний вид.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ChatListPane } from "@/features/chats/components/list/ChatListPane";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";

const ACCOUNTS = [
  { id: "acc-1", title: "LP-Москва" },
  { id: "acc-2", title: "LP-Питер" },
];

function mount() {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ["conversations:read", "accounts:read", "users:manage"],
    accessToken: "test-token",
    bootstrapped: true,
  });
  useChatUiStore.setState({ activeConversationId: null, filters: { tab: "all" }, assigneeLabel: null });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/avito-accounts")) {
        return jsonResponse(200, {
          items: ACCOUNTS,
          page: { limit: 50, offset: 0, total: ACCOUNTS.length },
        });
      }
      // Долг показывается, только когда сервер назвал число: без него ряд
      // недосчитался бы одного жильца, и сторож проверял бы неполный случай.
      if (url.pathname.endsWith("/conversations/counts")) {
        return jsonResponse(200, { mine: 59, mine_waiting: 11, inbox: 0 });
      }
      if (url.pathname.endsWith("/conversations")) {
        return jsonResponse(200, { items: [], page: { limit: 50, offset: 0, total: 0 } });
      }
      return jsonResponse(404, { error: { code: "not_found", message: "нет" } });
    }),
  );

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/chats"]}>
          <ChatListPane />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

describe("шапка списка диалогов", () => {
  beforeEach(() => mount());
  afterEach(() => vi.unstubAllGlobals());

  it("сужения стоят в строке поиска, а вкладки и долг — в своей", async () => {
    /*
     * ⚠ ЭТО ВТОРАЯ РЕДАКЦИЯ ПРАВИЛА, И ПЕРВАЯ БЫЛА НЕВЕРНОЙ.
     *
     * 04.09 вкладки, долг и сужения свели в ОДИН ряд по просьбе владельца со
     * скриншотом-образцом. Ряд помещался: 310 пикселей при доступных 314.
     * Запас в четыре пикселя — это везение, а не раскладка: трёхзначный
     * счётчик («Входящие 128»), поднятая на большом мониторе шкала или
     * системное увеличение шрифта съедают его целиком. 05.09 владелец прислал
     * тот же скриншот второй раз: значок сужений снова уехал на свою строку.
     *
     * Лечится раскладкой, а не подгонкой пикселей. Поиск тянется и запас
     * справа имеет ВСЕГДА — значок живёт там. Вкладкам достаётся своя строка
     * целиком. Рядов по-прежнему два, но второй больше не «остаток».
     */
    const поиск = await waitFor(() => {
      const el = document.querySelector(".chat-list-pane__seek");
      expect(el, "строки поиска нет").toBeTruthy();
      return el as HTMLElement;
    });
    const ряд = document.querySelector(".chat-list-pane__toolbar");
    expect(ряд, "ряда вкладок нет").toBeTruthy();

    // Сначала докажем, что жильцы приехали: пустой ряд «содержит всё».
    const долг = await waitFor(() => {
      const el = document.querySelector(".chat-debt");
      expect(el, "чип долга не отрисовался — сторож проверял бы полупустой ряд").toBeTruthy();
      return el as HTMLElement;
    });
    const вкладки = document.querySelector(".chat-tabs");
    const фильтр = document.querySelector(".chat-filters-toggle");
    expect(вкладки, "вкладок нет").toBeTruthy();
    expect(фильтр, "значка сужений нет").toBeTruthy();

    expect(поиск.contains(фильтр), "сужения не в строке поиска").toBe(true);
    expect(ряд!.contains(фильтр), "сужения вернулись к вкладкам — ряд снова переполнится").toBe(
      false,
    );
    expect(ряд!.contains(вкладки), "вкладки вне своего ряда").toBe(true);
    expect(ряд!.contains(долг), "долг вне ряда вкладок").toBe(true);
  });

  it("поле поиска тянется — иначе запас справа снова становится везением", () => {
    /*
     * ⚠ ЭТОГО НЕ ХВАТАЛО, И ДИВЕРСИЯ ЭТО ПОКАЗАЛА. Первая редакция сторожа
     * проверяла только РОДСТВО: значок внутри строки поиска. Подмена
     * `flex: 1 1 auto` на `flex: 0 0 auto` прошла насквозь — поле перестаёт
     * тянуться, ряд снова считается по содержимому, и мы возвращаемся ровно к
     * той раскладке, из-за которой всё и переносилось.
     */
    const css = readFileSync("src/features/chats/components/list/chat-list.css", "utf8");
    const поле = css.match(/\.chat-list-pane__search\s*\{([^}]*)\}/s);
    expect(поле, "нет правила .chat-list-pane__search").toBeTruthy();
    expect(поле![1], "поле поиска перестало тянуться").toMatch(/flex:\s*1\b/);
    expect(поле![1], "без min-width: 0 длинная подпись распирает ряд").toMatch(/min-width:\s*0/);
  });

  it("сужения открываются значком, а не словом", async () => {
    const фильтр = await waitFor(() => {
      const el = document.querySelector(".chat-filters-toggle");
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    // Подпись словом занимала треть ряда — именно она и не влезала к вкладкам.
    expect(фильтр.textContent?.trim()).toBe("");
    // Смысл при этом не потерян: он остался читалке с экрана.
    expect(фильтр.getAttribute("aria-label")).toBe("Фильтры");
    expect(фильтр.querySelector("svg"), "значка внутри нет").toBeTruthy();
  });

  it("ряд помещается в колонку: вкладки на УЗКИХ полях", () => {
    /*
     * ⚠ ОДНОЙ РАЗМЕТКИ МАЛО (второй скриншот владельца 04.09). Ряд был собран
     * в одну строку, но не ПОМЕЩАЛСЯ: колонка 340 минус поля 24 даёт 314
     * пикселей, а три вкладки на средних полях (16) со счётчиками, чип долга и
     * значок сужений просили около 369. Перенос — запасной выход — стал нормой,
     * и владелец увидел ровно то же, на что жаловался.
     *
     * Двенадцать против шестнадцати — двадцать четыре пикселя на трёх вкладках;
     * вместе со снятой точкой долга и плотными зазорами ряд встал в строку.
     */
    const css = readFileSync("src/features/chats/components/list/chat-list.css", "utf8");
    // Якорь на начало строки: без него первым совпадает
    // `.chat-tabs[data-dimmed] .chat-tabs__tab`, где полей нет вовсе.
    const вкладка = css.match(/^\.chat-tabs__tab\s*\{([^}]*)\}/ms);
    expect(вкладка, "нет правила .chat-tabs__tab").toBeTruthy();
    expect(вкладка![1], "вкладки вернулись на средние поля — ряд не поместится").toContain(
      "var(--lc-btn-px-sm)",
    );
  });

  it("колонка списка растёт там же, где кегль", () => {
    /*
     * ⚠ ШИРИНА И КЕГЛЬ СВЯЗАНЫ ПРИЧИНОЙ. От 1800px шкала поднимается на ступень
     * (жалоба про 27 дюймов) — и та же шапка становится шире на те же проценты.
     * Оставь колонку на 340, и ряд, который мы только что уместили, снова
     * перенесётся. Поэтому порог у них общий, и это стережётся.
     */
    const vars = readFileSync("src/app/lc-vars.css", "utf8");
    const страница = readFileSync("src/features/chats/chats-page.css", "utf8");
    const порог = vars.match(/@media \(min-width:\s*(\d+)px\)\s*\{\s*:root\s*\{[^}]*--lc-fz-body/s);
    expect(порог, "не нашёл порог шага шкалы").toBeTruthy();
    const блок = страница.match(
      new RegExp(`@media \\(min-width:\\s*${порог![1]}px\\)\\s*\\{([\\s\\S]*?)\\n\\}`),
    );
    expect(блок, `на пороге ${порог![1]}px колонка списка не расширена`).toBeTruthy();
    expect(блок![1]).toContain("chat-list-pane");
    expect(блок![1]).toMatch(/width:\s*\d+px/);
  });
});
