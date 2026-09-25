import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { Route, Routes } from "react-router-dom";
import { theme } from "@/app/theme";
import { NotifyNudge } from "@/shared/ui/ПредложитьУведомления";
import { BrowserAlertsBlock } from "@/features/settings/profile/BrowserAlertsBlock";
import { ChatsPage } from "@/features/chats/ChatsPage";
import type { Permission } from "@/shared/auth/usePermissions";
import { fakeUser, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * РАЗРЕШЕНИЕ НА УВЕДОМЛЕНИЯ НЕ ПРОСИЛ НИКТО, КРОМЕ ПЕРЕКЛЮЧАТЕЛЯ В ПРОФИЛЕ.
 *
 * Показ уведомлений написан, проверен и работает — но только при выданном
 * разрешении. Спрашивал его один переключатель в /settings/profile, куда
 * диспетчер за смену не заходит ни разу: у него список, лента и CRM в соседнем
 * окне. Пока он туда не зашёл, `Notification.permission` остаётся `default` и
 * не показывается НИЧЕГО. Это и есть самая массовая причина жалобы
 * «уведомления не работают»: не поломка показа, а невыданное разрешение.
 *
 * Цена (замер боя 05.09): 1357 новых диалогов и 625 возвратов в сутки, каналы
 * открыты 30–33 операторам, за неделю 11 739 взятий из очереди. Каждое взятие —
 * гонка, и выигрывает её тот, кто раньше узнал.
 *
 * ЧТО СТЕРЕЖЁМ ЗДЕСЬ (и чем это проверено — диверсии описаны у каждого набора):
 *   • полоса появляется при `default` (зовёт включить) и при `denied` (говорит,
 *     что уведомлений не будет), а при `granted` молчит;
 *   • её видит только тот, кто отвечает клиенту (`messages:send`);
 *   • нажатие ДЕЙСТВИТЕЛЬНО спрашивает браузер, а не красит кнопку;
 *   • «Больше не предлагать» переживает перезагрузку и не переносится на
 *     соседа за тем же компьютером;
 *   • полоса смонтирована в рабочем месте, а не живёт только в этом файле;
 *   • при запрете профиль говорит, ГДЕ его снять, — второго шанса спросить нет.
 */

/*
 * Панели рабочего места подменены: этот файл проверяет монтирование полосы, а
 * не список, ленту и карточку. Тот же приём и по той же причине — в
 * clientCardOverlay.test.tsx.
 */
vi.mock("@/features/chats/components/list/ChatListPane", () => ({
  ChatListPane: () => <div data-testid="list-pane" />,
}));
vi.mock("@/features/chats/components/thread/ChatThreadPane", () => ({
  ChatThreadPane: () => <div data-testid="thread-pane" />,
}));
vi.mock("@/features/chats/components/card/ClientCardPane", () => ({
  ClientCardPane: () => <aside data-testid="client-card" />,
}));

/** Тот, кто отвечает клиенту, — набор роли manager из app/core/rbac.py. */
const ОПЕРАТОР: Permission[] = [
  "conversations:read",
  "messages:send",
  "conversations:manage",
  "stats:own",
];

/** Роль head: клиентам не пишет — `messages:send` в её наборе нет. */
const РУКОВОДИТЕЛЬ: Permission[] = [
  "conversations:read",
  "conversations:manage",
  "stats:all",
  "audit:read",
];

function войти(permissions: Permission[], id: string = fakeUser.id) {
  resetSessionStore({
    user: { ...fakeUser, id },
    permissions,
    accessToken: "t",
    bootstrapped: true,
  });
}

/** Подмена браузерного `Notification` с заданным состоянием разрешения. */
function подменитьNotification(
  permission: NotificationPermission,
  наЗапрос?: () => NotificationPermission,
) {
  const requestPermission = vi.fn(async () => наЗапрос?.() ?? permission);
  class FakeNotification {
    static permission: NotificationPermission = permission;
    static requestPermission = requestPermission;
  }
  vi.stubGlobal("Notification", FakeNotification);
  return { requestPermission };
}

/*
 * ⚠ ПОЛОСУ РИСУЕМ ВНУТРИ РОУТЕРА, А НЕ ГОЛОЙ (07.09). При запрете в ней стоит
 * `<Link>` на /settings/profile, а он без контекста роутера падает —
 * «Cannot destructure property 'basename'». В бою полоса живёт внутри
 * `ChatsPage`, то есть внутри маршрута, и голый рендер проверял бы условия,
 * которых в жизни не бывает. `renderWithProviders` даёт ту же обвязку, что и
 * у соседних экранных тестов.
 */
function показатьПолосу() {
  return renderWithProviders(<NotifyNudge />);
}

const кнопкаВключить = () => screen.queryByRole("button", { name: "Включить уведомления" });

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/**
 * ДИВЕРСИЯ НАБОРА: в `ПредложитьУведомления.tsx` условие показа
 * `разрешение !== "default"` заменено на `разрешение === "granted"` — то есть
 * полоса начинает лезть и при запрете, и в браузере без уведомлений.
 * Результат: покраснели «запрет полосу не показывает» и «браузер без
 * уведомлений полосу не показывает»; остальные прошли. Байт восстановлен.
 */
describe("Полоса «включите уведомления»: когда её видно", () => {
  it("при невыданном разрешении зовёт включить", () => {
    войти(ОПЕРАТОР);
    подменитьNotification("default");
    показатьПолосу();

    expect(кнопкаВключить()).toBeTruthy();
    // Текст — про пользу человеку, а не про Notification API: полоса стоит в
    // рабочем месте, где читают по диагонали между диалогами.
    expect(screen.getByText(/Не пропускайте клиента/)).toBeTruthy();
  });

  it("выданное разрешение полосу убирает", () => {
    /*
     * Положительная пара к проверке запрета ниже: без неё «полоса видна при
     * denied» зеленела бы и на коде, который показывает её ВСЕГДА — то есть
     * висит поверх работы у человека, у которого всё уже включено.
     */
    войти(ОПЕРАТОР);
    подменитьNotification("granted");
    показатьПолосу();

    expect(кнопкаВключить()).toBeNull();
    expect(screen.queryByText(/Браузер запретил уведомления/)).toBeNull();
    expect(screen.queryByText(/Не пропускайте клиента/)).toBeNull();
  });

  it("⚠ при запрете полоса ОСТАЁТСЯ и говорит, что уведомлений не будет", () => {
    /*
     * ДИВЕРСИЯ: вернуть в условие показа `разрешение !== "default"` (то есть
     * прежнее «при запрете полосы нет»). Тест краснеет.
     *
     * ⚠ ЖИВАЯ ЖАЛОБА 07.09: «уведомления в Chrome не работают». Машинерия при
     * этом была исправна — воркер на бою зарегистрирован и активен, — а
     * разрешение оказывалось запрещено. И вот тут полоса ИСЧЕЗАЛА: ни
     * объяснения, ни следа. Причём запретить мог не человек, а сам Chrome — в
     * режиме тихих запросов он отклоняет молча, даже не показав вопрос.
     *
     * Кнопка «Включить» при запрете и правда обманка: браузер второй раз не
     * спросит. Но из «кнопка не поможет» не следует «человеку не о чем знать».
     */
    войти(ОПЕРАТОР);
    подменитьNotification("denied");
    показатьПолосу();

    expect(screen.getByText(/Браузер запретил уведомления/)).toBeTruthy();
    // Кнопки-обманки нет: вместо неё дорога туда, где разбор по шагам.
    expect(кнопкаВключить()).toBeNull();
    expect(screen.getByRole("link", { name: "Как включить" })).toBeTruthy();
  });

  it("браузер без уведомлений полосу не показывает", () => {
    войти(ОПЕРАТОР);
    vi.stubGlobal("Notification", undefined);
    показатьПолосу();

    expect(кнопкаВключить()).toBeNull();
  });
});

/**
 * ДИВЕРСИЯ НАБОРА: из условия показа убран `!can("messages:send")` — полоса
 * начинает висеть у всех, кто открыл рабочее место. Результат: покраснел
 * «руководителю полосы нет»; остальные прошли. Байт восстановлен.
 */
describe("Полоса «включите уведомления»: кому её видно", () => {
  it("руководителю полосы нет: он клиентам не отвечает", () => {
    войти(РУКОВОДИТЕЛЬ);
    подменитьNotification("default");
    показатьПолосу();

    expect(кнопкаВключить()).toBeNull();
  });

  it("оператору полоса есть", () => {
    войти(ОПЕРАТОР);
    подменитьNotification("default");
    показатьПолосу();

    expect(кнопкаВключить()).toBeTruthy();
  });
});

/**
 * ДИВЕРСИЯ НАБОРА: в обработчике кнопки вызов `Notification.requestPermission()`
 * заменён на `текущееРазрешение()` — кнопка перестаёт спрашивать браузер.
 * Результат: покраснели обе проверки набора — «нажатие спрашивает разрешение у
 * браузера» и «после выданного разрешения полоса уходит сама» (разрешение так и
 * осталось `default`, полоса висит). Байт восстановлен.
 *
 * ⚠ ПРОВЕРЯЕМ ИМЕННО ВЫЗОВ, А НЕ ИСЧЕЗНОВЕНИЕ ПОЛОСЫ. Разрешение обязано
 * запрашиваться из жеста человека: браузер отклоняет запрос без нажатия и
 * запоминает отказ навсегда. Полоса, которая гаснет сама по себе, выглядела бы
 * точно так же — и была бы обманом.
 */
describe("Полоса «включите уведомления»: нажатие", () => {
  it("нажатие спрашивает разрешение у браузера", async () => {
    const user = userEvent.setup();
    войти(ОПЕРАТОР);
    const { requestPermission } = подменитьNotification("default", () => "granted");
    показатьПолосу();

    await user.click(screen.getByRole("button", { name: "Включить уведомления" }));

    expect(requestPermission).toHaveBeenCalledTimes(1);
  });

  it("после выданного разрешения полоса уходит сама", async () => {
    const user = userEvent.setup();
    войти(ОПЕРАТОР);
    подменитьNotification("default", () => "granted");
    показатьПолосу();

    await user.click(screen.getByRole("button", { name: "Включить уведомления" }));

    await waitFor(() => expect(кнопкаВключить()).toBeNull());
  });
});

/**
 * ДИВЕРСИЯ НАБОРА: из `большеНеПредлагать` убрана запись в `localStorage` —
 * полоса скрывается до перезагрузки и возвращается на следующий день.
 * Результат: покраснело «закрытие переживает перезагрузку»; «чужой отказ»
 * прошёл (он и должен: без записи прятать нечего). Байт восстановлен.
 *
 * ВТОРАЯ ДИВЕРСИЯ: ключ отказа записан без id человека
 * (`lc-notify-nudge-off`). Результат: покраснело «отказ соседа не прячет полосу
 * у другого»; «закрытие переживает перезагрузку» прошло. Байт восстановлен.
 */
describe("Полоса «включите уведомления»: закрытие насовсем", () => {
  it("закрытие переживает перезагрузку рабочего места", async () => {
    const user = userEvent.setup();
    войти(ОПЕРАТОР);
    подменитьNotification("default");
    const { unmount } = показатьПолосу();

    await user.click(screen.getByRole("button", { name: "Больше не предлагать" }));
    expect(кнопкаВключить()).toBeNull();

    unmount();
    показатьПолосу();

    expect(кнопкаВключить()).toBeNull();
  });

  it("отказ соседа не прячет полосу у другого за тем же компьютером", async () => {
    const user = userEvent.setup();
    подменитьNotification("default");

    войти(ОПЕРАТОР, "оператор-а");
    const { unmount } = показатьПолосу();
    await user.click(screen.getByRole("button", { name: "Больше не предлагать" }));
    unmount();

    // Общий компьютер смены: следующий садится в тот же профиль браузера.
    войти(ОПЕРАТОР, "оператор-б");
    показатьПолосу();

    expect(кнопкаВключить()).toBeTruthy();
  });
});

/**
 * ДИВЕРСИЯ НАБОРА: строка `<NotifyNudge />` убрана из `ChatsPage.tsx`.
 * Результат: покраснел «полоса стоит в рабочем месте»; все остальные проверки
 * файла прошли — то есть без этого сторожа безупречная полоса могла бы жить
 * только в тестах (так уже было с окном серии, разбор `debounce-mimo-boya`).
 * Байт восстановлен.
 */
describe("Полоса «включите уведомления»: монтирование", () => {
  it("полоса стоит в рабочем месте, а не только в этом файле", () => {
    войти(ОПЕРАТОР);
    подменитьNotification("default");

    renderWithProviders(
      <Routes>
        <Route path="/chats/:id" element={<ChatsPage />} />
      </Routes>,
      { route: "/chats/conv-1" },
    );

    expect(кнопкаВключить()).toBeTruthy();
  });
});

/**
 * ДИВЕРСИЯ НАБОРА: из текста при запрете убрано указание на значок слева от
 * адреса — остаётся «снять запрет можно только в настройках браузера» без
 * места. Результат: покраснело «профиль говорит, где снять запрет». Байт
 * восстановлен.
 *
 * ⚠ ПРО ЗАМОК БЫЛО НЕВЕРНО. Chrome заменил замок в адресной строке значком
 * ползунков, в Yandex Browser замок остался; по журналу nginx весь парк —
 * Chromium (Chrome 4576 сеансов, Yandex Browser 484). Инструкция, которая ведёт
 * к несуществующему значку, хуже молчания: человек решает, что виновато
 * приложение, и больше не возвращается.
 */
describe("Профиль при запрете уведомлений", () => {
  it("говорит, где именно снять запрет", () => {
    подменитьNotification("denied");
    render(
      <MantineProvider theme={theme} defaultColorScheme="light">
        <BrowserAlertsBlock />
      </MantineProvider>,
    );

    const текст = screen.getByText(/Снять запрет можно только/);
    expect(текст.textContent).toMatch(/значок слева от адреса/);
    expect(текст.textContent, "названы оба вида значка — парк весь Chromium").toMatch(
      /замок или ползунки/,
    );
    expect(текст.textContent, "запасной путь, если строки «Уведомления» в меню нет").toMatch(
      /Настройки\s+сайтов/,
    );
  });
});
