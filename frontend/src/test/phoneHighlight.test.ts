import { describe, expect, it } from "vitest";
import {
  candidatesInMessage,
  splitPhones,
  type PhoneCandidateDto,
} from "@/shared/lib/phoneHighlight";

/**
 * РАЗРЕЗКА ТЕКСТА ПОД ПОДСВЕТКУ.
 *
 * Главная проверка здесь — не «нашёлся номер», а СКЛЕЙКА: куски обязаны
 * складываться обратно в исходную строку знак в знак. Номер из переписки
 * копируют мышью, чтобы позвонить, и один добавленный или потерянный символ
 * означает, что в буфер обмена уедет не то, что написал клиент.
 *
 * Второе, ради чего файл существует: здесь НЕТ ни одного правила разбора. Они
 * живут на сервере (`app/services/phone_parse.py`), и проверять их копию тут
 * значило бы завести ту самую вторую копию, от которой мы уходим.
 */

function candidate(overrides: Partial<PhoneCandidateDto> = {}): PhoneCandidateDto {
  return {
    id: "cand-1",
    phone: "+79001112240",
    raw: "+7(900)1112240",
    conversation_id: "conv-1",
    message_id: "msg-1",
    message_at: "2026-08-12T13:35:00Z",
    detected_at: "2026-08-12T13:35:01Z",
    source: "inbound",
    status: "pending",
    ...overrides,
  };
}

describe("разрезка текста сообщения под подсветку номера", () => {
  it("склеивается обратно в исходный текст знак в знак", () => {
    const text = "В любое время. Прошу сообщить в СМС по номеру : +7(900)1112240.";
    const parts = splitPhones(text, ["+7(900)1112240"]);

    expect(parts.map((p) => p.text).join("")).toBe(text);
    expect(parts.filter((p) => p.phone).map((p) => p.text)).toEqual(["+7(900)1112240"]);
  });

  it("ищет подстрокой, а не регулярным выражением: скобки и плюс — не шаблон", () => {
    // `+7(900)1112240` как выражение означало бы «одна или больше семёрок,
    // дальше одна из цифр 9, 1, 5» — и совпало бы не там.
    const text = "звоните +7(900)1112240 или 79001112240";
    const parts = splitPhones(text, ["+7(900)1112240"]);

    expect(parts.map((p) => p.text).join("")).toBe(text);
    expect(parts.filter((p) => p.phone)).toHaveLength(1);
    expect(parts.find((p) => p.phone)?.text).toBe("+7(900)1112240");
  });

  it("два номера в одной фразе подсвечиваются оба и по порядку", () => {
    const text = "мой 8 900 111 22 40, жены 8 916 000 11 22";
    const parts = splitPhones(text, ["8 900 111 22 40", "8 916 000 11 22"]);

    expect(parts.map((p) => p.text).join("")).toBe(text);
    expect(parts.filter((p) => p.phone).map((p) => p.text)).toEqual([
      "8 900 111 22 40",
      "8 916 000 11 22",
    ]);
  });

  it("один и тот же номер, названный дважды, подсвечен дважды", () => {
    const text = "89001112240 — записал? 89001112240";
    const parts = splitPhones(text, ["89001112240"]);

    expect(parts.map((p) => p.text).join("")).toBe(text);
    expect(parts.filter((p) => p.phone)).toHaveLength(2);
  });

  it("текст без совпадений остаётся одним куском", () => {
    // Кандидат приехал, а его записи в тексте нет (сообщение исправлено на
    // площадке, разошлась обрезка). Подсветки нет — выдумывать её место нельзя.
    const text = "Позвоните мне вечером";
    expect(splitPhones(text, ["89001112240"])).toEqual([{ text, phone: false }]);
  });

  it("пустая запись номера не вешает разрезку", () => {
    // `indexOf("")` совпадает в каждой позиции; без отдельной проверки цикл
    // поиска не кончится никогда, и экран замрёт вместе с ним.
    const text = "нет номера";
    expect(splitPhones(text, [""])).toEqual([{ text, phone: false }]);
  });

  it("перекрывающиеся записи не рвут текст: берётся первая", () => {
    const text = "звоните 89001112240 срочно";
    const parts = splitPhones(text, ["89001112240", "9001112240"]);

    expect(parts.map((p) => p.text).join("")).toBe(text);
    expect(parts.filter((p) => p.phone).map((p) => p.text)).toEqual(["89001112240"]);
  });
});

describe("кандидаты этого сообщения", () => {
  it("берутся только свои и только ожидающие решения", () => {
    const mine = candidate();
    const alien = candidate({ id: "cand-2", message_id: "msg-2" });
    // Отклонённый когда-то номер предлагать заново нельзя: оператор уже
    // сказал «это не он», и вопрос вернулся бы после каждого пересчёта.
    const rejected = candidate({ id: "cand-3", status: "rejected" });

    expect(candidatesInMessage([mine, alien, rejected], "msg-1")).toEqual([mine]);
  });

  it("нет ответа сервера — нет и кандидатов", () => {
    expect(candidatesInMessage(undefined, "msg-1")).toEqual([]);
  });
});
