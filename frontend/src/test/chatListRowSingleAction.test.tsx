import { beforeEach, describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { ConversationListItem } from "@/features/chats/components/list/ConversationListItem";
import type { ConversationDto } from "@/shared/api/types";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * СТРОКА СПИСКА ПОСЛЕ ПЕРЕКРОЙКИ 12 АВГУСТА (разбор живого экрана владельцем).
 *
 * Разбор нашёл в строке три лишние сущности разом:
 *
 *  1. ХОВЕР-ИКОНКА «ПЕРЕДАТЬ ДИАЛОГ». Четвёртое место одного действия: те же
 *     слова стояли иконкой в шапке ленты, кнопкой в правой карточке и пунктом
 *     меню. Человек переставал понимать, какая из четырёх «настоящая».
 *  2. ДВА ЗНАЧЕНИЯ ВРЕМЕНИ. У брошенного диалога чип печатал «никто не берёт ·
 *     25 мин», а шкала справа в той же строке — «25м». Хуже дубля: слагаемые
 *     считались из разных источников (`waiting_human` от сервера против
 *     собственного отсчёта) и расходились на минуту-другую.
 *  3. ОБЪЯВЛЕНИЕ В ПОДСТРОЧНИКЕ. Оно слово в слово повторяло блок «Заявка» в
 *     правой карточке, занимало всю ширину и вытесняло канал — единственное,
 *     что по списку и решается: каналов девять, у каждого свои люди.
 *
 * Тесты ниже стерегут каждое из трёх: вернётся любое — упадёт своё.
 */

const NOW = Date.parse("2026-08-12T10:00:00Z");

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
      body: "А сколько будет стоить замена насоса?",
      direction: "in",
      created_at: "2026-08-12T09:35:00Z",
    },
    unread_count: 1,
    bot_active: false,
    tags: [],
    transferred_to_me: false,
    last_message_at: "2026-08-12T09:35:00Z",
    ...overrides,
  };
}

function renderRow(data: ConversationDto) {
  return renderWithProviders(
    <ConversationListItem row={data} active={false} now={NOW} onOpen={() => {}} showChannel />,
  );
}

describe("Строка списка: одно действие, одно время, канал вместо объявления", () => {
  beforeEach(() => {
    resetSessionStore({ user: fakeUser, accessToken: "test-token", bootstrapped: true });
  });

  it("в строке ровно одна кнопка — открыть диалог", () => {
    const { container } = renderRow(row());

    // Не «нет кнопки с таким именем», а «кнопка ровно одна»: проверка обязана
    // падать и на любое НОВОЕ действие, которое кто-нибудь захочет сюда
    // вернуть, — «Закрыть», «В чёрный список», «Закрепить».
    const buttons = container.querySelectorAll("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAttribute("aria-label", "Диалог: Иван Петров, непрочитанных: 1");
    expect(screen.queryByLabelText(/Передать диалог/)).toBeNull();
  });

  it("ховер-действия не появляются и в разметке их нет", () => {
    const { container } = renderRow(row());

    // Класс проверяем нарочно: блок был скрыт прозрачностью и всплывал по
    // :hover, то есть отсутствие его на экране ещё не значит отсутствия в
    // разметке — он оставался в Tab-обходе и на нём срабатывал Enter.
    expect(container.querySelector(".conv-card__actions")).toBeNull();
    expect(container.querySelector(".conv-card__action")).toBeNull();
  });

  it("у брошенного диалога время сказано ОДИН раз — шкалой, не чипом", () => {
    // Клиент ждёт 25 минут, диалог никто не взял. `waitingForRow` считает от
    // `last_message_at` при непрочитанных, поэтому фикстура именно такая.
    renderRow(
      row({
        escalated: true,
        unread_count: 2,
        last_message_at: new Date(NOW - 25 * 60_000).toISOString(),
        waiting_human: "25 мин",
      }),
    );

    // Чип говорит ТОЛЬКО про то, чего шкала сказать не может.
    expect(screen.getByText("никто не берёт")).toBeInTheDocument();
    // И ни одного второго срока рядом: ни серверного «25 мин», ни своего.
    expect(screen.queryByText(/никто не берёт ·/)).toBeNull();
    expect(screen.queryByText("25 мин")).toBeNull();

    // Само время при этом никуда не делось — оно в шкале, и оно одно.
    expect(screen.getByText("25м")).toBeInTheDocument();
  });

  it("в подстрочнике канал, а объявления нет", () => {
    renderRow(row());

    expect(screen.getByText("Парт - 7")).toBeInTheDocument();
    expect(screen.queryByText(/Ремонт стиральной машины Bosch/)).toBeNull();
    expect(screen.queryByText(/без объявления/)).toBeNull();
  });

  it("чужой ответственный в подстрочнике остаётся — по нему решают, не открывая", () => {
    // Единственное, что в подстрочнике имеет право стоять рядом с каналом:
    // на вкладке «Все» это ответ на вопрос «кто уже ведёт».
    renderRow(row({ assignee: { id: "u-9", full_name: "Пётр Ковалёв" } }));

    expect(screen.getByText("Парт - 7 · Пётр Ковалёв")).toBeInTheDocument();
  });
});
