/**
 * АВТОПРИВЯЗКА АДРЕСА БЕЗ ЧЕЛОВЕКА — НАСТРОЙКИ И СТРОКА ВОРОНКИ (18.09).
 *
 * Решение владельца: адрес привязывается сам, оператор ничего не подтверждает.
 * Здесь стережётся то, что видит и нажимает администратор: тумблер
 * автопривязки зависит от автозаписи и шлёт ровно своё поле; отказ сервера
 * показывается его словами; вопроса клиенту об адресе на экране нет
 * (владелец 24.09: «сам LeadChat не должен ничего спрашивать»); строка
 * воронки в мониторе — со своими словами состояния и без кнопки «Проверить».
 *
 * ДИВЕРСИИ (обязаны краснеть): убрать `!value.autofill` из disabled тумблера
 * автопривязки — «погашен без автозаписи» краснеет; заменить `e.message` на
 * общий совет в onError — «словами сервера» краснеет; вернуть тумблер вопроса
 * — «вопроса клиенту нет» краснеет.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AddressDetectBlock } from "@/features/settings/accounts/AddressDetectBlock";
import { ApisPage } from "@/features/settings/apis/ApisPage";
import { showToast } from "@/shared/ui/toast";
import { errorEnvelope, fakeMe, fakeUser, jsonResponse, resetSessionStore } from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({ showToast: vi.fn(), showUndoToast: vi.fn() }));

function базовые(): Record<string, unknown> {
  return {
    enabled: true,
    autofill: true,
    levels: "A",
    geo_enabled: true,
    provider: "nominatim",
    yandex_key_present: false,
    yandex_daily_limit: 900,
    yandex_used_today: 0,
    suggest_enabled: false,
    suggest_key_present: false,
    suggest_daily_limit: 900,
    suggest_used_today: 0,
    dadata_enabled: false,
    dadata_key_present: false,
    dadata_daily_limit: 9000,
    dadata_used_today: 0,
    llm_enabled: false,
    llm_key_present: false,
    llm_daily_limit: 50,
    llm_used_today: 0,
    ahunter_enabled: false,
    ahunter_used_today: 0,
    speller_enabled: false,
    speller_daily_limit: 9000,
    speller_used_today: 0,
    auto_decide: true,
  };
}

describe("настройки автопривязки", () => {
  let обращения: Array<{ url: string; method: string; body: Record<string, unknown> }>;
  let настройки: Record<string, unknown>;
  let ответНаPatch: (() => Response) | null;

  beforeEach(() => {
    обращения = [];
    настройки = базовые();
    ответНаPatch = null;
    queryClient.clear();
    vi.mocked(showToast).mockClear();
    resetSessionStore({ user: fakeUser, permissions: fakeMe.permissions as never, accessToken: "t", bootstrapped: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        обращения.push({ url, method: init?.method ?? "GET", body });
        if (init?.method === "PATCH") {
          if (ответНаPatch) return ответНаPatch();
          настройки = { ...настройки, ...body };
        }
        return jsonResponse(200, настройки);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  const patches = () => обращения.filter((o) => o.method === "PATCH");

  it("тумблер автопривязки шлёт ровно своё поле", async () => {
    renderWithProviders(<AddressDetectBlock />);
    const тумблер = await screen.findByRole("switch", { name: /Привязывать адрес автоматически/ });
    expect(тумблер).toBeChecked();
    expect(тумблер).not.toBeDisabled();
    await userEvent.click(тумблер);
    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].url).toContain("/settings/address-detect");
    expect(patches()[0].body).toEqual({ auto_decide: false });
    await waitFor(() => expect(тумблер).not.toBeChecked());
  });

  it("без автозаписи тумблер автопривязки погашен: решать нечего", async () => {
    настройки = { ...настройки, autofill: false };
    renderWithProviders(<AddressDetectBlock />);
    const тумблер = await screen.findByRole("switch", { name: /Привязывать адрес автоматически/ });
    expect(тумблер).toBeDisabled();
  });

  it("отказ сервера показывается его словами", async () => {
    const причина = "Автопривязка недоступна: выключена проверка по карте";
    ответНаPatch = () =>
      jsonResponse(400, errorEnvelope("validation_error", причина, { fields: [{ field: "auto_decide", rule: "geo" }] }));
    renderWithProviders(<AddressDetectBlock />);
    const тумблер = await screen.findByRole("switch", { name: /Привязывать адрес автоматически/ });
    await userEvent.click(тумблер);
    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const тост = vi.mocked(showToast).mock.calls.at(-1)![0] as { message?: string; color?: string };
    expect(тост.color).toBe("red");
    expect(тост.message).toBe(причина);
  });

  it("вопроса клиенту об адресе на экране нет", async () => {
    renderWithProviders(<AddressDetectBlock />);
    await screen.findByRole("switch", { name: /Привязывать адрес автоматически/ });
    expect(screen.queryByRole("switch", { name: /Спрашивать адрес у клиента/ })).toBeNull();
    expect(screen.queryByLabelText(/Текст вопроса/)).toBeNull();
    expect(screen.queryByLabelText(/Ждать ответа человека/)).toBeNull();
    expect(screen.queryByLabelText(/Не раньше, чем клиент написал/)).toBeNull();
  });
});

function строкаМонитора(over: Record<string, unknown> = {}) {
  return {
    key: "dadata",
    kind: "builtin",
    name: "DaData",
    purpose: "Справочник",
    url: "https://example.test/dadata",
    docs_url: null,
    env_var: "DADATA_API_KEY",
    key_present: true,
    enabled: true,
    daily_limit: 9000,
    monthly_limit: null,
    used_today: 12,
    state: "ok",
    state_note: "",
    notes: "",
    checked: null,
    editable: false,
    ...over,
  };
}

function строкаВоронки(over: Record<string, unknown> = {}) {
  return строкаМонитора({
    key: "address_funnel",
    name: "Адреса в карточку (воронка за неделю)",
    purpose: "Сколько адресов из переписки дошло до карточки и с какой степенью",
    url: null,
    env_var: null,
    key_present: null,
    daily_limit: null,
    used_today: null,
    state: "unknown",
    state_note: "первый замер — в понедельник 04:40 МСК",
    ...over,
  });
}

describe("строка воронки адресов в мониторе внешних сервисов", () => {
  let строки: Array<Record<string, unknown>>;
  let проверок: string[];

  beforeEach(() => {
    проверок = [];
    строки = [строкаМонитора(), строкаВоронки()];
    queryClient.clear();
    vi.mocked(showToast).mockClear();
    resetSessionStore({ user: fakeUser, permissions: fakeMe.permissions as never, accessToken: "t", bootstrapped: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const проверка = url.match(/\/settings\/apis\/([^/]+)\/check/);
        if (проверка && init?.method === "POST") {
          проверок.push(decodeURIComponent(проверка[1]));
          return jsonResponse(200, {
            items: строки,
            key: проверка[1],
            checked: { ok: true, status: 200, ms: 12, error: null, at: "2026-09-18T10:00:00Z" },
          });
        }
        return jsonResponse(200, { items: строки });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it("без снимка — «Нет замера», кнопка «Проверить» погашена, «Проверить все» её обходит", async () => {
    renderWithProviders(<ApisPage />, { route: "/settings/apis" });
    const строка = (await screen.findByText(/воронка за неделю/)).closest("tr")!;
    expect(within(строка).getByText("Нет замера")).toBeInTheDocument();
    expect(within(строка).getByRole("button", { name: "Проверить" })).toBeDisabled();
    expect(within(строка).queryByRole("button", { name: "Править" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Проверить все" }));
    await waitFor(() => expect(проверок).toEqual(["dadata"]));
    const тост = vi.mocked(showToast).mock.calls.at(-1)![0] as { message?: string };
    expect(тост.message).not.toContain("без адреса");
  });

  it("падение доли — «Падение» красным с причиной, ровная неделя — «Ровно»", async () => {
    строки = [
      строкаВоронки({
        state: "down",
        state_note: "падение: доля карточек упала на 10 п.п. и больше · неделя с 07.09: диалогов с адресом 200, в карточке 100 (50 %)",
        notes: "точных 60 · приблизительных 30 · без точки 10",
      }),
    ];
    renderWithProviders(<ApisPage />, { route: "/settings/apis" });
    const строка = (await screen.findByText(/воронка за неделю/)).closest("tr")!;
    expect(within(строка).getByText("Падение")).toBeInTheDocument();
    expect(within(строка).getByText(/доля карточек упала/)).toBeInTheDocument();
    expect(within(строка).getByText(/точных 60/)).toBeInTheDocument();
    expect(within(строка).queryByText("Не отвечает")).toBeNull();

    строки = [строкаВоронки({ state: "ok", state_note: "неделя с 07.09: в карточке 150 (75 %)" })];
    queryClient.clear();
    renderWithProviders(<ApisPage />, { route: "/settings/apis" });
    const ровно = await screen.findAllByText("Ровно");
    expect(ровно.length).toBeGreaterThan(0);
  });
});
