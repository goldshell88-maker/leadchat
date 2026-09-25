import { beforeEach, describe, expect, it } from "vitest";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import type { ConversationDto } from "@/shared/api/types";
import {
  MSK_TIME_ZONE,
  formatInZone,
  moscowZoneHint,
  zonedTimeTitle,
} from "@/shared/lib/formatTime";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * ДЕФЕКТ 19: один момент — разные часы на соседних экранах.
 *
 * Проверяющий отправил сообщение и увидел «21:24» в списке чатов и ленте, а в
 * «Разборе диалогов» — «11.08 14:24». Колонка честно подписана «(МСК)», но
 * человек, сверяющий два экрана, каждый раз вычитал семь часов в уме.
 *
 * Менять пояс нельзя ни на одном из экранов: оператору нужны его собственные
 * часы («до обеда это было или после»), отчёту — общие («в споре о времени
 * ответа два руководителя обязаны называть одно число»). Значит лечится
 * подпись, и она обязана быть ОДНОЙ на оба экрана.
 *
 * ПОЯС ВЕЗДЕ ЗАДАН ЯВНО. Часы машины, на которой идут тесты, неизвестны: у
 * разработчика UTC+10, на сборке скорее всего UTC. Тест, полагающийся на них,
 * зеленел бы в одном месте и падал в другом — и падал бы не по делу.
 */

// 11 августа 2026, 11:24 UTC — то же самое сообщение, что видел проверяющий:
// 14:24 в Москве и 21:24 у него.
const SENT_AT = "2026-08-11T11:24:00Z";
const VLADIVOSTOK = "Asia/Vladivostok"; // UTC+10, как у проверяющего
const KALININGRAD = "Europe/Kaliningrad"; // UTC+2 — час НЕ московский, но рядом
const SIMFEROPOL = "Europe/Simferopol"; // UTC+3 — часы московские, имя другое

describe("Подпись часового пояса", () => {
  it("называет оба пояса теми самыми числами из отчёта проверяющего", () => {
    expect(zonedTimeTitle(SENT_AT, VLADIVOSTOK)).toBe("11.08 21:24 у вас · 11.08 14:24 по Москве");
  });

  it("дата стоит у обеих половин: сутки расходятся", () => {
    // 04.08 19:31 по Москве — это уже пятое августа у сотрудника на UTC+10.
    // Без даты у первой половины подпись читалась бы опечаткой.
    expect(zonedTimeTitle("2026-08-04T16:31:20Z", VLADIVOSTOK)).toBe(
      "05.08 02:31 у вас · 04.08 19:31 по Москве",
    );
  });

  it("у сотрудника с московскими часами второй раз то же самое не пишется", () => {
    // Иначе «14:24 у вас · 14:24 по Москве» выглядит поломкой и приучает
    // подсказку не читать — а не читают её как раз тогда, когда она нужна.
    expect(zonedTimeTitle(SENT_AT, MSK_TIME_ZONE)).toBe("11.08 14:24 по Москве");
    // Совпадение считается по ПОКАЗАННОМУ времени, а не по имени зоны: у
    // Симферополя те же часы, и говорить о разнице не о чем.
    expect(zonedTimeTitle(SENT_AT, SIMFEROPOL)).toBe("11.08 14:24 по Москве");
  });

  it("разница даже в один час названа полностью", () => {
    expect(zonedTimeTitle(SENT_AT, KALININGRAD)).toBe("11.08 13:24 у вас · 11.08 14:24 по Москве");
  });

  it("нет момента — нет и подписи, а не пустая подсказка", () => {
    expect(zonedTimeTitle(null, VLADIVOSTOK)).toBeUndefined();
    expect(zonedTimeTitle(undefined, VLADIVOSTOK)).toBeUndefined();
    expect(zonedTimeTitle("совсем не дата", VLADIVOSTOK)).toBeUndefined();
  });

  it("незнакомое имя зоны не роняет строку списка", () => {
    // Intl отвергает такое исключением. Уронить из-за этого весь левый список
    // нельзя: откатываемся на московское время, и оно честно подписано.
    expect(zonedTimeTitle(SENT_AT, "Мордор/Барад-дур")).toBe("11.08 14:24 по Москве");
  });

  it("формат московского времени — тот же, что печатает таблица", () => {
    expect(formatInZone(SENT_AT, MSK_TIME_ZONE)).toBe("11.08 14:24");
    expect(formatInZone("совсем не дата", MSK_TIME_ZONE)).toBe("");
  });
});

/**
 * ПОЧЕМУ ЭТОТ БЛОК ОСТАЛСЯ, ХОТЯ НАДПИСИ БОЛЬШЕ НЕТ.
 *
 * Строку «Время в таблице московское: сейчас в Москве…» с «Разбора диалогов»
 * убрали при перекройке: про пояс там говорили ТРИЖДЫ (эта строка, «(МСК)» в
 * заголовке колонки и подсказка у ячейки), а теперь говорит один переключатель
 * — тот, который пояс и меняет.
 *
 * Сама функция при этом жива и нужна: экран спрашивает у неё «а есть ли о чём
 * говорить» — `null` означает, что часы сотрудника и московские совпали, и
 * выбора предлагать не надо. Проверяется здесь ровно это её свойство, и оно
 * важнее прежнего текста: ошибись она в одну сторону — переключатель пропадёт
 * у того, кому он нужен; в другую — появится выбор без разницы.
 */
describe("«Часы сотрудника и московские расходятся?»", () => {
  const now = new Date("2026-08-11T11:24:00Z");

  it("расхождение названо, и названо обоими часами", () => {
    expect(moscowZoneHint(now, VLADIVOSTOK)).toBe(
      "Время в таблице московское: сейчас в Москве 14:24, у вас 21:24.",
    );
  });

  it("у сотрудника с московскими часами ответ пустой", () => {
    // Сравниваются ПОКАЗАННЫЕ часы, а не имена зон: у Симферополя имя своё, а
    // часы московские — предлагать ему выбор значит заставлять проверять, не
    // показалось ли ему.
    expect(moscowZoneHint(now, MSK_TIME_ZONE)).toBeNull();
    expect(moscowZoneHint(now, SIMFEROPOL)).toBeNull();
  });
});

describe("Время в строке списка чатов", () => {
  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "t", bootstrapped: true });
  });

  it("подписано тем же текстом, что и ячейка «Разбора диалогов»", () => {
    const row: ConversationDto = {
      id: "conv-1",
      status: "in_progress",
      channel: "avito",
      account: { id: "acc-1", title: "LP-Москва" },
      client: { id: "client-1", name: "Ольга Никитина", phone: null, avito_rating: null },
      assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
      item: { title: "Ремонт ноутбуков", url: null, price: null },
      last_message: { body: "Спасибо!", direction: "out", created_at: SENT_AT },
      unread_count: 0,
      bot_active: false,
      tags: [],
      transferred_to_me: false,
      last_message_at: SENT_AT,
    };
    renderWithProviders(
      <ConversationListItem row={row} active={false} now={Date.parse(SENT_AT)} onOpen={() => {}} showChannel />,
    );

    // Пояс машины здесь неизвестен, поэтому проверяется не буквальная строка, а
    // то единственное, что делает сверку двух экранов возможной: московская
    // половина подписи есть всегда — как раз её и печатает «Разбор диалогов».
    expect(document.querySelector(".wait-gauge")?.getAttribute("title")).toContain(
      `${formatInZone(SENT_AT, MSK_TIME_ZONE)} по Москве`,
    );
  });
});
