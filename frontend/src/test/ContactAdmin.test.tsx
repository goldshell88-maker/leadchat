import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { theme } from "@/app/theme";
import { ProfilePage } from "@/features/settings/profile/ProfilePage";
import { MESSAGE_SENT } from "@/features/settings/profile/ContactAdminForm";
import type { Role } from "@/shared/auth/usePermissions";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

/**
 * «Написать администратору» из профиля (14 §2.2, второй сценарий обращения).
 * Ручка `POST /support/message` живёт на сервере с самого начала спринта, но
 * до этого экрана у неё не было ни одного потребителя.
 */

function renderProfile(role: Role = "manager", permissions: string[] = []) {
  resetSessionStore({
    user: { ...fakeUser, role },
    permissions: permissions as never,
    accessToken: "t",
    bootstrapped: true,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <MemoryRouter initialEntries={["/settings/profile"]}>
          <ProfilePage />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
}

async function fillAndSend(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Тема"), "Не работает отправка");
  await user.type(screen.getByLabelText("Сообщение"), "Второй день не уходят сообщения из диалога");
  await user.click(screen.getByRole("button", { name: "Отправить" }));
}

describe("Форма «Написать администратору» (14 §2.2)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(202, { status: "accepted", message: "Сообщение отправлено администратору." })),
    );
  });

  it("отправляет обращение в POST /support/message и подтверждает это человеку", async () => {
    const user = userEvent.setup();
    renderProfile();

    await fillAndSend(user);

    expect(await screen.findByText(MESSAGE_SENT)).toBeInTheDocument();
    // Страница профиля сама спрашивает ещё и правило освобождения — ищем
    // именно обращение, а не первый запрос страницы.
    const обращение = vi
      .mocked(fetch)
      .mock.calls.find((c) => String(c[0]).includes("/support/message"));
    expect(обращение).toBeDefined();
    const body = JSON.parse(String(обращение?.[1]?.body));
    expect(body).toEqual({ subject: "Не работает отправка", text: "Второй день не уходят сообщения из диалога" });
    // Поля очищены — иначе второе нажатие отправит то же самое второй раз.
    expect(screen.getByLabelText("Тема")).toHaveValue("");
  });

  it("наблюдателю форма тоже доступна: писать администратору может любая роль", async () => {
    renderProfile("observer");
    expect(screen.getByRole("button", { name: "Отправить" })).toBeInTheDocument();
  });

  it("кнопка не активна, пока темы и текста нет — 422 ловить нечем", async () => {
    const user = userEvent.setup();
    renderProfile();

    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
    await user.type(screen.getByLabelText("Тема"), "ой"); // короче трёх символов
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
    await user.type(screen.getByLabelText("Тема"), "-ой");
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled(); // текста всё ещё нет
    await user.type(screen.getByLabelText("Сообщение"), "текст");
    expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
  });

  /*
   * PROF-04. Требование к длине темы серверное и обойти его нельзя — значит
   * назвать надо заранее. Тема «ЗП» — ровно то, что человек напишет про
   * зарплату: он набирает её, пишет сообщение, жмёт «Отправить», а кнопка
   * мёртвая, и почему — узнать было неоткуда.
   */
  it("минимальная длина темы названа, а не спрятана в погасшей кнопке", async () => {
    const user = userEvent.setup();
    renderProfile();

    // Сказано до того, как человек начал печатать.
    expect(screen.getByText(/Не короче 3 знаков/)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Тема"), "ЗП");
    expect(screen.getByText("Слишком коротко: нужно хотя бы 3 знака")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Тема"), " за июль");
    expect(screen.queryByText("Слишком коротко: нужно хотя бы 3 знака")).toBeNull();
  });

  /*
   * ⚠ ПРОБЕЛЫ — ЭТО ТОЖЕ НАБРАННОЕ, И РАНЬШЕ ОНИ ПРОВАЛИВАЛИСЬ В ЩЕЛЬ.
   *
   * Замер боя 14 августа: пять пробелов в «Теме» гасят кнопку и НЕ показывают
   * ошибку. Причина — «что-то есть» считалось по обрезанной строке, а у неё
   * длина ноль: поле считалось пустым, хотя человек в него печатал. Он видит
   * набранное, видит мёртвую кнопку и не видит причины.
   */
  it("одни пробелы в теме — это «слишком коротко», а не «поле пустое»", async () => {
    const user = userEvent.setup();
    renderProfile();

    await user.type(screen.getByLabelText("Тема"), "     ");

    expect(screen.getByText("Слишком коротко: нужно хотя бы 3 знака")).toBeInTheDocument();
  });

  /*
   * PROF-05. Подтверждение сбрасывалось только в начале СЛЕДУЮЩЕЙ отправки.
   * Человек отправлял письмо, начинал второе («забыл добавить») — и всё это
   * время над формой висело подтверждение первого. Отправив второе, он не
   * получал никакого нового признака: плашка та же и она не гасла.
   */
  it("подтверждение гаснет, как только человек начал писать следующее письмо", async () => {
    const user = userEvent.setup();
    renderProfile();

    await fillAndSend(user);
    expect(await screen.findByText(MESSAGE_SENT)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Тема"), "Ещё вопрос");
    expect(screen.queryByText(MESSAGE_SENT)).toBeNull();
  });

  it("429: показан срок повторной попытки, текст сотрудника не потерян", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(429, errorEnvelope("rate_limited", "Слишком часто", { retry_after_sec: 900 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderProfile();

    await fillAndSend(user);

    expect(await screen.findByText(/Попробуйте через 15 минут/)).toBeInTheDocument();
    expect(screen.queryByText(MESSAGE_SENT)).toBeNull();
    expect(screen.getByLabelText("Тема")).toHaveValue("Не работает отправка");
    // Молча повторять нельзя: обращение ушло ровно один раз.
    const обращения = fetchMock.mock.calls.filter((c) =>
      String((c as unknown[])[0]).includes("/support/message"),
    );
    expect(обращения).toHaveLength(1);
  });

  it("обрыв связи: честно говорим, что сообщение не ушло", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const user = userEvent.setup();
    renderProfile();

    await fillAndSend(user);

    expect(await screen.findByText(/Сообщение не отправлено/)).toBeInTheDocument();
    expect(screen.queryByText(MESSAGE_SENT)).toBeNull();
  });
});
