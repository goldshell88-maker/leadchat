import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { queryClient } from "@/app/queryClient";
import { theme } from "@/app/theme";
import { LoginPage } from "@/features/auth/LoginPage";
import { resetSessionMemory } from "@/features/auth/sessionEnd";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * ЭКРАН ВХОДА ДОЛЖЕН ОБЪЯСНЯТЬ, ЧТО СЛУЧИЛОСЬ (SHELL-03, TEXT-21).
 *
 * Диспетчер набирает ответ клиенту, протухает refresh-токен — и он оказывается
 * на пустой форме входа. Сам вышел? Отключил администратор? Упал сервер?
 * Раньше по всему интерфейсу не было ни одной строки про закончившийся вход.
 *
 * Плашка появляется РОВНО в одном случае: в этой вкладке кто-то работал, и
 * человека снял страж (переход с `state.from`). Осознанный выход и первый
 * заход по ссылке под это не подходят — иначе плашка врала бы.
 */

function renderLogin({ from }: { from?: string } = {}) {
  const entry = from ? { pathname: "/login", state: { from: { pathname: from } } } : "/login";
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={[entry]}>
          <LoginPage />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

/** Вкладка, в которой человек уже работал: подписка модуля это запоминает. */
function tabHadSession() {
  resetSessionStore({ user: fakeUser, permissions: [], accessToken: "t", bootstrapped: true });
  useSessionStore.setState({ user: null, accessToken: null, bootstrapped: true });
}

beforeEach(() => {
  queryClient.clear();
  resetSessionMemory();
  resetSessionStore({ bootstrapped: true });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetSessionMemory();
});

describe("Почему я оказался на экране входа (SHELL-03)", () => {
  it("работал и выкинуло — говорим об этом словами", () => {
    tabHadSession();
    renderLogin({ from: "/chats/conv-1" });

    expect(screen.getByRole("status")).toHaveTextContent(/Вход закончился/);
  });

  it("вышел сам — никакой плашки: человек знает, что сделал", () => {
    tabHadSession();
    renderLogin();

    expect(screen.queryByRole("status")).toBeNull();
  });

  it("первый заход по ссылке на закрытый раздел — тоже без плашки", () => {
    // Сессии в этой вкладке не было: сказать «вход закончился» было бы враньём.
    renderLogin({ from: "/stats" });

    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("Пять состояний отказа: каждое говорит, что делать", () => {
  async function submit(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText("Email"), "anna@partner-lead-centre.ru");
    await user.type(screen.getByLabelText("Пароль"), "LocalTest12345!");
    await user.click(screen.getByRole("button", { name: "Войти" }));
  }

  it("неизвестный отказ: человеку — что делать, код ответа — отдельной строкой", async () => {
    const user = userEvent.setup();
    // Ровно то, что собирает http.ts для не-JSON ответа nginx.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(502, errorEnvelope("internal_error", "Запрос завершился с кодом 502"))),
    );
    renderLogin();
    await submit(user);

    expect(await screen.findByText(/Войти не получилось/)).toBeInTheDocument();
    expect(screen.getByText(/покажите администратору: Ответ сервера: 502/)).toBeInTheDocument();
    // Машинная строка целиком на экран не выходит (TEXT-21).
    expect(screen.queryByText("Запрос завершился с кодом 502")).toBeNull();
  });

  it("блокировка после попыток: срок ожидания и выход через «Не помню пароль»", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(423, errorEnvelope("account_locked", "Слишком много попыток", { retry_after_sec: 300 })),
      ),
    );
    renderLogin();
    await submit(user);

    const alert = await screen.findByRole("alert");
    /*
     * ⚠ НЕ «5:00», А «ОТСЧЁТ ИДЁТ ОТ ПЯТИ МИНУТ» (правка 19.08). Сервер даёт
     * 300 секунд, экран показывает обратный отсчёт — и он ТИКАЕТ. Под нагрузкой
     * полного прогона между отрисовкой и проверкой успевала пройти секунда, на
     * экране оказывалось «4:59», и выкатка вставала на ровном месте: в
     * одиночку тест зелёный, в наборе красный.
     *
     * Проверяем то, ради чего тест написан: человеку названо, сколько ждать, и
     * это около пяти минут. Точная секунда — свойство часов, а не продукта.
     */
    expect(alert.textContent ?? "").toMatch(/[45]:\d{2}/);
    expect(alert).toHaveTextContent(/Не помню пароль/);
  });

  it("нет связи: говорим про интернет и повтор, а не «сервер недоступен»", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    renderLogin();
    await submit(user);

    expect(await screen.findByText(/Проверьте интернет и нажмите «Войти» ещё раз/)).toBeInTheDocument();
  });

  it("учётная запись отключена: сказано, что включает её администратор", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(403, errorEnvelope("forbidden", ""))),
    );
    renderLogin();
    await submit(user);

    expect(await screen.findByText(/Включить её может только администратор/)).toBeInTheDocument();
  });

  it("неверные данные: общий текст и подсказка про раскладку", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(401, errorEnvelope("invalid_credentials", "Неверный email или пароль"))),
    );
    renderLogin();
    await submit(user);

    expect(await screen.findByText("Неверный email или пароль")).toBeInTheDocument();
    expect(screen.getByText(/Проверьте раскладку и Caps Lock/)).toBeInTheDocument();
  });
});
