import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useRef } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MantineProvider } from "@mantine/core";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { QuickReplyBar } from "@/features/chats/components/composer/QuickReplyBar";
import { qk } from "@/shared/api/queryKeys";
import type { TemplateDto } from "@/shared/api/types";

/**
 * ПРАВКИ ПО РАЗБОРУ ДИЗАЙНА 28.08.
 *
 * Жалоба владельца дословно: «нужно поработать с дизайном всех вкладок… решить
 * все баги с дизайном. Много где едет текст и по-разному открывается у разных
 * пользователей».
 *
 * Разбор шёл по семи поверхностям, каждая находка проверялась отдельно на
 * ОПРОВЕРЖЕНИЕ — с замерами в настоящем браузере, а не на глаз. Здесь заперты
 * те правки, которые без сторожа развалились бы обратно первой же уборкой:
 * свойство в CSS удаляется одним движением и молча.
 */

const ШАБЛОНЫ: TemplateDto[] = [
  { id: "t1", owner_id: "u1", title: "Выезд сегодня", body: "Приедем сегодня", folder: null },
];

function css(путь: string): string {
  return (readFileSync(путь, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, " ");
}

/** Тело правила по точному селектору (первое вхождение). */
function правило(текст: string, селектор: string): string | null {
  const i = текст.indexOf(селектор);
  if (i < 0) return null;
  const открывающая = текст.indexOf("{", i);
  const закрывающая = текст.indexOf("}", открывающая);
  if (открывающая < 0 || закрывающая < 0) return null;
  // Убеждаемся, что между именем и «{» нет другого селектора через запятую.
  if (текст.slice(i + селектор.length, открывающая).trim().replace(/^,/, "").length > 0) return null;
  return текст.slice(открывающая + 1, закрывающая);
}

describe("Текст не едет: перенос там, где строка приходит одним куском", () => {
  const thread = css("src/features/chats/components/thread/chat-thread.css");

  it("имя вложения рвётся, а скрепка не сплющивается", () => {
    /*
     * Замер: имя `IMG_20260828_154233_фото_машины_шильдик_сзади.jpg` в полосе
     * 416px давало чернила на 149px ЗА скруглённым краем пузыря и
     * горизонтальную полосу прокрутки у всей ленты. Скрепка при этом сжималась
     * не «немного», а ДО НУЛЯ: у текстового узла автоминимум равен min-content,
     * у иконки — нулю, и весь дефицит ложился на неё.
     */
    expect(правило(thread, ".msg__file ")).toMatch(/overflow-wrap:\s*anywhere/);
    expect(правило(thread, ".msg__file svg")).toMatch(/flex:\s*none/);
  });

  it("служебное сообщение Авито со ссылкой остаётся внутри своей таблетки", () => {
    /*
     * `max-width: 80%` держит фон, но НЕ текст: замер дал URL на 179px правее
     * серой пилюли, голым на фоне ленты. Авито шлёт такие уведомления со
     * ссылками регулярно.
     */
    expect(правило(thread, ".msg__system-text ")).toMatch(/overflow-wrap:\s*anywhere/);
  });
});

describe("Раскладка не расходится у разных людей и на разных окнах", () => {
  it("ряд управления графиком переносится, а не давится", () => {
    /*
     * Естественная ширина ряда ≈650px, колонка даёт 566px при свёрнутой рейке и
     * 482px при развёрнутой — то есть на окне 1181–1475px он не влезает. Без
     * переноса дефицит уходил в сжатие, а у `.chart-toggle` есть
     * `overflow: hidden`, который обнуляет автоминимум: группа давилась без
     * нижней границы, «по часам» уезжало на вторую строку и срезалось.
     */
    const stats = css("src/features/stats/stats.css");
    expect(правило(stats, ".stats-chart__controls")).toMatch(/flex-wrap:\s*wrap/);
    expect(правило(stats, ".chart-toggle "), "группа сегментов снова сжимается").toMatch(
      /flex:\s*none/,
    );
    expect(правило(stats, ".chart-day "), "блок с датой снова сжимается").toMatch(/flex:\s*none/);
  });

  it("карточный режим таблиц ВКЛЮЧАЕТСЯ: ячейка не остаётся блоком", () => {
    /*
     * ⚠ КОЛЛИЗИЯ ВЕСОВ, А НЕ ОПЕЧАТКА. Ячейке `display: flex` задавался
     * селектором `.lc-table--cards td` — вес (0,1,1). А выше по файлу она же
     * попадала под `.lc-table--cards tr td` — вес (0,1,2) — и получала
     * `display: block`. Более сильное правило выигрывает независимо от порядка,
     * поэтому карточный режим не включался вовсе: подпись из `::before` вставала
     * вплотную к значению, одной строкой без зазора.
     */
    const cards = css("src/app/lc-table-cards.css");
    const блочные = правило(cards, ".lc-table--cards tr");
    expect(блочные, "правило рамы таблицы пропало").toBeTruthy();
    expect(
      блочные,
      "`display: block` снова накрывает ячейку и глушит карточный режим",
    ).toMatch(/display:\s*block/);
    expect(
      cards.includes(".lc-table--cards tr,\n  .lc-table--cards tr td"),
      "ячейка вернулась в блочное правило — карточный режим снова не включится",
    ).toBe(false);
    expect(правило(cards, ".lc-table--cards tr td")).toMatch(/display:\s*flex/);
  });

  it("полоса быстрых ответов — слой, а не строка в потоке", () => {
    /*
     * В потоке она отнимала у ленты ~180px, а при наборе список сужается с пяти
     * строк до нуля — то есть высота композера менялась НА КАЖДУЮ БУКВУ и лента
     * дёргалась под пальцами. Компенсировать нечем: прижимание к низу висит на
     * смене диалога, наблюдатель размера следит только за шириной поля.
     */
    const composer = css("src/features/chats/components/composer/composer.css");
    const полоса = правило(composer, ".quick-replies ");
    expect(полоса).toMatch(/position:\s*absolute/);
    expect(полоса, "слой без привязки к низу композера встанет не там").toMatch(/bottom:/);
    expect(полоса, "margin-bottom у слоя ничего не делает и вводит в заблуждение").not.toMatch(
      /margin-bottom/,
    );
  });
});

describe("Быстрые ответы слушают поле ввода, а не всё окно", () => {
  function стенд(onPick = vi.fn()) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(qk.templates.list("all"), {
      items: ШАБЛОНЫ,
      page: { limit: 50, offset: 0, total: 1 },
    });

    function Bench() {
      const ref = useRef<HTMLTextAreaElement>(null);
      return (
        <>
          <input aria-label="Поиск по диалогам" />
          <textarea aria-label="Текст сообщения" ref={ref} />
          {/* `typed` непустой: с 28.08 полоса молчит над пустым полем — она
              подсказка, а не список, и появляется с первых букв ответа. */}
          <QuickReplyBar convId="conv-1" visible typed="Выез" fieldRef={ref} onPick={onPick} />
        </>
      );
    }

    render(
      <QueryClientProvider client={qc}>
        <MantineProvider>
          <Bench />
        </MantineProvider>
      </QueryClientProvider>,
    );
    return onPick;
  }

  it("Tab в чужом поле НЕ вставляет заготовку", async () => {
    /*
     * ⚠ ПЕРВАЯ РЕДАКЦИЯ ВЕШАЛА `keydown` НА `window` В ФАЗЕ ПЕРЕХВАТА — то есть
     * раньше любого обработчика цели, на всей странице. Tab из поиска по
     * диалогам, из карточки клиента, из шапки вместо перевода фокуса вставлял
     * заготовку в черновик. Полоса видна почти всегда, значит и перехват был
     * почти всегда.
     */
    const onPick = стенд();
    await screen.findByRole("listbox", { name: "Быстрые ответы" });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Поиск по диалогам" }), { key: "Tab" });
    expect(onPick, "полоса украла Tab у чужого поля").not.toHaveBeenCalled();
  });

  it("Tab в поле сообщения вставляет заготовку", async () => {
    const onPick = стенд();
    await screen.findByRole("listbox", { name: "Быстрые ответы" });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Текст сообщения" }), { key: "Tab" });
    expect(onPick).toHaveBeenCalledWith(ШАБЛОНЫ[0]);
  });

  it("сочетания с модификаторами не наши", async () => {
    /*
     * Alt+↑ и Cmd+↓ ходят по тексту и по странице. Гасить их ради подсказки
     * нельзя: человек нажимает их не для неё.
     */
    const onPick = стенд();
    await screen.findByRole("listbox", { name: "Быстрые ответы" });
    const поле = screen.getByRole("textbox", { name: "Текст сообщения" });
    fireEvent.keyDown(поле, { key: "Tab", metaKey: true });
    fireEvent.keyDown(поле, { key: "Tab", altKey: true });
    expect(onPick).not.toHaveBeenCalled();
  });

  it("стрелки не перехватываются за пределами поля", async () => {
    /*
     * ↑ и ↓ на всём окне — это ещё и каретка в любом другом поле, и прокрутка
     * страницы. Проверяем через отметку выбранной строки: она обязана остаться
     * на первой.
     */
    стенд();
    await screen.findByRole("listbox", { name: "Быстрые ответы" });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Поиск по диалогам" }), {
      key: "ArrowDown",
    });
    expect(screen.getAllByRole("option")[0].getAttribute("aria-selected")).toBe("true");
  });
});

