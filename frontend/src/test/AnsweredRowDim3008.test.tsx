import { beforeEach, describe, expect, it } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import type { ConversationDto } from "@/shared/api/types";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * «ЭТОТ ЧАТ СЕРЫЙ, ЗДЕСЬ Я СВОЮ РАБОТУ СДЕЛАЛ» (просьба владельца 30.08).
 *
 * Жалоба: менеджеры не понимают, ответили они клиенту или нет, и вычитывают
 * превью последнего сообщения в каждой строке, чтобы понять, чья очередь.
 *
 * ЧТО ЗДЕСЬ СТОРОЖИТСЯ И ПОЧЕМУ ИМЕННО ЭТО.
 *
 * 1. Приглушение считается по КАНОНИЧЕСКОМУ полю `waiting_since`, а не по
 *    направлению последнего сообщения. Метку последнего сообщения двигают
 *    служебные записи Авито и наши заметки: строка «Вы: …» может стоять над
 *    неотвеченным вопросом клиента, и погасить её значило бы спрятать работу.
 *
 * 2. Строка без `waiting_since` (старый кэш, офлайн-снимок десктопа) НЕ
 *    гаснет. Там `waitingFor` уходит в запасной расчёт по непрочитанным, а тот
 *    считает отвеченным любой диалог, в который просто заглянули.
 *
 * 3. Приглушение сделано ЦВЕТОМ, а не `opacity` на карточке. Владелец просил
 *    прозрачность 0.6 на всю карточку — этот приём в проекте уже ронял
 *    контраст: прозрачность на контейнере создаёт групповую композицию, и
 *    красный бейдж очереди ушёл на 2.49:1 (разбор в chat-list.css над
 *    `.chat-tabs[data-dimmed]`).
 */

const NOW = Date.parse("2026-08-30T10:00:00Z");

function row(overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    id: "conv-1",
    status: "in_progress",
    channel: "avito",
    account: { id: "acc-1", title: "Парт - 7" },
    client: { id: "client-1", name: "Иван Петров", phone: null, avito_rating: null },
    assignee: { id: fakeUser.id, full_name: fakeUser.full_name },
    item: { title: "Ремонт стиральной машины Bosch", url: null, price: null },
    last_message: {
      body: "Хорошо, ждём мастера",
      direction: "out",
      created_at: "2026-08-30T09:35:00Z",
    },
    unread_count: 0,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-30T09:35:00Z",
    ...overrides,
  };
}

function карточка(data: ConversationDto): HTMLElement {
  renderWithProviders(
    <ConversationListItem row={data} active={false} now={NOW} onOpen={() => {}} showChannel />,
  );
  const el = document.querySelector(".conv-card");
  if (!el) throw new Error("строка списка не отрисовалась");
  return el as HTMLElement;
}

