/**
 * ПОЛИТИКА ПРАВИЛ, ПРАВИЛА РАЗБОРА И СВОИ АДРЕСА НА ЭКРАНЕ (проверка 24.09).
 *
 * Сервер отдавал и принимал эти настройки, а на экране их не было: вето на
 * подъём правила до exact, которое обещает уведомление лестницы, было
 * невыполнимо. Вето — повторное сохранение той же строки, поэтому кнопка
 * шлёт строку и без правок, и ровно своё поле: задача лестницы узнаёт
 * человека по подаче `rule_policy`.
 *
 * ДИВЕРСИИ (обязаны краснеть): слать всё значение формы вместо своего поля —
 * краснеет «ровно {rule_policy}»; запретить сохранение без правок — краснеет
 * «вето»; заменить `e.message` общим советом — краснеет «словами сервера».
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryClient } from "@/app/queryClient";
import { AddressDetectBlock } from "@/features/settings/accounts/AddressDetectBlock";
import { showToast } from "@/shared/ui/toast";
import {
  errorEnvelope,
  fakeMe,
  fakeUser,
  jsonResponse,
  resetSessionStore,
} from "./helpers";
import { renderWithProviders } from "./render";

vi.mock("@/shared/ui/toast", () => ({
  showToast: vi.fn(),
  showUndoToast: vi.fn(),
}));

function settings(): Record<string, unknown> {
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
    rule_policy: "street_point=approx",
    rule_policy_effective: [
      {
        rule: "street_point",
        label: "точка улицы, дом не найден",
        policy: "approx",
      },
      {
        rule: "suburb",
        label: "единственный дом области в 40 км от города",
        policy: "exact",
      },
    ],
    parse_rules: "",
    parse_rules_effective: [
      {
        rule: "STOP_LATIN_BRAND",
        label: "марка техники: «Хонор 8х», «Танк 581», «Эпсон 412»",
        state: "off",
      },
    ],
    own_addresses: "",
    own_addresses_auto: "ул Невская, 7а",
  };
}

describe("политика правил и свои адреса", () => {
  let calls: Array<{ method: string; body: Record<string, unknown> }>;
  let current: Record<string, unknown>;
  let patchAnswer: (() => Response) | null;

  beforeEach(() => {
    calls = [];
    current = settings();
    patchAnswer = null;
    queryClient.clear();
    vi.mocked(showToast).mockClear();
    resetSessionStore({
      user: fakeUser,
      permissions: fakeMe.permissions as never,
      accessToken: "t",
      bootstrapped: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        calls.push({ method: init?.method ?? "GET", body });
        if (init?.method === "PATCH") {
          if (patchAnswer) return patchAnswer();
          current = { ...current, ...body };
        }
        return jsonResponse(200, current);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  const patches = () => calls.filter((c) => c.method === "PATCH");

  it("показывает действующую политику правил словами и кодом", async () => {
    renderWithProviders(<AddressDetectBlock />);
    expect(
      await screen.findByText(/единственный дом области в 40 км от города/),
    ).toBeInTheDocument();
    expect(screen.getByText("suburb")).toBeInTheDocument();
    expect(screen.getByText("пишет как точный адрес")).toBeInTheDocument();
    expect(screen.getByTestId("own-addresses-auto")).toHaveTextContent(
      "ул Невская, 7а",
    );
  });

  it("вето: сохранение строки без правок шлёт ровно {rule_policy}", async () => {
    renderWithProviders(<AddressDetectBlock />);
    const save = await screen.findByRole("button", {
      name: "Сохранить политику",
    });
    await userEvent.click(save);
    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].body).toEqual({ rule_policy: "street_point=approx" });
  });

  it("правила разбора сохраняются своим полем", async () => {
    renderWithProviders(<AddressDetectBlock />);
    const line = await screen.findByLabelText("Перекрытия правил разбора");
    await userEvent.type(line, "STOP_LATIN_BRAND=shadow");
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить правила разбора" }),
    );
    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].body).toEqual({
      parse_rules: "STOP_LATIN_BRAND=shadow",
    });
  });

  it("свои адреса сохраняются своим полем", async () => {
    renderWithProviders(<AddressDetectBlock />);
    const area = await screen.findByLabelText(
      "Свои адреса: мастерская, офис, пункт приёма",
    );
    await userEvent.type(area, "ул Невская, 7а");
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить свои адреса" }),
    );
    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].body).toEqual({ own_addresses: "ул Невская, 7а" });
  });

  it("отказ сервера показывается его словами", async () => {
    const reason =
      "address_geo.rule_policy: неизвестное правило «surbub»; известны: suburb";
    patchAnswer = () =>
      jsonResponse(
        400,
        errorEnvelope("validation_error", reason, {
          fields: [{ field: "address_geo.rule_policy", rule: "content" }],
        }),
      );
    renderWithProviders(<AddressDetectBlock />);
    const line = await screen.findByLabelText("Перекрытия политики");
    await userEvent.clear(line);
    await userEvent.type(line, "surbub=off");
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить политику" }),
    );
    await waitFor(() => expect(vi.mocked(showToast)).toHaveBeenCalled());
    const toast = vi.mocked(showToast).mock.calls.at(-1)![0] as {
      message?: string;
      color?: string;
    };
    expect(toast.color).toBe("red");
    expect(toast.message).toBe(reason);
    // Черновик остался — человек поправит опечатку, а не наберёт заново.
    expect(line).toHaveValue("surbub=off");
  });
});
