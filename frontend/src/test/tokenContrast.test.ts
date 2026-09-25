/**
 * СТОРОЖ КОНТРАСТА: каждая пара «текст на фоне» обеих тем — арифметикой.
 *
 * ЧТО БЫЛО. Шаг 6 аудита (docs/37) померил 104 пары и нашёл ЧЕТЫРНАДЦАТЬ
 * мест ниже нормы AA. Самое тяжёлое — заголовок жёлтой плашки в светлой
 * теме, 1.75:1 при норме 4.5, на восьми боевых экранах: тело плашки при
 * этом чёрное (19.7:1), то есть человек видел объяснение, но не видел, о
 * чём оно. Следом три точки индикатора связи — 1.86–2.34 при норме 3.
 *
 * ПОЧЕМУ ЭТО ПРОЖИЛО ЧЕТЫРЕ МЕСЯЦА. Разработка идёт в тёмной теме, а в ней
 * та же плашка даёт 10.98:1 — дефект просто не показывался тому, кто его
 * мог заметить. Плюс восемь комментариев в файле токенов обещали
 * коэффициенты, которых у кода не было (DESIGN-20): «белый на #16A34A =
 * 4.6:1» при фактических 3.30, «slate-500 на slate-100 = 4.76» при 4.34,
 * «тёмный на янтаре 11.8» при 9.09. Комментарий не проверяется, поэтому
 * комментарий и врал.
 *
 * ПОЧЕМУ ТЕСТ, А НЕ РАЗОВАЯ ПРАВКА. Глаз к контрасту нечувствителен:
 * разница между 4.6:1 и 3.9:1 не видна, а первое проходит и второе нет.
 * Приём взят у `avatarContrast.test.ts` — он считает 16 пар аватаров и
 * падает при провале, — и расширен со шкалы аватаров на всю систему: все
 * живые пары «чернила на заливке» и «текст на поверхности» в обеих темах
 * плюс заливки Mantine, которые приходят мимо файла токенов.
 *
 * ЧЕГО ЭТОТ СТОРОЖ НЕ ЗАКРЫВАЕТ — честно, чтобы не считали закрытым:
 *   1. `--lc-text-5` на строке списка (wait-gauge.css:48, время последнего
 *      ответа) даёт 2.54–3.23 в тёмной теме. Чинится не значением токена, а
 *      сменой потребителя на `--lc-text-3`; файл вне правки.
 *   2. Точка «связь просела» (app-layout.css:191) берёт примитив
 *      `--lc-warn` напрямую, а он обязан остаться ярким — это заливка
 *      ячейки «ждёт больше 15 минут» под тёмными чернилами. В светлой теме
 *      точка остаётся на 1.90 при норме 3; чинится строкой
 *      `var(--lc-warning)` в том же файле.
 * Обе пары внесены в PENDING ниже: тест на них НЕ падает, но печатает их
 * список, чтобы про них нельзя было забыть молча.
 */

import { describe, expect, it } from "vitest";

/*
 * Токены живут в CSS, а не в коде, поэтому читаем сами файлы.
 *
 * `?raw` не подошёл: в тестовой среде Vite отдаёт по нему пустую строку, и
 * тест зеленел бы, ничего не проверив (см. тот же довод в avatarContrast).
 * Читаем файловой системой, а типы для неё объявляем здесь: `@types/node` в
 * проекте нет и тянуть его ради двух вызовов незачем.
 */
declare function require(id: string): { readFileSync(p: string, enc: string): string };

const VARS = require("node:fs").readFileSync("src/app/lc-vars.css", "utf-8");
const THEME = require("node:fs").readFileSync("src/app/theme.tsx", "utf-8");

/** Обычный текст (WCAG 2.1, SC 1.4.3). */
const TEXT = 4.5;
/** Элементы интерфейса и графика: рамки, точки, иконки (SC 1.4.11). */
const UI = 3;

/* ─────────────────────────────── арифметика WCAG 2.1 ─────────────────── */

function channel(value: number): number {
  const v = value / 255;
  return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
}

function rgb(hex: string): [number, number, number] {
  const clean = hex.trim().replace("#", "");
  const full =
    clean.length === 3
      ? clean
          .split("")
          .map((c) => c + c)
          .join("")
      : clean;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as [number, number, number];
}

