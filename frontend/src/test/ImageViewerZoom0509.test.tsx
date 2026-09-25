// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в VoicePlayer0509.test.tsx: сторож только читает файл.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { ImageViewer } from "@/features/chats/components/thread/ImageViewer";
import {
  ВИД_ИСХОДНЫЙ,
  МАСШТАБ_ДВОЙНОГО,
  МАСШТАБ_ПОТОЛОК,
  зажатьСдвиг,
  удержатьТочку,
} from "@/features/chats/components/thread/imageZoom";

/**
 * ПРИСЛАННЫЙ СНИМОК МОЖНО ПРИБЛИЗИТЬ (просьба владельца 05.09: «сделай так,
 * чтобы фото можно было увеличивать, приближать и отдалять»).
 *
 * ⚠ ЧЕГО НЕ БЫЛО. Снимок открывался ВПИСАННЫМ в экран и больше не менялся.
 * Клиент присылает не «картинку», а ДОКАЗАТЕЛЬСТВО: шильдик с моделью и
 * серийным номером, экран с кодом ошибки, потёк на задней стенке. Шильдик
 * снят с полуметра, и на снимке, вписанном в 1200 пикселей ширины, его буквы
 * занимают 8–10 пикселей высоты — то есть не читаются вовсе. Оператор шёл
 * смотреть их в само Авито: выходил из системы, ради которой её и ставили.
 *
 * ⚠ ПОЧЕМУ ЧАСТЬ ПРОВЕРОК — ЧИСТЫЕ ЧИСЛА, А НЕ ЭКРАН. В vitest стоит
 * `css: false`, в jsdom нет раскладки: все прямоугольники нулевые, ширины и
 * высоты — нули. Самая дорогая ошибка такого просмотрщика — приближение К
 * ЦЕНТРУ ЭКРАНА вместо точки под курсором — на экране в jsdom не видна вовсе,
 * зато однозначно видна в числах. Поэтому геометрия проверяется числами
 * (`imageZoom.ts`), поведение — событиями, а вид — текстом правила CSS.
 */

const СНИМКИ = [
  { url: "https://avito.example/a.jpg", name: "шильдик.jpg" },
  { url: "https://avito.example/b.jpg", name: "экран-ошибки.jpg" },
];

const CSS = "src/features/chats/components/thread/image-viewer.css";

/** Стенд с состоянием — как в панели ленты: индекс живёт снаружи. */
function Harness() {
  const [i, setI] = useState<number | null>(0);
  return <ImageViewer images={СНИМКИ} index={i} onClose={() => setI(null)} onIndex={setI} />;
}

/**
 * jsdom не знает `PointerEvent`. `MouseEvent` React разбирает как указательное
 * событие, но `pointerId` и `pointerType` в нём отсутствуют — а на них держится
 * и щипок (два пальца), и разделение «палец против мыши».
 */
class ТестовыйУказатель extends MouseEvent {
  pointerId: number;
  pointerType: string;
  constructor(
    type: string,
    init: MouseEventInit & { pointerId?: number; pointerType?: string } = {},
  ) {
    super(type, { bubbles: true, cancelable: true, ...init });
    this.pointerId = init.pointerId ?? 1;
    this.pointerType = init.pointerType ?? "mouse";
  }
}

function указатель(
  el: Element,
  тип: "pointerdown" | "pointermove" | "pointerup",
  init: MouseEventInit & { pointerId?: number; pointerType?: string },
) {
  act(() => {
    fireEvent(el, new ТестовыйУказатель(тип, init));
  });
}

/**
 * Раскладки в jsdom нет, а границы сдвига без неё не посчитать.
 *
 * Снимок 1000×700 в окне 1200×800, левый край в 100, верхний в 60 — то есть
 * центр снимка в (600, 410). Числа не выдуманы: `max-width: 100%` не даёт
 * вписанному снимку быть шире окна, а верх у просмотрщика с полем 48px под
 * полосу с именем — потому центр по вертикали НЕ середина экрана.
 */
