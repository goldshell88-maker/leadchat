// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в bubbleUnderPressure.test.ts: сторож только читает файлы стилей.
import { readFileSync } from "node:fs";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MessageDto } from "@/shared/api/types";
import { formatClock } from "@/shared/lib/formatTime";
import { ImageViewer } from "@/features/chats/components/thread/ImageViewer";
import { PhotoCollage } from "@/features/chats/components/thread/PhotoCollage";
import {
  собратьСерииСнимков,
  строкаПро,
  type FeedRow,
  type Снимок,
} from "@/features/chats/components/thread/messageSeries";
import { buildThreadRows } from "@/features/chats/components/thread/threadEvents";

/**
 * КОЛЛАЖ ИЗ ПОДРЯД ИДУЩИХ СНИМКОВ (просьба владельца 05.09: «хочу, чтобы фото
 * собирались в коллаж в один и не растягивали переписку»).
 *
 * ⚠ ЗАМЕР БОЯ ЗА 60 ДНЕЙ, ИЗ КОТОРОГО РАСТЁТ ВЕСЬ ЭТОТ ФАЙЛ. Вложений в одном
 * сообщении ВСЕГДА ровно одно (10 857 сообщений, ни одного с двумя) — коллаж
 * внутри сообщения не сработал бы ни разу. Фотографии приходят ПОДРЯД
 * ИДУЩИМИ СООБЩЕНИЯМИ: серий по 2 — 1414, по 3 — 583, по 4 — 244, по 5 — 123,
 * по 6 — 57, по 7 — 19. Каждая давала столько же пузырей по 280 px: семь
 * снимков — почти два экрана на одну мысль клиента.
 *
 * ⚠ ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ, КРОМЕ «КРАСИВО». Склейка ОБЯЗАНА терять ноль
 * признаков сообщения. Половина проверок ниже — про отказ склеивать: ошибка
 * доставки, «отправляется», цитата, подпись под снимком, заметка. Каждый из
 * этих признаков именной, то есть относится к ОДНОМУ сообщению, и общий ответ
 * за семерых был бы враньём.
 */

const ОЛЬГА = { id: "u-1", full_name: "Ольга Никитина" };

function снимок(i: number, over: Partial<MessageDto> = {}): MessageDto {
  return {
    id: `m-${i}`,
    conversation_id: "c-1",
    direction: "in",
    sender_type: "client",
    sender: null,
    body: null,
    attachments: [
      {
        media_id: `a-${i}`,
        kind: "image",
        url: `https://avito.example/${i}.jpg`,
        name: `${i}.jpg`,
        width: 1280,
        height: 960,
      },
    ],
    delivery_status: "delivered",
    // Секунды между снимками — так их и шлёт Авито, пачкой.
    created_at: `2026-09-05T09:00:${String(i * 5).padStart(2, "0")}.000Z`,
    ...over,
  } as MessageDto;
}

/** Серия из N подряд идущих снимков клиента. */
const серия = (n: number) => Array.from({ length: n }, (_, k) => снимок(k + 1));

const текст = (i: number, over: Partial<MessageDto> = {}): MessageDto =>
  снимок(i, { attachments: [], body: "А сколько будет стоить?", ...over });

/** Ровно то, что делает панель ленты: журнал сворачиваем, снимки собираем. */
const строки = (msgs: MessageDto[]): FeedRow[] => собратьСерииСнимков(buildThreadRows(msgs));

const видыСтрок = (msgs: MessageDto[]) => строки(msgs).map((r) => r.kind);

function снимкиСтроки(rows: FeedRow[]): Снимок[] {
  const r = rows.find((x) => x.kind === "shots");
  return r && r.kind === "shots" ? r.shots : [];
}

