import type { SVGProps } from "react";

/**
 * Набор иконок LeadChat (docs/16-DESIGN-SYSTEM-2026.md §5).
 *
 * Зачем свой набор, а не эмодзи. Эмодзи рисует операционная система: у
 * Windows, macOS и Android они разного стиля, веса и цвета, размер не
 * управляется, а `currentColor` на них не действует — значок остаётся жёлтым
 * на любой теме. Для интерфейса, который живёт и в браузере, и в десктопном
 * приложении на Windows, это означает разный вид на каждой машине.
 *
 * Внешнюю библиотеку не берём по той же причине, по какой её нет в остальном
 * проекте: две сотни килобайт ради двух десятков значков окупились бы, только
 * если бы значков были сотни.
 *
 * Геометрия общая: сетка 24, обводка 2, скруглённые концы. Размер по
 * умолчанию 18px — под текст в 14px.
 */

type IconProps = Omit<SVGProps<SVGSVGElement>, "children"> & {
  size?: number | string;
};

function Svg({ size = 18, ...rest }: IconProps & { children?: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      // Иконки здесь декоративные: рядом всегда есть текст или aria-label на
      // кнопке. Скринридер, прочитавший «галочка» перед словом «Принять»,
      // добавляет шум, а не смысл.
      aria-hidden="true"
      focusable="false"
      {...rest}
    />
  );
}

/* ─────────────────────────────────────────────────── статусы и тосты ── */

export const IconCheck = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20 6 9 17l-5-5" />
  </Svg>
);

export const IconCheckCircle = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21.8 10.5V12a10 10 0 1 1-5.9-9.1" />
    <path d="m9 11 3 3L22 4" />
  </Svg>
);

export const IconAlert = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
    <path d="M12 9v4" />
    <path d="M12 17h.01" />
  </Svg>
);

export const IconXCircle = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="10" />
    <path d="m15 9-6 6" />
    <path d="m9 9 6 6" />
  </Svg>
);

export const IconInfo = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 16v-4" />
    <path d="M12 8h.01" />
  </Svg>
);

export const IconClock = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 6v6l4 2" />
  </Svg>
);

/* ─────────────────────────────────────────────────────── навигация ──── */

export const IconMessage = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z" />
  </Svg>
);

export const IconUsers = (p: IconProps) => (
  <Svg {...p}>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M22 21v-2a4 4 0 0 0-3-3.9" />
    <path d="M16 3.1a4 4 0 0 1 0 7.8" />
  </Svg>
);

export const IconChart = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 3v16a2 2 0 0 0 2 2h16" />
    <path d="M18 17V9" />
    <path d="M13 17V5" />
    <path d="M8 17v-3" />
  </Svg>
);

export const IconSettings = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12.2 2h-.4a2 2 0 0 0-2 2 2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0 2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7 2 2 0 0 1 1 1.7v.5a2 2 0 0 1-1 1.7 2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7 2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7 2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2 2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0 2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7 2 2 0 0 1-1-1.7v-.5a2 2 0 0 1 1-1.7 2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7 2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7 2 2 0 0 0-2-2Z" />
    <circle cx="12" cy="12" r="3" />
  </Svg>
);

export const IconBell = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10.3 21a1.9 1.9 0 0 0 3.4 0" />
    <path d="M4 17h16a2 2 0 0 1-2-2v-4a6 6 0 1 0-12 0v4a2 2 0 0 1-2 2Z" />
  </Svg>
);

export const IconInbox = (p: IconProps) => (
  <Svg {...p}>
    <path d="M22 12h-6l-2 3h-4l-2-3H2" />
    <path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1Z" />
  </Svg>
);

/* ────────────────────────────────────────────── поиск и фильтрация ──── */

export const IconSearch = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="8" />
    <path d="m21 21-4.3-4.3" />
  </Svg>
);

export const IconFilter = (p: IconProps) => (
  <Svg {...p}>
    <path d="M22 3H2l8 9.5V19l4 2v-8.5L22 3Z" />
  </Svg>
);

