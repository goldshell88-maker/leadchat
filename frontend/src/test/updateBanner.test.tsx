import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UpdateBanner } from "@/platform/UpdateBanner";
import { DISMISS_TTL_MS, selectBannerUpdate, useUpdateStore } from "@/platform/updateStore";
import { AboutAppBlock } from "@/features/settings/profile/AboutAppBlock";
import { enterTauriRuntime, leaveTauriRuntime, makeFakeTauriBridge } from "./fakeBridge";
import { renderWithProviders } from "./render";

/**
 * Баннер обновления (04 §6.3): неблокирующий, ставится только по клику,
 * «Позже» прячет его на сутки. В вебе не рисуется вовсе.
 */

const UPDATE = { version: "1.4.2", notes: "Исправлен бейдж непрочитанных", pubDate: null };

describe("Баннер обновления и блок «О приложении» (04 §6.3)", () => {
  beforeEach(() => {
    useUpdateStore.setState({
      available: null,
      checking: false,
      applying: false,
      error: null,
      dismissedVersion: null,
      dismissedAt: null,
    });
  });

  afterEach(() => {
    leaveTauriRuntime();
  });

  it("без обновления баннера нет (веб-режим — всегда этот случай)", () => {
    renderWithProviders(<UpdateBanner />);
    expect(screen.queryByRole("status", { name: "Доступно обновление" })).toBeNull();
  });

  it("с обновлением показывает версию и обе кнопки", () => {
    useUpdateStore.getState().announce(UPDATE);
    renderWithProviders(<UpdateBanner />);

    expect(screen.getByText("Доступна версия 1.4.2")).toBeInTheDocument();
    expect(screen.getByText(/Исправлен бейдж/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Перезапустить" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Позже" })).toBeInTheDocument();
  });

  it("«Перезапустить» отдаёт установку мосту, «Позже» прячет баннер", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    enterTauriRuntime(bridge);
    useUpdateStore.getState().announce(UPDATE);

    const user = userEvent.setup();
    renderWithProviders(<UpdateBanner />);

    await user.click(screen.getByRole("button", { name: "Перезапустить" }));
    expect(calls.installUpdate).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Позже" }));
    await waitFor(() => expect(screen.queryByText("Доступна версия 1.4.2")).toBeNull());
  });

  it("отложенный баннер возвращается через сутки, новая версия — сразу", () => {
    useUpdateStore.getState().announce(UPDATE);
    useUpdateStore.getState().dismiss();
    const s = useUpdateStore.getState();

    expect(selectBannerUpdate(s)).toBeNull();
    expect(selectBannerUpdate(s, Date.now() + DISMISS_TTL_MS + 1)?.version).toBe("1.4.2");

    useUpdateStore.getState().announce({ ...UPDATE, version: "1.5.0" });
    expect(selectBannerUpdate(useUpdateStore.getState())?.version).toBe("1.5.0");
  });

  it("«О приложении»: в вебе блока нет, в приложении — версия, канал и проверка", async () => {
    const web = renderWithProviders(<AboutAppBlock />);
    expect(screen.queryByText("О приложении")).toBeNull();
    web.unmount();

    const { bridge, calls } = makeFakeTauriBridge();
    enterTauriRuntime(bridge);
    const user = userEvent.setup();
    renderWithProviders(<AboutAppBlock />);

    expect(await screen.findByText(/1\.4\.2/)).toBeInTheDocument();
    expect(screen.getByText(/stable/)).toBeInTheDocument();
    expect(screen.getByLabelText("Запускать при входе в Windows")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Проверить обновления" }));
    expect(calls.checkForUpdates).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("У вас последняя версия")).toBeInTheDocument();
  });

  it("«О приложении»: сбой проверки — не «последняя версия», а причина (проверка 24.09)", async () => {
    const { bridge, calls } = makeFakeTauriBridge();
    calls.checkForUpdates.mockRejectedValueOnce(new Error("сервер обновлений недоступен"));
    enterTauriRuntime(bridge);
    const user = userEvent.setup();
    renderWithProviders(<AboutAppBlock />);

    await user.click(await screen.findByRole("button", { name: "Проверить обновления" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось проверить обновления: сервер обновлений недоступен",
    );
    expect(screen.queryByText("У вас последняя версия")).toBeNull();
  });
});
