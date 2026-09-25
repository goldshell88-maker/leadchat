import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { theme } from "@/app/theme";
import { BrowserAlertsBlock } from "@/features/settings/profile/BrowserAlertsBlock";

/**
 * РАЗРЕШЕНИЕ НА УВЕДОМЛЕНИЯ НЕ ЗАПРАШИВАЛ НИКТО.
 *
 * Код показа системных уведомлений написан и корректен (`platform/web.ts`):
 * вкладка скрыта, разрешение выдано — показываем. Но `requestPermission` не
 * встречался в проекте ни разу, хотя комментарий рядом с показом утверждал, что
 * разрешение спрашивает переключатель в профиле. Переключателя не было. Значит
 * `Notification.permission` навсегда оставался `default`, и уведомление не
 * показывалось никогда — при полностью рабочем коде показа.
 *
 * Проверяем здесь не «есть ли переключатель», а три вещи, на которых такие
 * экраны врут: что нажатие действительно СПРАШИВАЕТ браузер, что отказ не
 * прикидывается «сейчас включим» (снять запрет из кода нельзя), и что
 * неподдерживающий браузер говорит об этом прямо, а не показывает мёртвую ручку.
 */

function renderBlock() {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <BrowserAlertsBlock />
    </MantineProvider>,
  );
}

/** Подмена браузерного `Notification` с заданным состоянием разрешения. */
function stubNotification(permission: NotificationPermission, onAsk?: () => NotificationPermission) {
  const requestPermission = vi.fn(async () => onAsk?.() ?? permission);
  const shown: Array<{ title: string; body?: string }> = [];
  class FakeNotification {
    static permission: NotificationPermission = permission;
    static requestPermission = requestPermission;
    constructor(title: string, options?: NotificationOptions) {
      shown.push({ title, body: options?.body });
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
  return { requestPermission, shown };
}

describe("Профиль: уведомления браузера", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("нажатие спрашивает разрешение у браузера", async () => {
    const user = userEvent.setup();
    const { requestPermission: ask } = stubNotification("default", () => "granted");
    renderBlock();

    await user.click(screen.getByRole("switch", { name: "Уведомления браузера" }));

    expect(ask).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Разрешены")).toBeTruthy();
  });

  it("отказ браузера объясняется, а не прячется", async () => {
    const user = userEvent.setup();
    stubNotification("default", () => "denied");
    renderBlock();

    await user.click(screen.getByRole("switch", { name: "Уведомления браузера" }));

    // Ключевое: не «попробуйте ещё раз». Второй раз браузер уже не спросит,
    // и человек обязан узнать, что снимается это только в настройках сайта.
    expect(await screen.findByText(/Снять запрет можно только в его настройках/)).toBeTruthy();
    expect(screen.getByRole("switch", { name: "Уведомления браузера" })).toBeDisabled();
  });

  it("уже выданное разрешение не обещает, что его можно снять отсюда", () => {
    stubNotification("granted");
    renderBlock();

    expect(screen.getByText("Разрешены")).toBeTruthy();
    expect(screen.getByText(/снять разрешение нельзя/)).toBeTruthy();
  });

  it("браузер без уведомлений говорит об этом прямо, без мёртвой ручки", () => {
    vi.stubGlobal("Notification", undefined);
    renderBlock();

    expect(screen.getByText(/системные уведомления не поддерживает/)).toBeTruthy();
    expect(screen.queryByRole("switch", { name: "Уведомления браузера" })).toBeNull();
  });

  it("пример показывает настоящее уведомление, а не тост внутри страницы", async () => {
    const user = userEvent.setup();
    const { shown } = stubNotification("granted");
    renderBlock();

    await user.click(screen.getByRole("button", { name: "Показать пример" }));

    // Смысл кнопки — дать человеку УВИДЕТЬ, как это выглядит в его системе.
    // Внутристраничный тост здесь был бы обманом: он рисуется всегда, а
    // системное уведомление зависит от настроек ОС («Не беспокоить», фокус-режим).
    // Показ асинхронный: сперва спрашивается сервис-воркер (проверка 24.09).
    await waitFor(() => expect(shown).toHaveLength(1));
    expect(shown[0].title).toBe("LeadChat");
  });
});
