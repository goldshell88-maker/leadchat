import { render } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { BotEditor } from "@/features/settings/bots/BotEditor";
import { BOT_ID } from "./botFixtures";

/**
 * Редактор бота — единственный экран, которому нужен НАСТОЯЩИЙ data-роутер, а
 * не `MemoryRouter` из `render.tsx`.
 *
 * Причина: редактор держит несохранённый сценарий в сторе и останавливает уход
 * со страницы через `useBlocker` (BOT-02). Эта ручка работает только в
 * data-роутере — том самом `createBrowserRouter` из `app/router.tsx`, на
 * котором собрано приложение; на `MemoryRouter` она бросает исключение. То
 * есть тесты редактора обязаны стоять на той же опоре, что и прод, иначе они
 * проверяют другое приложение.
 *
 * Второй маршрут — список ботов: ссылка «← К списку ботов» из шапки редактора
 * должна вести куда-то настоящим переходом, иначе блокировщику нечего ловить.
 */
/*
 * ПОЧЕМУ ЗДЕСЬ ПОДМЕНА `Request`.
 *
 * data-роутер на каждом переходе собирает `new Request(url, { signal })` для
 * загрузчиков маршрута. В тестовом окружении `Request` приходит из Node
 * (undici), а `AbortSignal` — из jsdom, и undici отвергает чужой сигнал:
 * «Expected signal to be an instance of AbortSignal». Переход падает
 * необработанным исключением, и тест показывал бы не поведение экрана, а
 * несовместимость двух реализаций одного стандарта.
 *
 * Загрузчиков у наших маршрутов нет — объект запроса никуда не уходит, поэтому
 * ставим заглушку. Только для файлов, которые тянут этого помощника.
 */
const requestAcceptsJsdomSignal = (() => {
  try {
    new Request("http://localhost/", { signal: new AbortController().signal });
    return true;
  } catch {
    return false;
  }
})();

if (!requestAcceptsJsdomSignal) {
  class TestRequest {
    readonly url: string;
    readonly method: string;
    readonly signal: AbortSignal | undefined;
    constructor(input: string | URL, init: { method?: string; signal?: AbortSignal } = {}) {
      this.url = String(input);
      this.method = init.method ?? "GET";
      this.signal = init.signal;
    }
  }
  globalThis.Request = TestRequest as unknown as typeof Request;
}

/**
 * `container` — необязательный: снималка разметки (`okna-снимок.dump.tsx`)
 * рисует редактор в СВОЁ место с классом `lc-main`, чтобы отделить его от
 * портала ящика. Тесты его не передают и получают прежнее поведение RTL.
 */
export function renderBotEditor(
  { botId = BOT_ID, container }: { botId?: string; container?: HTMLElement } = {},
) {
  const router = createMemoryRouter(
    [
      { path: "/settings/bots/:id", element: <BotEditor /> },
      { path: "/settings/bots", element: <h1>Список ботов</h1> },
    ],
    // История начинается со списка: в редактор приходят оттуда, и без этой
    // записи «Назад» браузера некуда нажимать — проверять было бы нечего.
    { initialEntries: ["/settings/bots", `/settings/bots/${botId}`], initialIndex: 1 },
  );

  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MantineProvider theme={theme} defaultColorScheme="light">
          <RouterProvider router={router} />
        </MantineProvider>
      </QueryClientProvider>,
      container ? { container } : undefined,
    ),
    router,
  };
}
