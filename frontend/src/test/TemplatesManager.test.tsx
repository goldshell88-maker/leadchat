import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { TemplatesManager } from "@/features/templates/TemplatesManager";
import type { Permission } from "@/shared/auth/usePermissions";
import type { TemplateDto } from "@/shared/api/types";
import { errorEnvelope, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

/**
 * Быстрые ответы (01 §7, 11 §3.2). Здесь проверяется то, что дороже всего
 * стоит человеку: набранный текст ответа клиенту не должен пропадать от
 * промаха мимо окна (TPL-02/MODAL-04).
 */

const ADMIN_PERMISSIONS: Permission[] = ["templates:shared", "templates:own"];

const SHARED: TemplateDto[] = [
  {
    id: "t-1",
    owner_id: null,
    title: "Приветствие",
    body: "Здравствуйте! Уточните, пожалуйста, модель техники",
    folder: "Первый контакт",
  },
];

const PERSONAL: TemplateDto[] = [
  { id: "t-9", owner_id: fakeUser.id, title: "Мой ответ", body: "Скоро буду", folder: null },
];

let fetchMock: ReturnType<typeof vi.fn>;

function calls() {
  return fetchMock.mock.calls.map((c) => ({
    url: decodeURIComponent(String(c[0])),
    init: c[1] as RequestInit | undefined,
  }));
}

function setupFetch() {
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    if (url.pathname.endsWith("/templates") && method === "GET") {
      const personal = url.searchParams.get("scope") === "personal";
      const items = personal ? PERSONAL : SHARED;
      return jsonResponse(200, { items, page: { limit: 200, offset: 0, total: items.length } });
    }
    if (url.pathname.endsWith("/templates") && method === "POST") {
      return jsonResponse(201, { ...SHARED[0], id: "t-new" });
    }
    return jsonResponse(404, errorEnvelope("not_found", "нет"));
  });
  vi.stubGlobal("fetch", fetchMock);
}

describe("Редактор быстрого ответа — не терять набранный текст", () => {
  beforeEach(() => {
    queryClient.clear();
    setupFetch();
    resetSessionStore({
      user: { ...fakeUser, role: "admin" },
      permissions: ADMIN_PERMISSIONS,
      accessToken: "t",
      bootstrapped: true,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function openEditorAndType(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Создать быстрый ответ" }));
    await user.type(await screen.findByLabelText("Название"), "Смета по холодильнику");
    await user.type(screen.getByLabelText("Текст"), "Выезд мастера 500 ₽, диагностика бесплатно");
  }

  it("Esc не закрывает окно с набранным текстом, а спрашивает", async () => {
    const user = userEvent.setup();
    await openEditorAndType(user);

    await user.keyboard("{Escape}");

    expect(await screen.findByText("Закрыть редактор и потерять набранный текст?")).toBeInTheDocument();
    // Само окно на месте: текст никуда не делся, его видно за предупреждением.
    expect(screen.getByLabelText("Текст")).toHaveValue("Выезд мастера 500 ₽, диагностика бесплатно");

    await user.click(screen.getByRole("button", { name: "Остаться" }));
    expect(screen.queryByText("Закрыть редактор и потерять набранный текст?")).toBeNull();
    expect(screen.getByLabelText("Текст")).toHaveValue("Выезд мастера 500 ₽, диагностика бесплатно");
  });

  it("«Отмена» с набранным текстом тоже спрашивает, и «Закрыть без сохранения» закрывает", async () => {
    const user = userEvent.setup();
    await openEditorAndType(user);

    await user.click(screen.getByRole("button", { name: "Отмена" }));
    expect(await screen.findByText("Закрыть редактор и потерять набранный текст?")).toBeInTheDocument();
    expect(screen.getByLabelText("Текст")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Закрыть без сохранения" }));
    await waitFor(() => expect(screen.queryByLabelText("Текст")).toBeNull());
    // Ничего не отправляли: закрытие без сохранения — это именно без сохранения.
    expect(calls().some((c) => c.init?.method === "POST")).toBe(false);
  });

  it("нетронутый редактор закрывается сразу, без лишнего вопроса", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TemplatesManager />, { route: "/settings/templates" });
    await screen.findByRole("table");

    // Открыли на редактирование существующего и ничего не трогали: поля
    // заполнены с самого начала, и вопрос здесь был бы шумом.
    await user.click(screen.getByLabelText("Действия: Приветствие"));
    await user.click(await screen.findByRole("menuitem", { name: "Редактировать" }));
    await screen.findByLabelText("Текст");

    await user.click(screen.getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByLabelText("Текст")).toBeNull());
    expect(screen.queryByText("Закрыть редактор и потерять набранный текст?")).toBeNull();
  });
});
