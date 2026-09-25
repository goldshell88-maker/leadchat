import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { queryClient } from "@/app/queryClient";
import { LeadbotTab } from "@/features/settings/leadbot/LeadbotTab";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * РАЗДЕЛ «ЛИД-БОТ» — свойства, каждое из которых ломается молча.
 *
 * Токен однажды появится в поле, потому что «так удобнее проверить». Пустое
 * поле однажды начнут слать как «убрать», и связь порвётся при исправлении
 * опечатки в адресе. Тестовый разговор однажды начнут писать в журнал работы —
 * и журнал перестанет отвечать на вопрос «как бот вёл себя в бою». Ни одно из
 * этих событий не падает и не подсвечивается.
 */

const OVERVIEW = {
  connection: { url: "http://10.10.0.2:8790", token_set: true, source: "db" },
  is_ready: true,
  enabled: false,
  mode: "suggest" as const,
  context_messages: 30,
  account_ids: [],
  accounts: [
    // Источник рядом с названием — просьба владельца 03.09 «источники должны
    // стоять везде». Два канала одного человека различает только он.
    { id: "acc-1", title: "Дамир", lead_origin: "В95", busy_with_other_bot: false },
    { id: "acc-2", title: "Служебный", lead_origin: null, busy_with_other_bot: false },
  ],
};

