import { describe, expect, it } from "vitest";

/**
 * Веб-бандл не тащит Tauri (03 §7, 04 §1.1). Проверяем по исходникам, а не по
 * dist, чтобы тест был быстрым и не зависел от того, собирали ли фронт:
 *
 *  1) пакетов `@tauri-apps/*` в коде нет вообще — мост говорит с ядром через
 *     инжектируемый `window.__TAURI_INTERNALS__`, поэтому в package.json их нет
 *     и попасть в бандл нечему;
 *  2) модули `platform/tauri/**` подключаются ТОЛЬКО динамическим импортом —
 *     Rollup выносит их в отдельный чанк, который веб никогда не запрашивает.
 */

/** Исходники читаем через import.meta.glob — без node:fs, @types/node в проекте нет. */
const sources = import.meta.glob("/src/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const files = Object.entries(sources).map(([rel, code]) => ({ rel, code }));

const isTauriModule = (rel: string) => rel.startsWith("/src/platform/tauri/");
const isTest = (rel: string) => rel.startsWith("/src/test/");

/** `import x from "y"` / `import "y"` — статические спецификаторы файла. */
function staticImports(code: string): string[] {
  const specs: string[] = [];
  const re = /^[ \t]*import\s+(?:[^;'"]*?\sfrom\s+)?["']([^"']+)["']/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(code)) !== null) specs.push(m[1]);
  return specs;
}

/** `import("y")` — динамические спецификаторы. */
function dynamicImports(code: string): string[] {
  const specs: string[] = [];
  const re = /\bimport\(\s*["']([^"']+)["']\s*\)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(code)) !== null) specs.push(m[1]);
  return specs;
}

describe("Веб-сборка не содержит Tauri-зависимостей (03 §7)", () => {
  it("ни один исходник не импортирует пакеты @tauri-apps/* (их нет и в package.json)", () => {
    const offenders: string[] = [];
    for (const f of files) {
      for (const spec of [...staticImports(f.code), ...dynamicImports(f.code)]) {
        if (spec.startsWith("@tauri-apps")) offenders.push(`${f.rel} → ${spec}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("модули platform/tauri подключаются только динамическим импортом", () => {
    const offenders: string[] = [];
    for (const f of files) {
      if (isTauriModule(f.rel) || isTest(f.rel)) continue; // внутри чанка и в тестах — можно
      for (const spec of staticImports(f.code)) {
        if (/(^|\/)tauri(\/|$)/.test(spec) || spec.includes("platform/tauri")) {
          offenders.push(`${f.rel} → ${spec}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("bridge.ts выбирает реализацию через await import()", () => {
    const bridge = files.find((f) => f.rel === "/src/platform/bridge.ts");
    expect(bridge).toBeDefined();
    expect(bridge?.code).toMatch(/await import\("\.\/tauri"\)/);
    expect(bridge?.code).toMatch(/await import\("\.\/web"\)/);
  });

  it("платформенный слой не импортируется статически из общего кода", () => {
    // Разрешены только лёгкие модули моста (bridge/updateStore/UpdateBanner/
    // warmup/buildVersion): они не тянут за собой ни IPC, ни SQLite-обвязку.
    // `buildVersion` — сравнение версии бандла с версией из /api/health
    // (SHELL-02); из зависимостей у него только bridge и updateStore, оба уже
    // в этом списке.
    const allowed = new Set([
      "@/platform/bridge",
      "@/platform/buildVersion",
      "@/platform/toast",
      "@/platform/updateStore",
      "@/platform/UpdateBanner",
      "@/platform/warmup",
    ]);
    const offenders: string[] = [];
    for (const f of files) {
      if (f.rel.startsWith("/src/platform/") || isTest(f.rel)) continue;
      for (const spec of staticImports(f.code)) {
        if (spec.startsWith("@/platform") && !allowed.has(spec)) offenders.push(`${f.rel} → ${spec}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
