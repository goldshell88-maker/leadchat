/* eslint-disable react-refresh/only-export-components -- экран отказа и его
   служебные функции живут в одном файле: разносить их по трём модулям ради
   правила горячей перезагрузки незачем, экран отказа никто не правит на лету */
import { Component, useEffect, useState, type ErrorInfo, type ReactNode } from "react";
import { Button, Group, Stack, Text, Title } from "@mantine/core";
import { useRouteError } from "react-router-dom";
import { pageActions } from "@/shared/lib/pageActions";

/**
 * Экран отказа вместо стек-трейса (аудит SHELL-01, SHELL-02).
 *
 * ЧТО БЫЛО. В приложении не было ни одного ErrorBoundary и ни одного
 * `errorElement` — проверено грепом, ноль совпадений. Любое исключение в
 * отрисовке ловил собственный `RenderErrorBoundary` react-router и рисовал
 * `DefaultErrorComponent`: страница без стилей, английское «Unexpected
 * Application Error!», текст ошибки курсивом и `<pre>` с полным стек-трейсом.
 * Ветка `devInfo` из сборки вырезается, а `<pre>` со стеком — нет, он вне
 * условия `NODE_ENV`, и в собранном бандле проекта он есть. Шапка, рельса,
 * колокольчик и плашки критичного исчезали целиком — они внутри маршрутного
 * дерева. Кнопки «назад» на той странице нет: выбраться можно было только
 * браузерным Back или перезагрузкой руками. Оператор посреди смены получал
 * английский стек вместо рабочего места.
 *
 * ЧТО ЗДЕСЬ. Один экран отказа на два разных механизма:
 *  - :class:`ErrorBoundary` — обычная граница React. Ловит исключения ОТРИСОВКИ
 *    и стоит внутри `AppLayout` вокруг `Outlet`, поэтому падение экрана не
 *    уносит шапку и рельсу: человек остаётся в рабочем месте и уходит в другой
 *    раздел одним нажатием.
 *  - :func:`RouteErrorScreen` — `errorElement` маршрутов. Ловит то, что граница
 *    React поймать не может в принципе: обрыв на `route.lazy`, то есть
 *    несостоявшийся динамический `import()` раздела.
 *
 * Чего экран НЕ делает: не показывает ни стек, ни текст исключения. Английский
 * `TypeError` оператору не говорит ничего, а администратору всё равно нужен
 * полный контекст — он уходит в консоль и в телеметрию (см. `reportCrash`).
 */

/** Насколько крупная беда: свежая сборка на сервере или настоящее исключение. */
export type CrashKind = "stale-build" | "crash";

/**
 * Признаки «раздел не докачался, потому что сборка на сервере уже другая»
 * (SHELL-02). `/assets` отдаётся с `Cache-Control: immutable` и
 * `try_files $uri =404` (docker/nginx/templates/leadchat.conf.template), так что
 * после каждой выкатки старые чанки исчезают, и вкладка, открытая до неё,
 * получает на переходе в ленивый раздел 404 вместо кода.
 *
 * Строки разные у каждого браузера, поэтому их несколько: Chrome — «Failed to
 * fetch dynamically imported module», Firefox — «error loading dynamically
 * imported module», Safari — «Importing a module script failed», плюс
 * собственная строка vite о предзагрузке стилей чанка.
 */
const STALE_BUILD_MARKERS = [
  "failed to fetch dynamically imported module",
  "error loading dynamically imported module",
  "importing a module script failed",
  "unable to preload css",
  "chunkloaderror",
];

export function isStaleBuildError(error: unknown): boolean {
  if (!error) return false;
  const e = error as { name?: unknown; message?: unknown };
  const text = `${typeof e.name === "string" ? e.name : ""} ${
    typeof e.message === "string" ? e.message : String(error)
  }`.toLowerCase();
  return STALE_BUILD_MARKERS.some((marker) => text.includes(marker));
}

export function crashKind(error: unknown): CrashKind {
  return isStaleBuildError(error) ? "stale-build" : "crash";
}

declare global {
  interface Window {
    /**
     * Sentry, если его когда-нибудь подключат. Во фронте пакета НЕТ — проверено:
     * ни `@sentry/*` в package.json, ни импортов в `src`. Sentry в проекте есть
     * только серверный (`app/core/observability.py`, три процесса Python), и у
     * него свой DSN. Поэтому здесь не импорт, а проверка глобали: браузерный
     * Sentry ставится тегом loader-скрипта в `index.html` и кладёт себя в
     * `window.Sentry` — в тот день телеметрия заработает без правки этого файла,
     * а до тех пор всё уходит в консоль.
     */
    Sentry?: { captureException?: (error: unknown, hint?: unknown) => void };
  }
}

/**
 * Сообщить о поломке. Порядок важен: сначала телеметрия, потом консоль, и
 * телеметрия обёрнута в try — падение отправки не имеет права утащить за собой
 * сам экран отказа (иначе человек снова увидит стек react-router).
 */
export function reportCrash(error: unknown, where: string, componentStack?: string | null): void {
  const sentry = window.Sentry;
  if (typeof sentry?.captureException === "function") {
    try {
      sentry.captureException(error, {
        tags: { where },
        contexts: componentStack ? { react: { componentStack } } : undefined,
      });
    } catch {
      /* телеметрия молчит — экран отказа всё равно должен показаться */
    }
  }
  // Консоль — не для оператора, а для того, кто разбирает беду по удалёнке.
  console.error(`[LeadChat] сбой отрисовки (${where})`, error, componentStack ?? "");
}

