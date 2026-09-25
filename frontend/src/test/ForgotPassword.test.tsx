import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { LoginPage } from "@/features/auth/LoginPage";
import { RESET_REQUEST_SENT } from "@/features/auth/ForgotPasswordForm";
import { theme } from "@/app/theme";
import { errorEnvelope, jsonResponse, resetSessionStore } from "./helpers";

/**
 * «Не помню пароль» (14 §2.2). Заявка уходит администратору — и ответ формы
 * НЕ должен превращаться в способ проверить, заведён ли такой сотрудник.
 */

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

async function submitRequest(email = "anna@partner-lead-centre.ru") {
  const user = userEvent.setup();
  renderLogin();
  await user.click(screen.getByRole("button", { name: "Не помню пароль" }));
  await user.type(await screen.findByLabelText("Email"), email);
  await user.click(screen.getByRole("button", { name: "Отправить заявку" }));
  return user;
}

describe("Заявка «не помню пароль»", () => {
  beforeEach(() => {
    resetSessionStore({ bootstrapped: true });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("ссылка со страницы входа открывает форму", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "Не помню пароль" }));

    expect(screen.getByRole("heading", { name: "Не помню пароль" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить заявку" })).toBeInTheDocument();
    // Форма входа уступила место заявке — двух полей «Пароль» на экране нет.
    expect(screen.queryByLabelText("Пароль")).toBeNull();
  });

  /**
   * ⚠ СЧИТАЕМ ТОЛЬКО СВОИ ВЫЗОВЫ, А НЕ ВСЕ ПОДРЯД.
   *
   * Экран входа с 27.08 делает ещё один запрос — HEAD `/download/latest.json`,
   * которым проверяется, выложен ли инсталлятор десктопа (иначе ссылка в
   * подвале ведёт в 404). Он приходит первым и раньше ломал обе проверки ниже:
   * `calls[0]` оказывался манифестом, а счётчик вызовов — на единицу больше.
   *
   * Правильная проверка не зависит от того, что рядом на экране делает
   * соседний блок: она отбирает вызовы по адресу.
   */
  function заявки(mock: { mock: { calls: unknown[][] } }): unknown[][] {
    return mock.mock.calls.filter((c) => String(c[0]).includes("/support/password-reset"));
  }

  it("существующая учётка: показывается общий текст", async () => {
    const fetchMock = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(async () => jsonResponse(202, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await submitRequest();

    expect(await screen.findByText(RESET_REQUEST_SENT)).toBeInTheDocument();
    expect(заявки(fetchMock)).toHaveLength(1);
  });

  it("несуществующая учётка: ТОТ ЖЕ текст — существование не раскрывается", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(404, errorEnvelope("not_found", "Пользователь не найден"))),
    );

    await submitRequest("nobody@partner-lead-centre.ru");

    expect(await screen.findByText(RESET_REQUEST_SENT)).toBeInTheDocument();
    // Сообщение сервера наружу не просачивается.
    expect(screen.queryByText("Пользователь не найден")).toBeNull();
  });

  /**
   * 429 — единственный ответ сервера, который НЕ выдаётся за успех: заявку
   * никто не принял. Лимит на адрес — 20 в час, а в офисе адрес общий, поэтому
   * упереться в него может человек, который сам ещё ничего не отправлял; ответь
   * ему «заявка принята» — и он будет ждать ссылку, которой никто не получал.
   * Существование учётки этим не выдаётся: счётчики сервер крутит до похода
   * в базу, и 429 приходит одинаково на любой адрес.
   */
  it("ограничение частоты (429): срок повторной попытки показан, успехом не притворяемся", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(429, errorEnvelope("rate_limited", "Слишком часто", { retry_after_sec: 180 })),
    );
    vi.stubGlobal("fetch", fetchMock);

    await submitRequest();

    expect(await screen.findByText(/Попробуйте через 3 минуты/)).toBeInTheDocument();
    expect(screen.queryByText(RESET_REQUEST_SENT)).toBeNull();
    // Молча повторять запрос нельзя — счётчик тот же, окно только продлится.
    expect(заявки(fetchMock)).toHaveLength(1);
    // Сообщение сервера наружу по-прежнему не просачивается.
    expect(screen.queryByText("Слишком часто")).toBeNull();
  });

  it("429 без срока в ответе: честное «позже», выдуманного времени нет", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(429, errorEnvelope("rate_limited", "Слишком часто"))),
    );

    await submitRequest();

    expect(await screen.findByText(/Попробуйте позже/)).toBeInTheDocument();
    expect(screen.queryByText(RESET_REQUEST_SENT)).toBeNull();
  });

  it("обрыв связи — честно говорим, что заявка не ушла", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    await submitRequest();

    expect(await screen.findByText(/Сервер недоступен — заявка не отправлена/)).toBeInTheDocument();
    expect(screen.queryByText(RESET_REQUEST_SENT)).toBeNull();
  });

  it("можно вернуться ко входу", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(202, { ok: true })));

    const user = await submitRequest();
    await screen.findByText(RESET_REQUEST_SENT);
    await user.click(screen.getByRole("button", { name: "Вернуться ко входу" }));

    expect(screen.getByLabelText("Пароль")).toBeInTheDocument();
  });
});
