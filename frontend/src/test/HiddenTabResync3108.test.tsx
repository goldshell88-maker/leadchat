import { describe, expect, it } from "vitest";
import { QueryClient, QueryClientProvider, focusManager, useQuery } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";

/**
 * СВЁРНУТАЯ ВКЛАДКА ОБНОВЛЯЕТСЯ ТОЖЕ.
 *
 * ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 31.08 дословно: «Обновляться должно всё, даже если
 * вкладка свёрнута, закрыта и т.д.»
 *
 * ЧТО БЫЛО. Тихая сверка намеренно пропускала скрытую вкладку: «смотреть на
 * неё некому, вернётся — сверим по возврату». Довод неверен дважды. У
 * свёрнутой вкладки видно главное — счётчик в заголовке браузера, бейдж
 * очереди и звук нового обращения, — а держатся они как раз на данных, которые
 * мы не сверяли. И кадр, потерянный при свёрнутой вкладке, не чинился НИКОГДА:
 * сверка по возврату случается, только если человек вернётся, а диспетчер
 * может полсмены работать в соседнем окне Авито.
 *
 * ⚠ ЗАЧЕМ ЗДЕСЬ ВТОРАЯ ПРОВЕРКА, ПРО БИБЛИОТЕКУ. Снять условие в нашем коде
 * мало: react-query знает про фокус окна сама (`focusManager.isFocused()` по
 * умолчанию читает `document.visibilityState`), и если бы она придерживала
 * запросы расфокусированной вкладки, правка вышла бы косметической — пометили
 * устаревшим, а на сервер не сходили.
 *
 * Проверено прогоном, а не по памяти: условие фокуса в `retryer` стоит в
 * `canContinue`, то есть гасит ПОВТОР ПОСЛЕ ОШИБКИ, а не сам запрос (`canStart`
 * фокус не смотрит). Свёрнутое окно ходит на сервер наравне с открытым.
 * Провалившийся в скрытой вкладке запрос дождётся возврата фокуса — и это
 * приемлемо ровно потому, что сверка повторяется сама каждые две минуты.
 */
describe("Свёрнутая вкладка", () => {
  it("сверка в работе не смотрит на видимость вкладки", async () => {
    /*
     * Читаем сам модуль: условие видимости легко вернуть «для экономии», и
     * тогда всё остальное здесь зеленеет впустую — таймер просто не позовёт
     * сверку.
     */
    // @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
    const { readFileSync } = await import("node:fs");
    const src = readFileSync("src/shared/realtime/quietResync.ts", "utf-8") as string;
    const тело = src.slice(src.indexOf("таймер = setInterval"));
    const код = тело.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
    expect(
      код.includes("visibilityState"),
      "в таймер сверки вернулось условие видимости — свёрнутая вкладка снова устареет молча",
    ).toBe(false);
  });

  it("расфокусированное окно ДЕЙСТВИТЕЛЬНО ходит на сервер, а не только метит устаревшим", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    let ходов = 0;
    const обёртка = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    renderHook(
      () =>
        useQuery({
          queryKey: ["проба-свёрнутой-вкладки"],
          queryFn: async () => {
            ходов += 1;
            return ходов;
          },
        }),
      { wrapper: обёртка },
    );
    await waitFor(() => expect(ходов).toBe(1));

    // Вкладку свернули: браузер шлёт visibilitychange, менеджер фокуса гасит окно.
    focusManager.setFocused(false);
    expect(focusManager.isFocused()).toBe(false);

    await qc.invalidateQueries({ refetchType: "active" });
    await waitFor(() =>
      expect(
        ходов,
        "библиотека придержала запрос расфокусированной вкладки — свёрнутая вкладка обновляться не будет",
      ).toBe(2),
    );

    // Возвращаем менеджеру фокуса «решай сам»: иначе соседние наборы,
    // идущие в этом же процессе, унаследуют расфокусированное окно.
    focusManager.setFocused(undefined);
    qc.clear();
  });
});
