import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { Composer } from "@/features/chats/components/composer/Composer";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { qk } from "@/shared/api/queryKeys";
import type { Permission } from "@/shared/auth/usePermissions";
import { useChatUiStore } from "@/shared/stores/chatUiStore";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import {
  CONV_ID,
  makeConversation,
  renderWithProviders,
  seedEmptyThread,
  threadMessages,
} from "./render";

/**
 * НАБРАННЫЙ ОТВЕТ КЛИЕНТУ НЕ ПРОПАДАЕТ (M1 и M2 аудита, docs/29 §D).
 *
 * Оба дефекта — про один и тот же черновик и про одну и ту же цену: потерянный
 * текст набирают заново, а меряется работа скоростью первого ответа.
 *
 * M1. Отправок в диалог две — композер по центру и форма заметки в карточке
 *     клиента справа, — а черновик у них считался общим: заметка гасила ответ
 *     клиенту вместе с режимом «Сообщение/Заметка».
 * M2. Возврат текста при отказе сети был перезаписью, а не слиянием: пока
 *     отправка десять секунд ждала отказа, оператор успевал набрать следующее,
 *     и старый текст ложился поверх нового.
 * M3. Перезагрузка вкладки. Дефекта здесь нет — и это ровно то, что надо
 *     закрепить: полоса «Вышло обновление» (SHELL-02) обещает оператору
 *     «набранные ответы сохранятся» и предлагает перезагрузиться посреди
 *     смены. Обещание держится на двух строчках `partialize` в chatUiStore;
 *     убрать оттуда `drafts` или `draftsOwnerId` — и после каждой выкатки
 *     тринадцать диспетчеров теряют по недописанному ответу.
 */

const CLIENT_HISTORY = { client: { id: "client-1", name: "Иван Петров" }, items: [] };

/** Ключ хранилища — тот же, что в `persist` (chatUiStore). */
const STORAGE_KEY = "leadchat-chat-ui";

/** Композер и карточка клиента в одном дереве — как на настоящем экране. */
function renderChatScreen() {
  return renderWithProviders(
    <>
      <Composer convId={CONV_ID} conversation={makeConversation()} />
      <ClientCardPane convId={CONV_ID} />
    </>,
  );
}

/**
 * Перезагрузка вкладки: память умирает, localStorage остаётся.
 *
 * ЛОВУШКА, НА КОТОРОЙ ЭТОТ ТЕСТ УЖЕ ОДИН РАЗ СОВРАЛ. Обнулить стор через
 * `setState` мало: `persist` подписан на КАЖДУЮ запись и немедленно кладёт в
 * localStorage то, что увидел, — то есть пустоту. Написанная в лоб
 * «перезагрузка» сама стирает то, выживание чего проверяет: `rehydrate`
 * поднимает пустой объект, поле остаётся пустым, а тест про смену сотрудника
 * («чужих черновиков быть не должно») становится зелёным при ЛЮБОМ
 * `partialize` — хоть при выброшенных оттуда черновиках.
 *
 * Поэтому снимок хранилища снимается ДО обнуления и возвращается на место
 * перед `rehydrate`: настоящему localStorage смерть вкладки не указ.
 */
async function reloadTab() {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  useChatUiStore.setState({
    drafts: {},
    draftsOwnerId: null,
    activeConversationId: null,
    uiIdentity: null,
  });
  if (stored === null) window.localStorage.removeItem(STORAGE_KEY);
  else window.localStorage.setItem(STORAGE_KEY, stored);
  await useChatUiStore.persist.rehydrate();
}

