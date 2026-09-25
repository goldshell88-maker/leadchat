import { describe, expect, it } from "vitest";
import type { MessageDto } from "@/shared/api/types";
import {
  buildThreadRows,
  eventKind,
  isStatusLogMessage,
  stripWaited,
} from "@/features/chats/components/thread/threadEvents";

/**
 * СТОРОЖ СВЁРНУТОГО ЖУРНАЛА СТАТУСОВ.
 *
 * ЧТО БЫЛО (разбор живого экрана владельцем, 12 августа). В открытом диалоге
 * подряд стояли двенадцать служебных плашек — «Диалог принят», «Диалог
 * возвращён во «Входящие»», «Диалог отклонён», «Отказ отменён» и так далее, —
 * и они вытеснили сообщения клиента за нижний край экрана. Оператор открывал
 * диалог и вместо вопроса, на который надо ответить, читал историю нажатий
 * коллег.
 *
 * Проверяется здесь ЧИСТАЯ функция, а не экран: раскладка от неё не зависит,
 * зато зависит всё остальное — сколько строк в ленте, какие из них журнал и
 * что в этом журнале погашено.
 */

let seq = 0;

function msg(overrides: Partial<MessageDto> = {}): MessageDto {
  seq += 1;
  return {
    id: `m-${seq}`,
    conversation_id: "conv-1",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: "Здравствуйте",
    attachments: [],
    delivery_status: "delivered",
    client_message_id: null,
    created_at: "2026-08-12T10:00:00Z",
    ...overrides,
  };
}

function sys(body: string, created_at = "2026-08-12T10:00:00Z"): MessageDto {
  return msg({ direction: "system", sender_type: "system", body, created_at });
}

describe("«Ждал N» — метрика диалога, а не событие", () => {
  it("хвост об ожидании срезается", () => {
    // Сколько клиент ждёт, видно в чипе статуса, и там значение живое. В
    // ленте застывшее число ещё и спорит с шапкой: «Ждал 7 мин» рядом с
    // «ждёт 40 мин» — два ответа на один вопрос.
    expect(stripWaited("Диалог принят: Ольга. Ждал 7 мин")).toBe("Диалог принят: Ольга");
    expect(stripWaited("Диалог принят: Ольга. Ждал 45 с")).toBe("Диалог принят: Ольга");
    expect(stripWaited("Диалог принят: Ольга. Ждал 2 ч 30 мин")).toBe("Диалог принят: Ольга");
    expect(stripWaited("Диалог принят: Ольга. Ждал 3 дн 4 ч")).toBe("Диалог принят: Ольга");
  });

  it("похожий по словам ХВОСТ ПО СМЫСЛУ не трогает", () => {
    // «Причина: клиент ждал ответа» — законный хвост записи об отказе, и
    // отрезать его значило бы потерять то единственное, ради чего причину и
    // спрашивают.
    const text = "Диалог отклонён: Пётр. Причина: клиент ждал ответа";
    expect(stripWaited(text)).toBe(text);
    expect(stripWaited("Диалог принят: Ольга")).toBe("Диалог принят: Ольга");
  });
});

describe("Что считается журналом", () => {
  it("наша системная запись — журнал", () => {
    expect(isStatusLogMessage(sys("Диалог принят: Ольга"))).toBe(true);
  });

  it("служебное сообщение АВИТО журналом не считается", () => {
    // «Заказ отменён» — чужое действие, о котором оператор не узнает ниоткуда
    // больше. Читать его надо сразу, а не после нажатия «развернуть».
    const avito = msg({ direction: "system", sender_type: "avito", body: "Заказ отменён" });
    expect(isStatusLogMessage(avito)).toBe(false);
    expect(buildThreadRows([msg(), avito]).every((r) => r.kind === "message")).toBe(true);
  });
});