describe("Отвеченная строка гаснет", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
  });

  it("мы ответили — строка помечена как погашенная", () => {
    const el = карточка(row({ waiting_since: null }));
    expect(
      el.getAttribute("data-answered"),
      "строка не погасла — менеджеру снова вычитывать превью",
    ).toBe("true");
  });

  it("клиент ждёт ответа — строка яркая", () => {
    const el = карточка(
      row({ waiting_since: "2026-08-30T09:50:00Z", unread_count: 1, last_message: {
        body: "А сколько будет стоить замена насоса?",
        direction: "in",
        created_at: "2026-08-30T09:50:00Z",
      } }),
    );
    expect(el.hasAttribute("data-answered"), "погасили строку, где клиент ждёт ответа").toBe(false);
  });

  it("закрытый диалог не гаснет — вкладка «Закрытые» иначе стала бы сплошь серой", () => {
    const el = карточка(row({ waiting_since: null, status: "closed" }));
    expect(el.hasAttribute("data-answered")).toBe(false);
  });

  it("строка из старого кэша остаётся яркой", () => {
    /*
     * ⚠ ГЛАВНАЯ ИЗ ЧЕТЫРЁХ. Здесь `waiting_since` нет вовсе, и `waitingFor`
     * вернёт «не ждёт» просто потому, что непрочитанных ноль, — то есть и для
     * диалога, в который заглянули и не ответили. Погаснув по такой догадке,
     * строка молча спрятала бы неотвеченного клиента.
     */
    const данные = row();
    delete (данные as { waiting_since?: string | null }).waiting_since;
    const el = карточка(данные);
    expect(
      el.hasAttribute("data-answered"),
      "погасили по догадке: у строки нет канонического поля ожидания",
    ).toBe(false);
  });

  it("непрочитанное не гасится ни при каких условиях", () => {
    // Счётчик оператора и каноническое ожидание считаются разными путями.
    // Разойдутся — выигрывает непрочитанное: лишняя тревога дешевле немой.
    const стили = readFileSync("src/features/chats/components/list/chat-list.css", "utf-8") as string;
    const правила = [...стили.matchAll(/([^{}]*\[data-answered\][^{}]*)\{([^}]*)\}/g)];
    expect(правила.length, "правил приглушения нет вовсе").toBeGreaterThan(0);
    /*
     * ⚠ СЕЛЕКТОРЫ РАЗБИРАЮТСЯ ПООДИНОЧКЕ, А НЕ ПРАВИЛОМ ЦЕЛИКОМ. Первая версия
     * проверяла всю строку селекторов сразу — и оставалась зелёной, когда
     * защиту снимали лишь у одного из перечисленных через запятую: `:not(...)`
     * находился у соседа. Заметила это диверсия.
     */
    const гасящие = правила.filter(([, , тело]) => /color|opacity/.test(тело));
    for (const [, список] of гасящие) {
      for (const селектор of список.split(",")) {
        if (!селектор.includes("[data-answered]")) continue;
        if (/:hover|:focus-within|\[data-active\]/.test(селектор)) continue;
        expect(
          селектор,
          `селектор «${селектор.trim()}» гасит строку, не проверив непрочитанное`,
        ).toMatch(/:not\(\[data-unread\]\)/);
      }
    }
  });

  it("прозрачность не навешена на саму карточку", () => {
    /*
     * ⚠ ЗАЩИТА ОТ ПОВТОРА ИЗВЕСТНОГО ДЕФЕКТА. `opacity` на `.conv-card`
     * создаёт групповую композицию и роняет ВСЁ поддерево разом — чипы,
     * бейджи, счётчики. На вкладках это уже стоило контраста 2.49:1.
     * Прозрачность разрешена только аватару: он картинка и текст не роняет.
     */
    const стили = readFileSync("src/features/chats/components/list/chat-list.css", "utf-8") as string;
    const правила = [...стили.matchAll(/([^{}]*\[data-answered\][^{}]*)\{([^}]*)\}/g)];
    for (const [, список, тело] of правила) {
      if (!/opacity/.test(тело)) continue;
      for (const селектор of список.split(",")) {
        if (!селектор.includes("[data-answered]")) continue;
        expect(
          селектор,
          `«${селектор.trim()}» гасит прозрачностью не аватар, а всё поддерево — так уже роняли контраст до 2.49:1`,
        ).toMatch(/__avatar/);
      }
    }
  });
});

/*
 * ⚠ СТРОКА СПИСКА НЕ ГОВОРИТ «ВЛОЖЕНИЕ» ВМЕСТО СМЫСЛА (07.09).
 *
 * С 07.09 служебная подпись про непоказуемое содержимое живёт вложением, а не
 * телом. Строка списка знала только тело и на видео, файле и звонке сказала бы
 * голое «Вложение» — то есть потеряла бы ровно ту мысль, ради которой подпись
 * и существует. Замер боя: у 1317 диалогов последнее сообщение как раз такое.
 * Это возврат беды 11 августа в то самое место, ради которого её и чинили.
 *
 * ⚠ ДИВЕРСИЯ: убрать `lm.attachment_name?.trim()` из `previewText`
 * (ConversationListItem.tsx) — проверка краснеет: в строке «Вложение».
 */
describe("Превью строки берёт имя вложения, когда тела нет", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
  });

  it("видео без тела названо словами, а не «Вложение»", () => {
    const el = карточка(
      row({
        last_message: {
          body: null,
          direction: "in",
          attachment_name: "Видео — посмотреть можно только в приложении Авито",
          created_at: "2026-08-30T09:35:00Z",
        },
      }),
    );
    expect(el.textContent).toContain("Видео — посмотреть можно только в приложении Авито");
    expect(el.textContent).not.toContain("Вложение");
  });

  it("а без имени и без тела остаётся честное «Вложение»", () => {
    // Запас на случай, которого сервер больше не создаёт: имя пропало, но
    // сказать что-то надо — иначе строка пустая, и человек идёт открывать.
    const el = карточка(
      row({
        last_message: { body: null, direction: "in", created_at: "2026-08-30T09:35:00Z" },
      }),
    );
    expect(el.textContent).toContain("Вложение");
  });
});