function задатьРаскладку(
  img: HTMLElement,
  { ширина = 1000, высота = 700, слева = 100, сверху = 60 } = {},
) {
  for (const [имя, значение] of [
    ["offsetWidth", ширина],
    ["offsetHeight", высота],
    ["offsetLeft", слева],
    ["offsetTop", сверху],
  ] as const) {
    Object.defineProperty(img, имя, { configurable: true, value: значение });
  }
  window.innerWidth = 1200;
  window.innerHeight = 800;
}

/** Масштаб и сдвиг, прочитанные из `style` — то, что реально видит человек. */
function вид(img: HTMLElement) {
  const t = img.style.transform;
  const сдвиг = /translate\((-?[\d.]+)px, (-?[\d.]+)px\)/.exec(t);
  const масштаб = /scale\(([\d.]+)\)/.exec(t);
  return {
    x: Number(сдвиг?.[1] ?? NaN),
    y: Number(сдвиг?.[2] ?? NaN),
    масштаб: Number(масштаб?.[1] ?? NaN),
  };
}

function колесо(el: Element, init: { deltaY: number; clientX: number; clientY: number }) {
  const событие = new WheelEvent("wheel", { bubbles: true, cancelable: true, ...init });
  act(() => {
    el.dispatchEvent(событие);
  });
  return событие;
}

function открыть() {
  const итог = render(<Harness />);
  const img = screen.getByAltText("шильдик.jpg");
  задатьРаскладку(img);
  return { ...итог, img, окно: screen.getByRole("dialog") };
}

/** Снимок ещё грузится: размеров нет, границы сдвига считать не по чему. */
function открытьБезРаскладки() {
  render(<Harness />);
  return { img: screen.getByAltText("шильдик.jpg"), окно: screen.getByRole("dialog") };
}

describe("Геометрия приближения — числами", () => {
  /*
   * ⚠ ЭТО ГЛАВНАЯ ПРОВЕРКА ФАЙЛА. Просмотрщик, который приближает к центру
   * экрана, уводит из виду ровно то место, куда человек целился курсором, — и
   * замечается это мгновенно, потому что целятся всегда в мелкий текст.
   */
  it("точка под курсором остаётся под курсором", () => {
    /*
     * ⚠ ТОЧКУ СНИМКА БЕРЁМ ДО ИЗМЕНЕНИЯ, А ПОТОМ СЧИТАЕМ ЕЁ ЖЕ ПОСЛЕ. Соблазн
     * посчитать её из НОВОГО вида даёт тождество «курсор === курсор», которое
     * зеленеет при любой реализации, включая приближение к центру.
     */
    const курсор = { x: 300, y: -200 };
    const было = { масштаб: 2, x: 40, y: -15 };
    // Какая точка снимка сейчас под курсором: экран = сдвиг + масштаб × точка.
    const подКурсором = {
      x: (курсор.x - было.x) / было.масштаб,
      y: (курсор.y - было.y) / было.масштаб,
    };

    const стало = удержатьТочку(было, 4, курсор, курсор);
    expect(
      стало.x + стало.масштаб * подКурсором.x,
      "точка интереса уехала из-под курсора",
    ).toBeCloseTo(курсор.x, 6);
    expect(стало.y + стало.масштаб * подКурсором.y).toBeCloseTo(курсор.y, 6);

    // И прямо, числами: k = 4/2 = 2, сдвиг' = стала − k × (была − сдвиг).
    // Приближение к центру экрана оставило бы сдвиг нетронутым (40 и −15).
    expect(стало.x, "приближаем к центру экрана, а не к точке интереса").toBeCloseTo(-220, 6);
    expect(стало.y).toBeCloseTo(170, 6);
  });

  it("перетаскивание — тот же расчёт при неизменном масштабе", () => {
    const стало = удержатьТочку({ масштаб: 3, x: 10, y: 20 }, 3, { x: 0, y: 0 }, { x: 50, y: -30 });
    expect(стало).toEqual({ масштаб: 3, x: 60, y: -10 });
  });

  it("масштаб не уходит ниже вписанного и выше 8×", () => {
    /*
     * ⚠ ЧИСЛО ЗДЕСЬ НАПИСАНО, А НЕ ВЗЯТО ИЗ КОДА. Сверка с той же константой,
     * которой пользуется код, — тавтология: подними потолок до 99, и проверка
     * уедет вместе с ним, оставшись зелёной.
     *
     * Почему 8: телефон снимает 3000–4000 пикселей по ширине, экран оператора
     * показывает около 1200 — вписанный снимок это уже примерно 0,3 своего
     * пикселя, то есть 3× возвращает снимку его собственные пиксели, а 8× даёт
     * два с половиной раза сверх оригинала. Дальше растёт не подробность, а
     * размер квадратиков.
     */
    expect(МАСШТАБ_ПОТОЛОК).toBe(8);
    expect(удержатьТочку(ВИД_ИСХОДНЫЙ, 0.2, { x: 0, y: 0 }, { x: 0, y: 0 }).масштаб).toBe(1);
    expect(удержатьТочку(ВИД_ИСХОДНЫЙ, 99, { x: 0, y: 0 }, { x: 0, y: 0 }).масштаб).toBe(8);
  });

  it("границы: под снимком не проглядывает пустота, и он не улетает за край", () => {
    /*
     * Снимок 2400px на экране 1200px, центр в 600 — уехать он может ровно на
     * (2400 − 1200) / 2 = 600 в каждую сторону. Дальше у края окна открылась бы
     * чернота, а человек «потерял» бы снимок.
     */
    expect(зажатьСдвиг(5000, 600, 1200, 1200)).toBe(600);
    expect(зажатьСдвиг(-5000, 600, 1200, 1200)).toBe(-600);
    expect(зажатьСдвиг(120, 600, 1200, 1200)).toBe(120);
  });

  it("снимок мельче окна стоит по месту раскладки, а не «немного вбок»", () => {
    expect(зажатьСдвиг(300, 600, 200, 1200)).toBe(0);
    expect(зажатьСдвиг(-300, 600, 200, 1200)).toBe(0);
  });
});

