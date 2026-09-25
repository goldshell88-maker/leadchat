// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в VoicePlayer0509.test.tsx: сторож только читает файл стилей.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { AttachmentChip } from "@/features/chats/components/thread/AttachmentChip";
import type { AttachmentDto } from "@/shared/api/types";

/**
 * ССЫЛКА, МЕСТО И ОБЪЯВЛЕНИЕ — ТРИ РАЗНЫХ ПРЕДМЕТА (просьба владельца 05.09).
 *
 * ЧТО БЫЛО. Все три рисовались одной строкой со скрепкой, а имя у них чаще
 * всего не своё: Авито названия не даёт, и сервер подставляет подпись вида
 * (`_ATTACHMENT_LABELS`). В ленте стояло «Ссылка» и «Геопозиция» — вид назван,
 * а куда ведёт и что за место, не сказано. Оператор открывал каждую наугад.
 *
 * Замер боя за 60 дней: ссылок 123, мест 15, объявлений 2.
 */

function вложение(over: Partial<AttachmentDto> = {}): AttachmentDto {
  return { media_id: "a1", kind: "file", name: "Ссылка", ...over };
}

describe("Прочие вложения различимы", () => {
  it("у ссылки видно, КУДА она ведёт", () => {
    render(
      <AttachmentChip
        attachment={вложение({
          avito_type: "link",
          url: "https://www.avito.ru/moskva/telefony/iphone_13_4242",
        })}
      />,
    );
    const ссылка = screen.getByRole("link");
    // Домен — то, по чему принимают решение «открывать ли»: `www.` выброшен,
    // он ничего не сообщает и съедает место.
    expect(ссылка.textContent, "домен не показан — решение принимать не по чему").toContain(
      "avito.ru",
    );
    expect(ссылка.getAttribute("href")).toBe(
      "https://www.avito.ru/moskva/telefony/iphone_13_4242",
    );
    expect(ссылка.getAttribute("target")).toBe("_blank");
    // Ссылка идёт через мост (`ExternalLink`, проверка 24.09): `noopener` вдобавок.
    expect(ссылка.getAttribute("rel")).toContain("noreferrer");
  });

  it("у места видно, что это МЕСТО, и какое", () => {
    render(
      <AttachmentChip
        attachment={вложение({ avito_type: "location", name: "Москва, Тверская, 7" })}
      />,
    );
    expect(screen.getByText("Место"), "место неотличимо от ссылки").toBeTruthy();
    expect(screen.getByText("Москва, Тверская, 7")).toBeTruthy();
    // Адреса Авито к геопозиции не даёт — обещать переход нечем.
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("объявление названо объявлением", () => {
    render(
      <AttachmentChip
        attachment={вложение({
          avito_type: "item",
          name: "Ремонт телевизоров на дому",
          url: "https://avito.ru/i/4242",
        })}
      />,
    );
    expect(screen.getByText("Объявление")).toBeTruthy();
    expect(screen.getByRole("link").textContent).toContain("Ремонт телевизоров");
  });

  it("подпись сервера не печатается дважды", () => {
    // Имя «Геопозиция» — это подпись вида от сервера, а не адрес: печатать её
    // под нашим же словом «Место» значит показать одно и то же два раза.
    render(<AttachmentChip attachment={вложение({ avito_type: "location", name: "Геопозиция" })} />);
    expect(screen.queryByText("Геопозиция")).toBeNull();
    expect(screen.getByText("Точка на карте")).toBeTruthy();
  });

  it("незнакомый вид остаётся спокойной строкой с именем и размером", () => {
    render(
      <AttachmentChip
        attachment={вложение({ avito_type: "sticker", name: "Наклейка.webp", size: 2048 })}
      />,
    );
    // Неизвестное вложение обязано выглядеть как вложение, а не как поломка.
    expect(screen.getByText("Наклейка.webp")).toBeTruthy();
    expect(screen.getByText("2 КБ")).toBeTruthy();
  });
});

describe("Чип занимает свою ширину", () => {
  /*
   * ⚠ ТА ЖЕ ГРАБЛЯ, ЧТО У ПРОИГРЫВАТЕЛЯ (voice-player.css), и найдена тем же
   * замером: `width: min(280px, 100%)` при расчёте вклада считается `auto`,
   * потому что процент внутри `min()` неразрешим. Пузырь тогда сжимается до
   * содержимого, и чип живёт по ширине самой длинной строки, а не по своей.
   */
  it("ширина числом, потолок процентом", () => {
    const css = readFileSync(
      "src/features/chats/components/thread/attachment-chip.css",
      "utf-8",
    ) as string;
    const правило = css.slice(css.indexOf("\n.att-chip {"));
    const тело = правило.slice(0, правило.indexOf("}"));
    expect(тело, "ширина вернулась к min() с процентом").not.toMatch(/width:\s*min\(/);
    expect(тело).toContain("width: 280px");
    expect(тело, "без потолка чип вылезет за край узкого пузыря").toContain("max-width: 100%");
  });
});

describe("Чужой адрес не становится переходом", () => {
  /*
   * ⚠ В `href` попадает поле из ЧУЖОГО сообщения. `javascript:` в `href` — это
   * выполнение кода по нажатию оператора, у которого открыта вся консоль:
   * список диалогов, карточки клиентов, права. Проверка схемы стоит одной
   * строки, доверие к чужому полю не стоит ничего.
   */
  it("javascript: не превращается в ссылку", () => {
    render(
      <AttachmentChip
        attachment={вложение({ avito_type: "link", name: "Скидка", url: "javascript:alert(1)" })}
      />,
    );
    expect(screen.queryByRole("link"), "чужая схема стала переходом").toBeNull();
    // Само вложение при этом не прячется: клиент правда что-то прислал.
    expect(screen.getByText("Скидка")).toBeTruthy();
  });

  it("мусор вместо адреса не роняет ленту и не даёт «Ссылка / Ссылка»", () => {
    render(<AttachmentChip attachment={вложение({ avito_type: "link", url: "не адрес вовсе" })} />);
    expect(screen.queryByRole("link")).toBeNull();
    // Слово «Ссылка» стоит ровно один раз — подписью вида, а не ещё и вместо
    // сути: имя у вложения не своё, его дал сервер по виду.
    expect(screen.getAllByText("Ссылка")).toHaveLength(1);
    expect(screen.getByText("Адреса нет")).toBeTruthy();
  });
});
