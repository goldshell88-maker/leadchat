import type { ReactNode } from "react";

/**
 * Пустое состояние (docs/16-DESIGN-SYSTEM-2026.md §4.8):
 * иллюстрация 88px → заголовок 16/600 → строка пояснения → действие.
 *
 * Иллюстрации вместо эмодзи. Причина не в красоте: эмодзи рисует операционная
 * система, и «✅» на Windows, macOS и Android — три разных значка разного
 * цвета и веса, размером и цветом которых из CSS не управляют. Пустое
 * состояние занимает половину экрана и попадается человеку по несколько раз
 * в день; это самое заметное место, чтобы выглядеть самодельным.
 *
 * Рисунки намеренно скупые — контурная графика в мягком круге, цвета из
 * токенов. Пустой экран не событие: он должен объяснить, что происходит, и не
 * задерживать взгляд.
 */

export type Illustration =
  | "done" // всё разобрано — хороший исход, а не пустота
  | "chat" // диалог не выбран
  | "search" // ничего не нашлось
  | "error" // не загрузилось
  | "plug" // ничего не подключено
  | "bot" // нет ботов
  | "bolt" // нет быстрых ответов
  | "archive"; // нет закрытых

/** Мягкий круг под рисунком — он же задаёт общий силуэт всем состояниям. */
function Frame({ tone, children }: { tone: string; children: ReactNode }) {
  return (
    <svg
      width="88"
      height="88"
      viewBox="0 0 88 88"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="44" cy="44" r="44" fill={tone} fillOpacity="0.1" />
      <g
        stroke={tone}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
        transform="translate(26 26)"
      >
        {children}
      </g>
    </svg>
  );
}

/*
 * ТРИ ТОНА НА ВОСЕМЬ ИЛЛЮСТРАЦИЙ, А НЕ ШЕСТЬ.
 *
 * Было шесть — акцентный, успех, ошибка, фиолетовый бот, янтарная молния и
 * серый архив. Все шесть проходили по контрасту, то есть это была не поломка
 * доступности, а разнобой: цвет иллюстрации не значил ничего, а глаз всё
 * равно пытается его прочесть.
 *
 * Правку пришлось делать ВМЕСТЕ с позеленением акцента, а не отдельным
 * пунктом: как только primary стал зелёным, `chat`, `search` и `plug`
 * слились с `done` — с единственным тоном, который здесь несёт смысл
 * («всё разобрано»). Свести тона после перекраски — обязательное действие,
 * а не косметика.
 *
 * Остаются три: нейтральный по умолчанию, danger у ошибки, success у «всё
 * разобрано». Больше цветов — больше поводов их толковать.
 */
const ART: Record<Illustration, ReactNode> = {
  done: (
    <Frame tone="var(--lc-success)">
      <path d="M4 18 L13 27 L32 8" />
    </Frame>
  ),
  chat: (
    <Frame tone="var(--lc-empty-art)">
      <path d="M2 6a4 4 0 0 1 4-4h24a4 4 0 0 1 4 4v16a4 4 0 0 1-4 4H14l-8 7v-7H6a4 4 0 0 1-4-4Z" />
      <path d="M11 12h14M11 18h8" />
    </Frame>
  ),
  search: (
    <Frame tone="var(--lc-empty-art)">
      <circle cx="16" cy="16" r="12" />
      <path d="m25 25 9 9" />
    </Frame>
  ),
  error: (
    <Frame tone="var(--lc-danger)">
      <path d="M18 3 34 31H2Z" />
      <path d="M18 14v7M18 26h.01" />
    </Frame>
  ),
  plug: (
    <Frame tone="var(--lc-empty-art)">
      <path d="M11 2v10M25 2v10" />
      <path d="M6 12h24v5a12 12 0 0 1-24 0Z" />
      <path d="M18 29v6" />
    </Frame>
  ),
  bot: (
    <Frame tone="var(--lc-empty-art)">
      <rect x="3" y="11" width="30" height="21" rx="5" />
      <path d="M18 5v6" />
      <circle cx="18" cy="3" r="2" />
      <path d="M12 20h.01M24 20h.01M14 26h8" />
    </Frame>
  ),
  bolt: (
    <Frame tone="var(--lc-empty-art)">
      <path d="M21 2 6 21h12l-2 13 15-19H19Z" />
    </Frame>
  ),
  archive: (
    <Frame tone="var(--lc-empty-art)">
      <rect x="2" y="4" width="32" height="8" rx="2" />
      <path d="M5 14v16a3 3 0 0 0 3 3h20a3 3 0 0 0 3-3V14" />
      <path d="M14 21h8" />
    </Frame>
  ),
};

export function EmptyState({
  illustration,
  title,
  description,
  action,
  live,
}: {
  illustration: Illustration;
  title: string;
  description?: string;
  action?: ReactNode;
  /**
   * Объявлять ли текст скринридеру в момент появления.
   *
   * ЗАЧЕМ ЭТО ЗДЕСЬ. Часть пустых состояний в проекте — не «данных нет», а
   * «что-то пошло не так»: они рисуются по ошибке загрузки и несут
   * `role="alert"`. Перевести такие места на этот компонент, не умеющий
   * объявлять, значит МОЛЧА снять объявление живой области: зрячий увидит
   * красный блок, незрячий не узнает вовсе.
   *
   * `alert` — для ошибок (объявляется немедленно, перебивая чтение),
   * `status` — для «всё разобрано» и прочих спокойных сообщений.
   */
  live?: "alert" | "status";
}) {
  return (
    /*
     * `aria-live` НЕ задаём рядом с `role="alert"`.
     *
     * У роли `alert` уже есть подразумеваемая вежливость `assertive` —
     * объявить немедленно, перебив чтение. Дописанный `aria-live="polite"`
     * её ПОНИЖАЕТ: сообщение об ошибке встаёт в очередь и звучит после того,
     * что человек читает сейчас. Для «не получилось загрузить» это ровно
     * наоборот тому, что нужно.
     *
     * Для спокойных сообщений роль `status` сама означает `polite`, и второе
     * объявление ей тоже не нужно.
     */
    <div className="lc-empty" role={live}>
      {ART[illustration]}
      <p className="lc-empty__title">{title}</p>
      {description ? <p className="lc-empty__text">{description}</p> : null}
      {action}
    </div>
  );
}