describe("Приближение на экране", () => {
  it("колесо приближает и НЕ отдаёт жест браузеру", () => {
    /*
     * Ctrl+колесо — это щипок на трекпаде macOS и Windows. Не перехвати мы
     * его — браузер увеличил бы весь интерфейс, а снимок остался бы прежним.
     * Проверяем оба: и обычное колесо (прокручивать под просмотрщиком нечего),
     * и Ctrl.
     */
    const { img, окно } = открыть();
    expect(вид(img).масштаб).toBe(1);

    const обычное = колесо(окно, { deltaY: -400, clientX: 600, clientY: 400 });
    expect(обычное.defaultPrevented, "колесо отдано браузеру").toBe(true);
    expect(вид(img).масштаб, "колесо не приближает").toBeGreaterThan(1);

    const было = вид(img).масштаб;
    const сCtrl = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      deltaY: -400,
      clientX: 600,
      clientY: 400,
    });
    act(() => {
      окно.dispatchEvent(сCtrl);
    });
    expect(сCtrl.defaultPrevented, "Ctrl+колесо увеличит весь интерфейс").toBe(true);
    expect(вид(img).масштаб).toBeGreaterThan(было);
  });

  it("колесо тянет снимок к курсору, а не к середине экрана", () => {
    const { img, окно } = открыть();
    // Центр снимка на экране: offsetLeft −200 + 1600/2 = 600 по X, 400 по Y.
    // Курсор в левом верхнем углу снимка — сдвиг обязан уехать вправо и вниз.
    колесо(окно, { deltaY: -400, clientX: 100, clientY: 100 });
    expect(вид(img).x, "приближение идёт к центру, а не к курсору").toBeGreaterThan(0);
    expect(вид(img).y).toBeGreaterThan(0);
  });

  it("колесо в «строках» (Firefox) приближает так же, как в пикселях", () => {
    /*
     * `deltaMode` бывает не только пиксельным: Firefox шлёт СТРОКИ, и одно и то
     * же движение колеса приходит как deltaY 3 вместо 100. Без приведения к
     * пикселям приближение там оказывалось в тридцать раз слабее — то есть
     * колесо «почти не работало» ровно у тех, кто сидит в Firefox.
     */
    const { img, окно } = открыть();
    const событие = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      deltaMode: 1,
      deltaY: -3,
      clientX: 600,
      clientY: 400,
    });
    act(() => {
      окно.dispatchEvent(событие);
    });
    // 3 строки × 16px = 48px → примерно 1,127. Без приведения вышло бы 1,0075.
    expect(вид(img).масштаб, "колесо в строках приближает почти незаметно").toBeGreaterThan(1.1);
  });

  it("колесо не уводит выше потолка, сколько ни крути", () => {
    const { img, окно } = открыть();
    for (let i = 0; i < 40; i += 1) колесо(окно, { deltaY: -400, clientX: 600, clientY: 400 });
    expect(вид(img).масштаб).toBe(8);
  });

  it("колесо назад возвращает ровно во вписанный вид, без «почти»", () => {
    const { img, окно } = открыть();
    колесо(окно, { deltaY: -400, clientX: 300, clientY: 300 });
    for (let i = 0; i < 40; i += 1) колесо(окно, { deltaY: 400, clientX: 300, clientY: 300 });
    expect(вид(img).масштаб).toBe(1);
    // Вписанный снимок обязан стоять на месте раскладки: иначе он выглядит
    // сбитым набок, и человек не понимает, вернулся вид или нет.
    expect(вид(img).x).toBe(0);
    expect(вид(img).y).toBe(0);
  });

  it("двойное нажатие мышью приближает к точке нажатия и возвращает обратно", () => {
    const { img } = открыть();
    act(() => {
      fireEvent.dblClick(img, { clientX: 200, clientY: 200 });
    });
    /*
     * ⚠ ЧИСЛО НАПИСАНО, А НЕ ВЗЯТО ИЗ КОДА: сверка с той же константой уедет
     * вместе с ней. 3× — это примерно «один к одному» по пикселям снимка с
     * телефона: мелкий текст уже читается, а окружение ещё видно.
     */
    expect(МАСШТАБ_ДВОЙНОГО).toBe(3);
    expect(вид(img).масштаб).toBe(3);
    expect(вид(img).x, "двойное нажатие приближает к центру, а не к точке").not.toBe(0);

    act(() => {
      fireEvent.dblClick(img, { clientX: 200, clientY: 200 });
    });
    expect(вид(img).масштаб).toBe(1);
    expect(вид(img).x).toBe(0);
  });
});