describe("Серия снимков собирается в один пузырь", () => {
  it("⚠ СЕМЬ СНИМКОВ — ОДИН ПУЗЫРЬ, А НЕ СЕМЬ", () => {
    /*
     * Ровно та жалоба, ради которой всё делалось: 19 таких серий за 60 дней,
     * и каждая вытесняла вопрос клиента за нижний край экрана.
     */
    const rows = строки(серия(7));
    expect(rows.length, "серия снова растянула переписку").toBe(1);
    expect(rows[0].kind).toBe("shots");
    expect(снимкиСтроки(rows)).toHaveLength(7);
  });

  it("серия начинается с двух — одиночный снимок остаётся пузырём", () => {
    /*
     * Один снимок в сетке 2×1 был бы обрезан вдвое и потерял бы пропорции, ради
     * которых место под него резервируют с 03.09.
     */
    expect(видыСтрок([снимок(1)])).toEqual(["message"]);
    expect(видыСтрок(серия(2))).toEqual(["shots"]);
  });

  it("порядок снимков — как в ленте, снизу вверх по времени", () => {
    // От него зависит и счётчик «3 из 7», и то, куда ведёт стрелка «вперёд».
    expect(снимкиСтроки(строки(серия(4))).map((s) => s.att.media_id)).toEqual([
      "a-1",
      "a-2",
      "a-3",
      "a-4",
    ]);
  });
});

describe("Границы серии", () => {
  it("⚠ ЛЮБОЕ ДРУГОЕ СООБЩЕНИЕ РВЁТ СЕРИЮ", () => {
    // «Вот фото… а сколько стоит?… вот ещё фото» — две серии, а не одна:
    // между ними вопрос, на который надо ответить, и спрятать его нельзя.
    expect(видыСтрок([снимок(1), снимок(2), текст(3), снимок(4), снимок(5)])).toEqual([
      "shots",
      "message",
      "shots",
    ]);
  });

  it("⚠ СМЕНА НАПРАВЛЕНИЯ РВЁТ СЕРИЮ", () => {
    const мои = [
      снимок(3, { direction: "out", sender_type: "operator", sender: ОЛЬГА }),
      снимок(4, { direction: "out", sender_type: "operator", sender: ОЛЬГА }),
    ];
    expect(видыСтрок([снимок(1), снимок(2), ...мои])).toEqual(["shots", "shots"]);
  });

  it("⚠ СМЕНА ОТПРАВИТЕЛЯ РВЁТ СЕРИЮ", () => {
    // Два оператора отвечают в один диалог: подпись автора у каждой серии своя.
    const первый = снимок(3, { direction: "out", sender_type: "operator", sender: ОЛЬГА });
    const второй = снимок(4, {
      direction: "out",
      sender_type: "operator",
      sender: { id: "u-2", full_name: "Пётр Ковалёв" },
    });
    expect(видыСтрок([первый, второй])).toEqual(["message", "message"]);
  });

  it("⚠ СИСТЕМНАЯ ЗАПИСЬ РВЁТ СЕРИЮ", () => {
    const запись = снимок(3, {
      direction: "system",
      sender_type: "avito",
      body: "Заказ отменён",
      attachments: [],
    });
    expect(видыСтрок([снимок(1), снимок(2), запись, снимок(4), снимок(5)])).toEqual([
      "shots",
      "message",
      "shots",
    ]);

    /*
     * ⚠ И ДВЕ ТАКИЕ ЗАПИСИ ПОДРЯД, ЕСЛИ У НИХ ОКАЖЕТСЯ СНИМОК, — ТОЖЕ.
     *
     * Проверка выше сама по себе ничего не стоит: у записи пустой список
     * вложений, плиткой она не стала бы и без всякого заслона по виду пузыря
     * («отрицательная проверка зеленеет и без заслона»). Одной записи тоже
     * мало — её отрезает граница серии по смене вида пузыря. Настоящее
     * правило видно ровно здесь: две служебные записи подряд склеились бы
     * между собой, и обе исчезли бы из ленты, превратившись в две плитки.
     */
    const запись3 = снимок(3, { direction: "system", sender_type: "avito", body: null });
    const запись4 = снимок(4, { direction: "system", sender_type: "avito", body: null });
    expect(видыСтрок([запись3, запись4])).toEqual(["message", "message"]);
  });

  it("⚠ ПАУЗА БОЛЬШЕ ПЯТИ МИНУТ РВЁТ СЕРИЮ", () => {
    /*
     * Окно то же, что у схлопывания подписи автора (docs/17). Одно число на
     * оба правила: разъедься они, и на экране выйдет спор с самим собой —
     * подписи схлопнуты в одну группу, а снимки разорваны на две.
     */
    const поздний = снимок(2, { created_at: "2026-09-05T09:06:00.000Z" });
    expect(видыСтрок([снимок(1), поздний])).toEqual(["message", "message"]);

    const вовремя = снимок(2, { created_at: "2026-09-05T09:04:00.000Z" });
    expect(видыСтрок([снимок(1), вовремя])).toEqual(["shots"]);
  });

  it("⚠ ГРАНИЦА СУТОК РВЁТ СЕРИЮ", () => {
    // Иначе внутрь коллажа попал бы дата-разделитель, и лента потеряла бы день.
    /*
     * Полночь считаем МЕСТНУЮ, а не UTC: `localDayKey` работает в поясе
     * машины, и записанные буквами «23:59Z» и «00:01Z» на любом сдвиге, кроме
     * нулевого, попадают в одни сутки — тест зеленел бы, ничего не проверяя.
     */
    const полночь = new Date(2026, 8, 6, 0, 0, 0).getTime();
    const вечер = снимок(1, { created_at: new Date(полночь - 60_000).toISOString() });
    const ночь = снимок(2, { created_at: new Date(полночь + 60_000).toISOString() });
    expect(видыСтрок([вечер, ночь])).toEqual(["message", "message"]);
  });
});