export const IconDownload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
  </Svg>
);

export const IconChevronDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="m6 9 6 6 6-6" />
  </Svg>
);

/** Парная к `IconChevronDown` — «предыдущий» в вертикальном списке диалогов. */
export const IconChevronUp = (p: IconProps) => (
  <Svg {...p}>
    <path d="m18 15-6-6-6 6" />
  </Svg>
);

export const IconChevronLeft = (p: IconProps) => (
  <Svg {...p}>
    <path d="m15 18-6-6 6-6" />
  </Svg>
);

/** Парная к `IconChevronLeft` — для перехода к следующему (просмотр снимков). */
export const IconChevronRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="m9 18 6-6-6-6" />
  </Svg>
);

export const IconArrowDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5v14" />
    <path d="m19 12-7 7-7-7" />
  </Svg>
);

/* ──────────────────────────────────────────────────────── композер ──── */

export const IconSend = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14.5 12H6" />
    <path d="M21.4 3.6a1 1 0 0 0-1.1-.2L3.4 10.9a1 1 0 0 0 .1 1.9L8 14l1.2 4.5a1 1 0 0 0 1.9.1l7.5-16.9a1 1 0 0 0-.2-1.1Z" />
  </Svg>
);

export const IconPaperclip = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21.4 11.1 12.3 20a5.5 5.5 0 1 1-7.8-7.8l9.2-9.1a3.7 3.7 0 1 1 5.2 5.2l-9.2 9.1a1.8 1.8 0 1 1-2.6-2.6l8.5-8.4" />
  </Svg>
);

export const IconSmile = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="10" />
    <path d="M8 14s1.5 2 4 2 4-2 4-2" />
    <path d="M9 9h.01" />
    <path d="M15 9h.01" />
  </Svg>
);

export const IconMic = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
    <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
    <path d="M12 19v3" />
  </Svg>
);

export const IconBolt = (p: IconProps) => (
  <Svg {...p}>
    <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" />
  </Svg>
);

export const IconSparkles = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3 13.9 8.1 19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Z" />
    <path d="M19 15.5 19.8 17.7 22 18.5l-2.2.8L19 21.5l-.8-2.2L16 18.5l2.2-.8L19 15.5Z" />
  </Svg>
);

export const IconTemplate = (p: IconProps) => (
  <Svg {...p}>
    <rect width="18" height="18" x="3" y="3" rx="2" />
    <path d="M3 9h18" />
    <path d="M9 21V9" />
  </Svg>
);

/* ────────────────────────────────────────────── действия с диалогом ─── */

export const IconForward = (p: IconProps) => (
  <Svg {...p}>
    <path d="m15 17 5-5-5-5" />
    <path d="M20 12H9a5 5 0 0 0-5 5v2" />
  </Svg>
);

export const IconPin = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 17v5" />
    <path d="M9 4.6V9c0 .8-.3 1.6-.9 2.1L5.5 14a1 1 0 0 0 .7 1.7h11.6a1 1 0 0 0 .7-1.7l-2.6-2.9A3 3 0 0 1 15 9V4.6" />
    <path d="M8 2h8" />
  </Svg>
);

/** Перечёркнутый круг — «нежелательный клиент». Не замок и не крест: замок
 *  читается как «закрыто», крест — как «удалить», а тут ни то, ни другое. */
export const IconBan = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="m5.6 5.6 12.8 12.8" />
  </Svg>
);

export const IconArchive = (p: IconProps) => (
  <Svg {...p}>
    <rect width="20" height="5" x="2" y="3" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" />
  </Svg>
);

export const IconUserPlus = (p: IconProps) => (
  <Svg {...p}>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M19 8v6" />
    <path d="M22 11h-6" />
  </Svg>
);

export const IconMore = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="1" />
    <circle cx="12" cy="5" r="1" />
    <circle cx="12" cy="19" r="1" />
  </Svg>
);

export const IconX = (p: IconProps) => (
  <Svg {...p}>
    <path d="M18 6 6 18" />
    <path d="m6 6 12 12" />
  </Svg>
);