describe("Перетаскивание приближённого снимка", () => {
  function приблизить(окно: HTMLElement) {
    колесо(окно, { deltaY: -1200, clientX: 600, clientY: 400 });
  }

  it("снимок идёт за указателем", () => {
    const { img, окно } = открыть();
    приблизить(окно);
    const было = вид(img);

    указатель(img, "pointerdown", { clientX: 600, clientY: 400 });
    указатель(img, "pointermove", { clientX: 540, clientY: 370 });
    указатель(img, "pointerup", { clientX: 540, clientY: 370 });

    expect(вид(img).x).toBeCloseTo(было.x - 60, 5);
    expect(вид(img).y).toBeCloseTo(было.y - 30, 5);
  });

  it("вписанный снимок не таскается: ему некуда ехать", () => {
    const { img } = открыть();
    указатель(img, "pointerdown", { clientX: 600, clientY: 400 });
    указатель(img, "pointermove", { clientX: 300, clientY: 200 });
    указатель(img, "pointerup", { clientX: 300, clientY: 200 });
    expect(вид(img)).toEqual({ масштаб: 1, x: 0, y: 0 });
  });

  it("снимок ещё грузится — тащить его тоже нельзя", () => {
    /*
     * Границы сдвига считаются по размеру снимка, а до загрузки размера нет —
     * зажимать нечем. Держит здесь другое: точка интереса меряется ОТ ЦЕНТРА
     * снимка, а центра тоже ещё нет, и обе точки жеста сходятся в ноль. Проверка
     * стоит потому, что путь этот отдельный: снимок должен появиться на своём
     * месте, а не сбитым набок протяжкой по пустому экрану.
     */
    const { img } = открытьБезРаскладки();
    указатель(img, "pointerdown", { clientX: 600, clientY: 400 });
    указатель(img, "pointermove", { clientX: 200, clientY: 100 });
    указатель(img, "pointerup", { clientX: 200, clientY: 100 });
    expect(вид(img)).toEqual({ масштаб: 1, x: 0, y: 0 });
  });

  /*
   * ⚠ ГЛАВНАЯ ПРОВЕРКА ГРАНИЦЫ. Если шаг считать от НАЧАЛА жеста, то уезд на
   * 900px за край создаёт мёртвую зону в те же 900px: палец идёт обратно, а
   * снимок стоит. Это и есть «залипание», о котором просил владелец.
   */
  it("уехали далеко за край и вернулись — снимок трогается сразу", () => {
    const { img, окно } = открыть();
    приблизить(окно);

    указатель(img, "pointerdown", { clientX: 600, clientY: 400 });
    // Уезд на 2000px при пределе (1000×5 − 1200)/2 = 1900: снимок стоит у края.
    указатель(img, "pointermove", { clientX: 2600, clientY: 400 });
    const наГранице = вид(img).x;
    expect(наГранице, "снимок не дошёл до границы — проверка ни о чём").toBeCloseTo(1900, 5);

    указатель(img, "pointermove", { clientX: 2550, clientY: 400 });
    expect(вид(img).x, "снимок залип на границе").toBeCloseTo(1850, 5);
    указатель(img, "pointerup", { clientX: 2550, clientY: 400 });
  });

  it("за край не улетает: у окна остаётся снимок, а не чернота", () => {
    const { img, окно } = открыть();
    приблизить(окно);
    const масштаб = вид(img).масштаб;
    // Предел сдвига: (1000 × масштаб − 1200) / 2, центр снимка ровно посередине.
    const предел = (1000 * масштаб - 1200) / 2;

    указатель(img, "pointerdown", { clientX: 600, clientY: 400 });
    указатель(img, "pointermove", { clientX: 9000, clientY: 400 });
    указатель(img, "pointerup", { clientX: 9000, clientY: 400 });
    expect(вид(img).x).toBeCloseTo(предел, 5);
  });

  it("перетаскивание не анимируется — оно обязано идти за пальцем", () => {
    const { img, окно } = открыть();
    приблизить(окно);
    указатель(img, "pointerdown", { clientX: 600, clientY: 400 });
    указатель(img, "pointermove", { clientX: 500, clientY: 400 });
    expect(img.style.transition, "снимок догоняет палец с задержкой").toBe("none");
    указатель(img, "pointerup", { clientX: 500, clientY: 400 });
  });
});

