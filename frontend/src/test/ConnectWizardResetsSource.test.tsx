/**
 * Мастер подключения канала чистит поле «Источник» (27.08).
 *
 * ⚠ РЕГРЕССИЯ ОТ ПРАВКИ ТОГО ЖЕ ДНЯ. Поле «Источник» добавлено в мастер утром
 * 27.08 (просьба владельца «при добавлении аккаунта сразу присвоить источник»),
 * а в сброс при открытии окна не попало: ключи стирались, источник оставался.
 *
 * Сценарий: подключили первый канал с кодом «В95», окно закрылось; открыли
 * снова для второго аккаунта — поля ключей пусты, а «В95» стоит на месте и
 * молча уезжает на сервер. Заявки нового канала пошли бы с чужим кодом
 * источника, и заметили бы это в лид-центре, а не на экране.
 */
import { useState } from "react";
import { describe, expect, it, vi, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConnectChannelWizard } from "@/features/settings/accounts/ConnectChannelWizard";
import { renderWithProviders } from "./render";
import { resetSessionStore, fakeUser, fakeMe, jsonResponse } from "./helpers";

function монтировать() {
  resetSessionStore({
    user: fakeUser,
    permissions: fakeMe.permissions as never,
    accessToken: "t",
    bootstrapped: true,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/avito/app")) return jsonResponse(200, { live: true });
      return jsonResponse(200, {});
    }),
  );
}

describe("Мастер подключения канала", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /**
   * Окном управляет обёртка, а не `rerender`: перерисовка мимо провайдеров
   * роняет запрос внутри мастера («No QueryClient set»), и тест падал бы не по
   * той причине, ради которой написан.
   */
  // ⚠ Имя латиницей: правило `react-hooks/rules-of-hooks` узнаёт компонент по
  // ЗАГЛАВНОЙ ЛАТИНСКОЙ букве, и «Обёртка» для него — обычная функция, внутри
  // которой хук вызывать нельзя.
  function Wrapper() {
    const [открыто, setОткрыто] = useState(true);
    return (
      <>
        <button type="button" onClick={() => setОткрыто((v) => !v)}>
          переключить
        </button>
        <ConnectChannelWizard opened={открыто} onClose={() => setОткрыто(false)} />
      </>
    );
  }

  it("повторное открытие очищает источник вместе с ключами", async () => {
    const user = userEvent.setup();
    монтировать();
    renderWithProviders(<Wrapper />, { route: "/settings/accounts" });

    await user.type(await screen.findByLabelText("Client ID"), "id-1");
    await user.type(screen.getByLabelText("Источник"), "В95");
    expect(screen.getByLabelText("Источник")).toHaveValue("В95");

    // Закрыли и открыли снова — так и выглядит подключение второго аккаунта.
    const переключить = screen.getByRole("button", { name: "переключить" });
    await user.click(переключить);
    await user.click(переключить);

    await waitFor(() => {
      expect(screen.getByLabelText("Источник")).toHaveValue("");
    });
    // Ключи чистились и раньше — проверяем, что правка их не сломала.
    expect(screen.getByLabelText("Client ID")).toHaveValue("");
  });
});