describe("Кнопку не выдавливает текст рядом с ней", () => {
  it("крестик чипа сужения остаётся в чипе, а подпись уступает", () => {
    /*
     * `text-overflow` на flex-контейнере инертен: собственный текст становится
     * анонимным флекс-элементом с автоминимумом во всю строку. Он не сжимался,
     * и весь дефицит выдавливал крестик за край — снять сужение было НЕЧЕМ.
     */
    const list = css("src/features/chats/components/list/chat-list.css");
    expect(правило(list, ".chat-list-pane__chip-label"), "подписи чипа нет правила").toMatch(
      /flex:\s*1 1 auto/,
    );
    expect(правило(list, ".chat-list-pane__chip > button")).toMatch(/flex:\s*none/);
    expect(
      правило(list, ".chat-list-pane__chip "),
      "`text-overflow` вернулся на flex-контейнер — он там не работает и обещает то, чего нет",
    ).not.toMatch(/text-overflow/);

    /*
     * ⚠ ПРОВЕРЯЕМ ПРАВИЛО, А НЕ КОЛИЧЕСТВО (05.09). Здесь стояло «подписей
     * ровно две» — по числу чипов, которых тогда было два: состояние и
     * сотрудник. Сегодня чип есть у каждого включённого сужения и рисуется
     * один раз в цикле, то есть подпись в исходнике одна на все чипы. Число
     * стало ложным признаком в обе стороны: оно краснело от правки, которая
     * ничего не сломала, и промолчало бы, заведи кто-то третий чип с голым
     * текстом рядом с двумя правильными. Правило же не изменилось: в КАЖДОМ
     * чипе подпись обязана лежать в своём элементе — до крестика.
     */
    const pane = css("src/features/chats/components/list/ChatListPane.tsx");
    const чипы = pane.split('className="chat-list-pane__chip"').slice(1);
    expect(чипы.length, "чипов сужений в колонке не осталось вовсе").toBeGreaterThan(0);
    for (const хвост of чипы) {
      /*
       * Граница — ПЕРВЫЙ закрывающий `</span>`. Подпись в своём элементе даёт
       * его собственный тег до него; голым текстом первым закроется сам чип, и
       * в куске не окажется ни слова про `__chip-label`. Резать по `<Button`
       * нельзя: хвост тянется до конца файла, и туда попадала бы кнопка
       * «Сбросить всё» — проверка зеленела бы при любой разметке чипа.
       */
      expect(
        хвост.slice(0, хвост.indexOf("</span>")),
        "подпись чипа снова голым текстом внутри flex — крестик опять уедет",
      ).toContain("chat-list-pane__chip-label");
    }
  });

  it("подпись «не загрузилось» не рвётся внутри чипа вложения", () => {
    /*
     * У соседнего имени файла есть `overflow: hidden`, а он обнуляет
     * автоминимум: дележ шёл пропорционально базам, а не «сначала имя». Подпись
     * переносилась на вторую строку и срезалась высотой чипа в 26px.
     */
    const composer = css("src/features/chats/components/composer/composer.css");
    const hint = правило(composer, ".composer__chip-hint");
    expect(hint).toMatch(/flex:\s*none/);
    expect(hint).toMatch(/white-space:\s*nowrap/);
  });
});

