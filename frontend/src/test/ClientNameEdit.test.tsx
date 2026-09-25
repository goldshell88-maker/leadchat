import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ClientRef, ConversationDetailDto } from "@/shared/api/types";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * ИМЯ КЛИЕНТА ПРАВИТСЯ РУКАМИ (требование заказчика от 13 августа).
 *
 * ЗАЧЕМ. Имя приходит из профиля Авито, а профиль человек заводил один раз и мог
 * назвать как угодно: «Ак», «Продам всё», пусто. Диспетчер узнаёт настоящее имя в
 * разговоре — и деть его было некуда, кроме заметки, которой не видно ни в списке
 * чатов, ни в заявке.
 *
 * ⚠ ГЛАВНОЕ, ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ, — НЕ ЗАПИСЬ, А ГРАНИЦА С ЧУЖИМ РЕШЕНИЕМ.
 * Правка 10 убрала имя из карточки: на широком экране оно стоит в шапке ленты, и
 * печатать его дважды — тот самый дубль, из-за которого карточку переделывали.
 * Новая кнопка обязана появиться, НЕ вернув при этом имя на широкий экран. Иначе
 * следующий разбор интерфейса снова найдёт два имени в трёхстах пикселях.
 */

function withClient(client: Partial<ClientRef>): ConversationDetailDto {
  return makeConversation({
    client: { id: "client-1", name: "Иван Петров", phone: null, avito_rating: null, ...client },
  });
}