describe("Щипок двумя пальцами", () => {
  it("приближает к середине между пальцами, а не к центру экрана", () => {
    const { img } = открыть();
    // Пальцы сходятся вокруг точки (300, 200) — левее и выше центра снимка.
    указатель(img, "pointerdown", { pointerId: 1, pointerType: "touch", clientX: 250, clientY: 200 });
    указатель(img, "pointerdown", { pointerId: 2, pointerType: "touch", clientX: 350, clientY: 200 });
    указатель(img, "pointermove", { pointerId: 1, pointerType: "touch", clientX: 150, clientY: 200 });
    указатель(img, "pointermove", { pointerId: 2, pointerType: "touch", clientX: 450, clientY: 200 });

    expect(вид(img).масштаб, "щипок не приближает").toBeCloseTo(3, 5);
    expect(вид(img).x, "щипок приближает к центру, а не к пальцам").toBeGreaterThan(0);

    указатель(img, "pointerup", { pointerId: 1, pointerType: "touch", clientX: 150, clientY: 200 });
    указатель(img, "pointerup", { pointerId: 2, pointerType: "touch", clientX: 450, clientY: 200 });
  });

  /*
   * ⚠ ОТПУСТИЛИ ОДИН ПАЛЕЦ ИЗ ДВУХ. Не перезадай мы опору по оставшемуся,
   * следующий же `pointermove` посчитает шаг от СЕРЕДИНЫ между пальцами до
   * одного пальца — снимок прыгнул бы на пол-расстояния между ними. На щипке
   * пальцы расходятся на пол-экрана, то есть прыжок был бы в сотни пикселей.
   */
  it("один палец убрали — снимок не прыгает", () => {
    const { img } = открыть();
    указатель(img, "pointerdown", { pointerId: 1, pointerType: "touch", clientX: 500, clientY: 400 });
    указатель(img, "pointerdown", { pointerId: 2, pointerType: "touch", clientX: 700, clientY: 400 });
    указатель(img, "pointermove", { pointerId: 1, pointerType: "touch", clientX: 200, clientY: 400 });
    указатель(img, "pointermove", { pointerId: 2, pointerType: "touch", clientX: 1000, clientY: 400 });

    указатель(img, "pointerup", { pointerId: 1, pointerType: "touch", clientX: 200, clientY: 400 });
    const доДвижения = вид(img);
    // Оставшийся палец не двигается — и снимок обязан стоять.
    указатель(img, "pointermove", { pointerId: 2, pointerType: "touch", clientX: 1000, clientY: 400 });
    expect(вид(img).x, "снимок прыгнул при снятии пальца").toBeCloseTo(доДвижения.x, 5);
    указатель(img, "pointerup", { pointerId: 2, pointerType: "touch", clientX: 1000, clientY: 400 });
  });

  it("палец проехал по вписанному снимку — это не касание, приближения не будет", () => {
    /*
     * ⚠ НАЙДЕНО ДИВЕРСИЕЙ, А НЕ ПРИДУМАНО. В первой редакции стоял досрочный
     * выход «вписанный снимок тянуть некуда» — и стоял ДО подсчёта пройденного
     * пути. Палец, проехавший 200px по снимку, записывался как касание, и
     * следующее касание рядом складывалось с ним в двойное: снимок приближался
     * сам собой, без единого намерения человека.
     */
    const { img } = открыть();
    указатель(img, "pointerdown", { pointerType: "touch", clientX: 300, clientY: 300 });
    указатель(img, "pointermove", { pointerType: "touch", clientX: 500, clientY: 300 });
    указатель(img, "pointerup", { pointerType: "touch", clientX: 500, clientY: 300 });

    указатель(img, "pointerdown", { pointerType: "touch", clientX: 500, clientY: 300 });
    указатель(img, "pointerup", { pointerType: "touch", clientX: 500, clientY: 300 });

    expect(вид(img).масштаб, "снимок приблизился сам, без двойного касания").toBe(1);
  });

  it("браузерный dblclick после двойного касания не отменяет приближение", () => {
    /*
     * Браузеры синтезируют `dblclick` из двойного касания сами — с задержкой и
     * не везде. Считать оба значит приблизить и тут же вернуть обратно: палец
     * приближает снимок, и он на глазах схлопывается назад.
     */
    const { img } = открыть();
    for (const тап of [1, 2]) {
      void тап;
      указатель(img, "pointerdown", { pointerType: "touch", clientX: 300, clientY: 300 });
      указатель(img, "pointerup", { pointerType: "touch", clientX: 300, clientY: 300 });
    }
    expect(вид(img).масштаб).toBe(3);

    act(() => {
      fireEvent.dblClick(img, { clientX: 300, clientY: 300 });
    });
    expect(вид(img).масштаб, "снимок схлопнулся сразу после приближения пальцем").toBe(3);
  });

  it("двойное касание пальцем приближает — и второе возвращает", () => {
    const { img } = открыть();
    const тапнуть = () => {
      указатель(img, "pointerdown", { pointerType: "touch", clientX: 300, clientY: 300 });
      указатель(img, "pointerup", { pointerType: "touch", clientX: 300, clientY: 300 });
    };
    тапнуть();
    тапнуть();
    expect(вид(img).масштаб).toBe(3);

    тапнуть();
    тапнуть();
    expect(вид(img).масштаб).toBe(1);
  });
});