/**
 * Переход и перезагрузка вынесены в объект НАМЕРЕННО (подробности — в самом
 * модуле). Реэкспорт, а не объявление: тот же объект просит и мирный баннер
 * «Вышло обновление» из `platform/UpdateBanner`, и тянуть ради него аварийный
 * экран в платформенный слой незачем. Ссылка одна — подмена в тесте видна
 * обоим потребителям.
 */
export { pageActions };

/**
 * Автоперезагрузка при устаревшей сборке — не чаще раза в десять минут на
 * вкладку. Ключ в `sessionStorage`, потому что нужна память ИМЕННО этой вкладки
 * и ИМЕННО через перезагрузку: `location.reload()` стирает всё в памяти.
 *
 * Защита от петли обязательна: если раздел не грузится не из-за выкатки, а
 * из-за сломанного nginx, автоперезагрузка без счётчика превратила бы рабочее
 * место в мигающую страницу, которую нечем остановить.
 *
 * Черновики сообщений переживают перезагрузку — они в localStorage
 * (`shared/stores/chatUiStore.ts`, persist), — иначе автоперезагрузку нельзя
 * было бы включать вовсе.
 */
const STALE_RELOAD_KEY = "lc:stale-build-reload-at";
const STALE_RELOAD_COOLDOWN_MS = 10 * 60 * 1000;

export function allowAutoReload(now: number = Date.now()): boolean {
  try {
    const raw = window.sessionStorage.getItem(STALE_RELOAD_KEY);
    const last = raw ? Number(raw) : 0;
    if (Number.isFinite(last) && last > 0 && now - last < STALE_RELOAD_COOLDOWN_MS) return false;
    window.sessionStorage.setItem(STALE_RELOAD_KEY, String(now));
    return true;
  } catch {
    // Хранилище недоступно (приватный режим, политика браузера) — отличить
    // первую перезагрузку от сотой нечем, поэтому не перезагружаем совсем.
    return false;
  }
}

const TEXTS: Record<CrashKind, { title: string; body: string }> = {
  "stale-build": {
    title: "Вышла новая версия",
    body: "Пока эта вкладка была открыта, приложение обновилось, и раздел не догрузился. Обновите страницу — всё откроется. Ничего не потеряно: незаконченные ответы сохраняются.",
  },
  crash: {
    title: "Что-то сломалось на этом экране",
    body: "Мы не смогли показать этот раздел. Остальная система работает: обновите страницу или вернитесь к диалогам. Если повторится — скажите администратору, в каком разделе это случилось.",
  },
};

/** Сам экран отказа. Ни стека, ни текста исключения — только что делать дальше. */
export function ErrorScreen({ error }: { error: unknown }) {
  const kind = crashKind(error);
  const [reloading, setReloading] = useState(false);

  useEffect(() => {
    if (kind !== "stale-build") return;
    if (!allowAutoReload()) return;
    setReloading(true);
    pageActions.reload();
  }, [kind]);

  const text = TEXTS[kind];
  return (
    <Stack
      align="center"
      justify="center"
      gap="var(--lc-space-3)"
      role="alert"
      data-crash={kind}
      style={{ minHeight: "60dvh", padding: "var(--lc-space-6)" }}
    >
      <Title order={1} fz="var(--lc-fz-page)" c="var(--lc-text-1)" ta="center">
        {text.title}
      </Title>
      <Text fz="sm" c="var(--lc-text-2)" ta="center" maw="var(--lc-prose-max)">
        {reloading ? "Обновляем страницу…" : text.body}
      </Text>
      <Group gap="var(--lc-space-2)">
        <Button onClick={() => pageActions.reload()}>Обновить страницу</Button>
        <Button variant="outline" onClick={() => pageActions.goHome()}>
          К диалогам
        </Button>
      </Group>
    </Stack>
  );
}

/**
 * `errorElement` маршрутов. Ставится на КАЖДЫЙ ленивый раздел (см. router.tsx):
 * поставить его один раз на общего родителя было бы дешевле, но тогда ошибка
 * всплывала бы до `AppLayout` и уносила шапку с рельсой — то самое, от чего
 * лечимся.
 */
export function RouteErrorScreen() {
  const error = useRouteError();
  useEffect(() => {
    reportCrash(error, "route");
  }, [error]);
  return <ErrorScreen error={error} />;
}

interface BoundaryProps {
  children: ReactNode;
  /** Куда попала беда — уходит в телеметрию тегом: `app`, `screen`. */
  where: string;
  /**
   * Смена этого значения снимает экран отказа. Внутри `AppLayout` сюда идёт
   * путь маршрута: без сброса один сбой запирал бы человека на экране отказа
   * навсегда — граница React сама себя не чинит, а переход в другой раздел
   * меняет только содержимое `Outlet`. Так же ведёт себя и собственная граница
   * react-router (сброс по смене location).
   */
  resetKey?: string;
}

interface BoundaryState {
  error: unknown;
  resetKey?: string;
}

export class ErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  constructor(props: BoundaryProps) {
    super(props);
    this.state = { error: null, resetKey: props.resetKey };
  }

  static getDerivedStateFromError(error: unknown): Partial<BoundaryState> {
    return { error };
  }

  static getDerivedStateFromProps(props: BoundaryProps, state: BoundaryState): BoundaryState | null {
    if (state.resetKey === props.resetKey) return null;
    return { error: null, resetKey: props.resetKey };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    reportCrash(error, this.props.where, info.componentStack);
  }

  render() {
    if (this.state.error) return <ErrorScreen error={this.state.error} />;
    return this.props.children;
  }
}