describe("Ветки, до которых никто не доходил", () => {
  it("«Никто не берёт» и виджет менеджера — два блока, а не или/или", () => {
    /*
     * ⚠ Здесь была одна цепочка тернарников: «менеджер со `stats:own` →
     * виджет, ИНАЧЕ → предупреждение». А `stats:own` есть у менеджера ВСЕГДА,
     * то есть вторая ветка была недостижима: единственный, кто разбирает
     * очередь руками, не видел «Никто не берёт: N» никогда.
     */
    const src = css("src/features/chats/components/list/ChatListPane.tsx");
    expect(
      src,
      "подвал снова собран одной цепочкой — одна из веток станет недостижимой",
    ).not.toMatch(/role === "manager" && can\("stats:own"\) \? \(/);
    expect(src).toMatch(/\{inboxOpen && inboxEscalated > 0 && \(/);
    expect(src).toMatch(/\{role === "manager" && can\("stats:own"\) && <MyTodayWidget \/>\}/);
  });

  it("метку выбора в пикере двигает и ФОКУС, а не только стрелки", () => {
    /*
     * Enter обрабатывается на корне попапа и вставляет строку под меткой.
     * Строки — настоящие кнопки, ловушки фокуса нет: Tab доводил фокус до
     * второй строки, а Enter вставлял первую. Оператор видел кольцо фокуса на
     * одном шаблоне, а клиенту уходил другой.
     */
    const src = css("src/features/chats/components/composer/TemplatePickerPopover.tsx");
    expect(src).toMatch(/onFocus=\{\(\) => setCursor\(index\)\}/);
  });
});