describe("Клавиатура", () => {
  it("+ и − меняют масштаб, 0 возвращает исходный вид", () => {
    const { img } = открыть();
    act(() => {
      fireEvent.keyDown(window, { key: "+" });
    });
    expect(вид(img).масштаб).toBeCloseTo(1.5, 5);
    act(() => {
      fireEvent.keyDown(window, { key: "=" });
    });
    expect(вид(img).масштаб).toBeCloseTo(2.25, 5);
    act(() => {
      fireEvent.keyDown(window, { key: "-" });
    });
    expect(вид(img).масштаб).toBeCloseTo(1.5, 5);

    act(() => {
      fireEvent.keyDown(window, { key: "0" });
    });
    expect(вид(img)).toEqual({ масштаб: 1, x: 0, y: 0 });
  });

  it("Ctrl+= и Ctrl+0 не отнимаются у браузера", () => {
    /*
     * Ими увеличивают ВЕСЬ интерфейс, и пользуются этим те, кому мелок не один
     * снимок. Перехватив их, мы отняли бы у человека единственное средство.
     */
    const { img } = открыть();
    act(() => {
      fireEvent.keyDown(window, { key: "=", ctrlKey: true });
    });
    expect(вид(img).масштаб).toBe(1);
  });

  /*
   * ⚠ У СТРЕЛОК УЖЕ ЕСТЬ РАБОТА: они водят по снимкам диалога, и отнимать её
   * нельзя — клиент присылает снимки пачкой. Делим по масштабу: вписанный
   * снимок сдвигать некуда, приближённому переход не нужен (рядом кнопки ‹ ›
   * и клавиша 0).
   */
  it("вписанный снимок: стрелки по-прежнему ходят по снимкам диалога", () => {
    открыть();
    act(() => {
      fireEvent.keyDown(window, { key: "ArrowRight" });
    });
    expect(screen.getByAltText("экран-ошибки.jpg")).toBeTruthy();
  });

  it("приближённый снимок: стрелки двигают его, а снимок остаётся тот же", () => {
    /*
     * ⚠ ПОСЛЕ КАЖДОЙ СТРЕЛКИ СПРАШИВАЕМ, ТОТ ЛИ ЭТО СНИМОК. Соседний снимок
     * открывается ИСХОДНЫМ видом, то есть с нулевым сдвигом, — и проверка,
     * которая сверяет сдвиг с нулём, не отличает «вернулись на место» от
     * «ушли на другой снимок».
     */
    const { img } = открыть();
    act(() => {
      fireEvent.keyDown(window, { key: "+" });
    });
    expect(вид(img).масштаб).toBeCloseTo(1.5, 5);

    act(() => {
      fireEvent.keyDown(window, { key: "ArrowLeft" });
    });
    expect(screen.getByAltText("шильдик.jpg"), "стрелка увела на другой снимок").toBeTruthy();
    expect(вид(img).x, "стрелка не сдвинула приближённый снимок").toBeCloseTo(80, 5);

    act(() => {
      fireEvent.keyDown(window, { key: "ArrowRight" });
    });
    expect(screen.getByAltText("шильдик.jpg"), "стрелка увела на другой снимок").toBeTruthy();
    expect(вид(img).x).toBeCloseTo(0, 5);

    // ⚠ Сравниваем с тем, что БЫЛО: у приближённого к центру снимка сдвиг по
    // вертикали не нулевой и без стрелки (сверху поле 48px), и проверка
    // «больше нуля» зеленела бы, даже если ↑ не делает ничего.
    const поВертикали = вид(img).y;
    act(() => {
      fireEvent.keyDown(window, { key: "ArrowUp" });
    });
    expect(screen.getByAltText("шильдик.jpg")).toBeTruthy();
    expect(вид(img).y, "↑ не сдвинула приближённый снимок").toBeCloseTo(поВертикали + 80, 5);
  });
});

