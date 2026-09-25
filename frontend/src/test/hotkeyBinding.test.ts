import { всеСочетания } from "./helpers";
import { describe, expect, it } from "vitest";
import { bindingOf, formatBinding, formatBindings } from "@/features/hotkeys/binding";
import { ACTIONS, firesWhileTyping, hotkeyRows } from "@/features/hotkeys/catalog";
import { actionFor } from "@/features/hotkeys/dispatch";

/**
 * СОЧЕТАНИЕ КАК ЗНАЧЕНИЕ (требование заказчика 13 августа: «чтобы можно было
 * переназначать горячие клавиши»).
 *
 * Клавиши были лестницей `if`, где сравнение шло по `e.key` — по НАПЕЧАТАННОМУ знаку.
 * Переназначать там нечего: сочетание нигде не существовало как данные.
 */

function нажатие(init: Partial<KeyboardEvent>): KeyboardEvent {
  return new KeyboardEvent("keydown", init as KeyboardEventInit);
}

describe("Разбор нажатия", () => {
  it("буква даёт физическую клавишу, а не напечатанный знак", () => {
    expect(bindingOf(нажатие({ code: "KeyR", key: "r", ctrlKey: true }))).toBe("Mod+KeyR");
  });

  it("⚠ РУССКАЯ РАСКЛАДКА ДАЁТ ТО ЖЕ САМОЕ СОЧЕТАНИЕ", () => {
    /**
     * Ради этого всё и затевалось. Раньше кириллицу перечисляли руками и неполно:
     * у Ctrl+Shift+N не было «т», у Ctrl+K — только строчная «л». Поймать такую дыру
     * можно было лишь переключив раскладку и попробовав; тест её не видел никогда.
     */
    expect(bindingOf(нажатие({ code: "KeyR", key: "к", ctrlKey: true }))).toBe("Mod+KeyR");
    expect(bindingOf(нажатие({ code: "KeyN", key: "Т", ctrlKey: true, shiftKey: true }))).toBe(
      "Mod+Shift+KeyN",
    );
    expect(bindingOf(нажатие({ code: "KeyK", key: "Л", ctrlKey: true }))).toBe("Mod+KeyK");
  });

  it("Cmd и Ctrl — один модификатор", () => {
    // На маке жмут Cmd, на Windows Ctrl, и в голове человека это одно сочетание.
    expect(bindingOf(нажатие({ code: "KeyD", key: "d", metaKey: true }))).toBe("Mod+KeyD");
    expect(bindingOf(нажатие({ code: "KeyD", key: "d", ctrlKey: true }))).toBe("Mod+KeyD");
  });

  it("порядок модификаторов всегда один", () => {
    // Иначе одно сочетание дало бы две строки, и сравнение с сохранённым промахнулось бы.
    const b = bindingOf(нажатие({ code: "KeyT", key: "T", ctrlKey: true, shiftKey: true, altKey: true }));
    expect(b).toBe("Mod+Alt+Shift+KeyT");
  });

  it("один модификатор без клавиши — не сочетание", () => {
    expect(bindingOf(нажатие({ code: "ControlLeft", key: "Control", ctrlKey: true }))).toBe("");
  });

  it("знак берётся из key: «?» на разных раскладках живёт на разных клавишах", () => {
    // Shift у знака уже внутри знака: «Shift+?» не совпадал с «?» реестра, и
    // справка по «?» не открывалась ни в одной раскладке (проверка 24.09).
    expect(bindingOf(нажатие({ code: "Slash", key: "?", shiftKey: true }))).toBe("?");
    expect(bindingOf(нажатие({ code: "Digit7", key: "?", shiftKey: true }))).toBe("?");
    expect(bindingOf(нажатие({ code: "Slash", key: "?", shiftKey: true, ctrlKey: true }))).toBe("Mod+?");
  });

  it("пробел — клавиша, а не знак: Shift при нём сохраняется", () => {
    expect(bindingOf(нажатие({ code: "Space", key: " " }))).toBe("Space");
    expect(bindingOf(нажатие({ code: "Space", key: " ", shiftKey: true }))).toBe("Shift+Space");
  });
});

