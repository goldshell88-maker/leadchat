import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { qk } from "@/shared/api/queryKeys";
import type { ClientRef, ConversationDetailDto } from "@/shared/api/types";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import { formatPhone } from "@/features/chats/components/card/phone";
import { fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { CONV_ID, makeConversation, renderWithProviders } from "./render";

/**
 * КАРТОЧКА НЕ ВЫДАЁТ НАШИ ЗАГЛУШКИ ЗА ДАННЫЕ АВИТО И ПОКАЗЫВАЕТ ТЕЛЕФОН
 * ПО-ЧЕЛОВЕЧЕСКИ (дефекты аудита 16 и 17).
 *
 * ДЕФЕКТ 16. Блок называется «Клиент на Авито» и стоит вплотную к «ID
 * 923456789» — всё, что в нём, читается как присланное Авито. А первой строкой
 * там стояло наше слово-заглушка «Клиент». Имени у свежего обращения нет
 * штатно: вебхук v3 его не несёт, до первой сверки `client.name` = null. То
 * есть карточка сообщала то, чего Авито не отдавал, чаще всего именно на самых
 * новых диалогах.
 *
 * ДЕФЕКТ 17. Телефон показывался как в базе — «+79125550177», одиннадцать
 * цифр подряд. В тексте того же диалога клиент писал его читаемо: «+7 912
 * 555-01-77». Номер сверяют глазами и диктуют мастеру вслух, ошибка в одной
 * цифре — выезд не по адресу.
 *
 * ЧТО ЛОМАЛИ, ЧТОБЫ УБЕДИТЬСЯ, ЧТО ТЕСТЫ РАБОТАЮТ (каждый краснел):
 *  - вернули `<Text className="card-section__name">{client.name}</Text>` в блок
 *    «Клиент» — падают оба теста про имя;
 *  - убрали `formatPhone` из разметки телефона — падает «телефон показан по
 *    группам»;
 *  - подставили `formatPhone` в `href="tel:"` — падает «в набор уходят цифры
 *    без пробелов»;
 *  - в `RU_E164` сняли якорь `$` (маска стала цепляться к длинным строкам) —
 *    падает «незнакомую форму не трогаем».
 */

const PHONE_RAW = "+79125550177";
const PHONE_HUMAN = "+7 912 555-01-77";

function withClient(client: Partial<ClientRef>): ConversationDetailDto {
  return makeConversation({
    client: { id: "client-1", name: "Иван Петров", phone: null, avito_rating: null, ...client },
  });
}

describe("formatPhone — телефон для глаз", () => {
  it("режет российский номер на группы", () => {
    expect(formatPhone(PHONE_RAW)).toBe(PHONE_HUMAN);
  });

  it("незнакомую форму отдаёт как есть", () => {
    // Сервер нормализует к +7XXXXXXXXXX, но обещание живёт в его коде, а не в
    // типе. Нарезать чужую форму по русской маске — значит показать НЕ ТОТ
    // номер, а это хуже нечитаемого.
    for (const odd of ["+380501234567", "89125550177", "+7912555017", "+791255501777", "позвонить в среду", ""]) {
      expect(formatPhone(odd)).toBe(odd);
    }
  });
});

describe("Карточка клиента — имя профиля и телефон", () => {
  beforeEach(() => {
    queryClient.clear();
    resetSessionStore({
      user: fakeUser,
      permissions: ["conversations:read", "messages:send", "notes:read"],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { items: [] })),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function render(conversation: ConversationDetailDto) {
    queryClient.setQueryData(qk.conversations.detail(CONV_ID), conversation);
    return renderWithProviders(<ClientCardPane convId={CONV_ID} />);
  }

  /** Блок «Клиент» целиком — вся личность человека теперь живёт в нём. */
  function clientSection(): HTMLElement {
    const section = screen.getByText("Клиент", { selector: ".card-section__title" }).closest("section");
    if (!section) throw new Error("блок «Клиент» не найден");
    return section;
  }

  /*
   * ДЕФЕКТ 16 ЗАКРЫТ ТРЕТЬИМ ЗАХОДОМ (правка 10 от 12 августа).
   *
   * 11 августа блок «Клиент на Авито» перестал подставлять нашу заглушку
   * «Клиент» вместо имени профиля и писал «Авито не прислал имя профиля».
   * Правка 9 убрала сам блок: он повторял имя вторым экземпляром, а при
   * отсутствии имени занимал целую карточку, чтобы сообщить об отсутствии
   * данных.
   *
   * Разбор живого ЭКРАНА (а не карточки в отдельности) показал третий
   * экземпляр: заголовок чата и заголовок карточки печатают одно имя в
   * трёхстах пикселях друг от друга. Имя оставлено шапке — она шире и стоит
   * над лентой, где имя и нужно; в карточке хватает подписи «Клиент».
   *
   * ЕДИНСТВЕННОЕ ИСКЛЮЧЕНИЕ — экран у́же 768px, где карточка накрывает шапку
   * собой. Оно проверяется в CardNoDuplicates.test.tsx, вместе с остальными
   * дублями.
   */
  it("имя дублируется в карточке (решение владельца 17.08)", () => {
    render(withClient({ name: "Ольга Никитина", external_id: "777042" }));

    expect(screen.queryAllByText("Ольга Никитина").length).toBeGreaterThan(0);
    expect(within(clientSection()).getByText("ID 777042")).toBeInTheDocument();
  });

  it("без имени рядом с идентификатором не появляется наше слово", () => {
    render(withClient({ name: null, external_id: "923456789" }));

    // Идентификатор — сам по себе, без придуманного «имени профиля» рядом.
    expect(screen.getByText("ID 923456789")).toBeInTheDocument();
    expect(screen.queryByText("Авито не прислал имя профиля")).toBeNull();
  });

  it("телефон показан по группам", () => {
    render(withClient({ phone: PHONE_RAW }));

    expect(screen.getByText(PHONE_HUMAN)).toBeInTheDocument();
    expect(screen.queryByText(PHONE_RAW)).toBeNull();
  });

  it("в набор и в буфер уходят цифры без пробелов", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(withClient({ phone: PHONE_RAW }));

    // `tel:` и буфер разбирают набиратель, CRM и мессенджер на той стороне:
    // +7XXXXXXXXXX они понимают всегда, номер с пробелами — как повезёт.
    expect(screen.getByText(PHONE_HUMAN).closest("a")).toHaveAttribute("href", `tel:${PHONE_RAW}`);

    screen.getByLabelText("Скопировать телефон").click();
    expect(writeText).toHaveBeenCalledWith(PHONE_RAW);
  });
});