describe("Что не имеет права потеряться при склейке", () => {
  it("⚠ СООБЩЕНИЕ С ОШИБКОЙ ДОСТАВКИ В СЕРИЮ НЕ СКЛЕИВАЕТСЯ", () => {
    /*
     * У него своя судьба: крест, «Повторить», «Удалить». Одна кнопка на семь
     * снимков не сказала бы, какой именно не ушёл, — а «не отправилось» это
     * единственное, ради чего на значок статуса вообще смотрят.
     */
    const упало = снимок(2, {
      direction: "out",
      sender_type: "operator",
      sender: ОЛЬГА,
      delivery_status: "failed",
    });
    const целое = снимок(1, { direction: "out", sender_type: "operator", sender: ОЛЬГА });
    const ещё = снимок(3, { direction: "out", sender_type: "operator", sender: ОЛЬГА });
    expect(видыСтрок([целое, упало, ещё])).toEqual(["message", "message", "message"]);
  });

  it("⚠ «ОТПРАВЛЯЕТСЯ» ТОЖЕ НЕ СКЛЕИВАЕТСЯ", () => {
    // `pending` — состояние ОДНОГО сообщения; общая галочка обещала бы за него.
    const идёт = снимок(2, {
      direction: "out",
      sender_type: "operator",
      sender: ОЛЬГА,
      delivery_status: "pending",
    });
    const целое = снимок(1, { direction: "out", sender_type: "operator", sender: ОЛЬГА });
    expect(видыСтрок([целое, идёт])).toEqual(["message", "message"]);
  });

  it("⚠ ОТВЕТ НА СООБЩЕНИЕ НЕ СКЛЕИВАЕТСЯ", () => {
    // Цитата стоит в шапке пузыря и отвечает на «на что это ответ». У серии
    // единого ответа нет, и молча съесть цитату нельзя.
    const сЦитатой = снимок(2, { reply_to_id: "m-0" });
    expect(видыСтрок([снимок(1), сЦитатой, снимок(3)])).toEqual([
      "message",
      "message",
      "message",
    ]);
  });

  it("⚠ ПОДПИСЬ ПОД СНИМКОМ НЕ СКЛЕИВАЕТСЯ", () => {
    // «Вот шильдик, модель WF60F1» — это уже слова, и в сетке они пропали бы.
    const сПодписью = снимок(2, { body: "вот шильдик" });
    expect(видыСтрок([снимок(1), сПодписью])).toEqual(["message", "message"]);
  });

  it("⚠ ЗАМЕТКА НЕ СКЛЕИВАЕТСЯ", () => {
    // У каждой своя подпись автора и своя кнопка «Удалить», и обе именные.
    const a = снимок(1, { direction: "note", sender_type: "operator", sender: ОЛЬГА });
    const b = снимок(2, { direction: "note", sender_type: "operator", sender: ОЛЬГА });
    expect(видыСтрок([a, b])).toEqual(["message", "message"]);
  });

  it("вложение без ссылки не становится плиткой", () => {
    // Голосовое и часть файлов Авито отдаёт идентификатором — рисовать нечего.
    const безСсылки = снимок(2, {
      attachments: [{ media_id: "a-2", kind: "image", name: "2.jpg" }],
    } as Partial<MessageDto>);
    expect(видыСтрок([снимок(1), безСсылки])).toEqual(["message", "message"]);
  });

  it("⚠ ПЕРЕХОД ПО ЦИТАТЕ НАХОДИТ СНИМОК ВНУТРИ СЕРИИ", () => {
    /*
     * Панель искала строку по `row.msg.id`, а у коллажа своего `msg` нет
     * вовсе: ответ на склеенный снимок не прокручивал бы никуда и молчал.
     */
    const rows = строки(серия(7));
    expect(строкаПро(rows[0], "m-5"), "снимок внутри серии перестал находиться").toBe(true);
    expect(строкаПро(rows[0], "m-99")).toBe(false);
  });
});

