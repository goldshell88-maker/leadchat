/**
 * НЕ СТОРОЖ. Снималка ОСТАЛЬНЫХ ЭКРАНОВ для стенда адаптива: вход, забытый
 * пароль, быстрые ответы, «Что нового», загрузочный каркас и рельса.
 *
 * ⚠ ЗАЧЕМ. Жалоба владельца — «весь дизайн должен быть адаптивен», а не только
 * рабочее место. Экран входа человек видит первым; каркас загрузки рисуется на
 * КАЖДОМ открытии вкладки и держит жёсткие ширины панелей (380/420/340 в
 * app-layout.css) — на узком экране их сумма заведомо больше окна, и проверить
 * это можно только замером.
 *
 * Рельса снимается в трёх видах: развёрнутая (умолчание), свёрнутая и
 * мобильная — ветку выбирает `useMediaQuery`, и в одном снимке её не увидеть.
 *
 * Запуск: npx vitest run --config vitest.dump.config.ts
 */
import { expect, it, vi } from "vitest";
// @ts-expect-error — типов Node в проекте нет (tsconfig types: ["vite/client"]).
import { writeFileSync } from "node:fs";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render } from "@testing-library/react";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { AppChromeSkeleton } from "@/app/AppLayout";
import { AppRail } from "@/app/AppRail";
import { LoginPage } from "@/features/auth/LoginPage";
import { ForgotPasswordForm } from "@/features/auth/ForgotPasswordForm";
import { TemplatesManager } from "@/features/templates/TemplatesManager";
import { UpdatesPage } from "@/features/updates/UpdatesPage";
import { qk } from "@/shared/api/queryKeys";
import type { TemplateDto } from "@/shared/api/types";
import { useRailStore } from "@/shared/stores/railStore";
import { fakeUser, resetSessionStore } from "./helpers";

/**
 * Длинные значения взяты нарочно — короткими стенд покажет, что всё влезает,
 * и соврёт. Названия папок и заголовки — из боевой библиотеки заказчика.
 */
const ОБЩИЕ: TemplateDto[] = [
  { id: "t-1", owner_id: null, title: "Приветствие и уточнение модели техники", body: "Здравствуйте! Уточните, пожалуйста, модель техники и что именно случилось — так я сразу назову диапазон цены", folder: "Первый контакт" },
  { id: "t-2", owner_id: null, title: "Выезд мастера в день обращения", body: "Мастер может подъехать сегодня. Подскажите адрес (квартира, подъезд, этаж) и ваш номер телефона, запишу вас", folder: "Первый контакт" },
  { id: "t-3", owner_id: null, title: "Цена замены подсветки ЖК/LED телевизора", body: "Замена подсветки у нас идёт от 1000 рублей, точнее по запчастям мастер сориентирует на месте", folder: "Цены и коммерческое предложение" },
  { id: "t-4", owner_id: null, title: "Отказ: не занимаемся матрицами и плазменными панелями", body: "К сожалению, матрицами мы не занимаемся — ремонт выходит дороже нового телевизора", folder: "Отказы и границы работ" },
];

const ЛИЧНЫЕ: TemplateDto[] = [
  { id: "t-9", owner_id: fakeUser.id, title: "Мой ответ про сроки", body: "Скоро буду, задерживаюсь минут на двадцать", folder: null },
];

function ширинаОкна(width: number) {
  vi.stubGlobal("matchMedia", (query: string) => {
    const max = Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? Infinity);
    return {
      matches: width <= max,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  });
}

/** Провайдеры как в приложении (см. `render.tsx`), но с выбором маршрута. */
function нарисовать(ui: React.ReactElement, route = "/chats") {
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

function снять(имя: string, html: string, минимум = 700) {
  expect(html.length, `снимок ${имя} пуст`).toBeGreaterThan(минимум);
  writeFileSync(`./.dump/${имя}.html`, html, "utf8");
}

it("снимает экран входа", () => {
  queryClient.clear();
  resetSessionStore({ bootstrapped: true });
  const { container } = нарисовать(
    <Routes>
      <Route path="/login" element={<LoginPage />} />
    </Routes>,
    "/login",
  );
  снять("ekran-login", container.innerHTML);
});

it("снимает форму «забыли пароль»", () => {
  queryClient.clear();
  resetSessionStore({ bootstrapped: true });
  const { container } = нарисовать(<ForgotPasswordForm onBack={() => {}} />, "/login");
  снять("ekran-forgot", container.innerHTML);
});

it("снимает загрузочный каркас (рельса развёрнута — так у большинства)", () => {
  ширинаОкна(1920);
  useRailStore.setState({ expanded: true } as never);
  const { container } = нарисовать(<AppChromeSkeleton />);
  снять("ekran-boot-shirokiy", container.innerHTML, 200);
  vi.unstubAllGlobals();
});

it("снимает рельсу развёрнутой", () => {
  ширинаОкна(1920);
  resetSessionStore({
    user: fakeUser,
    permissions: ["conversations:read", "messages:send", "stats:own", "templates:own"] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  useRailStore.setState({ expanded: true } as never);
  const { container } = нарисовать(<AppRail />);
  снять("ekran-rail-expanded", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает рельсу свёрнутой", () => {
  ширинаОкна(1920);
  resetSessionStore({
    user: fakeUser,
    permissions: ["conversations:read", "messages:send", "stats:own", "templates:own"] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  useRailStore.setState({ expanded: false } as never);
  const { container } = нарисовать(<AppRail />);
  снять("ekran-rail-collapsed", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает рельсу на телефоне", () => {
  ширинаОкна(390);
  resetSessionStore({
    user: fakeUser,
    permissions: ["conversations:read", "messages:send", "stats:own", "templates:own"] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  useRailStore.setState({ expanded: true } as never);
  const { container } = нарисовать(<AppRail />);
  снять("ekran-rail-mobile", container.innerHTML);
  vi.unstubAllGlobals();
});

it("снимает раздел «Быстрые ответы»", () => {
  queryClient.clear();
  resetSessionStore({
    user: fakeUser,
    permissions: ["templates:own", "templates:shared"] as never,
    accessToken: "t",
    bootstrapped: true,
  });
  // ⚠ ФОРМА КЭША — РОВНО ТА, ЧТО ОТДАЁТ РУЧКА (`TemplatesPage`, `TemplateFolders`).
  // Массив вместо страницы экран проглатывает молча и рисует «ответов нет» — то
  // есть стенд показал бы пустой экран и объявил, что всё влезает.
  queryClient.setQueryData(qk.templates.list("shared"), {
    items: ОБЩИЕ,
    page: { limit: 200, offset: 0, total: ОБЩИЕ.length },
  });
  queryClient.setQueryData(qk.templates.list("personal"), {
    items: ЛИЧНЫЕ,
    page: { limit: 200, offset: 0, total: ЛИЧНЫЕ.length },
  });
  queryClient.setQueryData(qk.templates.folders, {
    shared: ["Первый контакт", "Цены и коммерческое предложение", "Отказы и границы работ"],
    personal: [],
  });
  const { container } = нарисовать(<TemplatesManager />, "/settings/templates");
  снять("ekran-templates", container.innerHTML);
});

it("снимает раздел «Что нового»", () => {
  queryClient.clear();
  const { container } = нарисовать(<UpdatesPage />, "/updates");
  снять("ekran-updates", container.innerHTML);
});
