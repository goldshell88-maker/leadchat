/**
 * Автоматика карточки (12.09): дополнительные номера с доказательством, кнопка
 * «Не его номер», пометка «похоже на исправление», подпись автоматической склейки.
 *
 * ЖИВАЯ КАРТОЧКА, А НЕ КОМПОНЕНТ В ИЗОЛЯЦИИ — по тому же доводу, что в
 * `VtoroyNomer0909`: правка, отрендеренная напрямую, могла уехать в бой
 * неподключённой.
 *
 * ДИВЕРСИИ: убрать `near_primary` из условия пометки — второй тест краснеет;
 * убрать `row.auto` из панели объединения — четвёртый; отправить `decision:
 * "add"` вместо `"reject"` — третий.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { ClientCardPane } from "@/features/chats/components/card/ClientCardPane";
import type { PhoneEntry } from "@/features/chats/components/card/clientApi";
import { qk } from "@/shared/api/queryKeys";
import { CONV_ID, makeConversation, seedEmptyThread } from "./render";
import { fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({
  showToast: vi.fn(),
  showUndoToast: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const ОСНОВНОЙ = "+79125550177";

function запись(value: string, extra: Partial<PhoneEntry> = {}): PhoneEntry {
  return {
    value,
    client_id: "client-1",
    primary: false,
    candidate_id: `cand-${value}`,
    source: "dialog",
    conversation_id: CONV_ID,
    message_id: "m-1",
    message_at: "2026-09-12T11:02:00Z",
    hint: null,
    decided_by: "auto",
    near_primary: false,
    ...extra,
  };
}

function личность(extra: Partial<Record<string, unknown>> = {}) {
  return {
    id: "client-1",
    name: "Иван Петров",
    phone: ОСНОВНОЙ,
    phone_manual: false,
    phone_source: "dialog",
    phone_candidates: [],
    external_id: "923456789",
    phones: [
      {
        ...запись(ОСНОВНОЙ),
        primary: true,
      },
      запись("+79995550188", { hint: "жены" }),
    ],
    avito_ids: [{ value: "923456789", client_id: "client-1", primary: true }],
    merged_from: [],
    merged_into: null,
    ...extra,
  };
}

let обращения: Array<{
  url: string;
  method: string;
  body: Record<string, unknown>;
}>;

function оснастка(данные: ReturnType<typeof личность>) {
  обращения = [];
  queryClient.clear();
  seedEmptyThread();
  resetSessionStore({
    user: fakeUser,
    permissions: fakeMe.permissions as never,
    accessToken: "t",
    bootstrapped: true,
  });
  const conv = makeConversation();
  queryClient.setQueryData(qk.conversations.detail(CONV_ID), conv);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      обращения.push({
        url,
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : {},
      });
      if (url.includes("/phone-candidates/") && url.endsWith("/resolve")) {
        return jsonResponse(200, { phone: ОСНОВНОЙ, candidate: {}, twins: [] });
      }
      if (url.includes("/identity")) return jsonResponse(200, данные);
      if (url.includes("/merge-candidates"))
        return jsonResponse(200, { items: [] });
      if (/\/conversations\//.test(url)) return jsonResponse(200, conv);
      return jsonResponse(200, { items: [] });
    }),
  );
}

describe("Карточка сама: дополнительные номера с доказательством", () => {
  beforeEach(() => oснасткаПоУмолчанию());
  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  function oснасткаПоУмолчанию() {
    оснастка(личность());
  }

  it("у дополнительного номера стоит, откуда он: сам, в этом диалоге, когда, со словом", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const подпись = await screen.findByText(
      /из переписки, сам · в этом диалоге/,
    );
    expect(подпись.textContent).toMatch(/со словом «жены»/);
    // Основной в списке «ещё номера» не повторяется.
    expect(
      screen.queryAllByText("+7 912 555-01-77").length,
    ).toBeLessThanOrEqual(1);
  });

  it("похожий номер помечен как возможное исправление, но основной не тронут", async () => {
    оснастка(
      личность({
        phones: [
          { ...запись(ОСНОВНОЙ), primary: true },
          запись("+79125550178", { near_primary: true }),
        ],
      }),
    );
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    expect(await screen.findByTestId("near-primary")).toHaveTextContent(
      /возможно, исправление/,
    );
    // Действия по умолчанию нет: только «Сделать основным» для человека.
    expect(
      screen.getByRole("button", {
        name: /Сделать \+7 912 555-01-78 основным номером/,
      }),
    ).toBeTruthy();
  });

  it("«Не его номер» шлёт отказ по строке-кандидату", async () => {
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    const кнопка = await screen.findByRole("button", {
      name: /Снять \+7 999 555-01-88: не его номер/,
    });
    await userEvent.click(кнопка);
    await waitFor(() =>
      expect(обращения.some((о) => о.url.endsWith("/resolve"))).toBe(true),
    );
    const запрос = обращения.find((о) => о.url.endsWith("/resolve"))!;
    expect(запрос.url).toContain(
      "/phone-candidates/cand-%2B79995550188/resolve",
    );
    expect(запрос.body).toEqual({ decision: "reject" });
  });

  it("у номера присоединённой карточки кнопок нет", async () => {
    оснастка(
      личность({
        phones: [
          { ...запись(ОСНОВНОЙ), primary: true },
          запись("+79995550188", { client_id: "client-2", source: "other" }),
        ],
      }),
    );
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    await screen.findByText("+7 999 555-01-88");
    expect(
      screen.queryByRole("button", { name: /основным номером/ }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /не его номер/ })).toBeNull();
  });

  it("автоматическая склейка подписана как автоматическая и обратимая", async () => {
    оснастка(
      личность({
        merged_from: [
          {
            id: "client-2",
            name: "Иван Петров",
            external_id: "555",
            phone: ОСНОВНОЙ,
            merged_at: "2026-09-12T02:00:00Z",
            confidence: "confirmed",
            auto: true,
            rule: "phone_twins_v2",
          },
        ],
      }),
    );
    renderWithProviders(<ClientCardPane convId={CONV_ID} />);
    expect(
      await screen.findByText(
        /Объединена автоматически по телефону с карточкой/,
      ),
    ).toBeTruthy();
    // Подсказка про отмену — в title кнопки, а не строкой в каждой карточке
    // (аудит 15.09): читают её один раз за всё время работы.
    const кнопка = screen.getByRole("button", { name: "Разъединить" });
    expect(кнопка).toBeTruthy();
    expect(кнопка.getAttribute("title")).toMatch(
      /больше эту пару автоматика не тронет/,
    );
  });
});
