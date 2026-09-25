import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { LoginPage } from "@/features/auth/LoginPage";
import { theme } from "@/app/theme";
import { errorEnvelope, jsonResponse, resetSessionStore } from "./helpers";

function renderLogin() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/login"]}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/chats" element={<div>chats-stub</div>} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    resetSessionStore({ bootstrapped: true });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("submit button is disabled until both fields are filled", async () => {
    const user = userEvent.setup();
    renderLogin();

    const submit = screen.getByRole("button", { name: "Войти" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Email"), "anna@partner-lead-centre.ru");
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Пароль"), "secret-password");
    expect(submit).toBeEnabled();
  });

  it("shows the single generic error text on 401 invalid_credentials and clears the password", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(401, errorEnvelope("invalid_credentials", "Неверный email или пароль")),
      ),
    );
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText("Email"), "anna@partner-lead-centre.ru");
    const passwordInput = screen.getByLabelText("Пароль");
    await user.type(passwordInput, "wrong-password");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByText("Неверный email или пароль")).toBeInTheDocument();
    expect(passwordInput).toHaveValue("");
  });

  it("renders the locked state with a countdown from details.retry_after_sec and disables the form", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          403,
          errorEnvelope("account_locked", "Слишком много попыток входа", { retry_after_sec: 899 }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText("Email"), "anna@partner-lead-centre.ru");
    await user.type(screen.getByLabelText("Пароль"), "whatever-pass");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(
      await screen.findByText(/Слишком много попыток входа\. Повторите через 14:5\d/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeDisabled();
    expect(screen.getByLabelText("Пароль")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Войти" })).toBeDisabled();
  });
});
