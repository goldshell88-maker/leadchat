import { describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMutation } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { MessageBubble } from "@/features/chats/components/thread/MessageBubble";

/**
 * ПУЗЫРЬ ЛЕНТЫ ПЕРЕРИСОВЫВАЕТСЯ ТОЛЬКО КОГДА ИЗМЕНИЛСЯ САМ (03.09).
 *
 * До правки любое состояние панели ленты перерисовывало ВСЕ отрисованные
 * пузыри, а их до тридцати четырёх при девяти-десяти видимых. Пузырь тяжёлый:
 * цитата, вложения, кандидаты в телефоны, меню действий.
 *
 * ⚠ У ПРАВКИ ДВЕ ПОЛОВИНЫ, И ВТОРАЯ ВАЖНЕЕ. `memo` без стабильных колбэков не
 * делает ничего: `onRetry`/`onDiscard`/`onDeleteNote` зависели от ОБЪЕКТОВ
 * мутаций, а `useMutation` возвращает новый объект на каждый рендер — пропс
 * всегда «изменялся». Теперь зависимость взята от метода `mutate`.
 */

const обёртка = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    {children}
  </QueryClientProvider>
);

describe("перерисовка пузырей ленты", () => {
  it("пузырь обёрнут в memo", () => {
    /*
     * Проверка структурная намеренно, и это НЕ греп по тексту исходника:
     * читается значение самого экспорта в рантайме. Снимут `memo` — здесь
     * упадёт, чем бы файл при этом ни выглядел.
     */
    expect(
      (MessageBubble as unknown as { $$typeof?: symbol }).$$typeof,
      "пузырь не мемоизирован — любое состояние панели перерисовывает всю ленту",
    ).toBe(Symbol.for("react.memo"));
  });

  it("ссылка на mutate постоянна между рендерами, а на сам объект — нет", () => {
    /*
     * ⚠ ЭТО ДОПУЩЕНИЕ, НА КОТОРОМ ДЕРЖИТСЯ ПРАВКА, И ЕГО НАДО СТОРОЖИТЬ.
     *
     * `useCallback(..., [retry])` пересоздавался всегда, потому что
     * `useMutation` собирает новый объект результата на каждый рендер.
     * `mutate` при этом стабилен. Обновление react-query, которое это
     * изменит, тихо вернёт лишние перерисовки — и заметить это будет нечем,
     * кроме вот такой проверки.
     */
    const { result, rerender } = renderHook(
      () => useMutation({ mutationFn: async () => undefined }),
      { wrapper: обёртка },
    );
    const первыйОбъект = result.current;
    const перваяФункция = result.current.mutate;

    rerender();

    expect(result.current, "объект результата вдруг стал стабильным").not.toBe(первыйОбъект);
    expect(
      result.current.mutate,
      "mutate перестал быть стабильным — зависимости колбэков ленты снова придётся пересматривать",
    ).toBe(перваяФункция);
  });
});