export const IconPlus = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5v14" />
    <path d="M5 12h14" />
  </Svg>
);

/**
 * Одна полоса — «отдалить». Пара к `IconPlus`: та же длина линии и та же
 * сетка. Заведена 05.09 под кнопки масштаба в просмотре снимка; знак «−»
 * шрифтом рядом с нарисованным плюсом отличается и толщиной, и длиной, и в
 * одном ряду кнопок это видно сразу.
 */
export const IconMinus = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5 12h14" />
  </Svg>
);

/* ──────────────────────────────────────────────── карточка клиента ──── */

export const IconPhone = (p: IconProps) => (
  <Svg {...p}>
    <path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2 4.2 2 2 0 0 1 4 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.1a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2Z" />
  </Svg>
);

export const IconMail = (p: IconProps) => (
  <Svg {...p}>
    <rect width="20" height="16" x="2" y="4" rx="2" />
    <path d="m22 7-8.9 5.3a2 2 0 0 1-2.1 0L2 7" />
  </Svg>
);

export const IconCopy = (p: IconProps) => (
  <Svg {...p}>
    <rect width="14" height="14" x="8" y="8" rx="2" />
    <path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2" />
  </Svg>
);

export const IconEdit = (p: IconProps) => (
  <Svg {...p}>
    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
    <path d="M18.4 2.6a2 2 0 0 1 2.8 2.8L12 14.6l-4 1 1-4 9.4-9Z" />
  </Svg>
);

export const IconTag = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2c0 .5.2 1 .6 1.4l8.8 8.8a2 2 0 0 0 2.8 0l7.2-7.2a2 2 0 0 0 0-2.8Z" />
    <circle cx="7.5" cy="7.5" r="1.5" />
  </Svg>
);

export const IconNote = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
    <path d="M14 2v6h6" />
    <path d="M9 13h6" />
    <path d="M9 17h4" />
  </Svg>
);

export const IconBot = (p: IconProps) => (
  <Svg {...p}>
    <rect width="18" height="12" x="3" y="8" rx="2" />
    <path d="M12 5V2" />
    <circle cx="12" cy="4" r="1" />
    <path d="M8 13h.01" />
    <path d="M16 13h.01" />
    <path d="M9 17h6" />
  </Svg>
);

/** Знак вопроса в круге — справка по сочетаниям. */
/*
 * Звук новых сообщений — два значка, а не один с перечёркиванием поверх.
 * Признак «выключено» держится на форме самого значка: в свёрнутой рельсе
 * рядом нет подписи, и косая черта поверх динамика на двадцати пикселях
 * читается хуже, чем отсутствующие волны.
 */
export const IconSound = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z" />
    <path d="M16 9.2a4 4 0 0 1 0 5.6" />
    <path d="M18.8 6.6a8 8 0 0 1 0 10.8" />
  </Svg>
);

export const IconSoundOff = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z" />
    <path d="m16.5 9.5 5 5M21.5 9.5l-5 5" />
  </Svg>
);

export const IconHelp = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.5 9a2.5 2.5 0 1 1 3.2 2.4c-.7.3-1.2.9-1.2 1.7v.4" />
    <path d="M12 17h.01" />
  </Svg>
);

/*
 * ─────────────────────────────────────── дописано 04.09: вместо символов ──
 *
 * ⚠ ЗАЧЕМ. По экранам символы и эмодзи стояли ВМЕСТО значков: «🔄» у
 * «Переподключить», «🧪» у «Протестировать», «▶» у голосового, «↑»/«↓» у
 * перестановки шагов бота. Каждый из них рисуется ШРИФТОМ, а не нашей
 * геометрией: толщина линии не совпадает с соседним значком, размер зависит от
 * кегля, а на Windows и на Маке эмодзи выглядят по-разному — в одном ряду
 * кнопок это видно сразу.
 *
 * Здесь набор дорисован ровно на те четыре недостающие фигуры. Остальные
 * символы заменяются на УЖЕ СУЩЕСТВУЮЩИЕ значки (× и ✕ → IconX, ▸ →
 * IconChevronRight, ⋮ → IconMore, ✓ → IconCheck, ← → IconChevronLeft).
 */

