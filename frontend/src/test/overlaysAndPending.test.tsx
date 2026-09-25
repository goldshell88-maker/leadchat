// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// как в surfaceLadder.test.ts: сторож только читает файлы.
import { readFileSync } from "node:fs";
import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { BlockClientDialog } from "@/features/chats/components/card/BlockClientButton";
import { TemplatePickerPopover } from "@/features/chats/components/composer/TemplatePickerPopover";
import { fakeMe, fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders, wrap } from "./render";

/*
 * Сторожа четырёх находок разбора 23.08 про всплывающие слои и ожидание сервера.
 *
 * Два первых проверяются поведением. Два последних — чтением исходника: у обоих
 * условие «пока летит запрос» и «эффект при размонтировании», а jsdom не даёт
 * ни настоящей мутации, ни размонтирования страницы вместе с роутером так,
 * чтобы проверка осталась честной. Сообщение об ошибке называет беду целиком,
 * чтобы упавший тест не пришлось расследовать.
 */

const источник = (путь: string) => readFileSync(путь, "utf-8") as string;

beforeEach(() => {
  queryClient.clear();
  resetSessionStore({
    user: fakeUser,
    permissions: fakeMe.permissions as never,
    accessToken: "t",
    bootstrapped: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

/*
 * №13. Пикер быстрых ответов закрывался только Escape или выбором строки.
 * Щелчок мимо не закрывал ничего — панель до 340px висела поверх переписки и
 * закрывала ровно то, ради чего человек туда щёлкнул.
 */
describe("Пикер быстрых ответов закрывается щелчком мимо", () => {
  it("щелчок вне панели закрывает её", () => {
    const закрыт = vi.fn();
    renderWithProviders(<TemplatePickerPopover onPick={() => {}} onClose={закрыт} />);

    fireEvent.mouseDown(document.body);

    expect(закрыт, "щелчок мимо не закрыл пикер — он останется поверх переписки").toHaveBeenCalled();
  });

  it("щелчок ВНУТРИ панели её не закрывает", () => {
    const закрыт = vi.fn();
    const { container } = renderWithProviders(
      <TemplatePickerPopover onPick={() => {}} onClose={закрыт} />,
    );
    const корень = container.querySelector(".tpl-popover") as HTMLElement;

    fireEvent.mouseDown(корень);

    expect(закрыт, "пикер закрывается от щелчка по себе — выбрать строку станет нельзя").not.toHaveBeenCalled();
  });
});

/*
 * №15. Причина пометки сбрасывалась только на успешном пути. Передумал человек
 * и закрыл окно — текст оставался и подставлялся при следующем открытии, уже
 * про ДРУГОГО клиента.
 */
describe("Причина пометки не переезжает к другому клиенту", () => {
  /*
   * ⚠ ОКНО НЕ РАЗМОНТИРУЕТСЯ МЕЖДУ ОТКРЫТИЯМИ, И В ЭТОМ ВСЯ БЕДА.
   * `<BlockClientDialog … opened={blockOpen} />` висит в дереве постоянно
   * (ChatThreadPane), меняется только проп. Значит `useState` переживает
   * закрытие — и введённая причина ждёт следующего клиента.
   *
   * Первая редакция этого теста размонтировала компонент между открытиями и
   * была зелёной ВСЕГДА: свежий экземпляр начинает с пустой строки при любом
   * коде. Здесь тот же экземпляр, что и в проде: перерисовываем пропом.
   */
  function окно(opened: boolean, clientId = "client-1") {
    return (
      <BlockClientDialog
        clientId={clientId}
        convId="conv-1"
        blocked={false}
        opened={opened}
        onClose={() => {}}
      />
    );
  }

  it("после закрытия окна поле пустое при следующем открытии", () => {
    const { rerender } = renderWithProviders(окно(true));
    const поле = () => screen.getByRole("textbox") as HTMLTextAreaElement;

    fireEvent.change(поле(), { target: { value: "хамит и угрожает" } });
    expect(поле().value).toBe("хамит и угрожает");

    // «Отмена» — тот самый путь, на котором сброса не было.
    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    rerender(wrap(окно(false)));
    rerender(wrap(окно(true, "client-2")));

    expect(
      поле().value,
      "причина про прежнего клиента подставилась новому — и выглядит написанной по делу",
    ).toBe("");
  });
});

/*
 * №1. Уход со страницы во время выгрузки оставлял вечный тост со спиннером: у
 * него нет ни автозакрытия, ни крестика, а эффекты, которые превращают его в
 * «готово» или «не удалось», умирают вместе с размонтированным хуком.
 */
describe("Тост выгрузки не остаётся висеть навсегда", () => {
  // Хук общий для статистики и «Разбора диалогов» с 24.09.
  const ПУТЬ = "src/shared/export/useExportJob.tsx";

  it("снимается при размонтировании хука", () => {
    expect(
      источник(ПУТЬ),
      "нет уборки тоста при уходе со страницы — спиннер повиснет до перезагрузки",
    ).toMatch(/useEffect\(\(\) => \(\) => \{\s*notifications\.hide\(TOAST_ID\);/);
  });

  it("и его можно закрыть руками", () => {
    expect(
      источник(ПУТЬ),
      "у тоста снова нет крестика, а автозакрытия у него нет по замыслу",
    ).toMatch(/withCloseButton: true/);
  });
});

/*
 * №14. Переключатель «Включён» привязан к значению, которое меняется только
 * после ответа сервера: щелчок не двигал ничего, человек щёлкал ещё раз, и
 * правки уходили пачкой, гоняясь между собой.
 */
describe("Переключатель бота не принимает второй щелчок, пока летит первый", () => {
  it("блокируется на время запроса", () => {
    const текст = источник("src/features/settings/bots/BotEditor.tsx");
    const кусок = текст.slice(текст.indexOf('label="Включён"'));
    expect(
      кусок.slice(0, 400),
      "у переключателя снова нет `disabled` на время запроса — щелчки уйдут пачкой и последний выиграет гонку",
    ).toMatch(/disabled=\{toggleEnabled\.isPending\}/);
  });
});