/** Ширина окна: ниже 768 карточка ложится поверх шапки, и имя печатается в ней. */
function stubViewport(width: number) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: width < 768,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe("Имя клиента — правка руками", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "conversations:manage", "messages:send", "notes:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    queryClient.clear();
    stubViewport(1440);
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/identity")) {
        return jsonResponse(200, { phone: null, phone_source: "none", merged_from: [] });
      }
      if (url.includes("/merge-candidates")) return jsonResponse(200, { items: [] });
      if (url.includes("/clients/") && url.includes("/name")) {
        return jsonResponse(200, { name: "Ольга Никитина", changed: true });
      }
      // ⚠ Диалог обязан вернуться ДИАЛОГОМ. После сохранения имени мутация
      // инвалидирует `conversations.detail`, и заглушка `{items: []}` оставляла бы
      // карточку без `client` — она падала бы на чтении телефона. Это ошибка мока,
      // а не продукта, но в выводе теста она выглядит как настоящая.
      if (url.includes(`/conversations/${CONV_ID}`)) {
        return jsonResponse(200, withClient({ name: "Ольга Никитина" }));
      }
      return jsonResponse(200, { items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function render(conv: ConversationDetailDto) {
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  it("само имя и есть кнопка правки (просьба владельца 28.08)", async () => {
    /*
     * ⚠ ЗДЕСЬ БЫЛА ОТДЕЛЬНАЯ ССЫЛКА «изменить» РЯДОМ СО ЗНАЧЕНИЕМ. Она отнимала
     * место в карточке и в шапке, а главное — заставляла целиться в мелкий
     * текст сбоку вместо того, чтобы нажать на то, что правишь. Просьба
     * владельца дословно: «изменять имя без кнопки изменить, а просто наводя на
     * поля имени».
     *
     * Подпись для скринридера несёт САМО ИМЯ: у кнопки, чьё видимое содержимое
     * и есть значение, подпись «Изменить имя клиента» не сказала бы, какое.
     */
    render(withClient({ name: "Иван Петров" }));
    expect(
      await screen.findByRole("button", { name: "Изменить имя клиента: Иван Петров" }),
    ).toBeInTheDocument();
    expect(screen.queryAllByText("Иван Петров").length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "изменить" }),
      "отдельная ссылка «изменить» вернулась",
    ).toBeNull();
  });

  it("без имени кнопка приглашает вписать", async () => {
    render(withClient({ name: null }));
    expect(await screen.findByRole("button", { name: "Вписать имя клиента" })).toBeInTheDocument();
  });

  it("сохраняет введённое имя", async () => {
    render(withClient({ name: null }));
    fireEvent.click(await screen.findByRole("button", { name: "Вписать имя клиента" }));

    const поле = await screen.findByRole("textbox", { name: "Имя клиента" });
    fireEvent.change(поле, { target: { value: "Ольга Никитина" } });
    fireEvent.keyDown(поле, { key: "Enter" });

    await waitFor(() => {
      const вызов = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes("/name") && (o as RequestInit)?.method === "PUT",
      );
      expect(вызов).toBeTruthy();
      expect(JSON.parse((вызов![1] as RequestInit).body as string)).toEqual({
        name: "Ольга Никитина",
      });
    });
  });

  it("пустое значение уходит на сервер — это очистка, а не отказ", async () => {
    /**
     * ⚠ «Ак» ХУЖЕ, ЧЕМ НИЧЕГО: под пустым именем карточка честно показывает
     * «Клиент». Запрети мы пустое значение — и диспетчер обязан оставить
     * заведомо неверное имя, потому что стереть его нечем.
     *
     * ⚠ КНОПКИ «СОХРАНИТЬ» БОЛЬШЕ НЕТ (28.08, просьба владельца: «это лишнее
     * нажатие»). Сохраняет Enter или уход из поля — тем и подтверждаем.
     */
    render(withClient({ name: "Ак" }));
    fireEvent.click(await screen.findByRole("button", { name: "Изменить имя клиента: Ак" }));

    const поле = await screen.findByRole("textbox", { name: "Имя клиента" });
    fireEvent.change(поле, { target: { value: "" } });
    fireEvent.keyDown(поле, { key: "Enter" });

    await waitFor(() => {
      const вызов = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes("/name") && (o as RequestInit)?.method === "PUT",
      );
      expect(вызов).toBeTruthy();
      expect(JSON.parse((вызов![1] as RequestInit).body as string)).toEqual({ name: "" });
    });
  });

  it("уход из поля СОХРАНЯЕТ, а не отменяет", async () => {
    /*
     * ⚠ КНОПКИ «СОХРАНИТЬ» НЕТ (28.08). Значит уход фокуса обязан сохранять:
     * обратное поведение молча теряло бы набранное — человек вписал имя,
     * отвлёкся на ленту, и всё пропало без единого слова. Потерять работу тише,
     * чем сохранить лишнее, нельзя: лишнее видно и правится, потерянное — нет.
     */
    render(withClient({ name: "Ак" }));
    fireEvent.click(await screen.findByRole("button", { name: "Изменить имя клиента: Ак" }));

    const поле = await screen.findByRole("textbox", { name: "Имя клиента" });
    fireEvent.change(поле, { target: { value: "Анатолий" } });
    fireEvent.blur(поле);

    await waitFor(() => {
      const вызов = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes("/name") && (o as RequestInit)?.method === "PUT",
      );
      expect(вызов, "уход из поля потерял набранное имя").toBeTruthy();
      expect(JSON.parse((вызов![1] as RequestInit).body as string)).toEqual({ name: "Анатолий" });
    });
  });

  it("Escape отменяет — и уход фокуса после него уже не сохраняет", async () => {
    /*
     * Escape закрывает поле, а закрытие уводит фокус. Не отличай мы одно от
     * другого — отмена сохраняла бы ровно то, что человек отменил.
     */
    render(withClient({ name: "Ак" }));
    fireEvent.click(await screen.findByRole("button", { name: "Изменить имя клиента: Ак" }));

    const поле = await screen.findByRole("textbox", { name: "Имя клиента" });
    fireEvent.change(поле, { target: { value: "Не сохранять" } });
    fireEvent.keyDown(поле, { key: "Escape" });
    fireEvent.blur(поле);

    await new Promise((r) => setTimeout(r, 20));
    const вызов = fetchMock.mock.calls.find(
      ([u, o]) => String(u).includes("/name") && (o as RequestInit)?.method === "PUT",
    );
    expect(вызов, "отменённое имя всё равно ушло на сервер").toBeFalsy();
  });

  it("ничего не меняли — запроса нет: молчаливое закрытие", async () => {
    render(withClient({ name: "Ак" }));
    fireEvent.click(await screen.findByRole("button", { name: "Изменить имя клиента: Ак" }));
    const поле = await screen.findByRole("textbox", { name: "Имя клиента" });
    fireEvent.blur(поле);

    await new Promise((r) => setTimeout(r, 20));
    expect(
      fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes("/name") && (o as RequestInit)?.method === "PUT",
      ),
      "открыл и закрыл поле — а на сервер ушёл запрос",
    ).toBeFalsy();
  });

  it("Escape закрывает поле, а не диалог", async () => {
    /**
     * Без остановки всплытия тот же Escape уводит из диалога — и набранное имя
     * пропадает вместе с экраном. Приём тот же, что у телефонного поля.
     */
    render(withClient({ name: "Ак" }));
    fireEvent.click(await screen.findByRole("button", { name: "Изменить имя клиента: Ак" }));
    const поле = await screen.findByRole("textbox", { name: "Имя клиента" });

    const наружу = vi.fn();
    document.addEventListener("keydown", наружу);
    fireEvent.keyDown(поле, { key: "Escape", bubbles: true });
    document.removeEventListener("keydown", наружу);

    expect(наружу).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Имя клиента" })).toBeNull(),
    );
  });

  it("наблюдателю кнопки нет", async () => {
    resetSessionStore({
      user: { ...fakeUser, role: "observer" },
      permissions: ["conversations:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    render(withClient({ name: "Иван Петров" }));
    await screen.findByText("Клиент");
    expect(screen.queryByRole("button", { name: /имя клиента/i })).toBeNull();
  });

  it("на узком экране имя печатается — там карточка накрывает шапку собой", async () => {
    stubViewport(400);
    render(withClient({ name: "Иван Петров" }));
    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
  });
});
