import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "coverage"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser } },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },
  {
    files: ["**/*.js"],
    languageOptions: { globals: { ...globals.node } },
  },
  // Снипеты для консоли браузера (`*.browser.js`) — их не собирает Vite и не
  // запускает vitest, их вставляют в DevTools живой страницы. Окружение
  // названо в имени файла: иначе линтер судит их по правилам Node и ругается
  // на `document`, которого там действительно нет.
  {
    files: ["**/*.browser.js"],
    languageOptions: { globals: { ...globals.browser } },
  },
  // Точки входа горячей заменой не обновляются по своей природе: они не
  // экспортируют модуль, а монтируют приложение в DOM. `dev/preview.tsx` —
  // стенд для разглядывания экранов вживую, и требовать от него экспортов
  // значит просить переписать вход ради правила, которое к нему не относится.
  // Предупреждение, которое нельзя выполнить, — это шум: следующее,
  // настоящее, прочитают вместе с ним и пропустят.
  {
    files: ["dev/**/*.tsx"],
    rules: { "react-refresh/only-export-components": "off" },
  },
);