function luminance(hex: string): number {
  const [r, g, b] = rgb(hex);
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Полупрозрачные чернила НЕЛЬЗЯ мерить как непрозрачные: подпись оператора в
 * исходящем пузыре задана как rgba(255,255,255,.88), и именно она была одним
 * из четырнадцати провалов. Складываем цвет на подложку и меряем результат.
 */
function composite(fg: string, alpha: number, bg: string): string {
  const f = rgb(fg);
  const b = rgb(bg);
  const mix = f.map((c, i) => Math.round(c * alpha + b[i] * (1 - alpha)));
  return "#" + mix.map((c) => c.toString(16).padStart(2, "0")).join("");
}

/* ───────────────────────────── чтение токенов из CSS ─────────────────── */

type Scheme = "dark" | "light";

/*
 * ─────────────────────────────────────────── ПРЕСЕТЫ ЦВЕТА (04.09) ──
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА: «сделай несколько пресетов стиля, не только чёрные и
 * белые». Пресет — ВТОРАЯ ОСЬ, независимая от светло/темно: тему держит Mantine
 * через `data-mantine-color-scheme`, и третьего значения этому атрибуту дать
 * нельзя. Поэтому пресеты живут в `data-lc-preset`, и каждый из них — ДВА
 * блока, потому что направление контраста в темах зеркальное.
 *
 * ⚠ ЗАЧЕМ ЗДЕСЬ СПИСОК, А НЕ ПЕРЕБОР ФАЙЛА. Он и есть перебор — ниже
 * `найденныеПресеты()` вычитывает ключи из самого lc-vars.css, а отдельный тест
 * сверяет их с этим списком. Появился пресет, о котором тесты не знают, —
 * падение, а не молчание.
 *
 * ⚠ ПОЧЕМУ ЭТО ВООБЩЕ ВАЖНО. До сегодняшнего дня разбор классифицировал блок
 * правилом «содержит :root и не содержит color-scheme=light — значит тёмный».
 * Третий блок попадал И в тёмную карту, И в светлую, а будучи последним в
 * файле — перезаписывал обе. Замерено: полная третья тема давала ЗЕЛЁНЫЕ
 * тесты при том, что светлая переставала проверяться совсем.
 */
const PRESETS = ["indigo", "amethyst", "copper"] as const;
type Preset = (typeof PRESETS)[number] | null;

/** Что именно проверяем: базовая палитра или пресет, в тёмной или светлой. */
type Variant = { preset: Preset; scheme: Scheme };

const VARIANTS: Variant[] = [null, ...PRESETS].flatMap((preset) => [
  { preset, scheme: "dark" as const },
  { preset, scheme: "light" as const },
]);

const имяВарианта = (v: Variant) => `${v.preset ?? "базовая"}/${v.scheme}`;

/** Ключи пресетов, объявленные в самом lc-vars.css. */
function найденныеПресеты(): string[] {
  const css = VARS.replace(/\/\*[\s\S]*?\*\//g, "");
  return [...new Set([...css.matchAll(/data-lc-preset="([\w-]+)"/g)].map((m) => m[1]))].sort();
}

/**
 * Разбираем `lc-vars.css` на блоки объявлений и собираем две карты токенов.
 *
 * Тёмная тема объявлена селектором `:root, :root[…="dark"]`, то есть её
 * значения достаются светлой по наследству, если та их не переобъявила.
 * Ровно из-за этого тринадцать цветовых имён годами протекали в светлую
 * тему тёмными значениями (DESIGN-26) — карта здесь строится по тому же
 * правилу, что и браузер, иначе тест проверял бы не то, что видит человек.
 */
function readTokens(variant: Variant): Map<string, string> {
  const map = new Map<string, string>();
  /*
   * Комментарии вырезаем ДО разбора, и это не мелочь: в них полно текста
   * вида «--lc-text-3: в светлой теме тот синеватый», и без вырезания
   * объявление собиралось из половины комментария и половины следующей
   * строки. Первый же прогон этого теста на такое и наткнулся.
   */
  const css = VARS.replace(/\/\*[\s\S]*?\*\//g, "");
  const blocks = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)];

  /*
   * ⚠ ДВА ПРОХОДА, А НЕ ОДИН, И ЭТО ПРО СПЕЦИФИЧНОСТЬ, А НЕ ПРО ОПРЯТНОСТЬ.
   *
   * `:root[data-lc-preset="x"]` весит столько же, сколько
   * `:root[data-mantine-color-scheme="light"]`, — при равном весе выигрывает
   * тот, кто ниже в файле. Пресеты лежат в конце, и «тёмный» блок пресета
   * молча перебил бы светлую тему. В самом CSS это решено оговоркой
   * `:not([data-mantine-color-scheme="light"])`, а здесь — порядком: сперва
   * база, потом пресет.
   */
  /*
  * ⚠ КЛАССИФИЦИРУЕМ ПО СЕЛЕКТОРУ БЕЗ `:not(...)`, И ЭТО НЕ ПРИДИРКА.
  *
  * Тёмный блок пресета обязан нести оговорку
  * `:not([data-mantine-color-scheme="light"])` — иначе он перекрасит светлую
  * тему (у него та же специфичность, а лежит он ниже). Но подстрока
  * `color-scheme="light"` внутри этой оговорки ЕСТЬ, и наивная проверка
  * `includes` относит тёмный блок к светлой карте. Тогда вариант
  * «пресет/тёмная» мерит базовую палитру и зеленеет, ничего не проверив, —
  * замерено на заведомо сломанном пресете: ноль провалов.
  */
  const безОговорки = (selector: string) => selector.replace(/:not\([^)]*\)/g, "");

  const применить = (нужен: (selector: string) => boolean) => {
    for (const [, selector, body] of blocks) {
      /*
       * ⚠ МЕДИАЗАПРОСЫ ПРОПУСКАЕМ. Плоская регулярка не знает вложенности, и
       * `@media (min-width: 1800px) { :root { … } }` разбирался как обычный
       * `:root`: шкала кегля для больших мониторов подменяла базовую в ОБЕИХ
       * картах. Для цвета это пока безвредно — цвета в медиазапросах нет, — и
       * ровно это утверждает отдельный тест ниже.
       */
      if (selector.includes("@media")) continue;
      if (!нужен(безОговорки(selector))) continue;
      for (const [, name, value] of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
        map.set(name, value.trim());
      }
    }
  };

  const светлая = variant.scheme === "light";
  const базовый = (selector: string) => {
    if (selector.includes("data-lc-preset")) return false;
    const light = selector.includes('color-scheme="light"');
    return light ? светлая : selector.includes(":root");
  };
  применить(базовый);

  if (variant.preset) {
    const мой = `data-lc-preset="${variant.preset}"`;
    применить((selector) => {
      if (!selector.includes(мой)) return false;
      const light = selector.includes('color-scheme="light"');
      // Тёмный блок пресета помечен `:not(...light)` — в светлой он не наш.
      return light === светлая;
    });
  }

  if (map.size < 100) throw new Error(`разобрано подозрительно мало токенов: ${map.size}`);
  return map;
}

/*
 * Карты токенов на КАЖДЫЙ вариант: базовая палитра и все пресеты, в тёмной и
 * светлой. Ключ — «пресет/схема».
 */
const TOKENS = new Map<string, Map<string, string>>(
  VARIANTS.map((v) => [имяВарианта(v), readTokens(v)] as const),
);

/*
 * Большинство проверок в этом файле про БАЗОВУЮ палитру — кортежи Mantine,
 * каналы акцента, выключенная кнопка. Им довольно схемы, и переписывать их на
 * варианты значило бы утверждать, будто у каждого пресета свой кортеж Mantine,
 * чего нет. Поэтому `resolve` принимает и то и другое.
 */
type Where = Scheme | Variant;
const ключВарианта = (w: Where) => (typeof w === "string" ? `базовая/${w}` : имяВарианта(w));

/** Разворачивает цепочку `var(--a)` → `var(--b)` → `#hex` до конца. */
function resolve(name: string, where: Where, depth = 0): string {
  const ключ = ключВарианта(where);
  if (depth > 12) throw new Error(`циклическая ссылка на ${name} в ${ключ}`);
  const карта = TOKENS.get(ключ);
  if (!карта) throw new Error(`нет карты токенов для ${ключ}`);
  const raw = карта.get(name);
  if (!raw) throw new Error(`токен ${name} не объявлен в ${ключ}`);
  const ref = raw.match(/^var\((--[\w-]+)\)$/);
  if (ref) return resolve(ref[1], where, depth + 1);
  return raw;
}

/**
 * Приводит значение токена к непрозрачному hex поверх известной подложки.
 * Понимает `#rrggbb` и `rgba(r, g, b, a)` — других форм у пар, которые мы
 * меряем, нет; всё остальное валит тест, а не молча пропускается.
 */
