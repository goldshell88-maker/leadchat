import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MantineProvider } from "@mantine/core";
import { KeywordsInput } from "@/features/settings/bots/components/KeywordsInput";

/**
 * ПОЛЕ КЛЮЧЕВЫХ СЛОВ ПРОГЛАТЫВАЛО ЗАПЯТУЮ НА ЛЕТУ.
 *
 * Значение бралось производным (`value.join(", ")`), а onChange на каждой букве
 * разбирал набранное в массив. В ту секунду, когда человек печатает ЗАПЯТУЮ,
 * разбор даёт ["да", ""], `filter(Boolean)` выбрасывает пустой хвост, массив
 * остаётся прежним — и React возвращает поле к «да». Запятая и пробел за ней
 * стираются, следующие буквы дописываются к предыдущему слову:
 * «телевизор, тв, плазма» превращается в «телевизортвплазма».
 *
 * Возразить некому: проверка редактора ловит только пустой список, схема
 * сервера разрешает любую строку, а движок сравнивает ключ целиком — «данет» в
 * ответе клиента не встретится никогда. Бот сохраняется, включается, и все
 * ветки меню оказываются мёртвыми.
 *
 * ЧТО ЛОМАЛИ: вернули производное значение вместо черновика — падает первый же
 * тест на «телевизор, тв, плазма».
 */

/** Обёртка, повторяющая устройство редактора: модель живёт снаружи поля. */
function Harness({ onModel }: { onModel: (v: string[]) => void }) {
  const [ключи, setКлючи] = useState<string[]>([]);
  return (
    <MantineProvider>
      <KeywordsInput
        ariaLabel="Ключевые слова"
        value={ключи}
        onChange={(v) => {
          setКлючи(v);
          onModel(v);
        }}
      />
    </MantineProvider>
  );
}

describe("Поле ключевых слов", () => {
  it("запятая остаётся на экране, а слова доезжают до модели по отдельности", async () => {
    const модель = vi.fn();
    const user = userEvent.setup();
    render(<Harness onModel={модель} />);

    const поле = screen.getByLabelText("Ключевые слова") as HTMLInputElement;
    await user.type(поле, "телевизор, тв, плазма");

    expect(поле.value).toBe("телевизор, тв, плазма");
    expect(модель).toHaveBeenLastCalledWith(["телевизор", "тв", "плазма"]);
  });

  it("пробелы вокруг запятых срезаются, пустые куски не едут в сценарий", async () => {
    const модель = vi.fn();
    const user = userEvent.setup();
    render(<Harness onModel={модель} />);

    const поле = screen.getByLabelText("Ключевые слова") as HTMLInputElement;
    await user.type(поле, "да ,,  нет ,");

    // На экране — ровно то, что набрали: поле не спорит с человеком по ходу.
    expect(поле.value).toBe("да ,,  нет ,");
    expect(модель).toHaveBeenLastCalledWith(["да", "нет"]);
  });

  it("значение, пришедшее СНАРУЖИ, поле показывает: переключили шаг — видно новое", async () => {
    function Switcher() {
      const [ключи, setКлючи] = useState<string[]>(["старое"]);
      return (
        <MantineProvider>
          <KeywordsInput ariaLabel="Ключевые слова" value={ключи} onChange={setКлючи} />
          <button type="button" onClick={() => setКлючи(["другой", "шаг"])}>
            Переключить
          </button>
        </MantineProvider>
      );
    }
    const user = userEvent.setup();
    render(<Switcher />);

    const поле = screen.getByLabelText("Ключевые слова") as HTMLInputElement;
    expect(поле.value).toBe("старое");
    await user.click(screen.getByRole("button", { name: "Переключить" }));
    expect(поле.value).toBe("другой, шаг");
  });
});
