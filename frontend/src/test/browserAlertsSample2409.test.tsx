/**
 * «Показать пример» — тем же путём, что боевые уведомления (проверка 24.09).
 *
 * На Android конструктор `new Notification` запрещён платформой, а пример
 * создавался только им: он не появлялся, и человек решал, что уведомления не
 * работают. Боевые уведомления идут через сервис-воркер.
 *
 * ДИВЕРСИИ: вернуть голый `new Notification` — краснеет «через воркер»;
 * убрать `try` вокруг конструктора — краснеет «отказ назван».
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { theme } from "@/app/theme";

const viaWorker = vi.fn<(n: unknown) => Promise<boolean>>();
vi.mock("@/platform/serviceWorker", () => ({
  показатьЧерезВоркер: (n: unknown) => viaWorker(n),
}));

import { BrowserAlertsBlock } from "@/features/settings/profile/BrowserAlertsBlock";

function stubNotification(construct: () => void) {
  class FakeNotification {
    static permission: NotificationPermission = "granted";
    static requestPermission = vi.fn(async () => "granted" as NotificationPermission);
    constructor() {
      construct();
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
}

function renderBlock() {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <BrowserAlertsBlock />
    </MantineProvider>,
  );
}

describe("пример уведомления", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    viaWorker.mockReset();
  });

  it("идёт через воркер, конструктор не нужен", async () => {
    const constructed = vi.fn();
    stubNotification(constructed);
    viaWorker.mockResolvedValue(true);
    const user = userEvent.setup();
    renderBlock();

    await user.click(screen.getByRole("button", { name: "Показать пример" }));

    expect(viaWorker).toHaveBeenCalledWith(expect.objectContaining({ title: "LeadChat" }));
    expect(constructed).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("воркера нет, конструктор запрещён — отказ назван", async () => {
    stubNotification(() => {
      throw new TypeError("Illegal constructor");
    });
    viaWorker.mockResolvedValue(false);
    const user = userEvent.setup();
    renderBlock();

    await user.click(screen.getByRole("button", { name: "Показать пример" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Браузер не показал пример (Illegal constructor)",
    );
  });
});
