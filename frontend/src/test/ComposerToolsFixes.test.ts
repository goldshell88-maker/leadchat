/**
 * Две правки панели ввода (27.08).
 *
 * ⚠ СКРЕПКА ПРОПАДАЛА В РЕЖИМЕ ЗАМЕТКИ. Она стояла под `!isNote`, при том что
 * красная плашка этого же композера велит: «Авито не принимает файлы от нас —
 * приложите файл К ЗАМЕТКЕ или отправьте клиенту ссылку». Человек читал
 * указание и не находил, чем его выполнить. Сервер вложения в заметках
 * принимает, а право на них своё — `notes:write`: у руководителя оно есть, а
 * `messages:send` нет, и он не мог приложить файл тем более.
 *
 * ⚠ ПИКЕР ⚡ НЕ ЗАКРЫВАЛСЯ ПОВТОРНЫМ НАЖАТИЕМ. `mousedown` мимо попапа закрывал
 * его, а следом приходил `click` по той же кнопке и открывал заново: попап
 * моргал и оставался. Закрыть можно было только щелчком в стороне или Esc.
 */
import { describe, expect, it } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { readFileSync } from "node:fs";

function код(путь: string): string {
  return (readFileSync(путь, "utf-8") as string)
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ");
}

const COMPOSER = "src/features/chats/components/composer/Composer.tsx";
const POPOVER = "src/features/chats/components/composer/TemplatePickerPopover.tsx";

describe("Панель ввода", () => {
  it("скрепка доступна и в заметке — по праву заметок, кроме режима руководителя", () => {
    /*
     * ⚠ РЕЖИМ РУКОВОДИТЕЛЯ ОСТАЁТСЯ БЕЗ ВЛОЖЕНИЙ. `noteOnly` (03 §5.3)
     * закрепляет композер в заметке и намеренно убирает быстрые ответы и
     * вложения. Первая редакция правки этого не учла и сломала
     * `ThreadFooterRoles` — правило оказалось живым и защищённым.
     */
    const src = код(COMPOSER);
    expect(src).toMatch(/canAttach\s*=\s*isNote\s*\?\s*canWriteNotes && !noteOnly\s*:\s*canSendMessages/);
    // Режим руководителя () намеренно остаётся без вложений — 03 §5.3.
    expect(src, "скрепка снова спрятана в режиме заметки").not.toMatch(
      /canSendMessages && !isNote && \(\s*<Tooltip label="Прикрепить файл"/,
    );
  });

  it("пикер знает свою кнопку и не считает её нажатие щелчком мимо", () => {
    const composer = код(COMPOSER);
    expect(composer).toContain("triggerRef={pickerBtnRef}");
    expect(composer).toContain("ref={pickerBtnRef}");

    const popover = код(POPOVER);
    expect(popover, "кнопка снова будет считаться «мимо» — попап замигает").toMatch(
      /triggerRef\?\.current\?\.contains/,
    );
  });

  it("закрытие по щелчку в стороне никуда не делось", () => {
    const popover = код(POPOVER);
    expect(popover).toMatch(/addEventListener\("mousedown"/);
    expect(popover).toMatch(/!корень\.contains\(цель\)/);
  });
});
