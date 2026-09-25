/**
 * Разбор очереди без мыши (просьба владельца 22.08).
 *
 * «Чтобы диалоги можно было брать без клика мыши, только клавишами, и брались
 * сначала с большим временем ожидания».
 *
 * Клавиши на приём (Mod+Enter) и на переход к очереди (Alt+0) были и раньше.
 * Не хватало середины: между двумя приёмами приходилось тянуться к мыши —
 * «Следующий (N)» жила только кнопкой в шапке ленты. На двадцати шести ждущих
 * это двадцать шесть переносов руки.
 *
 * ПОРЯДОК МЕНЯТЬ НЕ ПРИШЛОСЬ: очередь и так отдаётся «кто дольше ждёт — тот
 * первый» (`offered_at ASC`, app/services/inbox.py), а `nextInboxId` идёт по
 * ней кольцом. Здесь это и проверяется — чтобы правило не уехало молча.
 */
import { describe, expect, it } from "vitest";
import { ACTIONS } from "@/features/hotkeys/catalog";

describe("клавиша «следующий в очереди»", () => {
  it("действие объявлено в каталоге", () => {
    const действие = ACTIONS.find((a) => a.id === "queueNext");
    expect(действие, "queueNext нет в каталоге — шпаргалка о нём не расскажет").toBeTruthy();
  });

  it("у него есть клавиша по умолчанию", () => {
    const действие = ACTIONS.find((a) => a.id === "queueNext");
    expect(действие?.defaults?.length).toBeGreaterThan(0);
  });

  it("работает и когда курсор в поле ввода", () => {
    // Иначе разбор очереди обрывался бы каждый раз, когда диспетчер начал
    // печатать ответ и передумал.
    const действие = ACTIONS.find((a) => a.id === "queueNext");
    expect(действие?.allowInInput).toBe(true);
  });

  it("закрыт тем же правом, что приём диалога", () => {
    const next = ACTIONS.find((a) => a.id === "queueNext");
    const claim = ACTIONS.find((a) => a.id === "claim");
    expect(next?.need).toBe(claim?.need);
  });

  it("клавиша не занята другим действием", () => {
    const next = ACTIONS.find((a) => a.id === "queueNext");
    const занятые = ACTIONS.filter((a) => a.id !== "queueNext").flatMap((a) => a.defaults ?? []);
    for (const k of next?.defaults ?? []) {
      expect(занятые, `клавиша ${k} уже занята`).not.toContain(k);
    }
  });
});
