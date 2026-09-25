import { beforeEach, describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import { formatListTime } from "@/shared/lib/formatTime";
import type { ConversationDto } from "@/shared/api/types";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ДЕФЕКТ 12: служебное сообщение Авито не оставляло в строке списка следа.
 *
 * Как это выглядело у проверяющего. В диалог с Ольгой Никитиной приходит
 * «Клиент оформил заказ, ожидает подтверждения». В открытой ленте чип
 * «ⓘ Сообщение Авито» появляется правильно, а строка списка остаётся ровно
 * такой, какой была: превью «Вы: Здравствуйте, Ольга!…», время «5 авг».
 * Диспетчер, который сегодня в этот диалог не заходил, про оформленный заказ
 * не узнаёт вовсе.
 *
 * ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕ ПРОВЕРЯЕТСЯ — что строка всплыла наверх. Порядок
 * держит `last_message_at`, и служебная запись его не двигает: та же колонка
 * режет период в «Разборе диалогов» и считает «тихо N дней» у разгрузки
 * очереди. Размен зафиксирован тестами на стороне сервера
 * (`tests/unit/test_avito_system_messages.py`).
 */

const NOTICE_AT = "2026-08-11T12:35:00Z";
const TALKED_AT = "2026-08-04T17:52:39Z";

function row(overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id: "conv-olga",
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "LP-Москва" },
    client: { id: "client-1", name: "Ольга Никитина", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт ноутбуков", url: null, price: null },
    last_message: {
      body: "Клиент оформил заказ, ожидает подтверждения",
      direction: "system",
      // Сервер отдаёт `sender_type` в каждой строке; в `types.ts` поле пока не
      // объявлено (файл чужой), поэтому здесь оно добавляется через каст —
      // ровно тем же приёмом, что и в самом компоненте.
      created_at: NOTICE_AT,
      sender_type: "avito",
    } as ConversationDto["last_message"],
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: TALKED_AT,
    ...overrides,
  };
}

function renderRow(data: ConversationDto) {
  return renderWithProviders(
    <ConversationListItem row={data} active={false} now={Date.parse(NOTICE_AT) + 60_000} onOpen={() => {}} showChannel />,
  );
}

function preview(): HTMLElement {
  const el = document.querySelector(".conv-card__preview");
  if (!el) throw new Error("превью строки не найдено");
  return el as HTMLElement;
}

describe("Служебное сообщение Авито в строке списка", () => {
  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
  });

  it("уведомление площадки видно в превью и подписано «Авито»", () => {
    renderRow(row());
    expect(screen.getByText("Авито: Клиент оформил заказ, ожидает подтверждения")).toBeTruthy();
  });

  it("подпись обязательна: без неё уведомление читается как слова клиента", () => {
    renderRow(row());
    // Именно подпись, а не просто наличие текста: «Клиент оформил заказ»
    // без неё выглядит фразой, которую написал сам клиент.
    expect(preview().textContent?.startsWith("Авито: ")).toBe(true);
    expect(preview().getAttribute("data-source")).toBe("avito");
  });

  it("обрезанное превью договаривается подсказкой", () => {
    renderRow(row());
    // В колонке под превью 134px — уведомление обрывается на «Авито: Клиент
    // офо…», и целиком его больше негде прочесть, не открывая диалог.
    expect(preview().getAttribute("title")).toBe(
      "Служебное сообщение Авито: Клиент оформил заказ, ожидает подтверждения",
    );
  });

  it("время в строке — время уведомления, а не старой переписки", () => {
    /*
     * ⚠ ПРОВЕРКА ПЕРЕПИСАНА 02.09 ВМЕСТЕ СО СМЕНОЙ ВИДА, А НЕ СМЫСЛА.
     *
     * Раньше в ячейке стояли часы, и тест сверял их с `formatListTime`. Теперь
     * колонка показывает ВОЗРАСТ строки: у ждущих был счётчик «22м», у
     * отвеченных часы «12:39», и рядом их нельзя было прочитать одной мерой —
     * список выглядел рваным (жалоба владельца со скриншотом очереди).
     *
     * Смысл этой проверки прежний и по-прежнему важен: возраст обязан считаться
     * от УВЕДОМЛЕНИЯ, а не от старой переписки. Иначе свежее «Авито: клиент
     * оформил заказ» показывалось бы месячной давностью.
     */
    renderRow(row());
    const time = document.querySelector(".wait-gauge__time");
    const минут = (при: string) => Math.floor((Date.now() - new Date(при).getTime()) / 60000);
    expect(минут(NOTICE_AT), "уведомление и старая переписка неразличимы — тест ничего не докажет")
      .toBeLessThan(минут(TALKED_AT));
    // Возраст свежего уведомления — минуты; у старой переписки он был бы в днях.
    expect(time?.textContent, "строка показывает возраст старой переписки вместо уведомления")
      .not.toMatch(/д$/);
  });

  it("наша собственная системная запись «Авито» не подписывается", () => {
    // Строка после смены статуса приезжает кадром `message:new`, и
    // `applyWsEvent` кладёт в `last_message` запись с тем же
    // `direction: "system"`, но без `sender_type`. Подписать её «Авито»
    // значило бы приписать площадке наши собственные слова.
    renderRow(
      row({
        last_message: {
          body: "Статус: Новый → В работе. Анна Смирнова",
          direction: "system",
          created_at: NOTICE_AT,
        },
      }),
    );
    expect(preview().textContent).toBe("Статус: Новый → В работе. Анна Смирнова");
    expect(preview().getAttribute("data-source")).toBeNull();
  });

  it("обычная переписка выглядит как раньше", () => {
    renderRow(
      row({
        last_message: {
          body: "Здравствуйте, Ольга! Мастер подъедет сегодня",
          direction: "out",
          created_at: TALKED_AT,
        },
        last_message_at: TALKED_AT,
      }),
    );
    expect(preview().textContent).toBe("Вы: Здравствуйте, Ольга! Мастер подъедет сегодня");
    expect(preview().getAttribute("data-source")).toBeNull();
    expect(document.querySelector(".wait-gauge__time")?.textContent).toBe(
      formatListTime(TALKED_AT),
    );
  });
});
