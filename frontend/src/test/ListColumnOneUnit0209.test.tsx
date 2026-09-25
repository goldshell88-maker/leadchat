import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { WaitGauge } from "@/features/chats/components/list/WaitGauge";

/**
 * КОЛОНКА ВРЕМЕНИ ЧИТАЕТСЯ ОДНОЙ МЕРОЙ.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 02.09 СО СКРИНШОТОМ ОЧЕРЕДИ: «то, что вот так рвётся, —
 * это нормально?».
 *
 * В колонке чередовались ДВЕ РАЗНЫЕ ЕДИНИЦЫ: у ждущих оранжевый счётчик «22м»,
 * у отвеченных и закрытых — часы «12:39». Сравнить их взглядом нельзя, и список
 * читался как сломанный.
 *
 * ⚠ ПОРЯДОК ПРИ ЭТОМ БЫЛ ВЕРЕН, и это проверено на боевых данных: столбец
 * сортировки шёл строго по убыванию (03:03, 03:03, 03:02, 03:01, 02:59,
 * 02:53…), а в ячейке рядом стояли то счётчики, то часы. Чинить надо было
 * чтение, а не сортировку, — и именно поэтому сортировку я не тронул.
 *
 * ⚠ ЧТО НЕ ПОТЕРЯНО. Отличие «ждут нас» от «ответили» несёт цвет и заливка,
 * а точные часы остались в подсказке — сразу в двух поясах.
 */
describe("Колонка времени в строке списка", () => {
  it("⚠ у отвеченной строки — возраст, а не часы", () => {
    render(<WaitGauge waiting={null} time="12:39" sinceMinutes={22} timeTitle="12:39 у вас" />);
    expect(
      screen.getByText("22м"),
      "в колонке снова часы — рядом со счётчиками ждущих её нельзя прочитать одной мерой",
    ).toBeInTheDocument();
    expect(screen.queryByText("12:39")).toBeNull();
  });

  it("точное время не потеряно — оно в подсказке", () => {
    const { container } = render(
      <WaitGauge waiting={null} time="12:39" sinceMinutes={22} timeTitle="12:39 у вас · 06:39 по Москве" />,
    );
    expect(container.querySelector("[title]")?.getAttribute("title")).toContain("12:39");
  });

  it("часы остаются запасным видом, если возраст посчитать не из чего", () => {
    render(<WaitGauge waiting={null} time="12:39" sinceMinutes={null} timeTitle="12:39" />);
    expect(screen.getByText("12:39")).toBeInTheDocument();
  });

  it("⚠ у ждущей строки единица та же — счётчик ожидания", () => {
    render(
      <WaitGauge
        waiting={{ minutes: 22, text: "22 мин", level: "warn" }}
        time="12:39"
        sinceMinutes={22}
        timeTitle="12:39"
      />,
    );
    expect(screen.getByText("22м")).toBeInTheDocument();
  });

  it("⚠ ПРОВОДКА: строка списка ДЕЙСТВИТЕЛЬНО передаёт возраст", async () => {
    /*
     * Без этого всё остальное зеленеет впустую, и это не догадка: диверсия
     * «строка перестала передавать возраст» прошла мимо всех 1650 проверок.
     * Сама шкала умеет показывать возраст — а получать его ей было неоткуда.
     */
    // @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
    const { readFileSync } = await import("node:fs");
    const src = readFileSync(
      "src/features/chats/components/list/ConversationListItem.tsx",
      "utf-8",
    ) as string;
    /*
     * Проверяем ИМЕННО расчёт, а не «где-то рядом встречается rowAt»: ленивая
     * регулярка находила его в соседнем свойстве и пропускала диверсию.
     */
    expect(src, "строка не передаёт возраст — в колонке снова будут часы").toContain(
      "sinceMinutes={",
    );
    expect(src, "возраст считается не от времени строки — колонка соврёт").toContain(
      "new Date(rowAt).getTime()",
    );
  });

  it("⚠ старше суток — дата, а не счётчик дней", () => {
    /*
     * Рвался список у строк, отличающихся МИНУТАМИ, — там и нужна одна мера.
     * Дальше суток дата полезнее: «5 авг» говорит больше, чем «6д», а спутать
     * её с соседними минутами нельзя — такие строки лежат внизу списка.
     */
    render(<WaitGauge waiting={null} time="5 авг" sinceMinutes={60 * 24 * 6} timeTitle="" />);
    expect(screen.getByText("5 авг"), "у старой переписки дату заменили счётчиком дней")
      .toBeInTheDocument();
  });

  it("часы и минуты сокращаются одинаково для обоих видов строк", () => {
    /* Иначе «2ч» у ждущего и «120м» у отвеченного — снова две меры. */
    const { rerender } = render(
      <WaitGauge waiting={{ minutes: 120, text: "2 ч", level: "late" }} time="10:00" sinceMinutes={120} timeTitle="" />,
    );
    expect(screen.getByText("2ч")).toBeInTheDocument();
    rerender(<WaitGauge waiting={null} time="10:00" sinceMinutes={120} timeTitle="" />);
    expect(screen.getByText("2ч")).toBeInTheDocument();
  });
});
