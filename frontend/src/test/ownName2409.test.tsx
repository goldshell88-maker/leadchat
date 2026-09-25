/**
 * СВОЁ ИМЯ ПРАВИТСЯ В ПРОФИЛЕ (проверка 24.09).
 *
 * Ручка `PATCH /auth/me` есть с 07.08, а на экране имя было обычным текстом:
 * «Сотрудник 3» или имя с опечаткой в подписи, которую видят клиенты, мог
 * исправить только администратор.
 *
 * ДИВЕРСИИ (обязаны краснеть): не обновлять стор сессии после сохранения —
 * краснеет «имя в шапке новое»; заменить `e.message` общим советом —
 * краснеет «словами сервера».
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { theme } from "@/app/theme";
import { OwnNameField } from "@/features/settings/profile/OwnNameField";
import { useSessionStore } from "@/shared/stores/sessionStore";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

function Field() {
  const name = useSessionStore((s) => s.user?.full_name ?? "");
  return <OwnNameField name={name} />;
}

function renderField() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MantineProvider theme={theme} defaultColorScheme="light">
        <Field />
      </MantineProvider>
    </QueryClientProvider>,
  );
}

describe("своё имя в профиле", () => {
  let patches: Array<Record<string, unknown>>;
  let answer: (body: Record<string, unknown>) => Response;

  beforeEach(() => {
    patches = [];
    answer = (body) => jsonResponse(200, { full_name: body.full_name });
    resetSessionStore({
      user: { ...fakeUser, full_name: "Сотрудник 3" },
      permissions: [],
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        if (init?.method === "PATCH") {
          patches.push(body);
          return answer(body);
        }
        return jsonResponse(404, errorEnvelope("not_found", "нет"));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("нажатие на имя открывает поле, Enter сохраняет, имя в шапке новое", async () => {
    const user = userEvent.setup();
    renderField();
    await user.click(screen.getByRole("button", { name: "Изменить своё имя: Сотрудник 3" }));
    const input = screen.getByLabelText("Своё имя");
    await user.clear(input);
    await user.type(input, "Анна Смирнова{Enter}");

    await waitFor(() => expect(patches).toEqual([{ full_name: "Анна Смирнова" }]));
    expect(
      await screen.findByRole("button", { name: "Изменить своё имя: Анна Смирнова" }),
    ).toBeInTheDocument();
    expect(useSessionStore.getState().user?.full_name).toBe("Анна Смирнова");
  });

  it("отказ сервера — его словами, поле остаётся открытым", async () => {
    const reason = "Слишком коротко: нужно не меньше 2 символов";
    answer = () =>
      jsonResponse(
        400,
        errorEnvelope("validation_error", reason, {
          fields: [{ field: "full_name", rule: "string_too_short", message: reason }],
        }),
      );
    const user = userEvent.setup();
    renderField();
    await user.click(screen.getByRole("button", { name: /Изменить своё имя/ }));
    const input = screen.getByLabelText("Своё имя");
    await user.clear(input);
    await user.type(input, "А{Enter}");

    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(screen.getByLabelText("Своё имя")).toHaveValue("А");
    expect(useSessionStore.getState().user?.full_name).toBe("Сотрудник 3");
  });

  it("Escape закрывает поле без сохранения", async () => {
    const user = userEvent.setup();
    renderField();
    await user.click(screen.getByRole("button", { name: /Изменить своё имя/ }));
    await user.type(screen.getByLabelText("Своё имя"), " лишнее{Escape}");

    expect(screen.getByRole("button", { name: "Изменить своё имя: Сотрудник 3" })).toBeInTheDocument();
    expect(patches).toEqual([]);
  });
});