describe("Черновик ответа клиенту", () => {
  const urls: string[] = [];

  beforeEach(() => {
    urls.length = 0;
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as Permission[],
      accessToken: "t",
      bootstrapped: true,
    });
    useChatUiStore.setState({ drafts: {}, activeConversationId: CONV_ID });
    seedEmptyThread();
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), makeConversation());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** Всё, что запрашивает экран, отвечает 200; `onSend` решает судьбу отправки. */
  function stubFetch(onSend: (url: string) => Response | Promise<Response>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        if (url.includes("client-history")) return jsonResponse(200, CLIENT_HISTORY);
        if (url.includes("/users/assignable")) return jsonResponse(200, { items: [] });
        if (url.includes("/notes") || url.includes("/messages")) return onSend(url);
        return jsonResponse(200, makeConversation());
      }),
    );
  }

  const field = () => screen.getByLabelText("Текст сообщения") as HTMLTextAreaElement;

  /** Заметка из правой колонки: «+ добавить» → текст → «Сохранить». */
  async function addNoteFromCard(user: ReturnType<typeof userEvent.setup>, text: string) {
    await user.click(await screen.findByRole("button", { name: /Добавить заметку к диалогу/ }));
    await user.type(screen.getByLabelText("Текст заметки"), text);
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
  }

  it("M1: заметка из карточки клиента не трогает набранный ответ", async () => {
    const user = userEvent.setup();
    stubFetch(() =>
      jsonResponse(201, {
        id: "srv-note",
        conversation_id: CONV_ID,
        direction: "note",
        sender_type: "operator",
        sender: { id: fakeUser.id, full_name: fakeUser.full_name },
        body: "торгуется, скидка до 10%",
        attachments: [],
        delivery_status: "delivered",
        client_message_id: "cid",
        created_at: "2026-08-11T10:00:00Z",
      }),
    );

    renderChatScreen();

    await user.type(field(), "Замена экрана — 8900, сегодня до 19:00");
    await addNoteFromCard(user, "торгуется, скидка до 10%");

    // Заметка действительно ушла — иначе проверка ниже зелёная сама по себе.
    await waitFor(() => expect(urls.some((u) => u.includes("/notes"))).toBe(true));

    expect(field().value).toBe("Замена экрана — 8900, сегодня до 19:00");
    expect(useChatUiStore.getState().drafts[CONV_ID]).toEqual({
      text: "Замена экрана — 8900, сегодня до 19:00",
      isNote: false,
    });
  });

  it("M1: провалившаяся заметка из карточки тоже не лезет в поле ответа клиенту", async () => {
    // Обратная половина того же владения черновиком: подставить текст ЗАМЕТКИ
    // в поле, из которого пишут КЛИЕНТУ, — худший из возможных исходов.
    const user = userEvent.setup();
    stubFetch(() => jsonResponse(500, { error: { code: "internal_error", message: "сбой" } }));

    renderChatScreen();

    await user.type(field(), "Перезвоню в течение часа");
    await addNoteFromCard(user, "клиент из Химок, ехать далеко");

    await waitFor(() =>
      expect(threadMessages().some((m) => m.delivery_status === "failed")).toBe(true),
    );

    expect(field().value).toBe("Перезвоню в течение часа");
    // Текст заметки не потерян — он остался пузырём в ленте.
    expect(threadMessages().some((m) => m.body === "клиент из Химок, ехать далеко")).toBe(true);
  });

  it("M2: отказ сети не затирает то, что оператор набрал заново", async () => {
    const user = userEvent.setup();
    // Отказ приходит НЕ мгновенно — в этом весь дефект: между отправкой и
    // отказом человек успевает набрать следующее сообщение.
    let release!: () => void;
    const held = new Promise<void>((r) => {
      release = r;
    });
    stubFetch(async () => {
      await held;
      return jsonResponse(500, { error: { code: "internal_error", message: "сбой" } });
    });

    renderChatScreen();

    await user.type(field(), "Замена экрана — 8900");
    await user.keyboard("{Enter}");
    expect(field().value).toBe(""); // отправка поле очистила

    await user.type(field(), "Доставка нужна?");
    release();

    await waitFor(() => expect(threadMessages()[0].delivery_status).toBe("failed"));

    expect(field().value).toBe("Доставка нужна?");
    expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe("Доставка нужна?");
    // Упавший текст не пропал: он в пузыре ленты, там же «Повторить».
    expect(threadMessages()[0].body).toBe("Замена экрана — 8900");
  });

  it("M3: перезагрузка страницы возвращает набранный ответ в поле", async () => {
    const user = userEvent.setup();
    stubFetch(() => jsonResponse(200, {}));
    // Владелец черновиков — как после входа: их ставит useRoleUiSync.
    useChatUiStore.getState().resetForRole({ id: fakeUser.id, role: "manager" });
    useChatUiStore.setState({ activeConversationId: CONV_ID });

    renderChatScreen();
    await user.type(field(), "Мастер будет в 14:00, устроит?");

    // В хранилище текст обязан лежать УЖЕ сейчас. Между нажатием «Обновить» на
    // полосе и `location.reload()` у приложения нет ни одного кадра, чтобы
    // что-то досохранить: отложенная запись здесь равна потерянному ответу.
    expect(window.localStorage.getItem("leadchat-chat-ui") ?? "").toContain(
      "Мастер будет в 14:00, устроит?",
    );

    // ПЕРЕЗАГРУЗКА. Дальше приложение поднимается заново: rehydrate из
    // хранилища, затем bootstrap ставит пользователя и useRoleUiSync зовёт
    // resetForRole — тот самый вызов, который решает, чьи это черновики.
    cleanup();
    await reloadTab();
    useChatUiStore.getState().resetForRole({ id: fakeUser.id, role: "manager" });
    useChatUiStore.setState({ activeConversationId: CONV_ID });

    renderChatScreen();
    expect(field().value).toBe("Мастер будет в 14:00, устроит?");
  });

  it("M3: черновик ушедшего сотрудника не достаётся следующему за тем же компьютером", async () => {
    // Обратная половина: `draftsOwnerId` переживает перезагрузку не ради
    // удобства, а ради того, чтобы ответ Анны не всплыл в поле у Сергея,
    // который сел за её компьютер во вторую смену.
    const user = userEvent.setup();
    stubFetch(() => jsonResponse(200, {}));
    useChatUiStore.getState().resetForRole({ id: fakeUser.id, role: "manager" });
    useChatUiStore.setState({ activeConversationId: CONV_ID });

    renderChatScreen();
    await user.type(field(), "Замена компрессора — 12400");

    cleanup();
    await reloadTab();

    // Черновик действительно поднялся из хранилища — без этой строки проверка
    // ниже зелёная сама по себе: пустое сравнивать с пустым легко.
    expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe("Замена компрессора — 12400");

    useChatUiStore.getState().resetForRole({ id: "другой-сотрудник", role: "manager" });
    expect(useChatUiStore.getState().drafts).toEqual({});
  });

  it("M2: в пустое поле упавший текст по-прежнему возвращается", async () => {
    // Половина, которую чинить не надо: если оператор ничего не набрал заново,
    // возврат обязан работать, иначе текст переживёт только вкладку.
    const user = userEvent.setup();
    stubFetch(() => jsonResponse(500, { error: { code: "internal_error", message: "сбой" } }));

    renderChatScreen();

    await user.type(field(), "Перезвоню в течение часа");
    await user.keyboard("{Enter}");

    await waitFor(() =>
      expect(useChatUiStore.getState().drafts[CONV_ID]?.text).toBe("Перезвоню в течение часа"),
    );
    expect(field().value).toBe("Перезвоню в течение часа");
  });
});
