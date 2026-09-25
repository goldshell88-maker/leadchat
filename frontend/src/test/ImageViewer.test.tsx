// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { ImageViewer } from "@/features/chats/components/thread/ImageViewer";

/**
 * ПРИСЛАННЫЙ СНИМОК МОЖНО ОТКРЫТЬ И РАЗГЛЯДЕТЬ.
 *
 * ⚠ ЧЕГО НЕ БЫЛО. Клиент присылает не «картинку», а ДОКАЗАТЕЛЬСТВО: фотографию
 * экрана с ошибкой, шильдика с моделью, потёка на задней стенке. По ней оператор
 * ставит диагноз и называет цену. В ленте она рисовалась одним `<img>` со
 * стороной 280 пикселей и `object-fit: cover` — то есть ОБРЕЗАННОЙ по краям, —
 * и открыть её было нельзя ничем: ни нажатием, ни правой кнопкой (в десктопе
 * меню браузера нет). Текст ошибки на таком снимке нечитаем, и оператор шёл
 * смотреть его в само Авито — выходил из системы, ради которой её и ставили.
 *
 * Отрисовкой это не проверить полностью: в jsdom нет раскладки. Поэтому здесь
 * проверяется ПОВЕДЕНИЕ — открытие, ходьба по снимкам, клавиатура, честная
 * ошибка, — а правила вёрстки стережёт `bubbleUnderPressure.test.ts`.
 */

const СНИМКИ = [
  { url: "https://avito.example/a.jpg", name: "экран-ошибки.jpg" },
  { url: "https://avito.example/b.jpg", name: "шильдик.jpg" },
  { url: "https://avito.example/c.jpg", name: "разъём.jpg" },
];

/** Стенд с состоянием — как в панели ленты: индекс живёт снаружи. */
function Harness({ start = 0 }: { start?: number | null }) {
  const [i, setI] = useState<number | null>(start);
  return <ImageViewer images={СНИМКИ} index={i} onClose={() => setI(null)} onIndex={setI} />;
}