function flatten(value: string, backdrop: string, where: string): string {
  if (/^#[0-9a-fA-F]{3,8}$/.test(value)) return value;
  const rgba = value.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
  if (rgba) {
    const hex = "#" + [1, 2, 3].map((i) => Number(rgba[i]).toString(16).padStart(2, "0")).join("");
    return composite(hex, rgba[4] === undefined ? 1 : Number(rgba[4]), backdrop);
  }
  throw new Error(`${where}: значение «${value}» не свести к цвету`);
}

/* ───────────────────────────────── таблица пар ───────────────────────── */

type Pair = {
  /** Чернила: имя токена или литерал `#hex`. */
  ink: string;
  /** Подложка: имя токена или литерал `#hex`. */
  bg: string;
  /** Порог: TEXT для читаемого текста, UI для рамок, точек и значков. */
  min: number;
  /** Где это на экране — чтобы падение теста читалось без раскопок. */
  where: string;
  /** Только в одной схеме, если пара существует лишь там. */
  only?: Scheme;
};

/*
 * Каждая строка — ЖИВАЯ пара: и чернила, и подложка проверены грепом по
 * потребителям. Выдуманных пар здесь нет намеренно — тест, стерегущий
 * несуществующие сочетания, создаёт ложное чувство покрытия.
 */
const PAIRS: Pair[] = [
  // ── текст на поверхностях (весь продукт) ──────────────────────────────
  { ink: "--lc-text-1", bg: "--lc-bg-0", min: TEXT, where: "основной текст на фоне приложения" },
  { ink: "--lc-text-1", bg: "--lc-bg-1", min: TEXT, where: "основной текст на панели" },
  { ink: "--lc-text-1", bg: "--lc-surface", min: TEXT, where: "основной текст на карточке" },
  { ink: "--lc-text-1", bg: "--lc-surface-hover", min: TEXT, where: "основной текст на наведении" },
  { ink: "--lc-text-1", bg: "--lc-selected", min: TEXT, where: "основной текст на выбранной строке" },
  { ink: "--lc-text-2", bg: "--lc-bg-1", min: TEXT, where: "вторичный текст на панели" },
  { ink: "--lc-text-2", bg: "--lc-surface", min: TEXT, where: "вторичный текст на карточке" },
  { ink: "--lc-text-2", bg: "--lc-surface-hover", min: TEXT, where: "вторичный текст на наведении" },
  { ink: "--lc-text-2", bg: "--lc-selected", min: TEXT, where: "вторичный текст на выбранной строке" },
  { ink: "--lc-text-3", bg: "--lc-bg-0", min: TEXT, where: "приглушённый текст на фоне приложения" },
  { ink: "--lc-text-3", bg: "--lc-bg-1", min: TEXT, where: "приглушённый текст на панели" },
  { ink: "--lc-text-3", bg: "--lc-surface", min: TEXT, where: "приглушённый текст на карточке" },
  { ink: "--lc-text-3", bg: "--lc-surface-hover", min: TEXT, where: "приглушённый текст на наведении" },
  { ink: "--lc-text-3", bg: "--lc-selected", min: TEXT, where: "приглушённый текст на выбранной строке" },
  // Булавка закрепления — значок 13px, то есть элемент интерфейса (docs/37 §2.1).
  { ink: "--lc-text-4", bg: "--lc-bg-1", min: UI, where: "булавка закрепления на панели" },
  { ink: "--lc-text-4", bg: "--lc-selected", min: UI, where: "булавка на выбранной строке" },

  // ── чернила на заливках ───────────────────────────────────────────────
  { ink: "--lc-on-primary", bg: "--lc-primary-solid", min: TEXT, where: "подпись на главной кнопке" },
  { ink: "--lc-on-primary", bg: "--lc-primary-solid-hover", min: TEXT, where: "главная кнопка под курсором" },
  { ink: "--lc-on-primary", bg: "--lc-primary-solid-active", min: TEXT, where: "главная кнопка нажата" },
  { ink: "--lc-on-success", bg: "--lc-success-solid", min: TEXT, where: "подпись на кнопке подтверждения" },
  { ink: "--lc-on-success", bg: "--lc-success-solid-hover", min: TEXT, where: "кнопка подтверждения под курсором" },
  { ink: "--lc-on-danger", bg: "--lc-danger-solid", min: TEXT, where: "бейдж очереди, счётчик на колокольчике" },
  { ink: "--lc-on-danger", bg: "--lc-danger-solid-hover", min: TEXT, where: "красная кнопка под курсором" },
  { ink: "--lc-on-warning", bg: "--lc-warning-solid", min: TEXT, where: "кнопка отправки в режиме заметки" },
  { ink: "--lc-on-warn", bg: "--lc-warn", min: TEXT, where: "минуты ожидания в залитой ячейке" },
  { ink: "--lc-on-accent", bg: "--lc-accent-solid", min: TEXT, where: "значок бота в строке диалога" },
  { ink: "--lc-on-brand", bg: "--lc-brand", min: TEXT, where: "монограмма знака Lead Partner" },
  { ink: "--lc-unread-text", bg: "--lc-unread-bg", min: TEXT, where: "число непрочитанных" },

  // ── подложки subtle ───────────────────────────────────────────────────
  { ink: "--lc-primary-text", bg: "--lc-primary-subtle", min: TEXT, where: "текст на акцентной подложке" },
  { ink: "--lc-success-text", bg: "--lc-success-subtle", min: TEXT, where: "текст на подложке успеха" },
  { ink: "--lc-danger-text", bg: "--lc-danger-subtle", min: TEXT, where: "плашка критичного, бейдж важности" },
  { ink: "--lc-warning-text", bg: "--lc-warning-subtle", min: TEXT, where: "полоса «связь просела», бейдж важности" },
  { ink: "--lc-info-text", bg: "--lc-info-subtle", min: TEXT, where: "информационная плашка" },
  // Плашка критичного: заголовок красный, а ТЕЛО обычным текстом на той
  // же красной подложке (notifications.css:231-234).
  { ink: "--lc-text-2", bg: "--lc-danger-subtle", min: TEXT, where: "тело плашки критичного" },
  // Утопленный блок: капсула клавиши, плитка настроек, чип важности
  // «просто к сведению» в центре уведомлений.
  { ink: "--lc-text-2", bg: "--lc-bg-2", min: TEXT, where: "текст на утопленном блоке" },
  { ink: "--lc-text-3", bg: "--lc-bg-2", min: TEXT, where: "чип важности «к сведению»" },
  { ink: "--lc-accent-text", bg: "--lc-accent-subtle", min: TEXT, where: "подпись бота" },

  // ── текст акцентом и ссылки на поверхностях ───────────────────────────
  { ink: "--lc-primary-text", bg: "--lc-bg-1", min: TEXT, where: "текст акцентом на панели" },
  { ink: "--lc-primary-text", bg: "--lc-surface", min: TEXT, where: "текст акцентом на карточке" },
  { ink: "--lc-link", bg: "--lc-bg-1", min: TEXT, where: "ссылка на панели" },
  { ink: "--lc-link", bg: "--lc-surface", min: TEXT, where: "ссылка на карточке" },
  { ink: "--lc-danger-text", bg: "--lc-bg-1", min: TEXT, where: "текст ошибки на панели" },
  { ink: "--lc-danger-text", bg: "--lc-surface", min: TEXT, where: "подпись под полем с ошибкой" },
  // Число «клиент ждёт N мин» — самое читаемое число смены, и оно лежит на
  // всех трёх состояниях строки списка.
  { ink: "--lc-warn-text", bg: "--lc-bg-1", min: TEXT, where: "минуты ожидания в списке" },
  { ink: "--lc-warn-text", bg: "--lc-surface-hover", min: TEXT, where: "минуты ожидания под курсором" },
  { ink: "--lc-warn-text", bg: "--lc-selected", min: TEXT, where: "минуты ожидания в выбранной строке" },

  // ── лента сообщений ───────────────────────────────────────────────────
  { ink: "--lc-bubble-out-text", bg: "--lc-bubble-out", min: TEXT, where: "текст исходящего сообщения" },
  { ink: "--lc-bubble-out-meta", bg: "--lc-bubble-out", min: TEXT, where: "подпись оператора и время в пузыре" },
  { ink: "--lc-bubble-out-danger", bg: "--lc-bubble-out", min: TEXT, where: "пометка «не доставлено»" },
  { ink: "--lc-bubble-in-text", bg: "--lc-bubble-in", min: TEXT, where: "текст входящего сообщения" },
  { ink: "--lc-bubble-bot-text", bg: "--lc-bubble-bot", min: TEXT, where: "сообщение бота" },
  { ink: "--lc-bubble-note-text", bg: "--lc-bubble-note", min: TEXT, where: "внутренняя заметка" },
  // Текст системного сообщения лежит НЕ на фоне ленты, а на капсуле
  // --lc-bg-2 (chat-thread.css:373). Первая редакция теста мерила его на
  // фоне, и в светлой теме пара расходилась на 0.4 — как раз через порог.
  { ink: "--lc-bubble-system-text", bg: "--lc-bg-2", min: TEXT, where: "системное сообщение в ленте" },

  // ── статусы диалога ───────────────────────────────────────────────────
  { ink: "--lc-status-new-text", bg: "--lc-status-new-bg", min: TEXT, where: "чип «новый»" },
  { ink: "--lc-status-inprogress-text", bg: "--lc-status-inprogress-bg", min: TEXT, where: "чип «в работе»" },
  { ink: "--lc-status-closed-text", bg: "--lc-status-closed-bg", min: TEXT, where: "чип «закрыт»" },

  // ── элементы интерфейса: рамки, точки, значки, графика ────────────────
  { ink: "--lc-primary", bg: "--lc-bg-1", min: UI, where: "рамки и иконки акцентом на панели" },
  { ink: "--lc-primary", bg: "--lc-selected", min: UI, where: "рамка выбранной строки списка" },
  { ink: "--lc-focus-border", bg: "--lc-bg-1", min: UI, where: "кольцо фокуса на панели" },
  { ink: "--lc-focus-border", bg: "--lc-surface", min: UI, where: "кольцо фокуса на карточке" },
  { ink: "--lc-danger", bg: "--lc-bg-1", min: UI, where: "рамка поля с ошибкой" },
  { ink: "--lc-danger", bg: "--lc-surface", min: UI, where: "рамка карточки в отказе" },
  { ink: "--lc-warning", bg: "--lc-bg-1", min: UI, where: "значок «передан вам» на панели" },
  { ink: "--lc-warning", bg: "--lc-surface-hover", min: UI, where: "значок «передан вам» под курсором" },
  { ink: "--lc-warning", bg: "--lc-selected", min: UI, where: "значок «передан вам» в выбранной строке" },
  { ink: "--lc-presence-online", bg: "--lc-bg-1", min: UI, where: "точка «в сети» в шапке" },
  { ink: "--lc-presence-away", bg: "--lc-bg-1", min: UI, where: "точка «отошёл»" },
  { ink: "--lc-presence-offline", bg: "--lc-bg-1", min: UI, where: "точка «нет соединения» в шапке" },
  { ink: "--lc-presence-offline", bg: "--lc-surface", min: UI, where: "точка «нет соединения» в карточке клиента" },
  { ink: "--lc-empty-art", bg: "--lc-bg-1", min: UI, where: "иллюстрация пустого состояния" },
  { ink: "--lc-empty-art", bg: "--lc-surface", min: UI, where: "иллюстрация пустого состояния на карточке" },
  { ink: "--lc-chart-1", bg: "--lc-surface", min: UI, where: "столбцы и линия графика" },
  { ink: "--lc-heat-5", bg: "--lc-surface", min: UI, where: "верхняя ступень тепловой карты" },
  { ink: "--lc-accent", bg: "--lc-surface", min: UI, where: "рамка пузыря бота" },
];

/**
 * Пары, чей провал ЗНАЕМ и чинить обязаны не здесь. Тест их не заваливает,
 * но и забыть не даёт: печатает список и падает, если такая пара вдруг
 * ПОЧИНИЛАСЬ — значит потребителя поправили и строку пора убрать.
 */
const PENDING: Array<Pair & { fix: string }> = [
  {
    ink: "--lc-text-5",
    bg: "--lc-selected",
    min: TEXT,
    only: "dark",
    where: "время последнего ответа в списке диалогов",
    fix: "wait-gauge.css:48 — color: var(--lc-text-5) → var(--lc-text-3)",
  },
  {
    ink: "--lc-warn",
    bg: "--lc-bg-1",
    min: UI,
    only: "light",
    where: "точка «связь просела» в шапке",
    fix: "app-layout.css:191 — background: var(--lc-warn) → var(--lc-warning)",
  },
];

/* ───────────────────────────────── проверки ──────────────────────────── */

function measure(pair: Pair, where: Where): { ratio: number; ink: string; bg: string } {
  const scheme = where;
  const bgRaw = pair.bg.startsWith("#") ? pair.bg : resolve(pair.bg, scheme);
  // Подложка обязана быть непрозрачной сама по себе: складывать её не на что.
  const bg = flatten(bgRaw, "#000000", `${pair.where} (фон)`);
  const inkRaw = pair.ink.startsWith("#") ? pair.ink : resolve(pair.ink, scheme);
  const ink = flatten(inkRaw, bg, `${pair.where} (чернила)`);
  return { ratio: contrast(ink, bg), ink, bg };
}

describe("Пресеты объявлены так, как их читают", () => {
  /*
   * ⚠ ЭТИ ТРИ ПРОВЕРКИ — ЗАМОК НА ГЛАВНУЮ ЛОВУШКУ ФАЙЛА. Разбор блоков здесь
   * плоский: он не знает вложенности и опирается на форму селектора. Пока форма
   * соблюдена, карты токенов совпадают с тем, что покажет браузер; отступи от
   * неё — и тесты начнут проверять не то, что видит человек, МОЛЧА.
   */
  it("в файле ровно те пресеты, о которых знают тесты", () => {
    expect(найденныеПресеты()).toEqual([...PRESETS].sort());
  });

  it("тёмный блок пресета исключает светлую схему явной оговоркой", () => {
    /*
     * `:root[data-lc-preset="x"]` весит ровно столько же, сколько
     * `:root[data-mantine-color-scheme="light"]`. При равном весе выигрывает
     * тот, кто ниже в файле, — а пресеты лежат в конце. Без оговорки
     * `:not(...)` тёмный блок пресета перекрасил бы и светлую тему.
     */
    const css = VARS.replace(/\/\*[\s\S]*?\*\//g, "");
    const плохие: string[] = [];
    for (const [, selector] of css.matchAll(/([^{}]+)\{[^{}]*\}/g)) {
      if (!selector.includes("data-lc-preset")) continue;
      const светлый = selector.includes('color-scheme="light"');
      const оговорка = selector.includes(':not([data-mantine-color-scheme="light"])');
      if (!светлый && !оговорка) плохие.push(selector.trim());
    }
    expect(плохие, `тёмный блок пресета без :not(...light):\n${плохие.join("\n")}`).toEqual([]);
  });

  it("цвет не объявляют внутри медиазапроса", () => {
    /*
     * Разбор пропускает `@media` целиком — плоская регулярка всё равно
     * прочитала бы его как обычный `:root` и подменила бы значения в ОБЕИХ
     * картах. Сегодня в медиазапросе только кегли и высоты кнопок; появится там
     * цвет — тесты замолчат, поэтому запрет записан явно.
     */
    const css = VARS.replace(/\/\*[\s\S]*?\*\//g, "");
    const плохие: string[] = [];
    for (const [, selector, body] of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      if (!selector.includes("@media")) continue;
      for (const [, name, value] of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
        if (/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(|color-mix\(/i.test(value)) {
          плохие.push(`${name}: ${value.trim()}`);
        }
      }
    }
    expect(плохие, `цвет внутри @media:\n${плохие.join("\n")}`).toEqual([]);
  });
});

describe("Контраст токенов", () => {
  /*
   * ⚠ ПЕРЕБИРАЕМ ВАРИАНТЫ, А НЕ СХЕМЫ (04.09). Пресет — это своя палитра, и
   * непроверенный пресет ничем не лучше непроверенной темы: у владельца в
   * консоли тринадцать человек по смене, и «зато красиво» им не поможет.
   */
  it.each(VARIANTS)("все живые пары проходят AA: $preset/$scheme", (variant) => {
    const scheme = variant.scheme;
    const failures = PAIRS.filter((p) => !p.only || p.only === scheme)
      .map((p) => ({ p, ...measure(p, variant) }))
      .filter((r) => r.ratio < r.p.min)
      .map(
        (r) =>
          `${r.p.where}: ${r.p.ink} (${r.ink}) на ${r.p.bg} (${r.bg}) = ` +
          `${r.ratio.toFixed(2)}:1 при норме ${r.p.min}`,
      );

    expect(failures).toEqual([]);
  });

  it("известные провалы всё ещё провалы — иначе строку пора убрать", () => {
    const healed = PENDING.filter((p) => measure(p, p.only ?? "dark").ratio >= p.min).map(
      (p) => `${p.where}: починилось, удалите строку из PENDING (${p.fix})`,
    );
    expect(healed).toEqual([]);
  });

  it("сам расчёт умеет находить провал — иначе тест зелёный ни о чём", () => {
    // Ровно то, что было в бою: заголовок жёлтой плашки Mantine в светлой теме.
    expect(contrast("#fab005", "#fef7e6")).toBeLessThan(TEXT);
    // И точка «в сети» на светлой панели — 2.08 при норме 3.
    expect(contrast("#22c55e", "#f1f5f9")).toBeLessThan(UI);
    // Заведомо хорошая пара, чтобы порог не оказался недостижимым для всех.
    expect(contrast("#ffffff", "#000000")).toBeGreaterThan(TEXT);
    // И проверка композитинга: без него подпись в пузыре мерилась бы как
    // чистый белый (7.38) вместо фактических 6.12.
    expect(composite("#ffffff", 0.88, "#1b631a")).toBe("#e4ece4");
    expect(contrast(composite("#ffffff", 0.88, "#1b631a"), "#1b631a")).toBeCloseTo(6.12, 1);
  });

  /*
   * КОЛЬЦО ФОКУСА ВНУТРИ ИСХОДЯЩЕГО ПУЗЫРЯ — особый случай, и мерить его
   * как обычную пару нельзя: кольцо составное. Обводка (--lc-focus-border)
   * и ореол под ней (--lc-focus-halo) работают в РАЗНЫХ темах:
   *   тёмная  — обводка 3.12, ореол 2.56 (держит обводка);
   *   светлая — обводка 1.38, ореол 6.30 (держит ореол).
   * Первый прогон этого теста и поймал светлый случай: пузырь позеленел, и
   * зелёная обводка на нём растворилась. Ровно про это предупреждает
   * комментарий к кольцу фокуса в lc-vars.css §6 — теперь предупреждение
   * проверяется.
   */
  it.each(["dark", "light"] as const)("фокус виден внутри пузыря: схема %s", (scheme) => {
    const bubble = resolve("--lc-bubble-out", scheme);
    const border = contrast(resolve("--lc-focus-border", scheme), bubble);
    const halo = contrast(
      flatten(resolve("--lc-focus-halo", scheme), bubble, "ореол кольца фокуса"),
      bubble,
    );
    expect(Math.max(border, halo)).toBeGreaterThanOrEqual(UI);
  });
});

/* ──────────────────────── лестница поверхностей ──────────────────────── */

/**
 * WCAG на соседние поверхности порога не даёт — это вопрос читаемости, а не
 * соответствия. Порог 1.10 взят по факту беды: до правки ни одна пара
 * соседних ступеней тёмной темы не превышала 1.14, карточка к панели давала
 * 1.028, а `--lc-bg-2` и `--lc-surface` были буквально одним цветом. Панели
 * и карточки на экране не существовало.
 */
const LADDER_MIN = 1.1;

const LADDER: Array<{ upper: string; lower: string; where: string }> = [
  { upper: "--lc-surface", lower: "--lc-bg-1", where: "карточка над панелью" },
  { upper: "--lc-surface-hover", lower: "--lc-bg-0", where: "наведение над фоном" },
  { upper: "--lc-selected", lower: "--lc-bg-0", where: "выбранное над фоном" },
  { upper: "--lc-selected", lower: "--lc-surface-hover", where: "выбранное над наведением" },
  { upper: "--lc-border", lower: "--lc-surface", where: "рамка на карточке" },
  { upper: "--lc-bubble-in", lower: "--lc-bg-0", where: "входящий пузырь над фоном ленты" },
];

describe("Лестница поверхностей", () => {
  it.each(VARIANTS)("ступени различимы: $preset/$scheme", (variant) => {
    const scheme = variant;
    const flat = LADDER.map((s) => ({
      ...s,
      ratio: contrast(resolve(s.upper, scheme), resolve(s.lower, scheme)),
    }))
      .filter((s) => s.ratio < LADDER_MIN)
      .map((s) => `${s.where}: ${s.upper} × ${s.lower} = ${s.ratio.toFixed(3)}:1`);

    expect(flat).toEqual([]);
  });

  it.each(VARIANTS)("рама отделима от страницы: $preset/$scheme", (variant) => {
    const scheme = variant;
    /*
     * `--lc-bg-1` (шапка, рельса, зазоры между колонками) и `--lc-bg-0`
     * (фон приложения) в тёмной теме дают заливкой 1.024, и поднять их
     * НЕЛЬЗЯ: обе лежат у самого чёрного края, где разница яркостей
     * физически мала — даже чистый чёрный дал бы против #0b0e0d только
     * 1.082. Поэтому докс/37 §4 эту пару и не чинит.
     *
     * Но граница между рамой и страницей обязана существовать хоть как-то,
     * и держат её ДВА разных механизма в разных темах:
     *   тёмная  — линия --lc-border, 1.47 (заливка 1.02);
     *   светлая — заливка, 1.13 (линия там всего 1.09).
     * Стережём именно это «или-или»: пропадут оба — шапка и рельса
     * сольются со страницей, а глядя на один механизм этого не увидишь.
     */
    const byFill = contrast(resolve("--lc-bg-1", scheme), resolve("--lc-bg-0", scheme));
    const byLine = contrast(resolve("--lc-border", scheme), resolve("--lc-bg-1", scheme));
    expect(Math.max(byFill, byLine)).toBeGreaterThanOrEqual(1.12);
  });

  it.each(VARIANTS)("два имени — не один цвет: $preset/$scheme", (variant) => {
    const scheme = variant;
    // `--lc-bg-2` и `--lc-surface` были равны в тёмной теме, а в светлой
    // совпадали ещё и с `--lc-bg-1` и с входящим пузырём. Токен, равный
    // соседу, выглядит рычагом, но ничего не двигает.
    //
    // Пары `--lc-bg-2` × `--lc-bg-1` в списке НЕТ, и это решение, а не
    // недосмотр: в светлой теме `--lc-bg-2` — утопленный блок (капсула
    // клавиши, плитка настроек, полоса скелетона) на БЕЛОЙ карточке, и тот
    // же серый, что у страницы, ему подходит. В тёмной теме они разные:
    // там `--lc-bg-2` это карточка.
    const same = [
      ["--lc-selected", "--lc-surface-hover"],
      ["--lc-bubble-in", "--lc-bg-1"],
      ["--lc-surface", "--lc-bg-1"],
      ["--lc-surface-hover", "--lc-bg-1"],
    ]
      .filter(([a, b]) => resolve(a, scheme) === resolve(b, scheme))
      .map(([a, b]) => `${a} и ${b} — один и тот же цвет`);

    expect(same).toEqual([]);
  });
});

/* ─────────────────────── заливки, приходящие из Mantine ──────────────── */

/**
 * ВТОРОЙ ИСТОЧНИК ЦВЕТА. Шапка `lc-vars.css` годами утверждала, что HEX
 * встречается только там, — а заливки кнопок, бейджей и чекбоксов всё это
 * время приходили из кортежей `theme.ts` (DESIGN-19). Значит и стеречь их
 * надо отдельно: тест выше про них ничего не знает.
 */
/*
 * Комментарии из theme.ts тоже вырезаем — по той же причине, что и из CSS,
 * и тоже по факту: в комментарии к правке `green` стояло «colors: { green:
 * lp }», и разбор списка зарегистрированных цветов уехал в этот текст.
 * Строк с «//» внутри кавычек в файле нет (проверено грепом по «://»).
 */
const CODE = THEME.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");

function tuple(name: string, depth = 0): string[] {
  if (depth > 4) throw new Error(`циклический алиас кортежа ${name}`);
  // `const yellow = amber;` — алиас, а не свой кортеж; идём по ссылке.
  const alias = CODE.match(new RegExp(`const ${name} = (\\w+);`))?.[1];
  if (alias) return tuple(alias, depth + 1);
  const block = CODE.match(new RegExp(`const ${name}: MantineColorsTuple = \\[([\\s\\S]*?)\\];`));
  if (!block) throw new Error(`кортеж ${name} не найден в theme.ts`);
  const shades = [...block[1].matchAll(/"(#[0-9a-fA-F]{6})"/g)].map((m) => m[1]);
  if (shades.length !== 10) throw new Error(`в кортеже ${name} ${shades.length} шагов вместо 10`);
  return shades;
}

const THRESHOLD = Number(CODE.match(/luminanceThreshold:\s*([\d.]+)/)?.[1]);
const PRIMARY_SHADE = Number(CODE.match(/primaryShade:\s*(\d+)/)?.[1]);
const TUPLES = ["lp", "info", "red", "amber", "violet", "gray"];

/**
 * Какие имена цветов ЗАРЕГИСТРИРОВАНЫ в теме и на какой кортеж смотрят.
 * Незарегистрированное имя не ошибка сборки: Mantine молча отдаёт свою
 * заводскую палитру — ровно так `color="yellow"` и получил #fab005.
 */
function registered(): Map<string, string> {
  const body = CODE.match(/colors:\s*\{([^}]*)\}/)?.[1];
  if (!body) throw new Error("не нашёл colors в theme.ts");
  const map = new Map<string, string>();
  for (const entry of body.split(",")) {
    const [, name, source] = entry.trim().match(/^(\w+)(?:\s*:\s*(\w+))?$/) ?? [];
    if (name) map.set(name, source ?? name);
  }
  return map;
}

/** Заводские шаги Mantine — то, что достаётся имени БЕЗ своего кортежа. */
const FACTORY: Record<string, string> = { yellow: "#fab005", gray: "#868e96" };

/** Шаг-заливка цвета: свой кортеж или заводской, если кортежа нет. */
function fill(name: string): string {
  const source = registered().get(name);
  if (source) return tuple(source)[PRIMARY_SHADE];
  if (FACTORY[name]) return FACTORY[name];
  throw new Error(`цвет ${name} не зарегистрирован и заводского значения для него не записано`);
}

/**
 * Значение переменной Mantine из `cssVariablesResolver`, если её там правят.
 *
 * ЧИТАЕМ ФАЙЛ, А НЕ ПОВТОРЯЕМ ЕГО В ТЕСТЕ. Первая редакция этого теста
 * держала правки списком у себя — и когда правку из `theme.ts` убрали,
 * тест остался зелёным: он проверял собственную копию. Сторож, знающий
 * ответ наизусть, не сторож.
 */
function mantineVar(variable: string, scheme: Scheme): string | undefined {
  const block = CODE.match(new RegExp(`\\n  ${scheme}:\\s*\\{([\\s\\S]*?)\\n  \\},`))?.[1];
  const raw = block?.match(new RegExp(`"${variable}":\\s*"([^"]+)"`))?.[1];
  if (!raw) return undefined;
  const token = raw.match(/^var\((--[\w-]+)\)$/);
  return token ? resolve(token[1], scheme) : raw;
}

describe("Заливки Mantine", () => {
  it("порог autoContrast читаемо красит ЛЮБОЙ шаг любой шкалы", () => {
    /*
     * Mantine выбирает чернила так: `luminance(fill) > luminanceThreshold`
     * → чёрные, иначе белые (color-functions/luminance/luminance.mjs).
     * Чёрные берут 4.5:1 при L ≥ 0.175, белые — при L ≤ 0.1833, значит
     * порог обязан лежать в этой полосе. Стоявшие здесь раньше 0.22 (и
     * предложенные аудитом 0.20) полосу перелетали, и фиолетовый #8b5cf6
     * (L = 0.198) получал белые чернила при 4.23:1.
     */
    const failures: string[] = [];
    for (const name of TUPLES) {
      tuple(name).forEach((fill, shade) => {
        const ink = luminance(fill) > THRESHOLD ? "#000000" : "#ffffff";
        const ratio = contrast(ink, fill);
        if (ratio < TEXT) {
          failures.push(
            `${name}-${shade} ${fill}: L=${luminance(fill).toFixed(3)}, порог ${THRESHOLD} → ` +
              `${ink === "#000000" ? "чёрные" : "белые"} чернила, ${ratio.toFixed(2)}:1`,
          );
        }
      });
    }
    expect(failures).toEqual([]);
  });

  it.each(VARIANTS.filter((v) => v.preset !== null))(
    "чернила Mantine читаются на заливке пресета: $preset/$scheme",
    (variant) => {
      /*
       * ПРЕСЕТ ПЕРЕКРАШИВАЕТ ЗАЛИВКУ, НО НЕ ЧЕРНИЛА (проверка 24.09). Mantine
       * выбирает цвет надписи по JS-кортежу `lp` один раз и о пресете не знает:
       * в светлой Индиго/Аметист/Медь заливка стояла на тёмной ступени 700, а
       * надпись оставалась чёрной — 1,7–2,7:1 у всех filled-кнопок, бейджей и
       * галочек. Сторож пары `--lc-on-primary` × `--lc-primary-solid` этого не
       * видел: он мерил белые чернила проекта, а не чёрные Mantine.
       */
      const ink = luminance(tuple("lp")[PRIMARY_SHADE]) > THRESHOLD ? "#000000" : "#ffffff";
      const failures = ["--mantine-color-lp-filled", "--mantine-color-lp-filled-hover"]
        .map((name) => ({ name, fill: resolve(name, variant) }))
        .filter(({ fill }) => contrast(ink, fill) < TEXT)
        .map(({ name, fill }) => `${name} ${fill}: ${contrast(ink, fill).toFixed(2)}:1`);
      expect(failures).toEqual([]);
    },
  );

  it("кортеж lp значение в значение повторяет рампу --lc-a-*", () => {
    // Литералы в theme.ts вынужденные (Mantine считает яркость в JS и для
    // строки «var(…)» молча отдаёт белые чернила), но разъехаться с файлом
    // токенов они не имеют права: это и есть «наполовину перекрашенное
    // приложение», которым уже кончалась одна перекраска.
    const steps = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900];
    const mismatch = tuple("lp")
      .map((hex, i) => ({ hex, token: resolve(`--lc-a-${steps[i]}`, "dark") }))
      .filter((p) => p.hex.toLowerCase() !== p.token.toLowerCase())
      .map((p) => `${p.hex} ≠ ${p.token}`);
    expect(mismatch).toEqual([]);
  });

  it("шкала поверхностей dark монотонна: от светлого к тёмному", () => {
    // Перепутать порядок — значит сделать фон приложения светлее карточек и
    // вывернуть экран наизнанку; на глаз в тёмной теме это ловится плохо.
    const shades = tuple("dark").map(luminance);
    const broken = shades
      .slice(0, -1)
      .map((l, i) => ({ i, l, next: shades[i + 1] }))
      .filter((s) => s.l <= s.next)
      .map((s) => `шаг ${s.i} не светлее шага ${s.i + 1}`);
    expect(broken).toEqual([]);
  });

  it.each(["dark", "light"] as const)("заливка под курсором не теряет чернила: схема %s", (scheme) => {
    /*
     * САМАЯ КОВАРНАЯ ИЗ ЗДЕШНИХ ЛОВУШЕК, и она стоит в бою прямо сейчас.
     *
     * Mantine считает цвет чернил ОДИН раз — по основному шагу — и на
     * наведении не пересчитывает, а заливка уходит на шаг ТЕМНЕЕ. Если
     * чернила тёмные (а у зелёного они тёмные), контраст падает ровно в тот
     * момент, когда человек на кнопку смотрит и целится: сегодня в светлой
     * теме это 4.19:1 при норме 4.5. Правится подстановкой
     * `--mantine-color-lp-filled-hover`, и правка есть только там, где о ней
     * вспомнили, — то есть проверять надо обе схемы.
     */
    const shades = tuple("lp");
    const base = shades[PRIMARY_SHADE];
    const ink = luminance(base) > THRESHOLD ? "#000000" : "#ffffff";
    // Без правки Mantine уводит заливку на шаг ТЕМНЕЕ основного.
    const hover = mantineVar("--mantine-color-lp-filled-hover", scheme) ?? shades[PRIMARY_SHADE + 1];

    expect(contrast(ink, hover)).toBeGreaterThanOrEqual(TEXT);
  });

  it("заливка variant=light в светлой теме держит свой текст", () => {
    /*
     * Ровно тот дефект, что дал 1.75:1 на восьми боевых экранах: Mantine
     * красит заголовок `Alert` и текст `Badge` переменной `*-light-color`
     * (в светлой схеме это шаг primaryShade), а подложку делает из того же
     * шага с прозрачностью 0.1.
     *
     * Меряем на ОБЕИХ подложках, на которых плашки реально лежат: на белой
     * карточке (login.css:15 — карточка входа) и на странице (login.css:8,
     * секции настроек фона не имеют вовсе и наследуют страницу). Аудит
     * померил только белую — и потому счёл достаточным седьмой шаг янтаря,
     * которого на панели не хватает.
     *
     * СПИСОК ЦВЕТОВ — ТОЛЬКО ТЕ, ЧТО РЕАЛЬНО СТОЯТ С `variant="light"`
     * (грепом по разметке: red 17, yellow 8, gray 5, lp 4, info 2, amber 1,
     * green 1). Фиолетового среди них нет, и выдумывать ему пару здесь
     * значило бы стеречь то, чего на экране не бывает.
     */
    const backdrops = [resolve("--lc-surface", "light"), resolve("--lc-bg-1", "light")];

    const failures: string[] = [];
    for (const name of ["lp", "green", "info", "red", "amber", "yellow", "gray"]) {
      const shade = fill(name);
      // По умолчанию Mantine красит текст тем же шагом, что и подложку.
      const ink = mantineVar(`--mantine-color-${name}-light-color`, "light") ?? shade;
      for (const backdrop of backdrops) {
        const bg = composite(shade, 0.1, backdrop);
        const ratio = contrast(ink, bg);
        if (ratio < TEXT) {
          failures.push(`${name} на ${backdrop}: ${ink} на ${bg} = ${ratio.toFixed(2)}:1`);
        }
      }
    }
    expect(failures).toEqual([]);
  });

  it("порог autoContrast стережёт и заводские палитры, если кортеж забыли", () => {
    // Незарегистрированное имя цвета — не ошибка сборки, а тихая подмена
    // палитры. Значит и список имён надо стеречь, а не только значения.
    const names = registered();
    const missing = ["lp", "info", "red", "amber", "violet", "gray", "yellow"].filter(
      (n) => !names.has(n),
    );
    expect(missing).toEqual([]);
  });
});

/*
 * ПОЛУПРОЗРАЧНОЕ СЛЕДУЕТ ЗА ЦВЕТОМ, А НЕ ЖИВЁТ ОТДЕЛЬНОЙ ЖИЗНЬЮ.
 *
 * CSS не умеет `rgba()` от `var()`, поэтому кольцо фокуса и свечение выбранного
 * диалога записывались каналами цифрами. Проект на этом уже обжигался: акцент
 * позеленел по-новому, рамка выбранной строки — вместе с ним, а свечение вокруг неё
 * осталось старого тона, и строка светилась двумя зелёными сразу. Лечение тогда
 * выбрали такое: переписать цифрами и объяснить комментарием.
 *
 * Комментарий не проверяется ничем. Тройка каналов теперь отдельный токен, и здесь
 * сверяется с HEX, от которого она произошла: разъехались — тест краснеет в ту же
 * минуту, а не через месяц на разборе интерфейса.
 */
describe("Каналы акцента совпадают с его HEX", () => {
  it.each([
    ["--lc-a-500", "--lc-a-500-rgb"],
    ["--lc-a-700", "--lc-a-700-rgb"],
  ])("%s", (hexToken, rgbToken) => {
    const css = VARS;
    const hex = new RegExp(`${hexToken}:\\s*#([0-9a-fA-F]{6})`).exec(css);
    const rgb = new RegExp(`${rgbToken}:\\s*(\\d+)\\s+(\\d+)\\s+(\\d+)`).exec(css);
    expect(hex, `${hexToken} не найден`).not.toBeNull();
    expect(rgb, `${rgbToken} не найден`).not.toBeNull();
    const из_hex = [1, 3, 5].map((i) => parseInt(hex![1].slice(i - 1, i + 1), 16));
    const из_каналов = [1, 2, 3].map((i) => Number(rgb![i]));
    expect(из_каналов, `${rgbToken} разъехался с ${hexToken}`).toEqual(из_hex);
  });
});

/*
 * ЗАЛИВКА В ТОКЕНАХ СОВПАДАЕТ С НАСТОЯЩЕЙ ЗАЛИВКОЙ MANTINE.
 *
 * Шапка `lc-vars.css` предупреждает: источников цвета два, и «правишь акцент здесь —
 * правишь кортеж lp там, иначе получишь наполовину перекрашенное приложение». Это
 * правило было прозой и не проверялось ничем — а `--lc-primary-solid` тем временем
 * указывал на шаг 500, тогда как настоящая заливка кнопок это шаг 6 кортежа.
 *
 * Поймать такое иначе нельзя: у токена НОЛЬ потребителей, экран красится мимо него,
 * и любая правка выглядит применённой, оставаясь невидимой.
 */
describe("Токен заливки совпадает с кортежем Mantine", () => {
  it("--lc-primary-solid равен шагу primaryShade", () => {
    const shade = Number(/primaryShade:\s*(\d+)/.exec(THEME)?.[1]);
    expect(Number.isInteger(shade), "primaryShade не найден в theme.ts").toBe(true);

    const кортеж = /const lp: MantineColorsTuple = \[([\s\S]*?)\]/.exec(THEME)?.[1] ?? "";
    const шаги = [...кортеж.matchAll(/"(#[0-9a-fA-F]{6})"/g)].map((m) => m[1].toLowerCase());
    expect(шаги.length, "кортеж lp разобран не полностью").toBe(10);

    const из_токена = resolve("--lc-primary-solid", "dark").toLowerCase();
    expect(из_токена, `токен заливки разошёлся с lp[${shade}]`).toBe(шаги[shade]);
  });
});

/**
 * ВЫКЛЮЧЕННАЯ КНОПКА ОБЯЗАНА ОСТАВАТЬСЯ ПРЕДМЕТОМ (разбор интерфейса 13.08).
 *
 * ЧТО БЫЛО. Претензия «„Отправить" в выключенном состоянии почти растворяется в
 * фоне» проверялась замером на живом экране. Подпись к тому времени уже читалась
 * (4.36), а вот заливка против полотна давала 1.22 в тёмной теме и 1.03 в светлой,
 * рамка стояла `transparent` — от кнопки оставалась серая надпись без предмета.
 * И не в одном месте: обход настроек нашёл «Сохранить распределение», «Сохранить
 * часы», «Сохранить» у лид-бота — главные действия своих экранов.
 *
 * ⚠ ПОЧЕМУ ИМЕННО СТОРОЖ, А НЕ ПРОСТО ПРАВКА. Здесь связаны ТРИ файла: значение
 * живёт в lc-vars.css, потребитель — в lc-base.css, а сравнивается всё с фоном,
 * который задан третьим токеном. Любое звено можно вернуть назад поодиночке, и
 * экран снова опустеет, не уронив ни одного теста: невидимая кнопка отрисовывается
 * без ошибок и находится по имени в любом запросе testing-library. Ровно так и
 * прожил этот дефект — вместе с комментарием, который честно писал «человек не
 * понимает, есть там кнопка или нет» и чинил при этом только подпись.
 *
 * ⚠ ВЕРХНЯЯ ГРАНИЦА ВАЖНЕЕ НИЖНЕЙ. Рамку легко «починить» ярче подписи — и тогда
 * выключенная кнопка станет заметнее живой, а это хуже исходного дефекта. Порядок
 * «форма тише подписи» проверяется отдельным утверждением.
 *
 * Норма 3:1 здесь НЕ применяется намеренно: SC 1.4.11 неактивные контролы
 * освобождает, и тянуть рамку до 3 значило бы спорить со смыслом слова «выключена».
 * Порог 1.8 — это «форму видно», а не «форма кричит».
 */
const EDGE_MIN = 1.8;

describe("Выключенная кнопка", () => {
  const BASE = require("node:fs").readFileSync("src/app/lc-base.css", "utf-8");

  it.each(["dark", "light"] as const)("форму кнопки видно на обоих фонах: %s", (scheme) => {
    const рамка = resolve("--lc-disabled-edge", scheme);
    const слабые = [
      { фон: "--lc-bg-0", где: "на полотне страницы" },
      { фон: "--lc-surface", где: "на карточке" },
    ]
      .map((с) => ({ ...с, ratio: contrast(рамка, resolve(с.фон, scheme)) }))
      .filter((с) => с.ratio < EDGE_MIN);

    expect(
      слабые,
      `рамка выключенной кнопки сливается: ${слабые
        .map((с) => `${с.где} ${с.ratio.toFixed(2)}`)
        .join(", ")}`,
    ).toEqual([]);
  });

  it.each(["dark", "light"] as const)("форма остаётся тише подписи: %s", (scheme) => {
    /*
     * Обе величины меряются от своего фона: подпись лежит НА заливке кнопки, а
     * рамка отделяет кнопку ОТ полотна. Сравнивать их напрямую можно потому, что
     * вопрос один — что первым бросится в глаза.
     */
    const подпись = contrast(resolve("--lc-disabled-text", scheme), resolve("--lc-disabled-bg", scheme));
    const рамка = contrast(resolve("--lc-disabled-edge", scheme), resolve("--lc-bg-0", scheme));
    expect(рамка, `рамка ${рамка.toFixed(2)} громче подписи ${подпись.toFixed(2)}`).toBeLessThan(
      подпись,
    );
  });

  it("⚠ ЗАЛИВКА И КАРТОЧКА — ОДИН ЦВЕТ, И ТОЛЬКО РАМКА ЭТО СПАСАЕТ", () => {
    /*
     * `--lc-disabled-bg` и `--lc-surface` оба равны `--lc-bg-raise`. Выключенная
     * кнопка НА КАРТОЧКЕ даёт ровно 1.00 — тот же цвет, форма исчезает полностью.
     * Сегодня таких кнопок на экранах нет (проверено обходом настроек), поэтому это
     * не дефект, а заряд: первая же карточка с выключенной кнопкой его подорвёт.
     *
     * Утверждение зафиксировано НЕ чтобы запретить совпадение, а чтобы объяснить,
     * почему выше проверяется фон «на карточке». Разведут токены — тест упадёт и
     * приведёт сюда: тогда эту проверку можно снять, а проверку рамки — оставить.
     */
    expect(resolve("--lc-disabled-bg", "dark")).toBe(resolve("--lc-surface", "dark"));
  });

  it("правило рамки не вернулось к `transparent`", () => {
    const правило = /\.lc-btn\[data-disabled\][^{]*\{([^}]*)\}/.exec(
      BASE.replace(/\/\*[\s\S]*?\*\//g, ""),
    )?.[1];
    expect(правило, "правило выключенной кнопки не найдено в lc-base.css").toBeTruthy();
    expect(правило).toContain("var(--lc-disabled-edge)");
  });
});