describe("Подпись для человека", () => {
  it("превращает запись в читаемое", () => {
    expect(formatBinding("Mod+Shift+KeyN")).toBe("Ctrl + Shift + N");
    expect(formatBinding("Alt+Digit0")).toBe("Alt + 0");
    expect(formatBinding("Mod+ArrowDown")).toBe("Ctrl + ↓");
    expect(formatBinding("Escape")).toBe("Esc");
  });

  it("несколько сочетаний — через дробь", () => {
    expect(formatBindings(["KeyJ", "ArrowDown"])).toBe("J / ↓");
  });
});

describe("Что срабатывает при наборе текста", () => {
  /**
   * ⚠ САМОЕ ДОРОГОЕ МЕСТО ВСЕЙ ЗАТЕИ. Ctrl+Backspace в Windows и Linux — «удалить
   * слово», одно из самых частых движений при наборе. Пока проверка «человек печатает»
   * была ПОЗИЦИЕЙ ветки в лестнице `if`, она однажды оказалась ниже этого сочетания —
   * и диспетчер, стиравший слово в ответе клиенту, ОТКЛОНЯЛ диалог: тот уходил другим
   * операторам, а человек оставался с недописанным текстом и без объяснения.
   *
   * Теперь это данные, и переназначение обойти правило не может.
   */
  const по = (id: string) => ACTIONS.find((a) => a.id === id)!;

  it("отклонение не срабатывает при наборе, даже с модификатором", () => {
    expect(firesWhileTyping(по("decline"), "Mod+Backspace")).toBe(false);
  });

  it("принять и закрыть — срабатывают: их жмут прямо в поле ввода", () => {
    expect(firesWhileTyping(по("claim"), "Mod+KeyR")).toBe(true);
    expect(firesWhileTyping(по("close"), "Mod+KeyD")).toBe(true);
  });

  it("⚠ ГОЛАЯ БУКВА НЕ СРАБАТЫВАЕТ ПРИ НАБОРЕ НИКОГДА", () => {
    // Иначе «j», назначенная на переход по списку, уводила бы из диалога посреди слова.
    expect(firesWhileTyping(по("listNext"), "KeyJ")).toBe(false);
    // А то же действие с модификатором — срабатывает.
    expect(firesWhileTyping(по("listNext"), "Mod+ArrowDown")).toBe(true);
  });

  it("Escape — исключение: он не печатается, а заметку закрывают из поля ввода", () => {
    expect(firesWhileTyping(по("escape"), "Escape")).toBe(true);
  });
});

describe("Поиск действия по нажатию", () => {
  it("находит по сочетанию из реестра", () => {
    const a = actionFor(нажатие({ code: "KeyR", key: "r", ctrlKey: true }), false);
    expect(a?.id).toBe("claim");
  });

  it("личное переназначение перебивает умолчание", () => {
    const своё = { claim: ["Mod+KeyY"] };
    expect(actionFor(нажатие({ code: "KeyY", key: "y", ctrlKey: true }), false, своё)?.id).toBe("claim");
    // Прежнее сочетание после переназначения этому действию больше не принадлежит.
    expect(actionFor(нажатие({ code: "KeyR", key: "r", ctrlKey: true }), false, своё)?.id).not.toBe("claim");
  });

  it("при наборе опасное действие не находится вовсе", () => {
    const событие = нажатие({ code: "Backspace", key: "Backspace", ctrlKey: true });
    /*
     * Сочетания включены ЯВНО: с 02.09 они выключены по умолчанию, а проверка
     * здесь про другое — что опасное действие не срабатывает ПРИ НАБОРЕ, даже
     * когда клавиша ему назначена. Оставь мы здесь умолчания, тест зеленел бы
     * по неверной причине: «не сработало, потому что выключено».
     */
    expect(actionFor(событие, false, всеСочетания())?.id).toBe("decline");
    expect(actionFor(событие, true, всеСочетания())).toBeNull();
  });
});

describe("Справка читает тот же реестр, что и обработчик", () => {
  it("парные действия склеены в одну строку", () => {
    const строки = hotkeyRows(всеСочетания());
    const шаг = строки.find((r) => r.what.startsWith("Следующий и предыдущий диалог — как в Jivo"));
    expect(шаг?.keys).toContain("/");
  });

  it("личные сочетания видны в справке", () => {
    const строки = hotkeyRows({ claim: ["Mod+KeyY"] });
    expect(строки.find((r) => r.what.startsWith("Принять"))?.keys).toBe("Ctrl + Y");
  });
});