describe("Просмотр снимка", () => {
  it("закрыт — на экране ничего", () => {
    render(<Harness start={null} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("открытый снимок показан целиком и назван", () => {
    render(<Harness />);
    const img = screen.getByAltText("экран-ошибки.jpg") as HTMLImageElement;
    expect(img.src).toBe("https://avito.example/a.jpg");
    // Имя видно: у файла клиента оно иногда единственная подсказка, что это.
    expect(screen.getByTitle("экран-ошибки.jpg")).toBeTruthy();
  });

  it("по снимкам диалога ходят стрелками, не закрывая просмотр", async () => {
    /*
     * Клиент присылает их пачкой: «вот экран, вот шильдик, вот разъём».
     * Закрывать-открывать каждый значит терять место в ленте — виртуализатор
     * возвращает прокрутку не туда, откуда ушли.
     */
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Следующий снимок" }));
    expect(screen.getByAltText("шильдик.jpg")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Предыдущий снимок" }));
    expect(screen.getByAltText("экран-ошибки.jpg")).toBeTruthy();
  });

  it("список закольцован: с последнего вперёд — на первый", async () => {
    const user = userEvent.setup();
    render(<Harness start={2} />);
    await user.click(screen.getByRole("button", { name: "Следующий снимок" }));
    expect(screen.getByAltText("экран-ошибки.jpg")).toBeTruthy();
  });

  it("клавиатура: ← → ходят по снимкам, Esc закрывает", async () => {
    /*
     * Оператор работает быстро и мышью пользуется меньше, чем кажется. Плюс без
     * клавиатуры просмотр недоступен тем, кто ей и работает.
     */
    render(<Harness />);

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(screen.getByAltText("шильдик.jpg")).toBeTruthy();

    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(screen.getByAltText("экран-ошибки.jpg")).toBeTruthy();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("нажатие по тёмному фону закрывает, по самому снимку — нет", async () => {
    /*
     * Промах при попытке разглядеть деталь не должен закрывать просмотр: человек
     * тянется к краю картинки именно потому, что там мелко.
     */
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByAltText("экран-ошибки.jpg"));
    expect(screen.getByRole("dialog"), "нажатие по снимку закрыло просмотр").toBeTruthy();

    fireEvent.mouseDown(screen.getByRole("dialog"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("ссылка Авито просрочилась — говорим словами, а не битым значком", () => {
    /*
     * «Не открылась» — это НЕ «клиент прислал пустоту». Причина разная и лечится
     * по-разному: за просроченной ссылкой идут в Авито, а не переспрашивают
     * клиента.
     */
    render(<Harness />);
    fireEvent.error(screen.getByAltText("экран-ошибки.jpg"));

    expect(screen.getByText("Снимок не открылся")).toBeTruthy();
    expect(screen.getByText(/Откройте диалог в Авито/)).toBeTruthy();
  });

  it("новый снимок — новая попытка: чужая ошибка на него не переносится", () => {
    render(<Harness />);
    fireEvent.error(screen.getByAltText("экран-ошибки.jpg"));
    expect(screen.getByText("Снимок не открылся")).toBeTruthy();

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(screen.queryByText("Снимок не открылся")).toBeNull();
    expect(screen.getByAltText("шильдик.jpg")).toBeTruthy();
  });

  it("один снимок — стрелок нет: ходить некуда", () => {
    render(
      <ImageViewer
        images={[СНИМКИ[0]]}
        index={0}
        onClose={vi.fn()}
        onIndex={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Следующий снимок" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Предыдущий снимок" })).toBeNull();
  });

  it("снимок можно скачать: диагноз показывают мастеру, а он не за этим экраном", () => {
    render(<Harness />);
    const ссылка = screen.getByRole("link", { name: "Скачать снимок" }) as HTMLAnchorElement;
    expect(ссылка.href).toBe("https://avito.example/a.jpg");
    expect(ссылка.getAttribute("download")).toBe("экран-ошибки.jpg");
  });
});


describe("Проводка просмотра", () => {
  /*
   * ⚠ БЕЗ ЭТИХ ПРОВЕРОК ВСЁ ВЫШЕ ЗЕЛЕНЕЕТ ВПУСТУЮ. Они проверяют компонент, а не
   * то, что его кто-то показывает и что снимок в ленте вообще нажимается. Этот
   * проект уже дважды попадался на такой проводке: заслон существовал,
   * докстрока объясняла, зачем он, — и не звался ниоткуда.
   */
  const без_комментариев = (t: string) =>
    (t as string).replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

  it("панель ленты показывает просмотрщик и знает все снимки диалога", () => {
    const src = без_комментариев(
      readFileSync("src/features/chats/components/thread/ChatThreadPane.tsx", "utf-8") as string,
    );
    expect(src, "просмотрщик не подключён к ленте").toContain("<ImageViewer");
    // Список собирается по ВСЕМ сообщениям, иначе стрелки некуда вести.
    expect(src).toMatch(/flat\.flatMap/);
    expect(src).toMatch(/kind === "image"/);
  });

  it("снимок в пузыре — кнопка, а не картинка", () => {
    /*
     * Открыть его должно быть можно и мышью, и с клавиатуры. `<div onClick>`
     * читался бы скринридером как текст и не получал бы фокус.
     */
    const src = без_комментариев(
      readFileSync("src/features/chats/components/thread/MessageBubble.tsx", "utf-8") as string,
    );
    const i = src.indexOf('kind === "image"');
    expect(i, "ветка картинки исчезла из пузыря").toBeGreaterThan(-1);
    const ветка = src.slice(i, i + 1600);
    expect(ветка, "снимок снова нельзя открыть").toContain("onOpenImage");
    expect(ветка, "снимок перестал быть кнопкой — с клавиатуры не открыть").toContain("<button");
  });
});
