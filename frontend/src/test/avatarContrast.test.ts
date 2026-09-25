/**
 * Инициалы в аватаре обязаны читаться — в обеих темах.
 *
 * ЧТО БЫЛО. Аватар сотрудника брал цвет у Mantine (`color="lp"
 * variant="light"`): светло-голубые инициалы на синем, 2.08:1. Это ниже любого
 * порога — инициалы читались хуже фона, на котором лежат. На экране «Команда»
 * их тринадцать подряд, и понять, кто есть кто, было нельзя.
 *
 * ПОЧЕМУ ТЕСТ, А НЕ РАЗОВАЯ ПРАВКА. Палитра аватаров — шестнадцать пар
 * «фон/чернила» в двух темах. Пары подбираются глазами, а глаз к контрасту
 * нечувствителен: разница между 4.6:1 и 3.9:1 не видна, а первое проходит и
 * второе нет. Один раз это уже привело к 2.08:1 в бою. Считаем арифметикой.
 *
 * Порог 4.5:1 — требование WCAG AA к обычному тексту. Инициалы формально
 * крупные (для них хватило бы 3:1), но берём строгий порог намеренно: аватар
 * бывает и 24px, и подпись в нём — единственный способ узнать человека.
 */

import { describe, expect, it } from "vitest";

/*
 * Палитра живёт в CSS, а не в коде, поэтому читаем сам файл.
 *
 * `?raw` не подошёл: в тестовой среде Vite отдаёт по нему пустую строку, и
 * тест зеленел бы, ничего не проверив. Читаем файловой системой, а типы для
 * неё объявляем здесь: `@types/node` в проекте нет и тянуть его ради одного
 * вызова незачем.
 */
declare function require(id: string): { readFileSync(p: string, enc: string): string };

const VARS = require("node:fs").readFileSync("src/app/lc-vars.css", "utf-8");

const MIN_RATIO = 4.5;

function channel(value: number): number {
  const v = value / 255;
  return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
}

function luminance(hex: string): number {
  const clean = hex.trim().replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(clean.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Пары «фон/чернила» из блока темы: `dark` — первый блок, `light` — второй. */
function palette(scheme: "dark" | "light"): Array<[number, string, string]> {
  // Блоки палитры идут подряд в конце файла; берём тот, что помечен схемой.
  const marker =
    scheme === "dark"
      ? /:root,\s*:root\[data-mantine-color-scheme="dark"\]\s*\{([^}]*--lc-avatar-0-bg[^}]*)\}/
      : /:root\[data-mantine-color-scheme="light"\]\s*\{([^}]*--lc-avatar-0-bg[^}]*)\}/;
  const block = VARS.match(marker)?.[1];
  if (!block) throw new Error(`не нашёл палитру аватаров для схемы ${scheme}`);

  const pairs: Array<[number, string, string]> = [];
  for (let i = 0; i < 8; i++) {
    const bg = block.match(new RegExp(`--lc-avatar-${i}-bg:\\s*(#[0-9a-fA-F]{6})`))?.[1];
    const ink = block.match(new RegExp(`--lc-avatar-${i}-ink:\\s*(#[0-9a-fA-F]{6})`))?.[1];
    if (!bg || !ink) throw new Error(`пара ${i} неполная в схеме ${scheme}`);
    pairs.push([i, bg, ink]);
  }
  return pairs;
}

describe("Палитра аватаров", () => {
  it.each(["dark", "light"] as const)("все восемь пар читаются: схема %s", (scheme) => {
    const failures = palette(scheme)
      .map(([i, bg, ink]) => ({ i, bg, ink, ratio: contrast(ink, bg) }))
      .filter((p) => p.ratio < MIN_RATIO);

    expect(
      failures.map((f) => `пара ${f.i}: ${f.ink} на ${f.bg} = ${f.ratio.toFixed(2)}:1`),
    ).toEqual([]);
  });

  it("сам расчёт умеет находить провал — иначе тест зелёный ни о чём", () => {
    // Ровно тот случай, что был в бою: светло-голубые инициалы на синем.
    expect(contrast("#93c5fd", "#2563eb")).toBeLessThan(MIN_RATIO);
    // И заведомо хороший, чтобы порог не оказался недостижимым для всех.
    expect(contrast("#ffffff", "#000000")).toBeGreaterThan(MIN_RATIO);
  });
});
