/**
 * СОЧЕТАНИЕ КАК ДАННЫЕ: разбор события в строку и обратно в подпись для человека.
 *
 * ЗАЧЕМ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Клавиши были лестницей `if` в одном обработчике, где
 * порядок веток и есть приоритет, а сравнение шло по `e.key`. Переназначать в такой
 * конструкции нечего: сочетание нигде не существует как значение — его нельзя ни
 * сохранить, ни сравнить, ни показать.
 *
 * ⚠ РАЗБОР ПО `e.code`, А НЕ ПО `e.key`, И ЭТО НЕ ПРИДИРКА. `e.key` — это НАПЕЧАТАННЫЙ
 * знак, то есть он зависит от раскладки: в русской Ctrl+R приходит как «Ctrl+к».
 * Прежний код разбирался с этим вручную и неполно — у Ctrl+Shift+N не было «т», у
 * Ctrl+K была только строчная «л», — а поймать такую дыру можно лишь переключив
 * раскладку и попробовав. `e.code` — это ФИЗИЧЕСКАЯ клавиша: `KeyR` остаётся `KeyR`
 * в любой раскладке, и перечислять кириллицу больше не нужно вовсе.
 *
 * ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ. Различения левого и правого модификатора: людям оно
 * незачем, а промах при назначении («вы нажали ПРАВЫЙ Ctrl») выглядел бы поломкой.
 * Ctrl и Cmd сведены в один модификатор `Mod` по той же причине: на маке жмут Cmd,
 * на Windows — Ctrl, и это одно и то же сочетание в голове человека.
 */

/** Каноническая запись сочетания: `Mod+Shift+KeyT`, `Alt+Digit0`, `Escape`. */
export type Binding = string;

/**
 * Клавиши, которые сами по себе ничего не значат: сочетание из одного модификатора
 * бессмысленно, а назначить его человек может случайно, просто взявшись за Ctrl.
 */
const BARE_MODIFIERS = new Set([
  "ControlLeft",
  "ControlRight",
  "ShiftLeft",
  "ShiftRight",
  "AltLeft",
  "AltRight",
  "MetaLeft",
  "MetaRight",
]);

/**
 * Событие → каноническая строка. Пустая строка значит «нажат только модификатор»,
 * и вызывающий обязан такое пропускать.
 */
// ⚠ ПОРЯДОК МОДИФИКАТОРОВ В ЗАПИСИ ФИКСИРОВАН — Mod, Alt, Shift. Иначе одно и то же
// сочетание даст две разные строки, и сравнение с сохранённой привязкой промахнётся.
export function bindingOf(e: KeyboardEvent): Binding {
  if (BARE_MODIFIERS.has(e.code)) return "";
  const key = baseKey(e);
  const parts: string[] = [];
  if (e.ctrlKey || e.metaKey) parts.push("Mod");
  if (e.altKey) parts.push("Alt");
  // ⚠ У ЗНАКА SHIFT УЖЕ ВНУТРИ САМОГО ЗНАКА. «?» — это и есть Shift+/ (в русской
  // раскладке Shift+7), поэтому «Shift+?» не совпадал ни с одной записью реестра:
  // справка по «?» с 13.08 открывалась только мышью (проверка 24.09).
  if (e.shiftKey && !isPunctuation(key)) parts.push("Shift");
  parts.push(key);
  return parts.join("+");
}

/** Печатный знак, не буква и не цифра: «?», «/», «!». Пробел знаком не считается. */
function isPunctuation(key: string): boolean {
  return key.length === 1 && !/[\sa-zA-Zа-яА-ЯёЁ0-9]/.test(key);
}

/**
 * Физическая клавиша строкой.
 *
 * ⚠ ЗНАКИ ПРЕПИНАНИЯ БЕРЁМ ИЗ `e.key`, А НЕ ИЗ `e.code`. «?» на разных раскладках
 * живёт на разных физических клавишах (в русской это Shift+7, в английской Shift+/),
 * и человек, который жмёт «вопрос», имеет в виду именно знак. Для букв и цифр всё
 * наоборот — там важна физическая клавиша, иначе привычка ломается при переключении
 * языка. Отсюда разное правило для разных классов, а не одно на всё.
 */
function baseKey(e: KeyboardEvent): string {
  // ⚠ ЗНАК ПРЕПИНАНИЯ ПРОВЕРЯЕТСЯ ПЕРВЫМ, И ЭТО НЕ ПРИДИРКА К ПОРЯДКУ. «?» в русской
  // раскладке — это Shift+7, то есть `code: "Digit7"`, а в английской Shift+/, то есть
  // `code: "Slash"`. Пропусти знак вперёд цифрового правила — и одно и то же намерение
  // человека («жму вопрос») дало бы два разных сочетания: `Shift+Digit7` и `Shift+?`.
  // Это ровно та болезнь раскладки, ради которой всё и переписывалось.
  if (isPunctuation(e.key)) return e.key;
  const code = e.code || codeFromKey(e);
  if (/^(Key[A-Z]|Digit\d|Numpad\d|Arrow(Up|Down|Left|Right)|F\d{1,2})$/.test(code)) return code;
  if (code === "Enter" || code === "NumpadEnter") return "Enter";
  if (code === "Escape" || code === "Backspace" || code === "Tab" || code === "Space") {
    return code;
  }
  return e.key;
}

/**
 * `code` из `key` — ТОЛЬКО когда `code` не пришёл вовсе.
 *
 * ⚠ В НАСТОЯЩЕМ БРАУЗЕРЕ ЭТОГО НЕ БЫВАЕТ: `code` есть у любого физического нажатия.
 * Пустым он приходит у событий, собранных руками, — а такие в проекте есть, и не
 * только в тестах. Без запаса такое событие давало бы «j» там, где ожидается `KeyJ`,
 * и сочетание молча не срабатывало бы: разбор ищет точное совпадение строки.
 *
 * Кириллицу здесь НЕ разбираем намеренно. Вся правка затем и делалась, чтобы
 * перестать перечислять раскладки руками; в браузере кириллическая «к» приезжает с
 * `code: "KeyR"` и до этой ветки не доходит.
 */
function codeFromKey(e: KeyboardEvent): string {
  const k = e.key;
  if (/^[a-zA-Z]$/.test(k)) return `Key${k.toUpperCase()}`;
  if (/^\d$/.test(k)) return `Digit${k}`;
  if (/^(ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Enter|Escape|Backspace|Tab)$/.test(k)) return k;
  if (k === " ") return "Space";
  return "";
}

const LABELS: Record<string, string> = {
  Mod: "Ctrl",
  Alt: "Alt",
  Shift: "Shift",
  ArrowUp: "↑",
  ArrowDown: "↓",
  ArrowLeft: "←",
  ArrowRight: "→",
  Enter: "Enter",
  Escape: "Esc",
  Backspace: "Backspace",
  Space: "Пробел",
  Tab: "Tab",
};

/** `Mod+Shift+KeyN` → «Ctrl + Shift + N». Для справки, профиля и экрана настройки. */
export function formatBinding(b: Binding): string {
  if (!b) return "";
  return b
    .split("+")
    .map((part) => {
      if (LABELS[part]) return LABELS[part];
      if (/^Key[A-Z]$/.test(part)) return part.slice(3);
      if (/^(Digit|Numpad)\d$/.test(part)) return part.slice(-1);
      return part;
    })
    .join(" + ");
}

/** Несколько сочетаний одной строкой: «Ctrl + ↑ / Ctrl + ↓». */
export function formatBindings(bs: readonly Binding[]): string {
  return bs.map(formatBinding).join(" / ");
}