describe("Сборка строк ленты", () => {
  it("серия из двенадцати записей занимает ОДНУ строку", () => {
    const flat = [
      msg({ body: "Сломалась стиральная машина" }),
      ...Array.from({ length: 12 }, (_, i) => sys(`Диалог передан: А → Б ${i}`)),
      msg({ body: "Так вы приедете?" }),
    ];

    const rows = buildThreadRows(flat);

    // Было бы 14 строк, и два сообщения клиента разъехались бы на два экрана.
    expect(rows).toHaveLength(3);
    expect(rows[1].kind).toBe("events");
    if (rows[1].kind === "events") expect(rows[1].events).toHaveLength(12);
  });

  it("одиночная запись остаётся обычным чипом", () => {
    // «Диалог передан: Борис → Ольга» — ответ на вопрос «почему диалог у
    // меня». Спрятать его за нажатие значит поменять шум на загадку.
    const rows = buildThreadRows([msg(), sys("Диалог передан: Борис → Ольга"), msg()]);
    expect(rows.map((r) => r.kind)).toEqual(["message", "message", "message"]);
  });

  it("`prev` у строки — предыдущее СООБЩЕНИЕ, а не предыдущая строка", () => {
    // На `prev` держатся дата-разделитель и схлопывание подписей автора. Если
    // подсунуть туда группу, лента потеряет разделитель дня либо нарисует его
    // дважды.
    const first = msg({ created_at: "2026-08-12T09:00:00Z" });
    const events = [sys("Диалог принят: А"), sys("Диалог отклонён: Б")];
    const last = msg({ created_at: "2026-08-12T11:00:00Z" });

    const rows = buildThreadRows([first, ...events, last]);

    expect(rows[1].prev).toBe(first);
    expect(rows[2].prev).toBe(events[1]);
  });

  it("серия не пересекает границу суток", () => {
    // Иначе внутрь свёрнутой группы попал бы дата-разделитель, и лента молча
    // съела бы переход через полночь.
    const rows = buildThreadRows([
      sys("Диалог принят: А", "2026-08-11T20:55:00Z"),
      sys("Диалог отклонён: А", "2026-08-11T20:56:00Z"),
      sys("Отказ отменён: А", "2026-08-12T21:05:00Z"),
      sys("Диалог передан: А → Б", "2026-08-12T21:06:00Z"),
    ]);

    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.kind === "events")).toBe(true);
  });

  it("ключ строки-группы стабилен и не совпадает с id сообщения", () => {
    // Ключ считает виртуализатор. Порядковый номер поехал бы при дозагрузке
    // истории вверх — и лента перерисовалась бы целиком, потеряв прокрутку.
    const a = sys("Диалог принят: А");
    const rows = buildThreadRows([a, sys("Диалог отклонён: А")]);
    expect(rows[0].key).toBe(`events:${a.id}`);
    expect(buildThreadRows([a, sys("Диалог отклонён: А")])[0].key).toBe(rows[0].key);
  });
});

describe("Противоположные соседи гасят друг друга", () => {
  function kinds(bodies: string[]) {
    const rows = buildThreadRows(bodies.map((b) => sys(b)));
    expect(rows[0].kind).toBe("events");
    if (rows[0].kind !== "events") throw new Error("не группа");
    return rows[0].events;
  }

  it("принял → вернул во «Входящие» — пара погашена", () => {
    const events = kinds(["Диалог принят: Ольга. Ждал 1 мин", "Диалог возвращён во «Входящие»: Ольга"]);
    expect(events.map((e) => e.cancelled)).toEqual([true, true]);
    // И «Ждал» из текста ушёл — его режет та же функция.
    expect(events[0].text).toBe("Диалог принят: Ольга");
  });

  it("отклонил → отказ отменён — пара погашена", () => {
    const events = kinds(["Диалог отклонён: Пётр", "Отказ отменён: Пётр"]);
    expect(events.map((e) => e.cancelled)).toEqual([true, true]);
  });

  it("каждое событие гасится не больше одного раза", () => {
    // «принят → возвращён → принят» — это одна погашенная пара и одно живое
    // принятие, а не полторы пары. Иначе журнал сказал бы, что диалог ничей,
    // когда он у Ольги.
    const events = kinds([
      "Диалог принят: Ольга",
      "Диалог возвращён во «Входящие»: Ольга",
      "Диалог принят: Ольга",
    ]);
    expect(events.map((e) => e.cancelled)).toEqual([true, true, false]);
  });

  it("непротивоположные соседи не гасятся", () => {
    const events = kinds(["Диалог принят: Ольга", "Диалог передан: Ольга → Борис"]);
    expect(events.map((e) => e.cancelled)).toEqual([false, false]);
  });

  it("незнакомая формулировка не ломает ленту, а лишь не гасится", () => {
    // Разбор текста — вынужденный: вида события в MessageOut нет. Если сервер
    // перепишет формулировку, группа обязана остаться группой.
    expect(eventKind("Что-то совсем новое случилось")).toBe("other");
    const events = kinds(["Что-то совсем новое", "И ещё одно"]);
    expect(events.map((e) => e.cancelled)).toEqual([false, false]);
  });
});
