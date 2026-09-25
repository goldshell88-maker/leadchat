import { beforeEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { queryClient } from "@/app/queryClient";
import { CONVERSATIONS_LIST_KEY, qk } from "@/shared/api/queryKeys";
import { resyncNow } from "@/shared/realtime/quietResync";
import { useChatUiStore } from "@/shared/stores/chatUiStore";

/**
 * ТИХАЯ СВЕРКА: КЛЮЧИ ЧАТОВ ЯВНО, ОСТАЛЬНОЕ — ПО СВОЕМУ СРОКУ.
 *
 * ⚠ ЭТОТ ФАЙЛ ПЕРЕПИСАН 06.09 ВМЕСТЕ С ПРАВИЛОМ. 31.08 по просьбе владельца
 * («чтобы мне никогда не пришлось обновлять страницу») сверка стала
 * сплошной: всё, у чего есть наблюдатель. Три беды того дня, ради которых её
 * заводили, были про кадры чатов — шкала ожидания, `in_inbox` при закрытии,
 * имя поля у провала доставки. А цена сплошного обхода измерена 06.09: на
 * экране чатов 10–17 активных запросов, включая шаблоны со `staleTime` пять
 * минут, раз в две минуты с каждой из 11–13 вкладок за одним офисным адресом
 * — часть потока, что дал 344 ответа 429 в сутки и «Не получилось загрузить».
 *
 * Сверяем то, что живёт на кадрах сокета и без них стареет: строки списка,
 * числа над вкладкой, очередь и ОТКРЫТАЯ лента. Прочее правится на своих
 * экранах (мутация сбрасывает ключ) и обновляется своим `staleTime` при
 * следующем открытии.
 */

describe("Тихая сверка обновляет всё видимое", () => {
  beforeEach(() => {
    queryClient.clear();
    vi.restoreAllMocks();
  });

  it("⚠ сверка перечисляет ключи чатов и не обходит всё подряд", () => {
    useChatUiStore.setState({ activeConversationId: "conv-1" });
    const шпион = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);

    resyncNow(true);

    const вызовы = шпион.mock.calls.map((c) => c[0] ?? {});
    expect(
      вызовы.some((v) => !("queryKey" in v)),
      "сплошная сверка вернулась — шаблоны и настройки снова едут раз в две минуты",
    ).toBe(false);
    const ключи = вызовы.map((v) => JSON.stringify((v as { queryKey?: unknown }).queryKey));
    expect(ключи).toContain(JSON.stringify(CONVERSATIONS_LIST_KEY));
    expect(ключи).toContain(JSON.stringify(qk.conversations.counts));
    expect(ключи).toContain(JSON.stringify(qk.inbox.list));
    expect(ключи, "открытая лента не сверяется — потерянный кадр останется потерянным").toContain(
      JSON.stringify(qk.messages.list("conv-1")),
    );
  });

  it("сверяет только видимое, а не весь кэш", () => {
    /*
     * `refetchType: "active"` — это про цену. Ключ без наблюдателя не
     * перезапрашивается: он помечается устаревшим и обновится, когда экран
     * откроют. Иначе тринадцать вкладок раз в две минуты тянули бы всё,
     * что человек когда-либо открывал за смену.
     */
    const шпион = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    resyncNow(true);
    for (const вызов of шпион.mock.calls) {
      const v = (вызов[0] ?? {}) as { queryKey?: unknown; refetchType?: string };
      if (v.queryKey) continue; // точечные сверки живут по своим правилам
      expect(v.refetchType, "сплошная сверка тянет и невидимое — это лишний трафик").toBe("active");
    }
  });

  it("счётчик очереди сверяется отдельно: у него может не быть наблюдателя", () => {
    // Бейдж висит на вкладке, в заголовке браузера и в рейке — то есть живёт
    // дольше открытого списка очереди.
    const исходник = readFileSync("src/shared/realtime/quietResync.ts", "utf-8") as string;
    expect(исходник, "счётчик очереди перестал сверяться").toMatch(/refreshInboxCount\(\)/);
  });

  it("догон после обрыва сверяет всё так же", () => {
    /*
     * После разрыва связи устареть могло что угодно: карточка клиента,
     * настройки, счётчики. Раньше догон перечислял два ключа.
     */
    const весь = readFileSync("src/shared/realtime/applyWsEvent.ts", "utf-8") as string;
    const тело = весь.slice(
      весь.indexOf("function refetchOpenScreens"),
      весь.indexOf("\n}", весь.indexOf("function refetchOpenScreens")),
    );
    expect(тело, "догон после обрыва снова ходит по списку ключей").toMatch(
      /invalidateQueries\(\{ refetchType: "active" \}\)/,
    );
  });
});