describe("Возврат и видимый выход", () => {
  it("переход на другой снимок открывает его исходным видом", () => {
    /*
     * Иначе следующий снимок открывается приближённым в случайное место: у
     * снимков разные пропорции, и «то же место» на другом — это, как правило,
     * пустой угол.
     */
    const { окно } = открыть();
    колесо(окно, { deltaY: -1200, clientX: 300, clientY: 300 });

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Следующий снимок" }));
    });
    const второй = screen.getByAltText("экран-ошибки.jpg");
    expect(вид(второй)).toEqual({ масштаб: 1, x: 0, y: 0 });
  });

  it("масштаб видно и из него есть выход нажатием", () => {
    /*
     * Про колесо и двойное нажатие знают не все. Приближённый снимок без единой
     * кнопки — ловушка: человек не понимает, как вернуть прежний вид, и
     * закрывает просмотр целиком, теряя место в ленте.
     */
    const { img, окно } = открыть();
    expect(screen.getByText("100 %")).toBeTruthy();

    колесо(окно, { deltaY: -1200, clientX: 600, clientY: 400 });
    const проценты = Math.round(вид(img).масштаб * 100);
    expect(screen.getByText(`${проценты} %`)).toBeTruthy();

    act(() => {
      fireEvent.click(
        screen.getByRole("button", { name: new RegExp(`Масштаб ${проценты} процентов`) }),
      );
    });
    expect(вид(img)).toEqual({ масштаб: 1, x: 0, y: 0 });
    expect(screen.getByText("100 %")).toBeTruthy();
  });

  it("кнопки «Приблизить» и «Отдалить» работают и честно гаснут на краях", () => {
    const { img } = открыть();
    const приблизить = screen.getByRole("button", { name: "Приблизить" });
    const отдалить = screen.getByRole("button", { name: "Отдалить" });

    // На вписанном снимке отдалять нечего — и это видно читалке.
    expect(отдалить.getAttribute("aria-disabled")).toBe("true");
    expect(приблизить.getAttribute("aria-disabled")).toBeNull();

    act(() => {
      fireEvent.click(приблизить);
    });
    expect(вид(img).масштаб).toBeCloseTo(1.5, 5);
    expect(отдалить.getAttribute("aria-disabled")).toBeNull();

    act(() => {
      fireEvent.click(отдалить);
    });
    expect(вид(img).масштаб).toBe(1);
    expect(отдалить.getAttribute("aria-disabled")).toBe("true");
  });

  it("снимок не открылся — кнопок масштаба нет: приближать нечего", () => {
    const { img } = открыть();
    act(() => {
      fireEvent.error(img);
    });
    expect(screen.getByText("Снимок не открылся")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Приблизить" })).toBeNull();
  });
});