/* ─────────────────────────────────────────────── как это выглядит ────── */

const снимкиДляПросмотра = (shots: Снимок[]) =>
  shots.map((s) => ({ mediaId: s.att.media_id, url: s.att.url as string, name: s.att.name }));

/**
 * Стенд повторяет проводку панели ленты (имя латиницей — правило
 * react-hooks/rules-of-hooks не считает кириллическую «С» заглавной): коллаж говорит, какой снимок открыть,
 * просмотрщик знает их все и ходит по ним стрелками.
 */
function Harness({ shots }: { shots: Снимок[] }) {
  const все = снимкиДляПросмотра(shots);
  const [i, setI] = useState<number | null>(null);
  return (
    <>
      <PhotoCollage
        shots={shots}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onOpenImage={(mediaId) => {
          const k = все.findIndex((s) => s.mediaId === mediaId);
          if (k >= 0) setI(k);
        }}
      />
      <ImageViewer images={все} index={i} onClose={() => setI(null)} onIndex={setI} />
    </>
  );
}

describe("Сетка коллажа", () => {
  const сетка = (n: number) => {
    const { container } = render(
      <PhotoCollage
        shots={снимкиСтроки(строки(серия(n)))}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onOpenImage={() => {}}
      />,
    );
    return container.querySelector(".msg__shots") as HTMLElement;
  };

  it("⚠ ДВЕ КОЛОНКИ ПРИ 2–4 СНИМКАХ, ТРИ ПРИ 5 И БОЛЬШЕ", () => {
    for (const n of [2, 3, 4]) {
      expect(сетка(n).style.getPropertyValue("--shots-cols"), `${n} снимков`).toBe("2");
    }
    for (const n of [5, 6, 7]) {
      expect(сетка(n).style.getPropertyValue("--shots-cols"), `${n} снимков`).toBe("3");
    }
  });

  it("плиток ровно столько, сколько снимков, и каждая — кнопка", () => {
    // `<div onClick>` не получал бы фокус и читался бы читалкой как текст.
    const сетка7 = сетка(7);
    expect(сетка7.querySelectorAll("button.msg__shot")).toHaveLength(7);
  });

  it("подпись плитки называет её место в серии", () => {
    // Без номера читалка произносит семь одинаковых «Открыть снимок».
    render(
      <PhotoCollage
        shots={снимкиСтроки(строки(серия(7)))}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onOpenImage={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Открыть снимок 3 из 7: 3.jpg" })).toBeTruthy();
  });

  it("⚠ ВРЕМЯ У СЕРИИ — ПОСЛЕДНЕГО СНИМКА, А НЕ ПЕРВОГО", () => {
    /*
     * Часы первого врали бы в обратную сторону: «прислано в 09:00», хотя
     * фотографии шли до 09:04, и отвечавший в 09:02 решил бы, что ответил на всё.
     */
    const первый = снимок(1, { created_at: "2026-09-05T09:00:00.000Z" });
    const последний = снимок(2, { created_at: "2026-09-05T09:04:00.000Z" });
    const { container } = render(
      <PhotoCollage
        shots={снимкиСтроки(строки([первый, последний]))}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onOpenImage={() => {}}
      />,
    );
    const meta = container.querySelector(".msg__meta") as HTMLElement;
    expect(meta.textContent).toContain(formatClock(последний.created_at));
    expect(meta.textContent, "часы показывают начало серии, а не её конец").not.toContain(
      formatClock(первый.created_at),
    );
  });

  it("⚠ ОТМЕТКА ДОСТАВКИ У СВОЕЙ СЕРИИ НЕ ПРОПАЛА", () => {
    // В серию берут только доставленное, поэтому одна галочка честна за всех.
    const мои = [
      снимок(1, { direction: "out", sender_type: "operator", sender: ОЛЬГА }),
      снимок(2, { direction: "out", sender_type: "operator", sender: ОЛЬГА }),
    ];
    render(
      <PhotoCollage
        shots={снимкиСтроки(строки(мои))}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onOpenImage={() => {}}
      />,
    );
    expect(screen.getByLabelText("Доставлено")).toBeTruthy();
    expect(screen.getByText("Ольга Никитина"), "подпись автора у серии пропала").toBeTruthy();
  });

  it("⚠ «ОТВЕТИТЬ» У СЕРИИ ЕСТЬ И ВЕДЁТ НА ПОСЛЕДНИЙ СНИМОК", async () => {
    const user = userEvent.setup();
    let кому: MessageDto | null = null;
    render(
      <PhotoCollage
        shots={снимкиСтроки(строки(серия(7)))}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onOpenImage={() => {}}
        onReply={(m) => {
          кому = m;
        }}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Ответить на эти снимки" }));
    expect(кому).not.toBeNull();
    expect((кому as unknown as MessageDto).id).toBe("m-7");
  });

  it("битая ссылка гасит одну плитку, а не весь коллаж", () => {
    // Ссылки Авито живут не вечно; значок битой картинки читался бы как
    // «клиент прислал пустоту».
    const { container } = render(
      <PhotoCollage
        shots={снимкиСтроки(строки(серия(4)))}
        prev={null}
        clientId="cl-1"
        clientName="Иван"
        onOpenImage={() => {}}
      />,
    );
    fireEvent.error(screen.getByAltText("2.jpg"));
    const плитки = container.querySelectorAll(".msg__shot");
    expect(плитки[1].getAttribute("data-broken")).toBe("true");
    expect(плитки[0].getAttribute("data-broken")).toBeNull();
  });
});

describe("Просмотр во весь экран открывается на нажатой плитке", () => {
  it("⚠ НАЖАТИЕ ОТКРЫВАЕТ ИМЕННО ЭТОТ СНИМОК, А НЕ ПЕРВЫЙ", async () => {
    const user = userEvent.setup();
    render(<Harness shots={снимкиСтроки(строки(серия(7)))} />);
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Открыть снимок 3 из 7: 3.jpg" }));
    const окно = screen.getByRole("dialog");
    expect(within(окно).getByAltText("3.jpg")).toBeTruthy();
  });

  it("⚠ СЧЁТЧИК И СТРЕЛКИ ХОДЯТ ПО ВСЕЙ СЕРИИ", async () => {
    const user = userEvent.setup();
    render(<Harness shots={снимкиСтроки(строки(серия(7)))} />);
    await user.click(screen.getByRole("button", { name: "Открыть снимок 3 из 7: 3.jpg" }));

    const счётчик = () =>
      (screen.getByRole("dialog").querySelector(".imgview__count") as HTMLElement).textContent;
    expect(счётчик()).toBe("3 из 7");

    await user.click(screen.getByRole("button", { name: "Следующий снимок" }));
    expect(счётчик()).toBe("4 из 7");
    expect(within(screen.getByRole("dialog")).getByAltText("4.jpg")).toBeTruthy();

    // Клавиатура: оператор работает быстро и мышью пользуется меньше, чем кажется.
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(счётчик()).toBe("3 из 7");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

/* ─────────────────────────────────────────────────── проводка и место ── */

const без_комментариев = (t: string) =>
  (t as string).replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");

function правило(файл: string, селектор: string): string {
  const css = (readFileSync(файл, "utf-8") as string).replace(/\/\*[\s\S]*?\*\//g, "");
  const i = css.indexOf(`${селектор} {`);
  expect(i, `в ${файл} не найдено правило ${селектор}`).toBeGreaterThan(-1);
  return css.slice(i, css.indexOf("}", i));
}

const COLLAGE_CSS = "src/features/chats/components/thread/photo-collage.css";

describe("Проводка коллажа", () => {
  /*
   * ⚠ БЕЗ ЭТИХ ПРОВЕРОК ВСЁ ВЫШЕ ЗЕЛЕНЕЕТ ВПУСТУЮ: они проверяют компонент, а
   * не то, что его кто-то показывает. Проект уже дважды попадался на такой
   * проводке — заслон существовал, докстрока объясняла, зачем он, и не звался
   * ниоткуда.
   */
  const ПАНЕЛЬ = "src/features/chats/components/thread/ChatThreadPane.tsx";

  it("панель ленты собирает серии и рисует коллаж", () => {
    /*
     * ⚠ ИЩЕМ ВЫЗОВ И ТЕГ ЦЕЛИКОМ, А НЕ УПОМИНАНИЕ ИМЕНИ. Первая редакция
     * писала `toContain("собратьСерииСнимков")` и `toContain("<PhotoCollage")`
     * — обе диверсии прошли её насквозь: имя осталось в строке импорта, а
     * `<PhotoCollageОтключён` начинается с проверяемой подстроки. Сторож
     * зеленел на ленте, где коллажа больше нет.
     */
    const src = без_комментариев(readFileSync(ПАНЕЛЬ, "utf-8") as string);
    expect(src, "серии снимков больше не собираются").toMatch(
      /собратьСерииСнимков\(\s*buildThreadRows\(/,
    );
    expect(src, "коллаж не подключён к ленте").toMatch(/<PhotoCollage[\s/>]/);
    expect(src, "коллаж не открывает просмотр").toMatch(/<PhotoCollage[\s\S]{0,400}onOpenImage/);
  });

  it("коллаж рисуется отдельной строкой виртуализатора", () => {
    // Иначе семь снимков остались бы семью строками, и лента не сжалась бы.
    const src = без_комментариев(readFileSync(ПАНЕЛЬ, "utf-8") as string);
    expect(src).toMatch(/row\.kind === "shots"/);
  });
});

describe("Место под коллаж занято до загрузки", () => {
  /*
   * ⚠ ИНАЧЕ ЛЕНТА ПРЫГАЕТ ПОД КУРСОРОМ — ровно то, от чего избавлялись 03.09:
   * строка с фотографией занимала 57 px до загрузки и 210–332 px после.
   *
   * У коллажа это держится двумя правилами разом, и оба обязательны:
   * определённая ширина сетки (доля в shrink-to-fit пузыре равна ничему, пока
   * картинок нет) и квадратная плитка (высота ряда считается из ширины).
   */
  it("у сетки определённая ширина, а не доля", () => {
    const r = правило(COLLAGE_CSS, ".msg__shots");
    expect(r, "ширина сетки снова доля — коллаж схлопнется до загрузки").toMatch(
      /width:\s*var\(--shots-cap\)/,
    );
    expect(r, "коллаж вылезет за скруглённый край пузыря").toMatch(/max-width:\s*100%/);
  });

  it("плитка квадратная — высота ряда известна заранее", () => {
    const r = правило(COLLAGE_CSS, ".msg__shot");
    expect(r, "место под ряд перестало считаться заранее").toMatch(/aspect-ratio:\s*1/);
  });

  it("плитка заполняет свой квадрат, а не растягивает ряд", () => {
    /*
     * ⚠ ЗДЕСЬ `cover` УМЕСТЕН, ХОТЯ У ОДИНОЧНОГО СНИМКА ЗАПРЕЩЁН
     * (bubbleUnderPressure). Плитка — не просмотр, а вход в него: нажатие
     * открывает снимок целиком и в своих пропорциях. `contain` в сетке дал бы
     * поля разной высоты, и занять место заранее было бы нечем.
     */
    const r = правило(COLLAGE_CSS, ".msg__shot-img");
    expect(r).toMatch(/object-fit:\s*cover/);
  });

  it("движение — токеном и с оглядкой на настройку человека", () => {
    const css = readFileSync(COLLAGE_CSS, "utf-8") as string;
    expect(правило(COLLAGE_CSS, ".msg__shot-img")).toMatch(/var\(--lc-motion-fast\)/);
    expect(css, "приближение плитки не выключается по prefers-reduced-motion").toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*transition:\s*none/,
    );
  });

  it("узкий экран сжимает коллаж по той же арифметике, что и одиночный снимок", () => {
    // 360 − 24 поля ленты = 336, из них 86% = 289, минус 32 поля пузыря = 257.
    const css = readFileSync(COLLAGE_CSS, "utf-8") as string;
    expect(css).toMatch(/@media \(max-width: 767px\)[\s\S]*--shots-cap:\s*257px/);
  });
});
