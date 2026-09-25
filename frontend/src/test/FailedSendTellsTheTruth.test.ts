/**
 * Неудавшаяся отправка не должна выглядеть удачной (27.08).
 *
 * Две находки обхода кода, механизм у них общий: оптимистичное состояние
 * применяется широко, а откат — узко.
 *
 * ⚠ СПИСОК СЛЕВА. `onMutate` переписывает строку во ВСЕХ закэшированных
 * списках: превью «Вы: …», `last_message_at` — время нажатия, строка уезжает
 * наверх. Откат трогал только ленту и деталь. Отправка не прошла, у пузыря
 * красный крест — а слева диалог первый, с подписью «Вы: …». Оператор уверен,
 * что ответил; клиент ждёт. Само оно не перерисуется: `staleTime` тридцать
 * секунд, `refetchOnWindowFocus` выключен.
 *
 * ⚠ ЗАМЕТКА В КАРТОЧКЕ. Список отбирался по одному направлению, а
 * оптимистичный пузырь рождается с тем же направлением и при отказе остаётся с
 * `failed`. Счётчик «Заметки (N)» считал такие наравне с настоящими.
 */
import { describe, expect, it } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";

function код(путь: string): string {
  return (readFileSync(путь, "utf-8") as string)
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ");
}

describe("Отказ отправки говорит правду", () => {
  it("список слева перезапрашивается при отказе", () => {
    const src = код("src/features/chats/hooks/useSendMessage.ts");
    const at = src.indexOf("onError");
    expect(at, "onError исчез").toBeGreaterThan(-1);
    expect(src.slice(at), "строка списка останется с «Вы: …» и наверху").toContain(
      "scheduleListRefetch()",
    );
  });

  it("счётчик заметок не считает неотправленные", () => {
    const src = код("src/features/chats/components/card/ClientCardPane.tsx");
    // Отбор обязан смотреть на состояние доставки, а не только на направление.
    expect(src).toMatch(/delivery_status !== "failed"/);
    expect(src).toMatch(/delivery_status === "failed"/);
  });

  it("неотправленная заметка всё же показана — текст не пропадает молча", () => {
    const src = код("src/features/chats/components/card/ClientCardPane.tsx");
    expect(src).toContain("не сохранилась");
    expect(src).toContain('data-failed="true"');
  });

  it("у неотправленной заметки свой вид, а не общий", () => {
    const css = код("src/features/chats/components/card/client-card.css");
    expect(css).toMatch(/\.card-notes__item\[data-failed\]/);
  });
});