describe("Вид: то, что в jsdom не измерить", () => {
  /*
   * `css: false` — стилей в прогоне нет. Эти три правила отвечают за то, что
   * ломается МОЛЧА: снимок налезает на кнопки, вылезает за экран, дёргается у
   * человека с «уменьшить движение».
   */
  const css = () => readFileSync(CSS, "utf-8") as string;
  const тело = (селектор: string) => {
    const текст = css();
    const i = текст.indexOf(`\n${селектор} {`);
    expect(i, `правило ${селектор} пропало`).toBeGreaterThan(-1);
    return текст.slice(i, текст.indexOf("}", i));
  };

  it("полоса с именем и стрелки лежат ПОВЕРХ приближённого снимка", () => {
    /*
     * ⚠ Трансформированный элемент рисуется в одном слое с абсолютно
     * позиционированными соседями — то есть по порядку в разметке, а снимок в
     * ней ПОЗЖЕ полосы и стрелок. Без явного `z-index` первое же приближение
     * прячет имя файла, «Скачать» и «Закрыть» под снимком.
     */
    expect(тело(".imgview__bar"), "имя файла и кнопки уйдут под снимок").toContain("z-index: 2");
    expect(тело(".imgview__nav"), "стрелки уйдут под снимок").toContain("z-index: 2");
  });

  it("приближённый снимок обрезается окном, а жест принадлежит нам", () => {
    const правило = тело(".imgview");
    expect(правило, "снимок на 8× вылезет за края экрана").toContain("overflow: hidden");
    expect(правило, "щипок увеличит всю страницу вместо снимка").toContain("touch-action: none");
  });

  it("движение — токеном, и его можно выключить", () => {
    expect(тело(".imgview__img"), "длительность литералом мимо шкалы").toContain(
      "transition: transform var(--lc-dur-fast) var(--lc-ease)",
    );
    /*
     * Общее правило в lc-vars.css гасит `animation`, но НЕ `transition` — и это
     * намеренно (иначе мигают состояния кнопок). Здесь же ездит сам снимок на
     * пол-экрана: ровно то движение, от которого человека укачивает.
     */
    const текст = css();
    const блок = текст.slice(текст.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(блок, "снимок продолжит ездить у тех, кто просил не двигать").toContain(
      ".imgview__img",
    );
    expect(блок).toContain("transition: none");
  });
});
