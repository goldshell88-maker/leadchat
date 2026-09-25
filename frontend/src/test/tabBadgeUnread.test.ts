import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ConversationDto } from "@/shared/api/types";
import { setDocumentSection, subscribeBadges, formatDocumentTitle, readBadges } from "@/shared/stores/badges";
import { useInboxStore } from "@/shared/stores/inboxStore";
import { useUnreadStore } from "@/shared/stores/unreadStore";
import { fakeUser, resetSessionStore } from "./helpers";

/**
 * СЧЁТЧИК В ЗАГОЛОВКЕ ВКЛАДКИ НЕ ЗАВИСИТ ОТ ВЫДАЧИ (разбор 12 августа).
 *
 * ЧТО БЫЛО. «(10)» в заголовке появлялось и пропадало от смены фильтра: число
 * выскочило ровно тогда, когда в выдачу попал ЗАКРЫТЫЙ диалог с десятью
 * непрочитанными — тот, которого в обычных списках не видно вовсе. Причина в
 * устройстве стора: `seedFromRows` зовётся на каждую загруженную страницу
 * ЛЮБОГО списка (чужие, закрытые, очередь), строки оттуда не убираются, и
 * сумма по стору отвечала на вопрос «что я успел пролистать», а не «сколько у
 * меня работы».
 *
 * Здесь проверяется именно неизменность: одно и то же число ДО и ПОСЛЕ того,
 * как в стор приехала посторонняя строка. Проверять «стало 3» мало — такое
 * пройдёт и на сумме, если подобрать данные.
 */

const ME = fakeUser.id;
const COLLEAGUE = "3a1b2c3d-0000-4000-8000-000000000001";

function row(
  id: string,
  unread: number,
  assigneeId: string | null,
  status: ConversationDto["status"],
): ConversationDto {
  return {
    id,
    status,
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: `client-${id}`, name: "Иван Петров", phone: null, avito_rating: null },
    assignee: assigneeId ? { id: assigneeId, full_name: "Кто-то" } : null,
    item: null,
    last_message: { body: "текст", direction: "in", created_at: "2026-08-12T09:00:00Z" },
    unread_count: unread,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-12T09:00:00Z",
  };
}

describe("Бейдж непрочитанных в заголовке вкладки", () => {
  beforeEach(() => {
    useUnreadStore.getState().clear();
    useInboxStore.getState().clear();
    resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
  });

  afterEach(() => {
    useUnreadStore.getState().clear();
    resetSessionStore();
  });

  it("закрытый диалог с непрочитанными не меняет заголовок вкладки", () => {
    useUnreadStore.getState().seedFromRows([row("mine", 3, ME, "in_progress")]);
    const before = formatDocumentTitle(readBadges());
    expect(before).toBe("(3) LeadChat");

    // Человек выбрал фильтр «Закрытые» — в выдачу приехала строка с десятью
    // непрочитанными. Работы у него от этого не прибавилось.
    useUnreadStore.getState().seedFromRows([row("closed-one", 10, ME, "closed")]);

    expect(formatDocumentTitle(readBadges())).toBe(before);
  });

  it("вкладка «Все» приносит чужие непрочитанные — и они тоже не в счёт", () => {
    useUnreadStore.getState().seedFromRows([row("mine", 2, ME, "new")]);
    const before = formatDocumentTitle(readBadges());

    useUnreadStore.getState().seedFromRows([row("theirs", 7, COLLEAGUE, "in_progress")]);

    expect(formatDocumentTitle(readBadges())).toBe(before);
    expect(readBadges().unread).toBe(2);
  });

  it("ничей диалог из очереди в непрочитанные не попадает — у очереди свой бейдж", () => {
    /*
     * Складывать две величины запрещено (см. `stores/badges.ts`): «мне
     * написали» и «диалог ничей и ждёт» — разные вопросы. Раньше ничьи строки
     * очереди попадали и туда, и туда, то есть считались дважды.
     */
    useUnreadStore.getState().seedFromRows([row("queued", 4, null, "new")]);
    useInboxStore.setState({ count: 1 });

    expect(readBadges().unread).toBe(0);
    expect(formatDocumentTitle(readBadges())).toBe("[1] LeadChat");
  });

  it("мои живые диалоги считаются, и только они", () => {
    useUnreadStore
      .getState()
      .seedFromRows([
        row("mine-1", 2, ME, "in_progress"),
        row("mine-2", 1, ME, "waiting_client"),
        row("mine-closed", 10, ME, "closed"),
        row("theirs", 5, COLLEAGUE, "new"),
      ]);

    expect(readBadges().unread).toBe(3);
  });

  it("сессия ещё не поднялась — ноль, а не «всё подряд»", () => {
    // Порядок реальный: строки приезжают из кэша раньше, чем закончится
    // silent refresh. Показать в этот момент сумму по чужим диалогам значит
    // мигнуть числом, которое тут же изменится.
    resetSessionStore();
    useUnreadStore.getState().seedFromRows([row("theirs", 9, COLLEAGUE, "new")]);

    expect(readBadges().unread).toBe(0);
    expect(formatDocumentTitle(readBadges())).toBe("LeadChat");
  });
});

/**
 * РАЗДЕЛ В ЗАГОЛОВКЕ ВКЛАДКИ (п. 16 отчёта тестирования, 15 августа).
 *
 * `document.title` всю жизнь был «LeadChat» на каждом экране: у диспетчера
 * рядом живут Авито, лид-центр и почта, и вкладку «Разбор диалогов» среди них
 * было не найти иначе как перебором.
 */
describe("Раздел в заголовке вкладки", () => {
  afterEach(() => setDocumentSection(null));

  it("раздел стоит ПОСЛЕ бейджей и ПЕРЕД именем продукта", () => {
    // Порядок — записанное решение: в свёрнутой вкладке видно шесть-семь
    // символов, и числа обязаны уцелеть первыми.
    expect(formatDocumentTitle({ unread: 5, queue: 2 }, "Статистика")).toBe(
      "[2] (5) Статистика · LeadChat",
    );
  });

  it("без бейджей — просто «Раздел · LeadChat»", () => {
    expect(formatDocumentTitle({ unread: 0, queue: 0 }, "Чаты")).toBe("Чаты · LeadChat");
  });

  it("незнакомый путь — прежняя строка до символа, ничего не «поехало»", () => {
    expect(formatDocumentTitle({ unread: 3, queue: 0 }, null)).toBe("(3) LeadChat");
  });

  it("смена раздела перерисовывает заголовок с теми же числами", () => {
    // Подписка живёт на бейджах; раздел меняется навигацией. Без reapply
    // заголовок обновлялся бы только со следующим сообщением клиента.
    const seen: string[] = [];
    const off = subscribeBadges((b) => seen.push(formatDocumentTitle(b)));
    setDocumentSection("Настройки");
    off();

    expect(seen.at(-1)).toBe("Настройки · LeadChat");
  });
});
