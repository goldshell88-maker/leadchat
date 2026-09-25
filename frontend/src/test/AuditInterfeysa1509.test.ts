/**
 * Аудит интерфейса 15.09 (карточка «Клиент» и левое меню) — что из него
 * сделано, держится этими сторожами по исходнику. Правила из аудита, которые
 * закреплены здесь:
 * - пункт меню либо помещается целиком, либо у него короткая подпись и полное
 *   название в подсказке — многоточия в навигации не остаётся;
 * - заголовки групп и сочетания клавиш — не самое тусклое в меню (≥ 4,5:1);
 * - состояние звука показано переключателем, а не словом «выкл»;
 * - воздух — между разделами и «Под рукой», а не пустая полоса в 300 px;
 * - две связи карточек сворачиваются в одну строку с раскрытием, подсказка
 *   про отмену — в title кнопки «Разъединить»;
 * - число в заголовке обращений равно числу строк списка;
 * - идентификаторы объединённых карточек — по кнопке «ещё N», с копированием;
 * - зона нажатия кнопки копирования — 32 × 32.
 */
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]),
// а тест читает исходники с диска, как и соседние сторожа по тексту.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const rail = readFileSync("src/app/AppRail.tsx", "utf8");
const railCss = readFileSync("src/app/app-rail.css", "utf8");
const merge = readFileSync(
  "src/features/chats/components/card/ClientMergePanel.tsx",
  "utf8",
);
const card = readFileSync(
  "src/features/chats/components/card/ClientCardPane.tsx",
  "utf8",
);
const cardCss = readFileSync(
  "src/features/chats/components/card/client-card.css",
  "utf8",
);

describe("левое меню после аудита 15.09", () => {
  it("длинные подписи укорочены, полное название — в title и подсказке", () => {
    for (const [short, full] of [
      ["Разборы", "Разбор диалогов"],
      ["Бот", "Диалоги бота"],
      ["Клавиши", "Горячие клавиши"],
    ]) {
      expect(rail).toContain(
        `<span className="lc-rail__label">${short}</span>`,
      );
      expect(rail).toContain(`title="${full}"`);
      expect(rail).toContain(`data-tip="${full}"`);
    }
    expect(rail).toContain(`<span className="lc-rail__label">Звук</span>`);
  });

  it("звук — переключателем, «?» — клавишей в рамке", () => {
    expect(rail).toMatch(
      /className="lc-rail__toggle"\s+data-on=\{soundEnabled \|\| undefined\}/,
    );
    expect(rail).not.toContain('{soundEnabled ? "вкл" : "выкл"}');
    expect(rail).toContain('className="lc-rail__key lc-rail__kbd">?</span>');
    expect(railCss).toMatch(
      /\.lc-rail__toggle\[data-on\] \{\s*background: var\(--lc-primary-solid\)/,
    );
  });

  it("заголовки групп и сочетания читаются: --lc-text-3, а не --lc-text-5", () => {
    const section = railCss.match(/\.lc-rail__section \{[^}]+\}/)?.[0] ?? "";
    const key = railCss.match(/\.lc-rail__key \{[^}]+\}/)?.[0] ?? "";
    expect(section).toContain("color: var(--lc-text-3)");
    expect(key).toContain("color: var(--lc-text-3)");
  });

  it("воздух стоит перед «Под рукой», а не между ней и уведомлениями", () => {
    const spacer = rail.indexOf('className="lc-rail__spacer"');
    const underHand = rail.indexOf(">Под рукой<");
    const bell = rail.indexOf("<NotificationBell");
    expect(spacer).toBeGreaterThan(0);
    expect(spacer).toBeLessThan(underHand);
    expect(rail.indexOf('className="lc-rail__spacer"', spacer + 1)).toBe(-1);
    expect(underHand).toBeLessThan(bell);
  });
});

describe("карточка «Клиент» после аудита 15.09", () => {
  it("две и больше связи — одной строкой с раскрытием, подсказка — в title", () => {
    expect(merge).toContain("const свёрнуто = merged.length >= 2 && !всё;");
    expect(merge).toMatch(
      /Объединена с \{merged\.length\} \{склонение\(merged\.length\)\}/,
    );
    expect(merge).toContain(
      "title={row.auto ? ПОДСКАЗКА_РАЗЪЕДИНИТЬ : undefined}",
    );
    expect(merge).not.toMatch(/<Text[^>]*>\s*Ошиблись — «Разъединить»/);
    expect(cardCss).toMatch(
      /\.card-merge__unmerge \{\s*color: var\(--lc-danger-text\)/,
    );
  });

  it("число обращений — по списку, идентификаторы — по кнопке, копирование 32 × 32", () => {
    expect(card).toContain("Другие обращения · {historyItems.length}");
    expect(card).not.toMatch(
      /Обращений: \{conversation\.client_conversations_count\}, включая это/,
    );
    expect(card).toContain('className="card-idline__more"');
    expect(card).not.toMatch(/Ещё идентификаторы: \{extraAvitoIds/);
    expect(card).toMatch(
      /className="card-history__status"\s+data-status=\{h\.status\}/,
    );
    const copy = cardCss.match(/\.card-phone__copy \{[^}]+\}/)?.[0] ?? "";
    expect(copy).toContain("width: 32px");
    expect(copy).toContain("height: 32px");
  });
});
