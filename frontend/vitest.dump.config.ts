/*
 * Конфиг ТОЛЬКО для снималки разметки (`src/test/*.dump.tsx`).
 *
 * Отдельный файл, потому что общий прогон не должен её видеть: это инструмент
 * стенда адаптива, а не проверка. В общий include она не попадает по
 * расширению, а здесь include задан явно.
 */
import { defineConfig, mergeConfig } from "vitest/config";
import base from "./vite.config";

export default defineConfig(async (env) =>
  mergeConfig(await (base as never as (e: unknown) => Promise<unknown>)(env), {
    test: { include: ["src/test/**/*.dump.tsx"] },
  } as never),
);
