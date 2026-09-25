import type { CSSProperties, ReactNode } from "react";
import { notifications } from "@mantine/notifications";
import { IconAlert, IconCheckCircle, IconInfo, IconXCircle } from "./Icon";

/**
 * Тосты (docs/16-DESIGN-SYSTEM-2026.md §4.6).
 *
 * Единственная точка показа всплывающих уведомлений. Прямой вызов
 * `notifications.show` в компонентах больше не нужен: тон, иконка, тайминг и
 * полоса прогресса должны решаться в одном месте, иначе через пару спринтов
 * половина тостов красная без иконки, а половина зелёная с эмодзи.
 *
 * ПОЛОСА ПРОГРЕССА И ЧЕСТНОСТЬ
 * ----------------------------
 * Полоса рисуется ТОЛЬКО когда тост действительно закрывается сам, и её
 * длительность равна фактической. Нарисовать её на `autoClose: false` —
 * значит показать человеку, что сообщение сейчас исчезнет само; он досмотрит
 * полосу до конца и отвернётся, а это как раз те случаи, ради которых
 * автозакрытие и отключали: «Аккаунт отключён», «Не удалось отправить».
 */

export type ToastTone = "success" | "danger" | "warning" | "info";

/** 4 секунды по брифу — хватает прочитать заголовок и первую строку. */
const DEFAULT_AUTO_CLOSE = 4000;

/** Ключ выключателя всплывающих уведомлений. Личный и на этом устройстве. */
export const TOASTS_OFF_KEY = "lc-toasts-off";

/**
 * Выключены ли всплывающие уведомления на этом устройстве.
 *
 * ⚠ ХРАНИМ В БРАУЗЕРЕ, А НЕ НА СЕРВЕРЕ, И ЭТО РЕШЕНИЕ. Всплывашка — свойство
 * ЭКРАНА, а не человека: на большом мониторе она не мешает, на ноутбуке
 * закрывает половину переписки. Один диспетчер работает и там и там.
 *
 * Чтение в try: приватный режим и заблокированные сайтовые данные бросают на
 * самом обращении к хранилищу, и уронить из-за настройки показ уведомления —
 * ровно наоборот тому, зачем она заводилась.
 */
export function toastsMuted(): boolean {
  try {
    return localStorage.getItem(TOASTS_OFF_KEY) === "1";
  } catch {
    return false;
  }
}

export function setToastsMuted(muted: boolean): void {
  try {
    if (muted) localStorage.setItem(TOASTS_OFF_KEY, "1");
    else localStorage.removeItem(TOASTS_OFF_KEY);
  } catch {
    /* приватный режим — настройка не сохранится, показ останется прежним */
  }
}

const ICONS: Record<ToastTone, ReactNode> = {
  success: <IconCheckCircle size={20} />,
  danger: <IconXCircle size={20} />,
  warning: <IconAlert size={20} />,
  info: <IconInfo size={20} />,
};

const MANTINE_COLOR: Record<ToastTone, string> = {
  success: "green",
  danger: "red",
  warning: "amber",
  // `info`, а не `lp`: акцент стал зелёным, и информационный тост слился бы с
  // подтверждением «Сохранено». Синий здесь — не украшение, а единственное,
  // что отличает «вот что произошло» от «всё получилось».
  info: "info",
};

/**
 * Совместимость со старыми вызовами, где тон задавался цветом Mantine.
 * `lp` означал подтверждение («Сценарий сохранён», «Аккаунт подключён»),
 * поэтому едет в success, а не в info.
 */
const TONE_BY_LEGACY_COLOR: Record<string, ToastTone> = {
  red: "danger",
  green: "success",
  lp: "success",
  yellow: "warning",
  amber: "warning",
  blue: "info",
};

/**
 * Заголовок ИЛИ текст, но хотя бы одно. Короткие подтверждения («Скопировано»)
 * заголовка не требуют, а пустой тост — всегда ошибка вызова, и её лучше
 * поймать типами, чем увидеть на экране.
 */
type ToastContent =
  | { title: string; message?: ReactNode }
  | { title?: string; message: ReactNode };

export type ToastOptions = ToastContent & {
  tone?: ToastTone;
  /** @deprecated тон задаётся через `tone`; принимается ради старых вызовов */
  color?: string;
  /** `false` — тост ждёт человека и полосы прогресса не получает */
  autoClose?: number | false;
  id?: string;
  withCloseButton?: boolean;
  onClose?: () => void;
  /**
   * Новость о чужом событии, которая лежит и в колокольчике. Только такие
   * тосты глушит выключатель всплывашек.
   */
  news?: boolean;
};

