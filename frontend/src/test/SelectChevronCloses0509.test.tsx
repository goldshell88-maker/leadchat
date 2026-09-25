/**
 * Шеврон сворачивает выпадающий список, а не только разворачивает.
 *
 * ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 05.09: «кнопка свернуть фильтр работает только во втором
 * поле, в 1 и 3 не работает, так как туда можно написать текст, и приходится,
 * чтобы его закрыть, нажимать в другое место».
 *
 * РАЗБОР. У поля БЕЗ поиска нажатие в любое место переключает список — там всё
 * в порядке, и это ровно «второе поле». У поля С ПОИСКОМ правая секция по
 * умолчанию не принимает нажатий: щелчок проваливается в текстовое поле, а
 * щелчок по текстовому полю список ОТКРЫВАЕТ. Открытый от этого не закрывался
 * никогда — оставался только щелчок мимо, по пустому месту экрана.
 *
 * Поиск двум полям из трёх нужен: каналов тридцать пять и людей столько же.
 * Значит чинить надо шеврон, и чинить в ОДНОМ месте — в теме, потому что
 * правило общее на все выпадающие списки продукта.
 */
import { describe, expect, it } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";
import { MantineProvider, Select } from "@mantine/core";
import { theme } from "@/app/theme";

function нарисовать(searchable: boolean) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <Select
        aria-label="Канал"
        placeholder="Канал: все"
        searchable={searchable}
        data={["Стас КП", "Матвей БТ"]}
      />
    </MantineProvider>,
  );
}

describe("Выпадающий список сворачивается шевроном", () => {
  /*
   * ⚠ ПРОВЕРЯЕМ СВОЙ ДОГОВОР, А НЕ ВНУТРЕННОСТИ БИБЛИОТЕКИ. В jsdom нет
   * раскладки, и настоящее раскрытие списка Mantine там не воспроизводится —
   * проверка «нажали и открылось» зеленела бы или краснела по причинам, к нашей
   * правке отношения не имеющим. Наш договор состоит ровно из двух частей:
   * правая секция ПРИНИМАЕТ нажатие, и при уже открытом списке это нажатие
   * снимает фокус (Mantine закрывает список по потере фокуса).
   */
  it("правая секция принимает нажатие — иначе щелчок проваливается в поле", () => {
    const { container } = нарисовать(true);
    const секция = container.querySelector("[data-position='right']") as HTMLElement;
    expect(секция, "правой секции с шевроном нет").toBeTruthy();
    /*
     * ⚠ ЧИТАЕМ ПЕРЕМЕННУЮ, А НЕ style.pointerEvents. Первая редакция этой
     * проверки смотрела на `секция.style.pointerEvents` — и диверсия «вернуть
     * none» прошла НАСКВОЗЬ: Mantine кладёт значение не в стиль секции, а в
     * переменную `--input-right-section-pointer-events` на обёртке поля.
     * Проверка смотрела не туда и зеленела при любом значении.
     */
    const обёртка = container.querySelector(".mantine-Input-wrapper") as HTMLElement;
    expect(обёртка.getAttribute("style") ?? "").toContain(
      "--input-right-section-pointer-events: all",
    );
  });

  /*
   * Открыт ли список, видно по `data-expanded` поля: его ставит обвязка
   * Combobox. ⚠ Не `aria-expanded` — Mantine 7 его без `withExpandedAttribute`
   * не ставит, и прежняя редакция этих проверок подставляла его руками: тест
   * зеленел, а в браузере шеврон не сворачивал список ни разу (24.09).
   */
  const открыт = (поле: HTMLInputElement) => поле.getAttribute("data-expanded") === "true";
  const нажатьШеврон = (секция: HTMLElement) =>
    act(() => {
      секция.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    });

  for (const searchable of [true, false]) {
    it(`шеврон открывает закрытый список и сворачивает открытый (поиск: ${searchable})`, () => {
      const { container } = нарисовать(searchable);
      const поле = container.querySelector("input") as HTMLInputElement;
      const секция = container.querySelector("[data-position='right']") as HTMLElement;
      expect(открыт(поле)).toBe(false);

      нажатьШеврон(секция);
      expect(открыт(поле), "закрытый список шевроном не открылся").toBe(true);

      нажатьШеврон(секция);
      expect(открыт(поле), "открытый список шевроном не свернулся").toBe(false);
    });
  }

  it("поле БЕЗ поиска не сломано — оно и раньше работало", () => {
    const { container } = нарисовать(false);
    expect(container.querySelector("[data-position='right']")).toBeTruthy();
  });

});

describe("Крестик очистки у выпадающего списка", () => {
  it("у поля с выбранным значением крестик есть и очищает поле", () => {
    const выбрано: Array<string | null> = [];
    const { container } = render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <Select
          aria-label="Канал"
          clearable
          defaultValue="Стас КП"
          data={["Стас КП", "Матвей БТ"]}
          onChange={(v) => выбрано.push(v)}
        />
      </MantineProvider>,
    );
    const крестик = container.querySelector("[data-position='right'] button") as HTMLButtonElement;
    expect(крестик, "крестика нет — вернуть фильтр к «все» нечем").toBeTruthy();

    fireEvent.click(крестик);

    expect(выбрано).toEqual([null]);
  });
});