function mount(overview = OVERVIEW, calls: unknown[] = []) {
  resetSessionStore({
    user: { ...fakeUser, role: "admin" },
    permissions: ["bots:manage"],
    accessToken: "t",
    bootstrapped: true,
  });
  const requests: Array<{ url: string; method: string; body: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({
        url,
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      const payload = url.includes("/leadbot/calls") ? { items: calls } : overview;
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  renderWithProviders(<LeadbotTab />, { route: "/settings/leadbot" });
  return requests;
}

describe("Раздел «Лид-бот»", () => {
  /*
   * КЭШ ЗАПРОСОВ ОБЩИЙ НА ВЕСЬ ФАЙЛ, И БЕЗ ЧИСТКИ ОН ВРЁТ.
   *
   * Ключи у всех проверок одинаковые, а `queryClient` один. Первый тест кладёт
   * в кэш свой ответ, и следующий получает ЕГО вместо своего: экран рисует
   * пустой журнал там, где подсунуты строки, и незапертую кнопку там, где связь
   * не настроена. Оба раза виноват кэш, а выглядит как сломанный экран — я на
   * это уже потратил заход.
   */
  beforeEach(() => queryClient.clear());
  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("правка адреса не затирается сохранением соседней настройки", async () => {
    /*
     * ⚠ ЭФФЕКТ ЗАВИСЕЛ ОТ ВСЕГО ОТВЕТА, А НЕ ОТ АДРЕСА.
     *
     * Все блоки страницы живут на одном ключе запроса, и каждое сохранение
     * кладёт в кэш НОВЫЙ объект (`setQueryData`). Эффект синхронизации стоял на
     * `[loaded]` и срабатывал на любой такой подмене — даже когда адрес не
     * менялся. Человек набирал новый адрес, трогал число реплик рядом и получал
     * обратно старый: молча, без сообщения и без следа в журнале.
     *
     * ⚠ ЖДЁМ ВИДИМОГО СЛЕДСТВИЯ, А НЕ «НЕМНОГО». Проверка «в поле по-прежнему
     * набранное» истинна и ДО обновления кэша, поэтому `waitFor` вокруг неё
     * зеленеет мгновенно и не ловит ничего — на этом мой первый заход и
     * попался. Ответ на сохранение приносит другое имя канала: дождавшись его
     * на экране, мы знаем, что кэш подменён и эффекты уже отработали.
     */
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/leadbot/calls")) {
          return new Response(JSON.stringify({ items: [] }), { status: 200 });
        }
        const сохранение = (init?.method ?? "GET") !== "GET";
        const тело = сохранение
          ? {
              ...OVERVIEW,
              context_messages: 12,
              accounts: [{ id: "acc-1", title: "Дамир-2", busy_with_other_bot: false }],
            }
          : OVERVIEW;
        return new Response(JSON.stringify(тело), { status: 200 });
      }),
    );
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ["bots:manage"],
      accessToken: "t",
      bootstrapped: true,
    });
    renderWithProviders(<LeadbotTab />, { route: "/settings/leadbot" });

    const адрес = await screen.findByLabelText("Адрес");
    await userEvent.clear(адрес);
    await userEvent.type(адрес, "http://10.10.0.9:8790");

    const реплики = screen.getByLabelText("Сколько реплик отправлять");
    await userEvent.clear(реплики);
    await userEvent.type(реплики, "12");
    await userEvent.tab(); // уход курсора = сохранение соседней настройки

    await waitFor(() => expect(screen.getAllByText("Дамир-2").length).toBeGreaterThan(0));
    expect((адрес as HTMLInputElement).value).toBe("http://10.10.0.9:8790");
  });

  it("токен не показывается — только «задан»", async () => {
    mount();
    // Поле пустое, а факт наличия виден подсказкой. Покажи здесь значение — и
    // токен ляжет в снимок экрана, в кэш вкладки и в чужую пересылку.
    const token = await screen.findByLabelText("Токен");
    expect(token).toHaveValue("");
    expect(token).toHaveAttribute("placeholder", expect.stringContaining("Задан"));
    expect(token).toHaveAttribute("type", "password");
  });

  it("сохранение без токена не шлёт поле токена вовсе", async () => {
    const requests = mount();
    const url = await screen.findByLabelText("Адрес");
    await userEvent.clear(url);
    await userEvent.type(url, "http://10.10.0.2:9999");
    await userEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => {
      const put = requests.find((r) => r.method === "PUT");
      expect(put).toBeTruthy();
      // Именно ОТСУТСТВИЕ ключа, а не пустая строка: сервер читает пустую
      // строку как «убрать токен», и исправление адреса рвало бы связь.
      expect(put!.body).toEqual({ url: "http://10.10.0.2:9999" });
      expect(Object.keys(put!.body as object)).not.toContain("token");
    });
  });

  it("тестовый разговор идёт в свою ручку, а не в диалоги", async () => {
    const requests = mount();
    await screen.findByLabelText("Сообщение клиента");
    await userEvent.click(screen.getByRole("button", { name: "Спросить лид-бота" }));

    await waitFor(() => {
      expect(requests.some((r) => r.url.includes("/leadbot/test"))).toBe(true);
    });
    // И НИ ОДНОГО обращения к сообщениям или диалогам: тест не имеет права
    // никого задеть — ради этого он и заводится.
    expect(requests.some((r) => r.url.includes("/messages"))).toBe(false);
    expect(requests.some((r) => r.url.includes("/conversations"))).toBe(false);
  });

  it("без адреса и токена включить нельзя", async () => {
    mount({ ...OVERVIEW, is_ready: false, connection: { url: "", token_set: false, source: "env" } });
    const enable = await screen.findByRole("button", { name: "Включить лид-бота" });
    // Кнопка заперта, и рядом сказано почему: запертая кнопка без объяснения
    // читается как поломка экрана.
    expect(enable).toBeDisabled();
    expect(screen.getAllByText("Сначала задайте адрес и токен").length).toBeGreaterThan(0);
  });

  it("штатная работа в журнале не красится, поломка — красится", async () => {
    mount(OVERVIEW, [
      {
        id: "1",
        at: "2026-08-12T19:29:41Z",
        conversation_id: null,
        account_id: null,
        request_id: null,
        question: "Ремонтируете?",
        reply: "Да",
        layer: "роутер",
        flag: null,
        confidence: 1,
        needs_operator: false,
        outcome: "sent",
        outcome_label: "Ответ ушёл клиенту",
        escalation: null,
        lead_ready: false,
        warnings: [],
        ms: 4,
        error: null,
      },
      {
        id: "2",
        at: "2026-08-12T19:30:00Z",
        conversation_id: null,
        account_id: null,
        request_id: null,
        question: "Сколько стоит?",
        reply: null,
        layer: null,
        flag: null,
        confidence: null,
        needs_operator: null,
        outcome: "unavailable",
        outcome_label: "Лид-бот не ответил",
        escalation: null,
        lead_ready: false,
        warnings: [],
        ms: 9000,
        error: "Не ответил за 9 с",
      },
    ]);

    await screen.findByText("Ответ ушёл клиенту");
    const items = document.querySelectorAll(".lb-log__item");
    expect(items).toHaveLength(2);
    /*
     * ПРАВИЛО ВЛАДЕЛЬЦА: жёлтым и красным — только то, что требует человека.
     * «Ответ ушёл клиенту» — штатная работа, и красить её нечем; покрась —
     * и журнал станет разноцветным, а настоящую поломку в нём никто не найдёт.
     */
    expect(items[0].getAttribute("data-tone")).toBeNull();
    expect(items[1].getAttribute("data-tone")).toBe("bad");
  });

  it("рядом с каналом стоит его источник", async () => {
    /*
     * ⚠ ПРОВЕРКА «ПОДКЛЮЧЕНО ЛИ». Формат подписи сторожит
     * `channelLabel0309.test.ts`; здесь проверяется, что список каналов
     * лид-бота его ЗОВЁТ. Владелец прислал снимок именно этого списка: два
     * десятка строк вида «Александр КП» / «Александр МНЧ», по которым не
     * отличить источник.
     *
     * Данных для подписи в ответе сервера раньше не было вовсе — поле
     * добавлено в `services/leadbot_admin`.
     */
    mount();
    /*
     * ⚠ ИЩЕМ ИМЕННО ФЛАЖОК, А НЕ ЛЮБОЙ ТЕКСТ НА СТРАНИЦЕ. Первая редакция
     * этой проверки брала `findAllByText` — и зеленела на сломанном коде:
     * ту же подпись рисует фильтр каналов рядом, и убрать её из СПИСКА можно
     * было незаметно. Проверено диверсией.
     */
    expect(
      await screen.findByRole("checkbox", { name: /Дамир · В95/ }),
      "в списке каналов лид-бота нет источника — по названиям каналы не отличить",
    ).toBeTruthy();
    // Канал без источника — подпись прежняя: висящая точка хуже её отсутствия.
    expect(await screen.findByRole("checkbox", { name: /^Служебный$/ })).toBeTruthy();
  });
});