export function showToast({
  title,
  message,
  tone,
  color,
  autoClose,
  id,
  withCloseButton,
  onClose,
  news,
}: ToastOptions): string {
  const resolved: ToastTone = tone ?? (color ? TONE_BY_LEGACY_COLOR[color] ?? "info" : "info");
  const duration = autoClose === undefined ? DEFAULT_AUTO_CLOSE : autoClose;
  const selfClosing = duration !== false;

  /*
   * ⚠ ВЫКЛЮЧАТЕЛЬ ВСПЛЫВАШЕК — ЗДЕСЬ, И ТОЛЬКО ЗДЕСЬ (просьба владельца 28.08:
   * «сделай так, чтобы все уведомления можно было отключить визуально, и
   * появлялись только в колокольчике»).
   *
   * Точка входа одна на все 87 мест, которые поднимают тост, — значит и
   * выключатель один. Расставь мы проверку по вызовам, следующий новый тост
   * появился бы мимо неё, и настройка тихо перестала бы работать.
   *
   * ⚠ ГЛУШАТСЯ ТОЛЬКО НОВОСТИ (`news`), и это граница смысла. «Сообщение не
   * ушло», «Диалог отклонён · Вернуть», «Без операторов остались…» — обратная
   * связь на то, что человек только что сделал сам. В колокольчике её нет: он
   * про события системы, а не про судьбу нажатой кнопки. До 24.09 выключатель
   * глушил всё, кроме красного, — и вместе с новостями пропадали «Вернуть» и
   * предупреждения по своим действиям, хотя экран обещал «останутся только в
   * колокольчике».
   *
   * Красное не глушится никогда, даже если это новость.
   */
  if (news && resolved !== "danger" && toastsMuted()) return id ?? "";

  return notifications.show({
    id,
    title,
    message,
    color: MANTINE_COLOR[resolved],
    icon: ICONS[resolved],
    autoClose: duration,
    // Крестик у ВСЕХ. У несамозакрывающегося он обязателен (иначе сообщение
    // не убрать вовсе), но и у обычного он нужен: четыре секунды — это долго,
    // когда тост закрывает то, на что человек сейчас смотрит. Прежнее
    // `!selfClosing` отнимало крестик у всех самозакрывающихся разом.
    withCloseButton: withCloseButton ?? true,
    onClose,
    // classNames, а не className: @mantine/notifications расширяет свой объект
    // тем, что пришло в show(), и `className` ЗАМЕНЯЛ бы класс библиотеки —
    // вместе с ним пропадал отступ между тостами, и стопка слипалась.
    classNames: { root: "lc-toast" },
    ...(selfClosing
      ? {
          // Атрибут включает полосу, переменная задаёт ей ровно тот срок,
          // который отсчитывает Mantine.
          "data-autoclose": true,
          "data-tone": resolved,
          style: { "--lc-toast-duration": `${duration}ms` } as CSSProperties,
        }
      : { "data-tone": resolved }),
  });
}

/**
 * Тост с действием — «сделано · отменить» (UX-аудит, docs/17 §Т7).
 *
 * Живёт здесь, а не в вызывающем месте, по прозаичной причине: разметку
 * кнопки нельзя написать в файле `.ts`, а хуки очереди — именно такие.
 *
 * Восемь секунд вместо четырёх: обычный тост человек не читает, он его
 * замечает, а этот надо успеть прочесть И принять решение. Крестик тоже есть —
 * отмена не должна быть единственным способом убрать сообщение с экрана.
 */
export function showUndoToast(opts: {
  title: string;
  message?: string;
  actionLabel: string;
  onAction: () => void;
}): string {
  const id = `undo-${opts.title}-${Math.random().toString(36).slice(2)}`;
  return showToast({
    id,
    title: opts.title,
    tone: "info",
    autoClose: 8000,
    message: (
      <span className="lc-toast__undo">
        {opts.message}
        <button
          type="button"
          className="lc-toast__undo-btn"
          onClick={() => {
            notifications.hide(id);
            opts.onAction();
          }}
        >
          {opts.actionLabel}
        </button>
      </span>
    ),
  });
}

export const toast = {
  success: (title: string, message?: ReactNode) => showToast({ title, message, tone: "success" }),
  info: (title: string, message?: ReactNode) => showToast({ title, message, tone: "info" }),
  warning: (title: string, message?: ReactNode) => showToast({ title, message, tone: "warning" }),
  error: (title: string, message?: ReactNode) => showToast({ title, message, tone: "danger" }),
  /** Ошибка, которая обязана дождаться человека: без автозакрытия и с крестиком. */
  errorPersistent: (title: string, message?: ReactNode) =>
    showToast({ title, message, tone: "danger", autoClose: false }),
  hide: (id: string) => notifications.hide(id),
};