/** Круговая стрелка — повторить, переподключить, обновить. */
export const IconRefresh = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21 12a9 9 0 1 1-2.6-6.4" />
    <path d="M21 4v5h-5" />
  </Svg>
);

/** Колба — пробный прогон, песочница бота. */
export const IconFlask = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 3h6" />
    <path d="M10 3v5.5L4.6 18a2 2 0 0 0 1.7 3h11.4a2 2 0 0 0 1.7-3L14 8.5V3" />
    <path d="M7.5 14h9" />
  </Svg>
);

/** Треугольник вправо — воспроизвести голосовое. */
export const IconPlay = (p: IconProps) => (
  <Svg {...p}>
    <path d="M7 5.5v13l11-6.5-11-6.5Z" />
  </Svg>
);

/** Стрелка вверх — переместить выше. Пара к IconArrowDown. */
export const IconArrowUp = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 20V5" />
    <path d="m6 11 6-6 6 6" />
  </Svg>
);

/** Глаз — «показать пароль». Пара к IconEyeOff. */
export const IconEye = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
    <circle cx="12" cy="12" r="3" />
  </Svg>
);

/**
 * Перечёркнутый глаз — «скрыть пароль».
 *
 * Косая черта, а не второй непохожий значок: пара «показать/скрыть» обязана
 * читаться как одно состояние с переключателем, иначе человек ищет глазами,
 * что изменилось. Здесь черта уместна — в отличие от звука в рельсе, где рядом
 * нет подписи и косая на двадцати пикселях читается хуже отсутствующих волн.
 */
export const IconEyeOff = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10.6 6.2A9.5 9.5 0 0 1 12 6c6 0 9.5 6 9.5 6a17 17 0 0 1-3 3.7" />
    <path d="M6.7 7.9A17 17 0 0 0 2.5 12S6 18 12 18a9.3 9.3 0 0 0 3.7-.7" />
    <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    <path d="m3 3 18 18" />
  </Svg>
);

/*
 * ──────────────────────────── дописано 05.09: голосовое и прочие вложения ──
 *
 * ⚠ ЗАЧЕМ. Просьба владельца 05.09 со скриншотами: «сделай качественный визуал
 * для аудио». Замер боя за 60 дней: голосовых 597, ссылок 123, мест 15,
 * объявлений 2. У голосового не было ЗНАКА ПАУЗЫ вовсе — браузерный плеер
 * рисовал её своим шрифтом и своей толщиной; ссылка, место и объявление
 * рисовались ОДНОЙ строкой со скрепкой, и по ней не отличить адрес дома от
 * ссылки на объявление.
 *
 * Фигур ровно три, а не четыре: объявление берёт уже существующий `IconTag` —
 * ценник и есть объявление, заводить вторую фигуру на тот же смысл значит
 * развести один смысл по двум значкам.
 *
 * ⚠ ВИДЕО ЗДЕСЬ НЕТ НАМЕРЕННО. За те же 60 дней видео пришло НОЛЬ раз (Авито
 * его не отдаёт, `_CONTENT_FREE_KINDS` в adapter.py: 106 сообщений с пустым
 * содержимым и без ссылки). Значок под него — фигура, которую никто никогда не
 * увидит, и обещание умения, которого у системы нет.
 */

/** Две полосы — пауза. Пара к IconPlay: тот же вес обводки, та же сетка. */
export const IconPause = (p: IconProps) => (
  <Svg {...p}>
    <rect x="6" y="4.5" width="4" height="15" rx="1" />
    <rect x="14" y="4.5" width="4" height="15" rx="1" />
  </Svg>
);

/** Два звена цепи — ссылка наружу. */
export const IconLink = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7L11.7 5.3" />
    <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" />
  </Svg>
);

/** Булавка на карте — геопозиция. */
export const IconMapPin = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
    <circle cx="12" cy="10" r="3" />
  </Svg>
);
